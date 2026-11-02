"""The OCR field reader, exercised with a stub binary instead of Tesseract.

Tesseract is not installed on the reference machine, so the command line is
replaced by a script that answers like it: the parsing and the failure handling
are what this covers, not the recognition quality.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from guidepilot.accessibility.ocr import OcrEngine


def _stub(tmp_path: Path, body: str) -> str:
    path = tmp_path / "tesseract-stub"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _png(tmp_path: Path) -> bytes:
    # The engine only writes the bytes to a file, so its content is irrelevant.
    return b"\x89PNG\r\n\x1a\nstub"


def test_field_text_is_read_as_one_line(tmp_path):
    stub = _stub(tmp_path, "#!/bin/sh\nprintf '  GuidePilot\\n Acceptance  \\n'\n")
    engine = OcrEngine(binary=stub)
    assert engine.available()[0] is True
    assert engine.read_text(_png(tmp_path)) == "GuidePilot Acceptance"


def test_an_empty_result_is_reported_as_no_text(tmp_path):
    stub = _stub(tmp_path, "#!/bin/sh\nprintf '   \\n'\n")
    assert OcrEngine(binary=stub).read_text(_png(tmp_path)) is None


def test_a_failing_binary_yields_no_text_and_keeps_the_reason(tmp_path):
    stub = _stub(tmp_path, "#!/bin/sh\necho 'boom' >&2\nexit 3\n")
    engine = OcrEngine(binary=stub)
    assert engine.read_text(_png(tmp_path)) is None
    assert "boom" in engine.last_error


def test_missing_tesseract_explains_itself():
    engine = OcrEngine(binary=None)
    available, reason = engine.available()
    assert available is False
    assert "tesseract" in reason.lower()


def test_the_stub_is_executable(tmp_path):
    """Guard: a silent chmod failure would make every other test pass vacuously."""
    stub = _stub(tmp_path, "#!/bin/sh\nexit 0\n")
    assert os.access(stub, os.X_OK)


@pytest.mark.parametrize("text", ["EU", "https://example.com/callback", "GuidePilot Acceptance"])
def test_expected_values_survive_the_round_trip(tmp_path, text):
    stub = _stub(tmp_path, f"#!/bin/sh\nprintf '%s\\n' '{text}'\n")
    assert OcrEngine(binary=stub).read_text(_png(tmp_path)) == text
