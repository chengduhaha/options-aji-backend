"""Health endpoint."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


def _health_payload() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health")
def health_ping() -> dict[str, str]:
    return _health_payload()


@router.get("/api/health")
def health_ping_api_prefix() -> dict[str, str]:
    """Alias for ops/docs that assume /api/* prefix (e.g. curl .../api/health)."""
    return _health_payload()
