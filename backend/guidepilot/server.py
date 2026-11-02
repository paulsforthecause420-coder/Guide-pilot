"""Local API service for GuidePilot.

Security model
--------------
* Binds to ``127.0.0.1`` only (never a public interface).
* Every endpoint except ``GET /health`` requires a per-run bearer token that is
  generated at start-up, written to ``~/.config/guidepilot/runtime/token`` with
  mode ``0600``, and used by the Electron *main* process. The token never enters
  the renderer: the renderer calls the main process over IPC, and the main
  process is the only HTTP client.
* Requests carrying a browser ``Origin`` header are rejected: a random web page
  open in the user's browser must not be able to reach this API even if it
  guesses the port.
* Bodies are size-limited, JSON-only, and rate-limited. All errors return a
  stable ``code`` so the UI can explain failures.
"""

from __future__ import annotations

import json
import secrets
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from . import APP_NAME, __version__, util
from .accessibility.atspi import atspi_available
from .accessibility.ocr import ocr_capability
from .config import Settings, SettingsError, generate_runtime_token, write_runtime_token
from .grounding.matcher import best_match
from .models import TaskState
from .perception.capture import CaptureError
from .perception.display import detect_display
from .perception.observer import ScreenObserver
from .providers.base import ProviderError
from .providers.gateway import ModelGateway
from .security.redaction import is_sensitive_label
from .security.secrets import SecretStore
from .speech.stt import SpeechInput, stt_capability
from .speech.tts import SpeechOutput, tts_capability
from .storage.db import Database
from .task_engine.engine import EngineError, TaskEngine
from .task_engine.planner import TaskPlanner
from .task_engine.workflows import check_drift, export_workflow, import_workflow, workflow_to_plan
from .validation import ValidationError
from .verification.verifier import Verifier

MAX_BODY_BYTES = 2 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


@dataclass
class ServiceRuntime:
    settings: Settings
    db: Database
    observer: ScreenObserver
    gateway: ModelGateway
    engine: TaskEngine
    secrets_store: SecretStore
    planner: TaskPlanner
    speech_out: SpeechOutput
    speech_in: SpeechInput
    token: str


def build_runtime(settings: Settings | None = None, db: Database | None = None) -> ServiceRuntime:
    settings = settings or Settings.load()
    db = db or Database()
    display = detect_display()
    observer = ScreenObserver(settings, display=display)
    secrets_store = SecretStore()
    gateway = ModelGateway(settings, api_key_provider=secrets_store.as_provider())
    planner = TaskPlanner(settings, gateway)
    verifier = Verifier()
    engine = TaskEngine(settings, observer, planner, gateway, verifier, db)
    speech_out = SpeechOutput(
        enabled=bool(settings.get("voice.enabled")) and bool(settings.get("voice.speak_steps")),
        engine=str(settings.get("voice.tts_engine") or "auto"),
    )
    speech_in = SpeechInput(enabled=bool(settings.get("voice.enabled")), engine=str(settings.get("voice.stt_engine") or "auto"))
    return ServiceRuntime(
        settings=settings,
        db=db,
        observer=observer,
        gateway=gateway,
        engine=engine,
        secrets_store=secrets_store,
        planner=planner,
        speech_out=speech_out,
        speech_in=speech_in,
        token=generate_runtime_token(),
    )


