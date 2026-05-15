# OptionsAji 2.0 Release Checklist

This is the final pre-release checklist for the current 2.0 milestone set.

## 1) Environment Baseline

- Frontend (`options-aji` / Vercel):
  - `OPTIONS_AJI_BACKEND_URL`
  - `OPTIONS_AJI_BACKEND_TIMEOUT_MS`
  - `OPTIONS_AJI_REQUIRE_HTTPS_BACKEND=1` (production)
- Backend (`options-aji-backend`):
  - `DATABASE_URL`
  - `CORS_ORIGINS`
  - `CORS_ALLOW_CREDENTIALS`
  - `OPENROUTER_API_KEY`
  - `XPOZ_API_KEY`
  - `FEATURE_SOCIAL_ENABLED=true`
  - `FEATURE_DEEP_AGENT_ENABLED=true`

See also: `DEPLOYMENT_BASELINE.md`.

## 2) One-Command Validation

Run from `options-aji-backend`:

```bash
bash scripts/preflight_check.sh
```

This script runs:

- frontend lint (`options-aji`)
- backend tests (`pytest -q`)
- database table bootstrap (`init_db`)
- optional HTTP smoke tests (if `OPTIONS_AJI_FRONTEND_URL` / `OPTIONS_AJI_BACKEND_URL` provided)

## 3) Manual Smoke Flow (UI)

- Dashboard:
  - `Market Pulse` renders
  - `AI 摘要` can load
- Stock Deep Dive:
  - `/stock/SPY/overview` renders key cards
- Scanner:
  - Run with DTE/Delta/IV filters
  - Sort switch works
  - Save / overwrite / apply / delete template works
- AI Analyst:
  - SSE planning and sub-agent events stream normally
  - timing summaries show in trace card
- Profile / Personal center:
  - `/profile` loads (guest: login CTA + API key 自选/提醒; user: /me summary)

## 4) API Smoke Endpoints

Frontend proxy:

- `/api/integration/status`
- `/api/market/overview`
- `/api/feed/unified?limit=1`

Backend direct:

- `/health`
- `/api/profile/scanner-templates?api_key=<key>`
- `/api/agent/query` (SSE)

## 5) Current Automated Coverage

- `test_profile_scanner_templates.py` (create/list/update/delete)
- `test_agent_sse.py` (SSE event contract + timestamps)
- existing analytics/gex tests

Current baseline: backend `7 passed`, frontend lint clean.
