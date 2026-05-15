# OptionsAji 2.0 Deployment Baseline

This document defines the minimum deployment baseline for `options-aji` (Vercel) and `options-aji-backend` (self-hosted FastAPI).

## 1) Required Environment Variables

### Frontend (`options-aji`)

- `OPTIONS_AJI_BACKEND_URL=https://<your-backend-domain>`
- `OPTIONS_AJI_BACKEND_TIMEOUT_MS=25000`
- `OPTIONS_AJI_REQUIRE_HTTPS_BACKEND=1` (recommended in production)
- `OPTIONS_AJI_API_KEY=<optional_shared_service_key>`

### Backend (`options-aji-backend`)

- `CORS_ORIGINS=https://options-aji.vercel.app,https://<your-preview-domain>`
- `CORS_ALLOW_CREDENTIALS=true` (set false if using wildcard origins)
- `SUBSCRIPTION_REQUIRED=<true|false>`
- `SUBSCRIPTION_TOKENS=<comma-separated tokens when required>`

## 2) Security Baseline

- Do not expose backend over raw HTTP on public internet in production.
- If frontend is public, backend endpoint must be HTTPS.
- Keep CORS allowlist explicit; avoid `*` in production.
- Rotate all API keys on a schedule and after incident response.

## 3) Connectivity Verification

After deploy, verify:

1. `GET /api/integration/status` via frontend route
2. `GET /api/market/overview` via frontend route
3. `GET /api/feed/unified?limit=1` via frontend route

You can also use the in-app page: `/settings/deployment`.
