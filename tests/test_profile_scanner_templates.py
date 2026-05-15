"""Integration tests for profile scanner templates endpoints."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.profile import router as profile_router


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(profile_router)
    return TestClient(app)


def test_scanner_template_crud_roundtrip() -> None:
    client = _build_client()
    api_key = f"test-key-{uuid.uuid4().hex}"

    payload = {
        "api_key": api_key,
        "name": "swing-high-iv",
        "config": {
            "preset": "high_iv_rank",
            "query_text": "高 IV 科技股",
            "symbol_scope": "NVDA,TSLA,AMD",
            "dte_min": "7",
            "dte_max": "45",
            "delta_min": "0.2",
            "delta_max": "0.6",
            "iv_min": "30",
            "iv_max": "120",
            "expiration_scope": "next_three",
            "sort_field": "iv",
            "sort_direction": "desc",
        },
    }

    create_resp = client.post("/api/profile/scanner-templates", json=payload)
    assert create_resp.status_code == 200
    create_json = create_resp.json()
    assert create_json["success"] is True
    template_id = int(create_json["data"]["id"])

    list_resp = client.get("/api/profile/scanner-templates", params={"api_key": api_key})
    assert list_resp.status_code == 200
    list_json = list_resp.json()
    assert list_json["success"] is True
    matched = [row for row in list_json["data"] if int(row["id"]) == template_id]
    assert len(matched) == 1
    assert matched[0]["config"]["sort_field"] == "iv"

    update_resp = client.post(
        "/api/profile/scanner-templates",
        json={
            **payload,
            "template_id": template_id,
            "name": "swing-high-iv-v2",
            "config": {**payload["config"], "sort_direction": "asc"},
        },
    )
    assert update_resp.status_code == 200
    update_json = update_resp.json()
    assert update_json["data"]["name"] == "swing-high-iv-v2"
    assert update_json["data"]["config"]["sort_direction"] == "asc"

    delete_resp = client.delete(
        f"/api/profile/scanner-templates/{template_id}",
        params={"api_key": api_key},
    )
    assert delete_resp.status_code == 200
    delete_json = delete_resp.json()
    assert delete_json["success"] is True
    assert int(delete_json["deleted"]) == 1
