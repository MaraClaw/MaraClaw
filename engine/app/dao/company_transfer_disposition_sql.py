FREE_INTEGRATION_DELETES = (
    "DELETE FROM agent_credentials WHERE agent_id=%(agent)s",
    "DELETE FROM gogcli_credential_states WHERE agent_id=%(agent)s",
    "DELETE FROM agent_tools WHERE agent_id=%(agent)s",
    "DELETE FROM agent_permissions WHERE agent_id=%(agent)s",
    "DELETE FROM agent_relationships WHERE agent_id=%(agent)s",
    "DELETE FROM agent_schedules WHERE agent_id=%(agent)s",
    "DELETE FROM agent_triggers WHERE agent_id=%(agent)s",
    "DELETE FROM channel_configs WHERE agent_id=%(agent)s",
    "DELETE FROM published_pages WHERE agent_id=%(agent)s",
    "DELETE FROM agent_setups WHERE agent_id=%(agent)s",
)

NEUTRALIZE_NOTIFICATIONS_SQL = (
    "UPDATE notifications SET user_id=NULL WHERE agent_id=ANY(%(agents)s) "
    "OR ref_id=ANY(%(agents)s) "
    "OR ref_id IN (SELECT id FROM tasks WHERE agent_id=ANY(%(agents)s)) "
    "OR ref_id IN (SELECT id FROM approval_requests WHERE agent_id=ANY(%(agents)s)) "
    "OR EXISTS(SELECT 1 FROM unnest(%(agents)s::uuid[]) a(id) "
    "WHERE notifications.link LIKE '%%' || a.id::text || '%%')"
)

VISIBLE_NOTIFICATION_SQL = (
    "NOT EXISTS(SELECT 1 FROM company_transfer_retained_agents r "
    "WHERE notifications.agent_id=r.agent_id OR notifications.ref_id=r.agent_id "
    "OR notifications.ref_id IN (SELECT id FROM tasks WHERE agent_id=r.agent_id) "
    "OR notifications.ref_id IN (SELECT id FROM approval_requests WHERE agent_id=r.agent_id) "
    "OR notifications.link LIKE '%%' || r.agent_id::text || '%%')"
)
