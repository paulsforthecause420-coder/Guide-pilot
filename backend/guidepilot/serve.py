"""Entry point for the GuidePilot local service.

Usage::

    python3 -m guidepilot.serve [--port 8765] [--print-ready]

Once the socket is listening, a single JSON handshake line is printed to stdout
so the Electron main process learns the port and where the session token lives.
All diagnostics go to stderr, keeping stdout machine-readable.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):  # allow `python3 backend/guidepilot/serve.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "guidepilot"

from guidepilot import APP_NAME, __version__, util  # noqa: E402
from guidepilot.config import Settings  # noqa: E402
from guidepilot.server import create_server  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GuidePilot local service")
    parser.add_argument("--port", type=int, default=None, help="Preferred port (0 chooses a free port)")
    parser.add_argument("--print-ready", action="store_true", help="Print a JSON readiness line on stdout")
    parser.add_argument("--log-level", default=os.environ.get("GUIDEPILOT_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    os.environ["GUIDEPILOT_LOG_LEVEL"] = args.log_level
    logger = util.get_logger("service")
    settings = Settings.load()
    server, runtime = create_server(settings, port=args.port)
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, name="guidepilot-api", daemon=True)
    thread.start()
    logger.info(f"{APP_NAME} {__version__} listening on http://{host}:{port}")

    handshake = {
        "app": APP_NAME,
        "version": __version__,
        "event": "ready",
        "host": host,
        "port": port,
        "pid": os.getpid(),
        "token_path": str(util.runtime_dir() / "token"),
        "session_type": runtime.observer.display.session_type,
        "overlay_supported": runtime.observer.display.overlay_supported,
    }
    print(json.dumps(handshake), flush=True)

    stopping = threading.Event()

    def _stop(signum: int, _frame: object) -> None:
        logger.info(f"received signal {signum}; shutting down")
        stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    try:
        while not stopping.is_set():
            time.sleep(0.25)
    finally:
        server.shutdown()
        server.server_close()
        runtime.observer.clear()
        try:
            runtime.db.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
