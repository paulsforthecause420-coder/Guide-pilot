"""Small dependency-free helpers shared across the GuidePilot backend."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

APP_DIR_NAME = "guidepilot"


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def _xdg(env: str, fallback: str) -> Path:
    raw = os.environ.get(env, "").strip()
    base = Path(raw) if raw else Path.home() / fallback
    return base / APP_DIR_NAME


def config_dir() -> Path:
    """User configuration directory (~/.config/guidepilot)."""
    return _xdg("XDG_CONFIG_HOME", ".config")


def data_dir() -> Path:
    """User data directory (~/.local/share/guidepilot)."""
    return _xdg("XDG_DATA_HOME", os.path.join(".local", "share"))


def cache_dir() -> Path:
    """Ephemeral capture scratch space (~/.cache/guidepilot)."""
    return _xdg("XDG_CACHE_HOME", ".cache")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return config_dir() / "settings.json"


def database_path() -> Path:
    return data_dir() / "guidepilot.db"


def runtime_dir() -> Path:
    """Directory holding the per-run API bearer token."""
    return config_dir() / "runtime"


def log_path() -> Path:
    return data_dir() / "logs" / "guidepilot.log"


# ---------------------------------------------------------------------------
# Time / math
# ---------------------------------------------------------------------------


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def round_rect(rect: Sequence[float]) -> list[int]:
    return [int(round(v)) for v in rect]


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    if denominator == 0 or math.isnan(denominator):
        return default
    return numerator / denominator


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def atomic_write_json(path: Path, payload: Any, mode: int | None = None) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", mode=mode)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default


def clean_temp_dir(path: Path) -> None:
    """Remove a scratch directory tree, ignoring errors."""
    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Shell


@dataclass
class CommandResult:
    argv: list[str]
    code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    def summary(self) -> str:
        if self.timed_out:
            return f"{self.argv[0]}: timed out after {self.duration_ms}ms"
        return f"{self.argv[0]}: exit {self.code}"


def which(name: str) -> str | None:
    return shutil.which(name)


def run_cmd(
    argv: Sequence[str],
    timeout: float = 10.0,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    cwd: str | None = None,
) -> CommandResult:
    """Run a command without a shell; never raises for non-zero exits.

    Only absolute binaries or binaries resolved via PATH are executed, and the
    argument vector is passed directly so no shell interpolation can happen.
    """
    started = now_ms()
    merged_env = dict(os.environ)
    if env:
        merged_env.update({k: str(v) for k, v in env.items()})
    try:
        completed = subprocess.run(
            [str(a) for a in argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=merged_env,
            input=input_text,
            cwd=cwd,
        )
        return CommandResult(
            argv=[str(a) for a in argv],
            code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_ms=now_ms() - started,
        )
    except subprocess.TimeoutExpired:
        return CommandResult([str(a) for a in argv], 124, "", f"timeout after {timeout}s", now_ms() - started, True)
    except FileNotFoundError as exc:
        return CommandResult([str(a) for a in argv], 127, "", str(exc), now_ms() - started)
    except OSError as exc:  # pragma: no cover - defensive
        return CommandResult([str(a) for a in argv], 126, "", str(exc), now_ms() - started)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+", re.UNICODE)


def normalize_text(value: str | None) -> str:
    """Casefold, strip accents, drop punctuation and collapse whitespace.

    Used for tolerant label matching between user/model target descriptions and
    accessibility labels ("Save Configuration" == "save  configuration:").
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(value))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = stripped.casefold()
    folded = _PUNCT_RE.sub(" ", folded)
    return _WS_RE.sub(" ", folded).strip()


def truncated(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "\u2026"


def unique_preserve(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{12,})"),
    re.compile(r"(gh[pousr]_[A-Za-z0-9]{16,})"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{12,})"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)(\S{6,})"),
    re.compile(r"(?i)(password\s*[=:]\s*)(\S{3,})"),
    re.compile(r"(?i)(token\s*[=:]\s*)(\S{8,})"),
]


def scrub_secrets(value: str) -> str:
    """Best-effort redaction for log lines. Never used as a security boundary."""
    if not value:
        return ""
    out = value
    for pattern in _SECRET_PATTERNS:
        def _sub(match: re.Match[str]) -> str:
            if match.lastindex and match.lastindex >= 2:
                return f"{match.group(1)}[REDACTED]"
            return "[REDACTED]"

        out = pattern.sub(_sub, out)
    return out


class Logger:
    """Thread-safe logger writing scrubbed lines to stderr and a log file."""

    def __init__(self, name: str = "guidepilot", level: str = "INFO", file_path: Path | None = None) -> None:
        self.name = name
        self.level = level.upper()
        self.file_path = file_path if file_path is not None else log_path()
        self._lock = threading.Lock()
        self._levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

    def _write(self, level: str, message: str) -> None:
        if self._levels.get(level, 20) < self._levels.get(self.level, 20):
            return
        line = f"{utc_iso()} {level:<8} {self.name}: {scrub_secrets(message)}"
        with self._lock:
            try:
                ensure_dir(self.file_path.parent)
                if self.file_path.exists() and self.file_path.stat().st_size > 2 * 1024 * 1024:
                    os.replace(self.file_path, self.file_path.with_suffix(".log.1"))
            except OSError:
                pass
            try:
                with open(self.file_path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass
            if os.environ.get("GUIDEPILOT_LOG_STDERR", "1") == "1":
                print(line, file=sys.stderr, flush=True)

    def debug(self, message: str) -> None:
        self._write("DEBUG", message)

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warning(self, message: str) -> None:
        self._write("WARNING", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)


_LOG_LOCK = threading.Lock()
_LOGGERS: dict[str, Logger] = {}


def get_logger(name: str = "guidepilot") -> Logger:
    with _LOG_LOCK:
        if name not in _LOGGERS:
            _LOGGERS[name] = Logger(name, level=os.environ.get("GUIDEPILOT_LOG_LEVEL", "INFO"))
        return _LOGGERS[name]


# ---------------------------------------------------------------------------
# Rate limiting / budgets


class SlidingWindow:
    """Counts events in a trailing time window (thread-safe)."""

    def __init__(self, window_seconds: float, limit: int) -> None:
        self.window_seconds = window_seconds
        self.limit = limit
        self._events: list[float] = []
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            cutoff = now - self.window_seconds
            self._events = [t for t in self._events if t >= cutoff]
            if len(self._events) >= self.limit:
                return False
            self._events.append(now)
            return True

    def count(self) -> int:
        now = time.monotonic()
        with self._lock:
            cutoff = now - self.window_seconds
            self._events = [t for t in self._events if t >= cutoff]
            return len(self._events)


@dataclass
class BudgetCounter:
    """Simple budget meter persisted by the gateway."""

    limit: int = 0
    used: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def remaining(self) -> int:
        if self.limit <= 0:
            return -1
        return max(0, self.limit - self.used)

    def consume(self, amount: int = 1) -> bool:
        with self.lock:
            if self.limit > 0 and self.used + amount > self.limit:
                return False
            self.used += amount
            return True

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {"limit": self.limit, "used": self.used, "remaining": self.remaining}


# ---------------------------------------------------------------------------
# Misc


def first_true(values: Iterable[Any], predicate) -> Any | None:
    for value in values:
        if predicate(value):
            return value
    return None


def chunked(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]