class GuidePilotApi:
    """Transport-independent request handlers (used by HTTP and tests)."""

    def __init__(self, runtime: ServiceRuntime) -> None:
        self.rt = runtime
        self.routes: list[tuple[str, str, Callable[..., Any]]] = [
            ("GET", r"/health", self.health),
            ("GET", r"/capabilities", self.capabilities),
            ("GET", r"/settings", self.get_settings),
            ("PUT", r"/settings", self.update_settings),
            ("POST", r"/settings/api-key", self.set_api_key),
            ("DELETE", r"/settings/api-key", self.delete_api_key),
            ("POST", r"/observe", self.observe),
            ("GET", r"/observation/latest", self.latest_observation),
            ("GET", r"/tasks", self.list_tasks),
            ("POST", r"/tasks", self.create_task),
            ("DELETE", r"/tasks", self.clear_tasks),
            ("GET", r"/tasks/resume-prompt", self.resume_prompt),
            ("GET", r"/tasks/(?P<task_id>[A-Za-z0-9_\-]+)", self.get_task),
            ("POST", r"/tasks/(?P<task_id>[A-Za-z0-9_\-]+)/observe", self.look_again),
            ("POST", r"/tasks/(?P<task_id>[A-Za-z0-9_\-]+)/verify", self.verify),
            ("POST", r"/tasks/(?P<task_id>[A-Za-z0-9_\-]+)/watch", self.watch),
            ("POST", r"/tasks/(?P<task_id>[A-Za-z0-9_\-]+)/confirm", self.confirm),
            ("POST", r"/tasks/(?P<task_id>[A-Za-z0-9_\-]+)/skip", self.skip),
            ("POST", r"/tasks/(?P<task_id>[A-Za-z0-9_\-]+)/next", self.next_step),
            ("POST", r"/tasks/(?P<task_id>[A-Za-z0-9_\-]+)/previous", self.previous_step),
            ("POST", r"/tasks/(?P<task_id>[A-Za-z0-9_\-]+)/pause", self.pause),
            ("POST", r"/tasks/(?P<task_id>[A-Za-z0-9_\-]+)/resume", self.resume_task),
            ("POST", r"/tasks/(?P<task_id>[A-Za-z0-9_\-]+)/cancel", self.cancel),
            ("POST", r"/tasks/(?P<task_id>[A-Za-z0-9_\-]+)/save-workflow", self.save_workflow),
            ("POST", r"/tasks/(?P<task_id>[A-Za-z0-9_\-]+)/context", self.set_context),
            ("DELETE", r"/tasks/(?P<task_id>[A-Za-z0-9_\-]+)", self.delete_task),
            ("POST", r"/chat", self.chat),
            ("GET", r"/workflows", self.list_workflows),
            ("POST", r"/workflows/import", self.import_workflow_route),
            ("GET", r"/workflows/(?P<workflow_id>[A-Za-z0-9_\-]+)/export", self.export_workflow_route),
            ("POST", r"/workflows/(?P<workflow_id>[A-Za-z0-9_\-]+)/start", self.start_workflow),
            ("DELETE", r"/workflows/(?P<workflow_id>[A-Za-z0-9_\-]+)", self.delete_workflow),
            ("GET", r"/audit", self.audit),
            ("GET", r"/usage", self.usage),
            ("POST", r"/privacy/stop", self.privacy_stop),
            ("POST", r"/privacy/enable", self.privacy_enable),
            ("POST", r"/privacy/purge", self.privacy_purge),
            ("GET", r"/speech", self.speech_status),
            ("POST", r"/speech/speak", self.speech_speak),
            ("POST", r"/actions/paste", self.assisted_paste),
            ("POST", r"/shutdown", self.shutdown),
        ]
        self._limiter = util.SlidingWindow(60.0, 600)

    # -- dispatch ----------------------------------------------------------
    def dispatch(self, method: str, path: str, body: Any, token: str = "") -> tuple[int, Any]:
        if not self._limiter.allow():
            raise ApiError(429, "rate_limited", "Too many requests to the local service")
        parsed = urllib.parse.urlparse(path)
        for route_method, pattern, handler in self.routes:
            if route_method != method:
                continue
            import re

            match = re.fullmatch(pattern, parsed.path)
            if not match:
                continue
            if handler.__name__ != "health" and not self._token_ok(token):
                raise ApiError(401, "unauthorized", "Missing or invalid service token")
            kwargs = {k: urllib.parse.unquote(v) for k, v in match.groupdict().items()}
            query = urllib.parse.parse_qs(parsed.query)
            try:
                return 200, {"ok": True, "data": handler(body, query=query, **kwargs)}
            except ApiError:
                raise
            except EngineError as exc:
                raise ApiError(409, exc.code, exc.message) from exc
            except ProviderError as exc:
                raise ApiError(502, exc.kind, exc.user_message(), exc.to_dict()) from exc
            except (ValidationError, SettingsError) as exc:
                detail = getattr(exc, "errors", None)
                raise ApiError(400, "invalid_input", str(exc), detail) from exc
            except CaptureError as exc:
                raise ApiError(409, exc.reason, exc.message, exc.to_dict()) from exc
            except ValueError as exc:
                raise ApiError(400, "invalid_input", str(exc)) from exc
        raise ApiError(404, "not_found", f"No route for {method} {parsed.path}")

    def _token_ok(self, token: str) -> bool:
        return bool(token) and secrets.compare_digest(token, self.rt.token)

    # -- health & capabilities --------------------------------------------
    def health(self, body: Any, **_: Any) -> dict[str, Any]:
        return {
            "app": APP_NAME,
            "version": __version__,
            "pid": __import__("os").getpid(),
            "stopped": self.rt.engine.stopped,
        }

    def capabilities(self, body: Any, **_: Any) -> dict[str, Any]:
        observer_caps = self.rt.observer.capabilities()
        display = self.rt.observer.display
        gateway_status = self.rt.gateway.status().to_dict()
        privacy = self.rt.settings.section("privacy")
        return {
            "app": {"name": APP_NAME, "version": __version__},
            "display": display.to_dict(),
            "capture": observer_caps["capture"],
            "accessibility": observer_caps["accessibility"],
            "ocr": observer_caps["ocr"],
            "vision": gateway_status,
            "speech": {"tts": tts_capability(str(self.rt.settings.get("voice.tts_engine") or "auto")), "stt": stt_capability()},
            "automation": {
                "enabled": bool(self.rt.settings.get("automation.assisted_actions_enabled")),
                "paste": bool(self.rt.settings.get("automation.allow_paste")),
                "focus": bool(self.rt.settings.get("automation.allow_focus")),
                "scroll": bool(self.rt.settings.get("automation.allow_scroll")),
                "implemented": ["paste_text"],
                "cli_tools": {
                    "xdotool": bool(util.which("xdotool")),
                    "xclip": bool(util.which("xclip")),
                },
            },
            "privacy": {
                "share_enabled": bool(privacy.get("share_enabled")),
                "paused": bool(privacy.get("paused")),
                "stop_capture": bool(privacy.get("stop_capture")),
                "retain_screenshots": False,
                "local_only": bool(privacy.get("local_only")),
                "exclusion_patterns": privacy.get("exclusion_patterns", []),
            },
            "stats": observer_caps["stats"],
            "stopped": self.rt.engine.stopped,
        }

    # -- settings ----------------------------------------------------------
    def get_settings(self, body: Any, **_: Any) -> dict[str, Any]:
        data = self.rt.settings.public_view
        data["secret_store"] = self.rt.secrets_store.describe()
        # ``public_view`` can only see the environment variable; the key is
        # normally stored in the OS keyring, so the real answer comes from the
        # secret store. Only a boolean is exposed, never the value.
        data["ai"]["has_api_key"] = self.rt.secrets_store.has_key()
        data["runtime"] = {
            "session_type": self.rt.observer.display.session_type,
            "overlay_supported": self.rt.observer.display.overlay_supported,
            "overlay_backend": self.rt.observer.display.overlay_backend,
            "overlay_notes": self.rt.observer.display.overlay_notes,
        }
        return data

    def update_settings(self, body: Any, **_: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_input", "Settings patch must be a JSON object")
        if "privacy" in body and isinstance(body["privacy"], dict):
            if body["privacy"].get("share_enabled") and not self.rt.settings.get("privacy.acknowledge_cloud_vision"):
                gateway = self.rt.settings.section("ai")
                remote = gateway.get("provider") == "openai_compat" and not str(gateway.get("base_url", "")).startswith(
                    ("http://127.0.0.1", "http://localhost")
                )
                if remote:
                    raise ApiError(
                        409,
                        "cloud_consent_required",
                        "A remote vision provider is configured. Acknowledge that screenshots may leave this machine before enabling screen sharing.",
                    )
        updated = self.rt.settings.update(body)
        if "voice" in updated:
            self.rt.speech_out.enabled = bool(updated["voice"].get("enabled")) and bool(updated["voice"].get("speak_steps"))
            self.rt.speech_out.engine = util.which(str(updated["voice"].get("tts_engine"))) or self.rt.speech_out.engine
        if "ai" in body:
            self.rt.gateway.reset_budget(int(updated["ai"].get("call_budget") or 0))
        self.rt.db.add_audit("settings_updated", None, {"sections": sorted(body.keys())})
        return self.rt.settings.public_view

    def set_api_key(self, body: Any, **_: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or not isinstance(body.get("api_key"), str):
            raise ApiError(400, "invalid_input", "api_key must be a string")
        value = body["api_key"].strip()
        if not value:
            raise ApiError(400, "invalid_input", "api_key must not be empty")
        backend = self.rt.secrets_store.set(value)
        self.rt.db.add_audit("api_key_stored", None, {"backend": backend})
        return {"stored": True, "backend": backend, "secret_store": self.rt.secrets_store.describe()}

    def delete_api_key(self, body: Any, **_: Any) -> dict[str, Any]:
        self.rt.secrets_store.delete()
        self.rt.db.add_audit("api_key_deleted", None, {})
        return {"deleted": True, "secret_store": self.rt.secrets_store.describe()}

    # -- observation -------------------------------------------------------
    def observe(self, body: Any, **_: Any) -> dict[str, Any]:
        payload = body if isinstance(body, dict) else {}
        if self.rt.engine.stopped:
            raise ApiError(409, "emergency_stopped", "Screen capture is stopped. Enable screen sharing first.")
        if not self.rt.settings.get("privacy.share_enabled"):
            raise ApiError(403, "share_disabled", "Screen sharing is off. Enable it in GuidePilot to capture the screen.")
        observation = self.rt.observer.observe(
            force=bool(payload.get("force", True)),
            scope=payload.get("scope"),
            app_hint=payload.get("app_hint") or "",
        )
        return {"observation": observation.to_dict()}

    def latest_observation(self, body: Any, **_: Any) -> dict[str, Any]:
        observation = self.rt.observer.latest()
        return {"observation": observation.to_dict() if observation else None}

    # -- tasks -------------------------------------------------------------
    def list_tasks(self, body: Any, **_: Any) -> dict[str, Any]:
        return {"tasks": self.rt.db.list_tasks()}

    def create_task(self, body: Any, **_: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_input", "Expected a JSON object")
        objective = body.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            raise ApiError(400, "invalid_input", "objective is required")
        if not self.rt.settings.get("privacy.share_enabled"):
            raise ApiError(403, "share_disabled", "Enable screen sharing first: GuidePilot guides from what it can see.")
        context_values = body.get("context_values") if isinstance(body.get("context_values"), dict) else {}
        result = self.rt.engine.create_task(
            objective=objective,
            context_values=context_values,
            app_hint=str(body.get("app_hint") or ""),
            project_dir=body.get("project_dir") if isinstance(body.get("project_dir"), str) else None,
            prefer_model=bool(body.get("prefer_model", True)),
        )
        self._speak_guidance(result)
        return result

    def _require_task_id(self, task_id: str) -> None:
        current = self.rt.engine.current_task_id
        if current != task_id:
            # Only operate on the in-memory active task; loaded history is read-only.
            stored = self.rt.db.load_task(task_id)
            if stored is None:
                raise ApiError(404, "not_found", "Unknown task")
            raise ApiError(409, "inactive_task", "That task is not the active one. Resume it first.")

    def get_task(self, body: Any, task_id: str, **_: Any) -> dict[str, Any]:
        payload = self.rt.engine.get_task(task_id)
        if payload is None:
            raise ApiError(404, "not_found", "Unknown task")
        return payload

    def look_again(self, body: Any, task_id: str, **_: Any) -> dict[str, Any]:
        self._require_task_id(task_id)
        return self.rt.engine.ground_current(force_observe=True)

    def verify(self, body: Any, task_id: str, **_: Any) -> dict[str, Any]:
        self._require_task_id(task_id)
        payload = body if isinstance(body, dict) else {}
        result = self.rt.engine.verify_current(user_confirmed=bool(payload.get("user_confirmed", False)))
        self._speak_guidance(result)
        return result

    def watch(self, body: Any, task_id: str, **_: Any) -> dict[str, Any]:
        self._require_task_id(task_id)
        auto = True
        if isinstance(body, dict) and "auto_verify" in body:
            auto = bool(body["auto_verify"])
        result = self.rt.engine.watch_tick(auto_verify=auto)
        self._speak_guidance(result)
        return result

    def confirm(self, body: Any, task_id: str, **_: Any) -> dict[str, Any]:
        self._require_task_id(task_id)
        result = self.rt.engine.confirm_current()
        self._speak_guidance(result)
        return result

    def skip(self, body: Any, task_id: str, **_: Any) -> dict[str, Any]:
        self._require_task_id(task_id)
        reason = ""
        if isinstance(body, dict):
            reason = str(body.get("reason") or "")
        return self.rt.engine.skip_current(reason or "User skipped this step")

    def next_step(self, body: Any, task_id: str, **_: Any) -> dict[str, Any]:
        self._require_task_id(task_id)
        return self.rt.engine.next_step()

    def previous_step(self, body: Any, task_id: str, **_: Any) -> dict[str, Any]:
        self._require_task_id(task_id)
        result = self.rt.engine.previous_step()
        self._speak_guidance(result)
        return result

    def pause(self, body: Any, task_id: str, **_: Any) -> dict[str, Any]:
        self._require_task_id(task_id)
        return self.rt.engine.pause()

    def cancel(self, body: Any, task_id: str, **_: Any) -> dict[str, Any]:
        self._require_task_id(task_id)
        return self.rt.engine.cancel()

    def delete_task(self, body: Any, task_id: str, **_: Any) -> dict[str, Any]:
        return {"deleted": self.rt.db.delete_task(task_id)}

    def clear_tasks(self, body: Any, **_: Any) -> dict[str, Any]:
        removed = self.rt.db.clear_tasks()
        self.rt.db.add_audit("task_history_cleared", None, {"removed": removed})
        return {"cleared": removed}

    def resume_prompt(self, body: Any, **_: Any) -> dict[str, Any]:
        return {"prompt": self.rt.engine.resume_prompt()}

    def resume_task(self, body: Any, task_id: str, **_: Any) -> dict[str, Any]:
        context_values = body.get("context_values") if isinstance(body, dict) and isinstance(body.get("context_values"), dict) else None
        return self.rt.engine.resume_task(task_id, context_values=context_values)

    def save_workflow(self, body: Any, task_id: str, **_: Any) -> dict[str, Any]:
        self._require_task_id(task_id)
        name = ""
        if isinstance(body, dict):
            name = str(body.get("name") or "")
        return self.rt.engine.save_as_workflow(task_id, name=name)

    def set_context(self, body: Any, task_id: str, **_: Any) -> dict[str, Any]:
        self._require_task_id(task_id)
        if not isinstance(body, dict) or not isinstance(body.get("values"), dict):
            raise ApiError(400, "invalid_input", "values object is required")
        task = self.rt.engine.current_task
        if task is None:
            raise ApiError(404, "not_found", "No active task")
        for key, value in body["values"].items():
            if isinstance(value, (str, int, float)):
                task.context_values[str(key)] = {"value": str(value), "source": "user_provided", "sensitive": is_sensitive_label(str(key))}
            elif isinstance(value, dict):
                task.context_values[str(key)] = value
        # Re-plan from the current screen with the new information.
        self.rt.engine.ground_current(force_observe=True)
        return self.rt.engine.state_payload(task)

    # -- chat --------------------------------------------------------------
    def chat(self, body: Any, **_: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or not isinstance(body.get("message"), str):
            raise ApiError(400, "invalid_input", "message is required")
        return self.rt.engine.chat(body["message"])

    # -- workflows ---------------------------------------------------------
    def list_workflows(self, body: Any, **_: Any) -> dict[str, Any]:
        workflows = []
        for summary in self.rt.db.list_workflows():
            workflow = self.rt.db.get_workflow(summary["id"])
            if workflow:
                workflows.append({**summary, "objective": workflow.get("objective"), "step_count": len(workflow.get("steps", []))})
        return {"workflows": workflows}

    def import_workflow_route(self, body: Any, **_: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or "workflow" not in body:
            raise ApiError(400, "invalid_input", "workflow is required")
        try:
            workflow = import_workflow(body["workflow"])
        except ValidationError as exc:
            raise ApiError(400, "invalid_workflow", "The imported workflow is invalid", exc.errors) from exc
        workflow_id = self.rt.db.save_workflow(workflow)
        self.rt.db.add_audit("workflow_imported", workflow_id, {"name": workflow.get("name")})
        return {"workflow_id": workflow_id, "workflow": workflow}

    def export_workflow_route(self, body: Any, workflow_id: str, **_: Any) -> dict[str, Any]:
        workflow = self.rt.db.get_workflow(workflow_id)
        if workflow is None:
            raise ApiError(404, "not_found", "Unknown workflow")
        return {"workflow": workflow, "json": export_workflow(workflow)}

    def delete_workflow(self, body: Any, workflow_id: str, **_: Any) -> dict[str, Any]:
        return {"deleted": self.rt.db.delete_workflow(workflow_id)}

    def start_workflow(self, body: Any, workflow_id: str, **_: Any) -> dict[str, Any]:
        workflow = self.rt.db.get_workflow(workflow_id)
        if workflow is None:
            raise ApiError(404, "not_found", "Unknown workflow")
        if not self.rt.settings.get("privacy.share_enabled"):
            raise ApiError(403, "share_disabled", "Enable screen sharing first")
        observation = self.rt.observer.observe(force=True)
        drift = check_drift(workflow, observation)
        notes: list[str] = []
        if drift.get("checked") and not drift.get("matches_expected"):
            notes.append(
                "This screen differs from when the workflow was recorded. Missing controls: "
                + ", ".join(drift["missing"][:6])
                + ". I will re-ground every step on the live screen."
            )
        plan = workflow_to_plan(workflow, adaptation_notes=notes)
        context_values = (
            body.get("context_values") if isinstance(body, dict) and isinstance(body.get("context_values"), dict) else {}
        )
        payload = self.rt.engine.create_task(
            objective=workflow.get("objective") or workflow.get("name", "Saved workflow"),
            context_values=context_values,
            app_hint=workflow.get("app_hint", ""),
            prefer_model=False,
            plan_override=plan,
            extra_warnings=notes,
            workflow_id=workflow_id,
        )
        payload["workflow_drift"] = drift
        return payload

    # -- privacy / audit / usage ------------------------------------------
    def audit(self, body: Any, query: dict[str, Any], **_: Any) -> dict[str, Any]:
        limit = 200
        if "limit" in query:
            try:
                limit = max(1, min(2000, int(query["limit"][0])))
            except (TypeError, ValueError):
                limit = 200
        return {"events": self.rt.db.list_audit(limit)}

    def usage(self, body: Any, **_: Any) -> dict[str, Any]:
        return {"usage": self.rt.db.usage_summary(), "gateway": self.rt.gateway.status().to_dict()}

    def privacy_stop(self, body: Any, **_: Any) -> dict[str, Any]:
        return self.rt.engine.emergency_stop()

    def privacy_enable(self, body: Any, **_: Any) -> dict[str, Any]:
        payload = body if isinstance(body, dict) else {}
        result = self.rt.engine.clear_stop()
        if payload.get("acknowledge_cloud_vision"):
            self.rt.settings.update({"privacy": {"acknowledge_cloud_vision": True}})
        return result

    def privacy_purge(self, body: Any, **_: Any) -> dict[str, Any]:
        counts = self.rt.db.purge_all()
        self.rt.observer.clear()
        self.rt.db.add_audit("local_data_cleared", None, counts)
        return {"cleared": counts}

    # -- speech ------------------------------------------------------------
    def speech_status(self, body: Any, **_: Any) -> dict[str, Any]:
        return {
            "enabled": bool(self.rt.settings.get("voice.enabled")),
            "tts": self.rt.speech_out.capability(),
            "stt": self.rt.speech_in.capability(),
            "push_to_talk": self.rt.settings.get("voice.push_to_talk"),
        }

    def speech_speak(self, body: Any, **_: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise ApiError(400, "invalid_input", "text is required")
        if not self.rt.settings.get("voice.enabled"):
            raise ApiError(409, "voice_disabled", "Voice output is disabled in settings")
        return self.rt.speech_out.speak(body["text"])

    # -- assisted actions --------------------------------------------------
    def assisted_paste(self, body: Any, **_: Any) -> dict[str, Any]:
        """Paste approved non-sensitive text into the focused field.

        Hard requirements: automation enabled, paste allowed, explicit
        per-request confirmation, non-sensitive value, no secret patterns, and
        accessibility metadata proving the focused window is not excluded.
        """
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_input", "Expected a JSON object")
        if not self.rt.settings.get("automation.assisted_actions_enabled") or not self.rt.settings.get(
            "automation.allow_paste"
        ):
            raise ApiError(403, "automation_disabled", "Assisted actions are disabled in settings")
        if not bool(body.get("confirmed")):
            raise ApiError(409, "confirmation_required", "Assisted paste needs explicit confirmation for this action")
        value = body.get("text")
        if not isinstance(value, str) or not value:
            raise ApiError(400, "invalid_input", "text is required")
        if len(value) > 512:
            raise ApiError(400, "invalid_input", "Assisted paste is limited to 512 characters")
        from .security.redaction import contains_secret

        if body.get("sensitive") or contains_secret(value):
            raise ApiError(403, "sensitive_value", "GuidePilot refuses to paste values that look like credentials")
        observation = self.rt.observer.observe(force=True)
        if observation.sensitive:
            raise ApiError(403, "sensitive_window", "The active window is privacy-excluded")
        xdotool = util.which("xdotool")
        if not xdotool:
            raise ApiError(503, "tool_missing", "xdotool is required for assisted paste and is not installed")
        if self.rt.observer.display.session_type != "x11":
            raise ApiError(501, "unsupported", "Assisted input is only implemented for X11 in this build")
        result = util.run_cmd([xdotool, "type", "--clearmodifiers", "--delay", "12", value], timeout=20)
        self.rt.db.add_audit(
            "assisted_paste",
            self.rt.engine.current_task_id,
            {"characters": len(value), "ok": result.ok, "window": observation.active_window.title if observation.active_window else None},
        )
        if not result.ok:
            raise ApiError(500, "paste_failed", "xdotool could not type into the focused window", util.truncated(result.stderr, 200))
        return {"pasted": True, "characters": len(value), "audited": True}

    # -- speech helper -----------------------------------------------------
    def _speak_guidance(self, payload: dict[str, Any]) -> None:
        if not self.rt.settings.get("voice.enabled"):
            return
        guidance = payload.get("guidance") if isinstance(payload, dict) else None
        if not guidance:
            return
        instruction = str(guidance.get("instruction") or "")
        if instruction:
            self.rt.speech_out.speak(instruction)

    # -- shutdown ----------------------------------------------------------
    def shutdown(self, body: Any, **_: Any) -> dict[str, Any]:
        return {"stopping": True}


class _Handler(BaseHTTPRequestHandler):
    server_version = f"GuidePilot/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib signature
        util.get_logger("api").debug(fmt % args)

    # -- helpers -----------------------------------------------------------
    @property
    def api(self) -> GuidePilotApi:
        return self.server.api  # type: ignore[attr-defined]

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _provided_token(self) -> str:
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return self.headers.get("X-GuidePilot-Token", "")

    def _authorize(self) -> bool:
        token = self.server.token  # type: ignore[attr-defined]
        provided = self._provided_token()
        return bool(provided) and secrets.compare_digest(provided, token)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin", "")
        if not origin:
            return True
        return origin in {"null", "app://guidepilot"} or origin.startswith("file://")

    def _read_body(self) -> Any:
        length_header = self.headers.get("Content-Length", "0")
        try:
            length = int(length_header)
        except ValueError:
            raise ApiError(400, "invalid_body", "Invalid Content-Length") from None
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "body_too_large", f"Request body exceeds {MAX_BODY_BYTES} bytes")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise ApiError(415, "unsupported_media_type", "Only application/json bodies are accepted")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "invalid_json", f"Invalid JSON body: {exc}") from exc

    def _handle(self, method: str) -> None:
        if not self._origin_ok():
            self._send(403, {"ok": False, "error": {"code": "forbidden_origin", "message": "Cross-origin requests are not allowed"}})
            return
        path = self.path
        is_health = urllib.parse.urlparse(path).path == "/health"
        if not is_health and not self._authorize():
            self._send(401, {"ok": False, "error": {"code": "unauthorized", "message": "Missing or invalid service token"}})
            return
        try:
            body = self._read_body() if method in {"POST", "PUT", "PATCH"} else None
            status, payload = self.api.dispatch(method, path, body, token=self._provided_token())
            self._send(status, payload)
        except ApiError as exc:
            self._send(exc.status, {"ok": False, "error": exc.to_dict()})
        except Exception as exc:  # pragma: no cover - defensive
            util.get_logger("api").error(f"unhandled API error: {exc!r}")
            self._send(500, {"ok": False, "error": {"code": "internal_error", "message": str(exc)}})

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")


class ServiceServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], api: GuidePilotApi, token: str) -> None:
        self.api = api
        self.token = token
        super().__init__(address, _Handler)


def create_server(settings: Settings | None = None, port: int | None = None, db: Database | None = None) -> tuple[ServiceServer, ServiceRuntime]:
    runtime = build_runtime(settings, db)
    token_path = write_runtime_token(runtime.token)
    util.get_logger("api").info(f"service token written to {token_path}")
    desired = int(port if port is not None else runtime.settings.get("server.port", 8765) or 8765)
    host = str(runtime.settings.get("server.host", "127.0.0.1"))
    if host not in {"127.0.0.1", "localhost", "::1"}:
        util.get_logger("api").warning(f"Refusing non-loopback bind '{host}'; using 127.0.0.1")
        host = "127.0.0.1"
    attempts = [desired, 0] if desired else [0]
    last_error: Exception | None = None
    for candidate in attempts:
        try:
            server = ServiceServer((host, candidate), GuidePilotApi(runtime), runtime.token)
            return server, runtime
        except OSError as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Could not bind the local service: {last_error}")
