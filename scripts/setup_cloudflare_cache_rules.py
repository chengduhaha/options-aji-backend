#!/usr/bin/env python3
"""Create Cloudflare Cache Rules so public read-only API endpoints are served
from the edge instead of always hitting FastAPI.

Requires CLOUDFLARE_API_TOKEN with Zone Cache Rules Edit on options-aji.com.

Usage:
    export CLOUDFLARE_API_TOKEN=...
    python scripts/setup_cloudflare_cache_rules.py

Optional env:
    CF_ZONE_NAME (default options-aji.com)
    CF_API_HOST (default api.options-aji.com)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

ZONE_NAME = os.environ.get("CF_ZONE_NAME", "options-aji.com")
API_HOST = os.environ.get("CF_API_HOST", "api.options-aji.com")
API_BASE = "https://api.cloudflare.com/client/v4"

# Public, JWT-free GET endpoints safe to edge-cache.
CACHEABLE_PREFIXES = (
    "/api/blog/posts",
    "/api/blog/courses",
    "/api/blog/documents",
)


class CfApiError(RuntimeError):
    pass


def _token() -> str:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not token:
        raise CfApiError(
            "CLOUDFLARE_API_TOKEN is not set. Create a token at "
            "https://dash.cloudflare.com/profile/api-tokens with "
            "Zone -> Cache Rules -> Edit on " + ZONE_NAME + "."
        )
    return token


def _request(method: str, path: str, *, body: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise CfApiError(f"{method} {url} -> {exc.code}: {detail}") from exc


def _zone_id() -> str:
    data = _request("GET", f"/zones?name={ZONE_NAME}")
    zones = data.get("result") or []
    if not zones:
        raise CfApiError(f"Zone {ZONE_NAME} not found on this account.")
    return zones[0]["id"]


def _existing_rules(zone_id: str) -> list[dict]:
    data = _request("GET", f"/zones/{zone_id}/cache_rules")
    return data.get("result") or []


def _build_rule(prefix: str) -> dict:
    return {
        "name": f"Cache {prefix}",
        "description": (
            f"Edge-cache public GET {prefix}* responses for 120s. Authenticated "
            "or JWT-gated variants must remain private/no-store on the origin."
        ),
        "expression": (
            f'(http.host eq "{API_HOST}" and starts_with(http.request.uri.path, "{prefix}"))'
        ),
        "action": "set_cache_settings",
        "action_parameters": {
            "cache": True,
            "edge_ttl": {"mode": "respect_origin", "default": 120},
            "browser_ttl": {"mode": "respect_origin", "default": 60},
            "serve_when_ttl_expired": True,
        },
    }


def main() -> int:
    zone_id = _zone_id()
    existing = _existing_rules(zone_id)
    existing_by_name = {r.get("name"): r for r in existing}

    for prefix in CACHEABLE_PREFIXES:
        rule = _build_rule(prefix)
        name = rule["name"]
        if name in existing_by_name:
            rule_id = existing_by_name[name]["id"]
            print(f"Updating {name} ({rule_id})...")
            _request("PUT", f"/zones/{zone_id}/cache_rules/{rule_id}", body=rule)
        else:
            print(f"Creating {name}...")
            _request("POST", f"/zones/{zone_id}/cache_rules", body=rule)

    print("\nDone. Public GET endpoints under:")
    for prefix in CACHEABLE_PREFIXES:
        print(f"  https://{API_HOST}{prefix}*")
    print("now respect origin Cache-Control and may be served from the edge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
