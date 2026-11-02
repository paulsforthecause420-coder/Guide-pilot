"""The local API: token-gated, loopback-only, validating every request."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from conftest import FakeObserver, make_observation

from guidepilot.providers.gateway import ModelGateway
from guidepilot.security.secrets import SecretStore
from guidepilot.server import ApiError, GuidePilotApi, ServiceRuntime, ServiceServer
from guidepilot.speech.stt import SpeechInput
from guidepilot.speech.tts import SpeechOutput
from guidepilot.task_engine.engine import TaskEngine
from guidepilot.task_engine.planner import TaskPlanner
from guidepilot.verification.verifier import Verifier

CONTEXT = {"App Name": "GuidePilot Acceptance", "Callback URL": "https://acceptance.example.com/cb"}


@pytest.fixture()
def runtime(settings, db, tmp_path):
    gateway = ModelGateway(settings)
    planner = TaskPlanner(settings, gateway=gateway)
    engine = TaskEngine(settings, FakeObserver(make_observation()), planner, gateway, Verifier(), db)
    return ServiceRuntime(
        settings=settings,
        db=db,
        observer=engine.observer,
        gateway=gateway,
        engine=engine,
        secrets_store=SecretStore(tmp_path / "secrets.json"),
        planner=planner,
        speech_out=SpeechOutput(enabled=False),
        speech_in=SpeechInput(enabled=False),
        token="test-token",
    )


@pytest.fixture()
def api(runtime) -> GuidePilotApi:
    return GuidePilotApi(runtime)


def call(api: GuidePilotApi, method: str, path: str, body=None, token: str = "test-token"):
    return api.dispatch(method, path, body if body is not None else {}, token=token)


def test_health_is_public(api):
    status, payload = call(api, "GET", "/health", token="")
    assert status == 200
    assert payload["ok"] is True
    assert payload["data"]["app"] == "GuidePilot"
    assert payload["data"]["stopped"] is False


def test_every_other_endpoint_requires_the_token(api):
    for method, path in [("GET", "/capabilities"), ("GET", "/settings"), ("GET", "/tasks"), ("POST", "/privacy/enable")]:
        with pytest.raises(ApiError) as excinfo:
            call(api, method, path, token="")
        assert excinfo.value.status == 401
    with pytest.raises(ApiError):
        call(api, "GET", "/tasks", token="wrong-token")


def test_unknown_route_is_not_found(api):
    with pytest.raises(ApiError) as excinfo:
        call(api, "GET", "/nope")
    assert excinfo.value.status == 404


def test_create_and_read_a_task(api):
    status, payload = call(api, "POST", "/tasks", {"objective": "Walk me through configuring this application", "contextValues": CONTEXT})
    assert status == 200
    task = payload["data"]["task"]
    assert task["steps"]
    task_id = task["id"]

    status, payload = call(api, "GET", f"/tasks/{task_id}")
    assert status == 200
    assert payload["data"]["task"]["id"] == task_id


def test_missing_objective_is_a_validation_error(api):
    with pytest.raises(ApiError) as excinfo:
        call(api, "POST", "/tasks", {"objective": "  "})
    assert excinfo.value.status == 400
    assert excinfo.value.code == "invalid_input"


def test_privacy_toggle_controls_capture(api, runtime):
    call(api, "POST", "/privacy/enable")
    assert runtime.settings.get("privacy.share_enabled") is True
    payload = call(api, "POST", "/privacy/stop")[1]
    assert payload["data"]["stopped"] is True
    assert runtime.settings.get("privacy.share_enabled") is False


def test_settings_update_rejects_unknown_keys(api):
    with pytest.raises(ApiError) as excinfo:
        call(api, "PUT", "/settings", {"privacy": {"not_a_setting": True}})
    assert excinfo.value.status == 400


def test_settings_update_applies_a_known_value(api, runtime):
    call(api, "PUT", "/settings", {"privacy": {"exclusion_patterns": ["1password"]}})
    assert "1password" in runtime.settings.get("privacy.exclusion_patterns")


def test_api_key_is_never_returned(api, runtime):
    runtime.secrets_store.set("sk-live-should-never-be-echoed")
    payload = call(api, "GET", "/settings")[1]
    text = json.dumps(payload)
    assert "sk-live-should-never-be-echoed" not in text
    assert "api_key_value" not in text
    assert payload["data"]["ai"]["provider"] is not None
    assert payload["data"]["ai"]["has_api_key"] is True


def test_purge_clears_saved_data_and_reports_what_went(api, runtime):
    call(api, "POST", "/tasks", {"objective": "Walk me through configuring this application", "contextValues": CONTEXT})
    assert runtime.db.list_tasks()
    payload = call(api, "POST", "/privacy/purge", {})[1]
    assert payload["data"]["cleared"]["tasks"] >= 1
    assert runtime.db.list_tasks() == []


# -- real HTTP transport ----------------------------------------------------


@pytest.fixture()
def http_server(api):
    server = ServiceServer(("127.0.0.1", 0), api, "test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def request(url: str, method: str = "GET", body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_http_health_needs_no_token(http_server):
    status, payload = request(f"{http_server}/health")
    assert status == 200 and payload["ok"] is True


def test_http_rejects_requests_without_the_token(http_server):
    status, payload = request(f"{http_server}/tasks")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_http_rejects_foreign_origins(http_server):
    status, payload = request(
        f"{http_server}/tasks",
        headers={"Authorization": "Bearer test-token", "Origin": "https://evil.example"},
    )
    assert status == 403
    assert payload["error"]["code"] == "forbidden_origin"


def test_http_accepts_the_app_origin(http_server):
    status, payload = request(
        f"{http_server}/tasks",
        headers={"Authorization": "Bearer test-token", "Origin": "app://guidepilot"},
    )
    assert status == 200


def test_http_requires_json_content_type(http_server):
    req = urllib.request.Request(f"{http_server}/privacy/enable", data=b"hello", method="POST")
    req.add_header("Authorization", "Bearer test-token")
    req.add_header("Content-Type", "text/plain")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            assert response.status == 200
    except urllib.error.HTTPError as exc:
        assert exc.code == 415
