#!/usr/bin/env bash
# 快速更新部署脚本 (已安装后使用)
set -euo pipefail
GREEN='\033[0;32m'; NC='\033[0m'
log() { echo -e "${GREEN}[DEPLOY]${NC} $*"; }

APP_DIR="/opt/options-aji-backend"
BRANCH="claude/redesign-options-platform-bxjbU"

log "拉取最新代码..."
cd "${APP_DIR}"
git fetch origin
git checkout "${BRANCH}"
git pull origin "${BRANCH}"

log "更新依赖..."
source venv/bin/activate
pip install -r requirements.txt -q

log "数据库迁移..."
export DATABASE_URL=$(grep ^DATABASE_URL .env | cut -d= -f2-)
python -c "from app.db.bootstrap import init_db; init_db()"
deactivate

log "重启服务..."
systemctl restart optionsaji
sleep 3
systemctl status optionsaji --no-pager | head -20

log "部署完成!"
