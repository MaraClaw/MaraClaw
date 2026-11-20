import os
import stat
from pathlib import Path

import pytest

from app.services.workspace_mirror_scope import (
    GuestPathError,
    WalkLimitError,
    case_collisions,
    in_scope,
    is_root_file,
    is_shareable,
    normalize_guest_path,
    scope_storage_prefixes,
    walk_local,
)

ALLOWED = [
    "workspace/a.txt",
    "workspace/r.docx",
    "workspace/sub/dir/notes.md",
    "workspace/Báo cáo tháng 11.docx",
    "memory/MEMORY.md",
    "daily_reports/2026-11-19.md",
    "skills/x/SKILL.md",
    "media/outbound/a.png",
    "media/outbound/nested/b.png",
    "AGENTS.md",
    "HEARTBEAT.md",
    "soul.md",
    "state.json",
]

DENIED = [
    "",
    "openclaw.json",
    ".maraclaw-gateway-key",
    ".maraclaw-mirror-generation",
    "agents/main/agent/x.sqlite",
    "workspace",
    "workspace/",
    "memory",
    "media/outbound",
    "media/outbound/",
    "media/inbound/a.png",
    "media/a.png",
    "SOUL.md",
    "soul.md/x",
    "workspace/a.sqlite",
    "workspace/a.sqlite3",
    "workspace/a.db",
    "workspace/A.DB",
    "workspace/a.sqlite-wal",
    "workspace/a.db-shm",
    "workspace/a-journal",
    "workspace/a.tmp",
    "workspace/a~",
    "workspace/a.swp",
    "workspace/a.part",
    "workspace/.env",
    "workspace/x/.git/config",
    "workspace/.hidden/a.txt",
    "workspace/dir/.DS_Store",
    "workspace/node_modules/a.js",
    "workspace/p/__pycache__/m.pyc",
    "workspace/venv/bin/python",
    "workspace/p/site-packages/m.py",
    "workspace/a\\b",
    "workspace//a",
    "workspace/./a",
    "workspace/../memory/a",
    "/workspace/a",
    " workspace/a",
    "workspace/a ",
    "workspace/a\x00b",
    "workspace/a\nb",
    "workspace/a\x7fb",
    "workspace/a\udcffb",
]


@pytest.mark.parametrize("rel", ALLOWED)
def test_in_scope_allows(rel: str):
    assert in_scope(rel)


@pytest.mark.parametrize("rel", DENIED)
def test_in_scope_denies(rel: str):
    assert not in_scope(rel)


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("workspace/a.docx", True),
        ("workspace/sub/b.md", True),
        ("media/outbound/a.png", True),
        ("memory/MEMORY.md", False),
        ("daily_reports/d.md", False),
        ("skills/x/SKILL.md", False),
        ("soul.md", False),
        ("workspace/.env", False),
        ("workspace/a.sqlite", False),
        ("media/inbound/a.png", False),
        ("openclaw.json", False),
    ],
)
def test_is_shareable(rel: str, expected: bool):
    assert is_shareable(rel) is expected


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("AGENTS.md", True),
        ("soul.md", True),
        ("state.json", True),
        ("workspace/AGENTS.md", False),
        ("openclaw.json", False),
    ],
)
def test_is_root_file(rel: str, expected: bool):
    assert is_root_file(rel) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/home/node/custom-state/workspace/Tavily_Skill_Test_Report.docx", "workspace/Tavily_Skill_Test_Report.docx"),
        ("/home/node/custom-state/.openclaw/workspace/a.txt", "workspace/a.txt"),
        (".openclaw/workspace/a.txt", "workspace/a.txt"),
        ("workspace/a.txt", "workspace/a.txt"),
        ("  workspace/a.txt\n", "workspace/a.txt"),
        ("./workspace/a.txt", "workspace/a.txt"),
        ("/home/node/custom-state/memory/MEMORY.md", "memory/MEMORY.md"),
        ("/home/node/custom-state/soul.md", "soul.md"),
    ],
)
def test_normalize_guest_path_maps_guest_names_to_relative_paths(raw: str, expected: str):
    assert normalize_guest_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "/etc/passwd",
        "/home/node/other/workspace/a.txt",
        "/home/node/custom-state",
        "/home/node/custom-state/",
        "/home/node/custom-state//etc/passwd",
        "/home/node/custom-state/../etc/passwd",
        "../x",
        "workspace/../../x",
        "",
        "   ",
        "workspace/a\x00b",
    ],
)
def test_normalize_guest_path_rejects_paths_outside_the_agent_directory(raw: str):
    with pytest.raises(GuestPathError):
        _ = normalize_guest_path(raw)


def test_normalize_guest_path_leaves_scope_to_the_caller():
    assert normalize_guest_path("/home/node/custom-state/openclaw.json") == "openclaw.json"
    assert not in_scope("openclaw.json")


def test_scope_storage_prefixes_are_the_in_scope_trees():
    prefixes = scope_storage_prefixes()

    assert prefixes == ("workspace/", "memory/", "daily_reports/", "skills/", "media/outbound/")
    assert all(in_scope(f"{prefix}a.txt") for prefix in prefixes)


def test_case_collisions_report_every_path_in_a_clash():
    paths = ["workspace/A.txt", "workspace/a.txt", "workspace/b.txt", "workspace/Straße.txt", "workspace/STRASSE.txt"]

    assert case_collisions(paths) == {
        "workspace/A.txt",
        "workspace/a.txt",
        "workspace/Straße.txt",
        "workspace/STRASSE.txt",
    }
    assert case_collisions(["workspace/a.txt", "workspace/a.txt"]) == set()
    assert case_collisions([]) == set()


