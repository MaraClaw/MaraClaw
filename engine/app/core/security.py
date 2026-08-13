"""Security utilities: JWT, password hashing, and authentication dependencies."""

import asyncio
import base64
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import bcrypt
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import get_settings
from app.dao import user_dao
from app.records.user import UserRecord

settings = get_settings()

# Bearer token scheme
security = HTTPBearer()

# Thread pool for CPU-intensive bcrypt operations (avoids blocking the event loop)
_bcrypt_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bcrypt")


class AccessTokenPayload(TypedDict):
    sub: str
    role: str
    exp: int


def hash_password(password: str) -> str:
    """Hash a password using bcrypt (sync, for use in background tasks)."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash (sync, for use in background tasks)."""
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


async def hash_password_async(password: str) -> str:
    """Hash a password using bcrypt without blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_bcrypt_executor, hash_password, password)


async def verify_password_async(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash without blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_bcrypt_executor, verify_password, plain_password, hashed_password)


def encrypt_data(plaintext: str, key: str) -> str:
    """Encrypt a string using AES-256-CBC with the given key.

    Args:
        plaintext: The string to encrypt
        key: The encryption key (will be hashed to 32 bytes)

    Returns:
        Base64-encoded encrypted string with IV prefix
    """
    if not plaintext:
        return ""

    # Derive 32-byte key from the secret key
    key_bytes = key.encode("utf-8")
    # Use SHA-256 hash to get exactly 32 bytes for AES-256
    import hashlib

    aes_key = hashlib.sha256(key_bytes).digest()

    # Generate random 16-byte IV
    iv = os.urandom(16)

    # Create cipher and encrypt
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    padded_data = pad(plaintext.encode("utf-8"), AES.block_size)
    encrypted = cipher.encrypt(padded_data)

    # Prepend IV to ciphertext and encode as base64
    return base64.b64encode(iv + encrypted).decode("utf-8")


def decrypt_data(ciphertext: str, key: str) -> str:
    """Decrypt a string encrypted with encrypt_data.

    Args:
        ciphertext: Base64-encoded encrypted string with IV prefix
        key: The encryption key (must match the key used for encryption)

    Returns:
        Decrypted plaintext string

    Raises:
        ValueError: If decryption fails (wrong key, corrupted data, etc.)
    """
    if not ciphertext:
        return ""

    try:
        # Decode base64
        raw = base64.b64decode(ciphertext)

        # Extract IV (first 16 bytes) and ciphertext
        iv = raw[:16]
        encrypted = raw[16:]

        # Derive key
        import hashlib

        aes_key = hashlib.sha256(key.encode("utf-8")).digest()

        # Decrypt
        cipher = AES.new(aes_key, AES.MODE_CBC, iv)
        padded_data = cipher.decrypt(encrypted)
        return unpad(padded_data, AES.block_size).decode("utf-8")
    except Exception as e:
        raise ValueError(f"Decryption failed: {e}") from e


def create_access_token(user_id: str, role: str, expires_delta: timedelta | None = None) -> str:
    """Create a JWT access token."""
    expire = datetime.now(UTC) + (expires_delta or timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode = {
        "sub": user_id,
        "role": role,
        "exp": expire,
    }
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> AccessTokenPayload:
    """Decode and validate a JWT access token."""
    try:
        return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UserRecord:
    """Dependency to get the current authenticated and active user."""
    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    try:
        uid = uuid.UUID(str(user_id))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    user = await user_dao.get_with_identity(uid)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


async def get_authenticated_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UserRecord:
    """Dependency to get the current authenticated user (even if not active yet)."""
    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    try:
        uid = uuid.UUID(str(user_id))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    user = await user_dao.get_with_identity(uid)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def get_current_admin(current_user: UserRecord = Depends(get_current_user)) -> UserRecord:
    """Dependency to require admin role (platform_admin or org_admin)."""
    identity_is_platform_admin = bool(getattr(getattr(current_user, "identity", None), "is_platform_admin", False))
    if current_user.role not in ("platform_admin", "org_admin") and not identity_is_platform_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


# Role hierarchy: higher index = more privileges
ROLE_HIERARCHY = ["member", "agent_admin", "org_admin", "platform_admin"]


def require_role(*allowed_roles: str):
    """Factory to create a dependency that checks if the user has one of the allowed roles.

    Usage:
        @router.post("/", dependencies=[Depends(require_role("org_admin", "platform_admin"))])
        async def my_endpoint(...):
    """

    async def _check(current_user=Depends(get_current_user)):
        identity_is_platform_admin = bool(getattr(getattr(current_user, "identity", None), "is_platform_admin", False))
        if current_user.role not in allowed_roles and not (
            "platform_admin" in allowed_roles and identity_is_platform_admin
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"One of the following roles is required: {', '.join(allowed_roles)}",
            )
        return current_user

    return _check
