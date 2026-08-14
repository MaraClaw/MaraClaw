import pytest

from app.scripts.cleanup_duplicate_feishu_users import _merge_user_profile, _user_merge_score


def test_duplicate_user_merge_prefers_real_email_without_legacy_user_fields() -> None:
    primary = {"display_name": "Primary", "email": "primary@feishu.local"}
    duplicate = {"display_name": "Duplicate", "email": "person@example.com"}

    class _U:
        def __init__(self, email: str) -> None:
            self.email: str = email

    if _user_merge_score(_U("primary@feishu.local")) != 0:
        pytest.fail("placeholder email must not be preferred")
    if _user_merge_score(_U("person@example.com")) != 100:
        pytest.fail("real email must be preferred")

    updates = _merge_user_profile(primary, duplicate)

    if updates.get("email") != "person@example.com":
        pytest.fail("real email must replace a placeholder email")
