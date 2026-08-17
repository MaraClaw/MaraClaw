from app.core.middleware import is_quiet_access_log


def test_session_message_polls_are_quiet() -> None:
    path = "/api/agents/2755f2dd-22e6-4f2b-962e-cc58d1624d40/sessions/1308c354-84f0-40a1-9446-90b77b85773e/messages"
    assert is_quiet_access_log("GET", path) is True
    assert is_quiet_access_log("POST", path) is False


def test_health_and_unread_and_guest_poll_are_quiet() -> None:
    assert is_quiet_access_log("GET", "/api/health") is True
    assert is_quiet_access_log("GET", "/api/notifications/unread-count") is True
    assert is_quiet_access_log("GET", "/api/gateway/poll") is True
    assert is_quiet_access_log("POST", "/api/gateway/heartbeat") is True


def test_chat_and_report_stay_loud() -> None:
    assert is_quiet_access_log("POST", "/api/gateway/report") is False
    assert is_quiet_access_log("GET", "/api/agents/2755f2dd-22e6-4f2b-962e-cc58d1624d40") is False