IN_SCOPE_FILES = [
    "workspace/a.txt",
    "workspace/sub/b.docx",
    "workspace/h1.txt",
    "memory/MEMORY.md",
    "skills/s/SKILL.md",
    "media/outbound/o.png",
    "AGENTS.md",
    "soul.md",
    "state.json",
]
OUT_OF_SCOPE_FILES = [
    "workspace/.env",
    "workspace/node_modules/x.js",
    "workspace/x.sqlite",
    "workspace/sub/.git/config",
    "workspace/.cache/c",
    "media/inbound/i.png",
    "openclaw.json",
    ".maraclaw-gateway-key",
    "agents/main/agent/x.sqlite",
    "memory/.hidden",
]


def _build_agent(tmp_path: Path) -> Path:
    agent = tmp_path / "agent"
    outside = tmp_path / "outside"
    outside.mkdir()
    _ = (outside / "secret.txt").write_text("secret")
    for rel in [*IN_SCOPE_FILES, *OUT_OF_SCOPE_FILES]:
        path = agent / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(rel)
    workspace = agent / "workspace"
    (workspace / "dirlink").symlink_to(outside, target_is_directory=True)
    (workspace / "filelink.txt").symlink_to(outside / "secret.txt")
    os.mkfifo(workspace / "pipe")
    os.link(workspace / "h1.txt", workspace / "h2.txt")
    (agent / "daily_reports").symlink_to(outside, target_is_directory=True)
    return agent


def test_walk_local_yields_only_in_scope_regular_files(tmp_path: Path):
    agent = _build_agent(tmp_path)

    found = walk_local(agent, deadline_seconds=30)

    assert [file.rel for file in found] == sorted([*IN_SCOPE_FILES, "workspace/h2.txt"])


def test_walk_local_reports_the_stat_of_each_file(tmp_path: Path):
    agent = _build_agent(tmp_path)

    by_rel = {file.rel: file for file in walk_local(agent, deadline_seconds=30)}

    info = (agent / "workspace" / "a.txt").stat()
    plain = by_rel["workspace/a.txt"]
    assert (plain.ino, plain.size, plain.mtime_ns, plain.ctime_ns, plain.nlink) == (
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        1,
    )
    first, second = by_rel["workspace/h1.txt"], by_rel["workspace/h2.txt"]
    assert (first.nlink, second.nlink) == (2, 2)
    assert first.ino == second.ino


def test_walk_local_agrees_with_the_scope_predicate(tmp_path: Path):
    agent = _build_agent(tmp_path)
    expected: set[str] = set()
    for directory, _dirs, names in os.walk(agent, followlinks=False):
        for name in names:
            path = Path(directory) / name
            rel = path.relative_to(agent).as_posix()
            if stat.S_ISREG(path.lstat().st_mode) and in_scope(rel):
                expected.add(rel)

    assert {file.rel for file in walk_local(agent, deadline_seconds=30)} == expected


def test_walk_local_raises_for_a_missing_agent_directory(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        _ = walk_local(tmp_path / "missing", deadline_seconds=30)


def test_walk_local_refuses_a_symlinked_agent_directory(tmp_path: Path):
    agent = _build_agent(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(agent, target_is_directory=True)

    with pytest.raises(OSError):
        _ = walk_local(link, deadline_seconds=30)


def _small_agent(tmp_path: Path) -> Path:
    agent = tmp_path / "agent"
    (agent / "workspace").mkdir(parents=True)
    _ = (agent / "workspace" / "a.txt").write_text("a")
    _ = (agent / "workspace" / "b.txt").write_text("b")
    return agent


def test_walk_local_counts_every_entry_it_examines(tmp_path: Path):
    agent = _small_agent(tmp_path)

    assert [file.rel for file in walk_local(agent, deadline_seconds=30, max_entries=3)] == [
        "workspace/a.txt",
        "workspace/b.txt",
    ]
    with pytest.raises(WalkLimitError):
        _ = walk_local(agent, deadline_seconds=30, max_entries=2)


def test_walk_local_bounds_entries_that_are_not_mirrored(tmp_path: Path):
    agent = tmp_path / "agent"
    (agent / "workspace").mkdir(parents=True)
    for index in range(30):
        _ = (agent / "workspace" / f".junk{index}").write_text("x")

    with pytest.raises(WalkLimitError):
        _ = walk_local(agent, deadline_seconds=30, max_entries=10)


def test_walk_local_stops_when_the_deadline_has_passed(tmp_path: Path):
    agent = _small_agent(tmp_path)

    with pytest.raises(WalkLimitError):
        _ = walk_local(agent, deadline_seconds=0)


def test_walk_local_refuses_trees_nested_beyond_its_depth_bound(tmp_path: Path):
    shallow = tmp_path / "shallow"
    (shallow / "workspace" / "/".join(["d"] * 10)).mkdir(parents=True)
    deep = tmp_path / "deep"
    (deep / "workspace" / "/".join(["d"] * 70)).mkdir(parents=True)

    assert walk_local(shallow, deadline_seconds=30) == []
    with pytest.raises(WalkLimitError):
        _ = walk_local(deep, deadline_seconds=30)
