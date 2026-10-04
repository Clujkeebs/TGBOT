"""Fernet encryption for the hot-wallet key + constant-time comparisons."""
from __future__ import annotations

import hmac

from cryptography.fernet import Fernet, InvalidToken


class KeyVault:
    def __init__(self, fernet_key: str):
        try:
            self._f = Fernet(fernet_key.encode() if isinstance(fernet_key, str) else fernet_key)
        except (ValueError, TypeError) as e:
            raise ValueError(
                "FERNET_KEY is invalid. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            ) from e

    def encrypt(self, data: bytes) -> str:
        return self._f.encrypt(data).decode()

    def decrypt(self, token: str) -> bytes:
        try:
            return self._f.decrypt(token.encode())
        except InvalidToken as e:
            raise ValueError("Cannot decrypt wallet: FERNET_KEY does not match the one used to create it") from e


def constant_time_eq(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode(), b.encode())
