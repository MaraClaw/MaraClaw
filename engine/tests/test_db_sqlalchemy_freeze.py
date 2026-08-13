"""Ensure app/ stays free of SQLAlchemy imports."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_no_new_sqlalchemy.py"


def _load_freeze_module():
    spec = importlib.util.spec_from_file_location("check_no_new_sqlalchemy", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_freeze_script_passes_on_current_tree() -> None:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_app_has_no_sqlalchemy_imports() -> None:
    module = _load_freeze_module()
    assert module.files_importing_sqlalchemy() == []


def test_freeze_detector_flags_sqlalchemy_import(tmp_path: Path, monkeypatch) -> None:
    module = _load_freeze_module()
    fake_app = tmp_path / "app"
    fake_app.mkdir()
    (fake_app / "sneaky.py").write_text("import sqlalchemy\n", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "APP_ROOT", fake_app)
    assert module.files_importing_sqlalchemy() == ["app/sneaky.py"]


def test_allowlist_is_empty() -> None:
    allowlist = ROOT / "scripts" / "sqlalchemy_import_allowlist.txt"
    assert allowlist.is_file()
    lines = [
        line.strip()
        for line in allowlist.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines == []
