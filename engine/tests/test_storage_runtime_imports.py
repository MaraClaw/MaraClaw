from app.services.storage_runtime import agent_workspace_key, normalize_storage_key


def test_storage_runtime_exports_key_helpers():
    assert normalize_storage_key("../agent//workspace/./note.md") == "agent/workspace/note.md"
    assert agent_workspace_key("agent-id", "../uploads/report.txt") == "agent-id/workspace/uploads/report.txt"
