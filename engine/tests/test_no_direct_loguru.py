"""Process-logger freeze detector."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_no_direct_loguru.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_no_direct_loguru", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_detector_flags_direct_import(tmp_path: Path, monkeypatch) -> None:
    module = _load()
    fake_app = tmp_path / "app"
    fake_app.mkdir()
    (fake_app / "bad.py").write_text("from loguru import logger\n", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "APP_ROOT", fake_app)
    assert any(path.endswith("bad.py") for path in module.files_importing_loguru())
