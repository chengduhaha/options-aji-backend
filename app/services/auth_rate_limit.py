"""Redis-backed rate limits for registration and login lockout."""
from __future__ import annotations

import logging

from app.config import get_settings
from app.services.cache_service import redis_client_optional

logger = logging.getLogger(__name__)

LOGIN_FAIL_MAX = 5
LOGIN_LOCK_SEC = 900


def _client():
    return redis_client_optional()


def register_rate_limited(client_ip: str) -> bool:
    """Return True if this client_ip should be blocked (too many register attempts per window)."""
    cfg = get_settings()
    if not cfg.auth_register_rate_limit_enabled:
        return False
    r = _client()
    if r is None:
        return False
    max_per = max(1, int(cfg.auth_register_max_per_hour))
    window = max(60, int(cfg.auth_register_window_seconds))
    key = f"auth:register_rl:{client_ip}"
    try:
        n = int(r.incr(key))
        if n == 1:
            r.expire(key, window)
        return n > max_per
    except Exception as exc:
        logger.debug("register_rate_limited redis error: %s", exc)
        return False


def is_login_locked(email_norm: str) -> bool:
    r = _client()
    if r is None:
        return False
    try:
        return bool(r.exists(f"auth:login_lock:{email_norm}"))
    except Exception as exc:
        logger.debug("is_login_locked redis error: %s", exc)
        return False


def record_login_failure(email_norm: str) -> None:
    r = _client()
    if r is None:
        return
    fk = f"auth:login_fail:{email_norm}"
    lk = f"auth:login_lock:{email_norm}"
    try:
        fails = int(r.incr(fk))
        if fails == 1:
            r.expire(fk, LOGIN_LOCK_SEC)
        if fails >= LOGIN_FAIL_MAX:
            r.setex(lk, LOGIN_LOCK_SEC, "1")
            r.delete(fk)
    except Exception as exc:
        logger.debug("record_login_failure redis error: %s", exc)


def clear_login_failure(email_norm: str) -> None:
    r = _client()
    if r is None:
        return
    try:
        r.delete(f"auth:login_fail:{email_norm}")
        r.delete(f"auth:login_lock:{email_norm}")
    except Exception as exc:
        logger.debug("clear_login_failure redis error: %s", exc)
