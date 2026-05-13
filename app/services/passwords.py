"""BCrypt password hashing (native ``bcrypt``; avoids passlib vs bcrypt 4.1+ wrap-bug crash)."""
from __future__ import annotations

import bcrypt

_BCRYPT_MAX_BYTES = 72


def _password_bytes(plain: str) -> bytes:
    raw = plain.encode("utf-8")
    return raw[:_BCRYPT_MAX_BYTES]


def hash_password(plain: str) -> str:
    pw = _password_bytes(plain)
    hashed = bcrypt.hashpw(pw, bcrypt.gensalt(rounds=12))
    return hashed.decode("ascii")


def verify_password(plain: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    pw = _password_bytes(plain)
    try:
        digest = password_hash.encode("ascii")
    except UnicodeEncodeError:
        return False
    try:
        return bool(bcrypt.checkpw(pw, digest))
    except ValueError:
        return False
