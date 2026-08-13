from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_doc(name: str) -> str:
    return (ROOT / "docs" / name).read_text(encoding="utf-8")


def test_keyring_doc_covers_v0340_macos_keychain_and_file_backend_runtime() -> None:
    # Given
    keyring_doc = read_doc("gogcli-keyring-password.md")

    # When
    required_notes = [
        "GOG_KEYRING_BACKEND=file",
        "upstream-required `GOG_KEYRING_PASSWORD`",
        "GOG_KEYRING_PASSWORD_FILE",
        "GOG_KEYCHAIN_TRUST_APPLICATION",
        "gogcli v0.34.0",
        "macOS artifacts",
        "signed and notarized locally",
        "OpenClaw Foundation",
        "macOS Keychain trust",
        "Docker/Linux file-keyring runtime remains unchanged",
    ]

    # Then
    for note in required_notes:
        assert note in keyring_doc


def test_oauth_handoff_doc_preserves_remote_step_and_runtime_notes() -> None:
    # Given
    oauth_doc = read_doc("gogcli-oauth-handoff.md")

    # When
    required_notes = [
        "gog auth add user@example.com --services all-user --remote --step 1 --plain --no-input",
        "GOG_KEYRING_BACKEND=file",
        "upstream-required `GOG_KEYRING_PASSWORD`",
        "GOG_KEYRING_PASSWORD_FILE",
        "GOG_KEYCHAIN_TRUST_APPLICATION",
        "gogcli v0.34.0",
        "macOS artifacts",
        "signed and notarized locally",
        "OpenClaw Foundation",
        "macOS Keychain",
        "Docker/Linux file-keyring runtime remains unchanged",
    ]

    # Then
    for note in required_notes:
        assert note in oauth_doc
