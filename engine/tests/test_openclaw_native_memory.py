import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.json_types import json_object_from
from app.records.agent import AgentRecord
from app.services import agent_manager as module
from app.services.agent_manager import AgentManager
from app.services.guest_resource_config import preserve_guest_resources

_RUNTIME_PROBE = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const cp = require('node:child_process');
const root = '/tmp/native-memory-proof';
const config = JSON.parse(fs.readFileSync(0, 'utf8'));
const preserved = {
  'workspace/MEMORY.md': 'The deployment codename is orchid-lantern-742.\n',
  'workspace/memory/2026-10-23.md': 'Keep the daily workspace memory.\n',
  'MEMORY.md': 'Keep the root memory file.\n',
  'memory/archive.md': 'Keep the root memory directory.\n',
  'memory/tencentdb.sqlite': 'SQLite format 3\0legacy-state',
  '.bootstrap-tencentdb-version': '1.0.3',
  'extensions/memory-tencentdb/data/memory.sqlite': 'SQLite format 3\0plugin-state',
  'extensions/memory-tencentdb/.installed': 'preserve installation evidence',
};
for (const id of ['memory-tencentdb', 'custom-memory-fixture']) {
  preserved[`extensions/${id}/package.json`] = JSON.stringify({
    name: id, version: '1.0.3', type: 'module', openclaw: {extensions: ['./index.js']},
  });
  preserved[`extensions/${id}/openclaw.plugin.json`] = JSON.stringify({
    id, ...(id === 'memory-tencentdb' ? {kind: 'context-engine'} : {}),
    activation: {onStartup: true}, configSchema: {type: 'object', properties: {}},
  });
  preserved[`extensions/${id}/index.js`] = `export default {id:'${id}',register(){}};`;
}
for (const [relative, content] of Object.entries(preserved)) {
  const path = `${root}/${relative}`;
  fs.mkdirSync(require('node:path').dirname(path), {recursive: true});
  fs.writeFileSync(path, content);
}
const env = {...process.env, OPENCLAW_HOME: '/tmp', OPENCLAW_STATE_DIR: root,
  OPENCLAW_CONFIG_PATH: `${root}/openclaw.json`};
function run(args) {
  const result = cp.spawnSync('openclaw', args, {env, encoding: 'utf8', timeout: 60000});
  assert.equal(result.status, 0, JSON.stringify({args, ...result}));
  return result.stdout;
}
function plugins() {
  return JSON.parse(run(['plugins', 'list', '--json'])).plugins;
}
const omitted = structuredClone(config);
delete omitted.plugins.entries['memory-tencentdb'];
fs.writeFileSync(env.OPENCLAW_CONFIG_PATH, JSON.stringify(omitted));
const before = plugins().find(plugin => plugin.id === 'memory-tencentdb');
assert.equal(before.enabled, true);
assert.equal(before.status, 'loaded');
fs.writeFileSync(env.OPENCLAW_CONFIG_PATH, JSON.stringify(config));
run(['config', 'validate']);
const discovered = plugins();
const retired = discovered.find(plugin => plugin.id === 'memory-tencentdb');
const custom = discovered.find(plugin => plugin.id === 'custom-memory-fixture');
assert.equal(retired.enabled, false);
assert.equal(retired.status, 'disabled');
assert.equal(custom.enabled, true);
assert.equal(custom.status, 'loaded');
run(['memory', 'index', '--force']);
const search = JSON.parse(run(['memory', 'search', '--query', 'orchid', '--json']));
assert(search.results.some(result => result.path === 'MEMORY.md'
  && result.snippet.includes('orchid-lantern-742')), JSON.stringify(search));
const miss = JSON.parse(run(['memory', 'search', '--query', 'unobtainium93642', '--json']));
assert.equal(miss.results.length, 0);
for (const [relative, content] of Object.entries(preserved)) {
  assert.equal(fs.readFileSync(`${root}/${relative}`, 'utf8'), content, relative);
}
console.log(JSON.stringify({version: run(['--version']).trim(),
  before: {enabled: before.enabled, status: before.status},
  retired: {enabled: retired.enabled, status: retired.status},
  custom: {enabled: custom.enabled, status: custom.status}, search, missing: miss.results.length}));
"""


@pytest.mark.skipif(os.environ.get("OPENCLAW_NATIVE_RUNTIME_TEST") != "1", reason="opt-in disposable Docker CLI proof")
def test_native_memory_search_and_legacy_retirement_with_pinned_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    monkeypatch.setattr(module.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setattr(module.settings, "LINKUP_PROXY_ENABLED", False)
    monkeypatch.setattr(module.settings, "LINKUP_API_KEY", "")
    agent = AgentRecord(id=uuid4(), creator_id=uuid4(), name="Native memory proof")
    directory = tmp_path / str(agent.id)
    directory.mkdir()
    path = directory / "openclaw.json"
    path.write_text(
        json.dumps(
            {
                "plugins": {
                    "entries": {"memory-tencentdb": {"enabled": True}, "custom-memory-fixture": {"enabled": True}},
                },
            }
        ),
        encoding="utf-8",
    )
    generated = AgentManager.__new__(AgentManager)._generate_openclaw_config(agent, None)
    defaults = json_object_from(json_object_from(generated["agents"])["defaults"])
    defaults["workspace"] = "/tmp/native-memory-proof/workspace"
    generated["agents"] = {"defaults": defaults}
    config = preserve_guest_resources(path, generated)
    docker = shutil.which("docker")
    assert docker is not None

    # When
    result = subprocess.run(  # noqa: S603 - fixed disposable argv; config is JSON stdin, never shell input.
        [
            docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--interactive",
            "--entrypoint",
            "node",
            "-e",
            "TMPDIR=/tmp",
            "-e",
            "NODE_COMPILE_CACHE=/tmp/node-compile-cache",
            "-e",
            "BUN_RUNTIME_TRANSPILER_CACHE_PATH=/tmp/bun-transpiler-cache",
            "openclaw:2026.10.6",
            "-e",
            _RUNTIME_PROBE,
        ],
        input=json.dumps(config),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    # Then
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert "2026.10.6" in evidence["version"]
    assert evidence["before"] == {"enabled": True, "status": "loaded"}
    assert evidence["retired"] == {"enabled": False, "status": "disabled"}
    assert evidence["custom"] == {"enabled": True, "status": "loaded"}
    assert evidence["missing"] == 0
