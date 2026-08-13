import shutil
import subprocess

from app import main


def test_load_version_info_uses_resolved_git_binary(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    git_path = "/usr/local/bin/git"
    commands: list[list[str]] = []

    monkeypatch.setattr(shutil, "which", lambda executable: git_path)
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda command, **_kwargs: commands.append(command) or b"abc123\n",
    )

    assert main._load_version_info() == {"version": "unknown", "commit": "abc123"}
    assert commands == [[git_path, "rev-parse", "--short", "HEAD"]]


def test_load_version_info_skips_probe_without_git(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    commands: list[list[str]] = []

    monkeypatch.setattr(shutil, "which", lambda executable: None)
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda command, **_kwargs: commands.append(command) or b"unexpected\n",
    )

    assert main._load_version_info() == {"version": "unknown", "commit": ""}
    assert commands == []
