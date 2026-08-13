from openclaw_officecli_smoke_cleanup import CleanupFailure, cleanup_error_message, run_cleanup


class CleanupProbeError(Exception):
    """Raised by the simulated permission-normalization probe."""


def test_cleanup_attempts_later_phases_when_probe_raises() -> None:
    # Given
    calls: list[str] = []
    probe_error = CleanupProbeError()

    def probe() -> None:
        calls.append("probe")
        raise probe_error

    def remove_online_container() -> None:
        calls.append("online-container")

    def remove_offline_container() -> None:
        calls.append("offline-container")

    def remove_image() -> None:
        calls.append("image")

    def remove_state() -> None:
        calls.append("state")

    # When
    failures = run_cleanup(
        (
            ("permission normalization", probe),
            ("online container removal", remove_online_container),
            ("offline container removal", remove_offline_container),
            ("image removal", remove_image),
            ("temporary state deletion", remove_state),
        )
    )

    # Then
    assert calls == ["probe", "online-container", "offline-container", "image", "state"]
    assert failures == (CleanupFailure("permission normalization", probe_error),)


def test_cleanup_failures_when_test_is_already_failing_do_not_mask_original_error() -> None:
    # Given
    cleanup_failure = CleanupFailure("image removal", RuntimeError("image missing"))
    original_error = RuntimeError("linux/arm64 image build failed")

    # When
    message = cleanup_error_message((cleanup_failure,), original_error)

    # Then
    assert message is None
    assert original_error.__notes__ == ["Cleanup failures:\nimage removal: RuntimeError('image missing')"]
