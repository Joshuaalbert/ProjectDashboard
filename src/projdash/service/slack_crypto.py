"""Passphrase encryption helpers for UI-managed Slack bot tokens."""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

KDF_NAME = "pbkdf2_hmac_sha256"
CIPHER_NAME = "fernet"
DEFAULT_KDF_ITERATIONS = 600_000
SALT_BYTES = 16


def encrypt_slack_bot_token(
    raw_token: str,
    passphrase: str,
    *,
    salt: bytes | None = None,
    kdf_iterations: int = DEFAULT_KDF_ITERATIONS,
) -> dict[str, str | int]:
    """Encrypt a Slack bot token into command-ready storage fields."""
    if not raw_token:
        raise ValueError("raw_token must be non-empty.")
    if not passphrase:
        raise ValueError("passphrase must be non-empty.")
    if kdf_iterations <= 0:
        raise ValueError("kdf_iterations must be positive.")
    resolved_salt = salt or os.urandom(SALT_BYTES)
    key = _derive_fernet_key(passphrase, resolved_salt, kdf_iterations)
    ciphertext = Fernet(key).encrypt(raw_token.encode("utf-8")).decode("ascii")
    return {
        "ciphertext": ciphertext,
        "kdf": KDF_NAME,
        "kdf_salt": base64.b64encode(resolved_salt).decode("ascii"),
        "kdf_iterations": kdf_iterations,
        "cipher": CIPHER_NAME,
    }


def decrypt_slack_bot_token(
    encrypted_token: Mapping[str, Any] | Any,
    passphrase: str,
) -> str:
    """Decrypt an encrypted Slack bot token blob with a passphrase."""
    if not passphrase:
        raise ValueError("passphrase must be non-empty.")
    kdf = _field(encrypted_token, "kdf")
    cipher = _field(encrypted_token, "cipher")
    if kdf != KDF_NAME:
        raise ValueError(f"Unsupported Slack token KDF {kdf!r}.")
    if cipher != CIPHER_NAME:
        raise ValueError(f"Unsupported Slack token cipher {cipher!r}.")
    salt = base64.b64decode(_field(encrypted_token, "kdf_salt"))
    iterations = int(_field(encrypted_token, "kdf_iterations"))
    key = _derive_fernet_key(passphrase, salt, iterations)
    try:
        plaintext = Fernet(key).decrypt(_field(encrypted_token, "ciphertext").encode("ascii"))
    except InvalidToken as exc:
        raise ValueError(
            "Unable to decrypt Slack bot token with the provided passphrase."
        ) from exc
    return plaintext.decode("utf-8")


def _derive_fernet_key(
    passphrase: str,
    salt: bytes,
    iterations: int,
) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _field(encrypted_token: Mapping[str, Any] | Any, name: str) -> Any:
    if isinstance(encrypted_token, Mapping):
        return encrypted_token[name]
    return getattr(encrypted_token, name)
