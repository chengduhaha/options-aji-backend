"""Health endpoint."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health_ping() -> dict[str, str]:
    return {"status": "ok"}
