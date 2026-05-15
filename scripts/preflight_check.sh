#!/usr/bin/env bash
set -euo pipefail

ROOT_BACKEND="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT_FRONTEND="$(cd "${ROOT_BACKEND}/../options-aji" && pwd)"

PYTHON_BIN="${ROOT_BACKEND}/venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

echo "==> [1/5] Frontend lint"
(
  cd "${ROOT_FRONTEND}"
  npm run -s lint
)

echo "==> [2/5] Backend tests"
(
  cd "${ROOT_BACKEND}"
  "${PYTHON_BIN}" -m pytest -q
)

echo "==> [3/5] Ensure DB tables"
(
  cd "${ROOT_BACKEND}"
  "${PYTHON_BIN}" -c "from app.db.bootstrap import init_db; init_db(); print('init_db_ok')"
)

echo "==> [4/5] Backend health smoke (optional)"
if [[ -n "${OPTIONS_AJI_BACKEND_URL:-}" ]]; then
  curl -fsS "${OPTIONS_AJI_BACKEND_URL%/}/health" >/dev/null
  echo "backend_health_ok"
else
  echo "skip backend health (OPTIONS_AJI_BACKEND_URL not set)"
fi

echo "==> [5/5] Frontend proxy smoke (optional)"
if [[ -n "${OPTIONS_AJI_FRONTEND_URL:-}" ]]; then
  curl -fsS "${OPTIONS_AJI_FRONTEND_URL%/}/api/integration/status" >/dev/null
  curl -fsS "${OPTIONS_AJI_FRONTEND_URL%/}/api/market/overview" >/dev/null
  curl -fsS "${OPTIONS_AJI_FRONTEND_URL%/}/api/feed/unified?limit=1" >/dev/null
  echo "frontend_proxy_smoke_ok"
else
  echo "skip frontend smoke (OPTIONS_AJI_FRONTEND_URL not set)"
fi

echo "preflight_check_done"
