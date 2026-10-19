_PARTICIPANTS = "SELECT id FROM participants WHERE type = 'agent' AND ref_id = %(aid)s"
_POSTS = "SELECT id FROM plaza_posts WHERE author_type = 'agent' AND author_id = %(aid)s"

AGENT_DELETE_CLEANUP_SQL: tuple[str, ...] = (
    "DELETE FROM agent_activity_logs WHERE agent_id = %(aid)s",
    "DELETE FROM audit_logs WHERE agent_id = %(aid)s",
    "DELETE FROM approval_requests WHERE agent_id = %(aid)s",
    "DELETE FROM chat_messages WHERE agent_id = %(aid)s OR participant_id IN (" + _PARTICIPANTS + ")",
    "DELETE FROM chat_sessions WHERE agent_id = %(aid)s OR peer_agent_id = %(aid)s "
    "OR participant_id IN (" + _PARTICIPANTS + ")",
    "DELETE FROM agent_schedules WHERE agent_id = %(aid)s",
    "DELETE FROM agent_triggers WHERE agent_id = %(aid)s",
    "DELETE FROM channel_configs WHERE agent_id = %(aid)s",
    "DELETE FROM agent_permissions WHERE agent_id = %(aid)s",
    "DELETE FROM agent_tools WHERE agent_id = %(aid)s",
    "DELETE FROM agent_relationships WHERE agent_id = %(aid)s",
    "DELETE FROM gateway_messages WHERE agent_id = %(aid)s",
    "DELETE FROM published_pages WHERE agent_id = %(aid)s",
    "DELETE FROM notifications WHERE agent_id = %(aid)s",
    "DELETE FROM daily_token_usage WHERE agent_id = %(aid)s",
    "DELETE FROM task_logs WHERE task_id IN (SELECT id FROM tasks WHERE agent_id = %(aid)s)",
    "DELETE FROM tasks WHERE agent_id = %(aid)s",
    "DELETE FROM gateway_messages WHERE sender_agent_id = %(aid)s",
    "DELETE FROM agent_agent_relationships WHERE agent_id = %(aid)s OR target_agent_id = %(aid)s",
    "DELETE FROM plaza_comments WHERE (author_type = 'agent' AND author_id = %(aid)s) OR post_id IN (" + _POSTS + ")",
    "DELETE FROM plaza_likes WHERE (author_type = 'agent' AND author_id = %(aid)s) OR post_id IN (" + _POSTS + ")",
    "DELETE FROM plaza_posts WHERE author_type = 'agent' AND author_id = %(aid)s",
    "DELETE FROM participants WHERE type = 'agent' AND ref_id = %(aid)s",
)
