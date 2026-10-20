_AGENTS = "SELECT id FROM agents WHERE tenant_id = %(tid)s"
_USERS = "SELECT id FROM users WHERE tenant_id = %(tid)s"
_AUTHORS = (
    "(author_type = 'agent' AND author_id IN (" + _AGENTS + ")) OR "
    "(author_type = 'user' AND author_id IN (" + _USERS + "))"
)
_PARTICIPANTS = (
    "SELECT id FROM participants WHERE "
    "(type = 'agent' AND ref_id IN (" + _AGENTS + ")) OR (type = 'user' AND ref_id IN (" + _USERS + "))"
)
_POSTS = "SELECT id FROM plaza_posts WHERE tenant_id = %(tid)s OR " + _AUTHORS
_TASKS = (
    "SELECT id FROM tasks WHERE agent_id IN (" + _AGENTS + ") OR created_by IN (" + _USERS + ") "
    "OR supervision_target_user_id IN (" + _USERS + ")"
)

TENANT_DELETE_CLEANUP_SQL: tuple[str, ...] = (
    "DELETE FROM plaza_comments WHERE " + _AUTHORS + " OR post_id IN (" + _POSTS + ")",
    "DELETE FROM plaza_likes WHERE " + _AUTHORS + " OR post_id IN (" + _POSTS + ")",
    "DELETE FROM plaza_posts WHERE tenant_id = %(tid)s OR " + _AUTHORS,
    "DELETE FROM sso_scan_sessions WHERE tenant_id = %(tid)s OR user_id IN (" + _USERS + ")",
    "DELETE FROM approval_requests WHERE agent_id IN (" + _AGENTS + ") OR resolved_by IN (" + _USERS + ")",
    "DELETE FROM notifications WHERE agent_id IN (" + _AGENTS + ") OR user_id IN (" + _USERS + ")",
    "DELETE FROM agent_agent_relationships WHERE agent_id IN ("
    + _AGENTS
    + ") OR target_agent_id IN ("
    + _AGENTS
    + ") OR created_by_user_id IN ("
    + _USERS
    + ") OR updated_by_user_id IN ("
    + _USERS
    + ")",
    "DELETE FROM agent_relationships WHERE agent_id IN (" + _AGENTS + ") OR member_id IN "
    "(SELECT id FROM org_members WHERE tenant_id = %(tid)s) OR created_by_user_id IN ("
    + _USERS
    + ") OR updated_by_user_id IN ("
    + _USERS
    + ")",
    "DELETE FROM task_logs WHERE task_id IN (" + _TASKS + ")",
    "DELETE FROM tasks WHERE id IN (" + _TASKS + ")",
    "DELETE FROM chat_messages WHERE agent_id IN ("
    + _AGENTS
    + ") OR user_id IN ("
    + _USERS
    + ") OR participant_id IN ("
    + _PARTICIPANTS
    + ")",
    "DELETE FROM chat_sessions WHERE agent_id IN ("
    + _AGENTS
    + ") OR peer_agent_id IN ("
    + _AGENTS
    + ") OR user_id IN ("
    + _USERS
    + ") OR participant_id IN ("
    + _PARTICIPANTS
    + ")",
    "DELETE FROM participants WHERE id IN (" + _PARTICIPANTS + ")",
    "DELETE FROM agent_schedules WHERE agent_id IN (" + _AGENTS + ") OR created_by IN (" + _USERS + ")",
    "DELETE FROM agent_triggers WHERE agent_id IN (" + _AGENTS + ")",
    "DELETE FROM channel_configs WHERE agent_id IN (" + _AGENTS + ")",
    "DELETE FROM agent_permissions WHERE agent_id IN (" + _AGENTS + ")",
    "DELETE FROM agent_credentials WHERE agent_id IN (" + _AGENTS + ")",
    "DELETE FROM agent_activity_logs WHERE agent_id IN (" + _AGENTS + ")",
    "DELETE FROM gateway_messages WHERE agent_id IN ("
    + _AGENTS
    + ") OR sender_agent_id IN ("
    + _AGENTS
    + ") OR sender_user_id IN ("
    + _USERS
    + ")",
    "DELETE FROM published_pages WHERE tenant_id = %(tid)s OR agent_id IN ("
    + _AGENTS
    + ") OR user_id IN ("
    + _USERS
    + ")",
    "DELETE FROM audit_logs WHERE agent_id IN (" + _AGENTS + ") OR user_id IN (" + _USERS + ")",
    "DELETE FROM agent_tools WHERE agent_id IN (" + _AGENTS + ")",
    "DELETE FROM workspace_edit_locks WHERE agent_id IN (" + _AGENTS + ") OR user_id IN (" + _USERS + ")",
    "DELETE FROM daily_token_usage WHERE tenant_id = %(tid)s OR agent_id IN (" + _AGENTS + ")",
    "DELETE FROM agents WHERE tenant_id = %(tid)s",
    "DELETE FROM skill_files WHERE skill_id IN (SELECT id FROM skills WHERE tenant_id = %(tid)s)",
    "DELETE FROM skills WHERE tenant_id = %(tid)s",
    "DELETE FROM llm_models WHERE tenant_id = %(tid)s",
    "DELETE FROM tools WHERE tenant_id = %(tid)s",
    "DELETE FROM okr_settings WHERE tenant_id = %(tid)s",
    "DELETE FROM work_reports WHERE tenant_id = %(tid)s",
    "DELETE FROM okr_objectives WHERE tenant_id = %(tid)s",
    "DELETE FROM org_members WHERE tenant_id = %(tid)s",
    "DELETE FROM identity_providers WHERE tenant_id = %(tid)s",
    "DELETE FROM invitation_codes WHERE tenant_id = %(tid)s OR created_by IN (" + _USERS + ")",
    "UPDATE agent_templates SET created_by = NULL WHERE created_by IN (" + _USERS + ")",
    "UPDATE enterprise_info SET updated_by = NULL WHERE updated_by IN (" + _USERS + ")",
    "DELETE FROM users WHERE tenant_id = %(tid)s",
    "DELETE FROM tenants WHERE id = %(tid)s",
)
