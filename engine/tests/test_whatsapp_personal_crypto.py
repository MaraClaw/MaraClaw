from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag

from app.services.channels.whatsapp_personal_crypto import AuthCipher, AuthKey


def test_auth_cipher_binds_ciphertext_to_exact_epoch_category_and_key():
    # Given an encrypted Signal key and a separate auth epoch.
    cipher = AuthCipher("test-only-secret")
    key = AuthKey(uuid4(), uuid4(), "session", "sender")
    sealed = cipher.seal(key, '{"private":"key"}')
    # When opened with the exact context or a different epoch.
    assert cipher.open(key, sealed) == '{"private":"key"}'
    with pytest.raises(InvalidTag):
        cipher.open(AuthKey(key.config_id, uuid4(), key.category, key.key_id), sealed)
    # Then ciphertext never contains the reusable plaintext.
    assert b"private" not in sealed


def test_auth_cipher_rejects_ciphertext_tampering():
    # Given authenticated encrypted credentials.
    cipher = AuthCipher("test-only-secret")
    key = AuthKey(uuid4(), uuid4(), "creds", "full")
    sealed = cipher.seal(key, "secret")
    # When one ciphertext byte changes.
    damaged = sealed[:-1] + bytes([sealed[-1] ^ 1])
    # Then authenticated decryption fails closed.
    with pytest.raises(InvalidTag):
        cipher.open(key, damaged)
