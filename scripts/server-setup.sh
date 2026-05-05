#!/usr/bin/env bash
# =============================================================================
# 阿吉美股期权平台 — 服务器一键安装脚本
# 适用: Ubuntu 22.04 / 24.04 LTS
# 用法: bash scripts/server-setup.sh
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[SETUP]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()  { echo -e "${RED}[ERR]${NC}   $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请以 root 运行: sudo bash scripts/server-setup.sh"

APP_DIR="/opt/options-aji-backend"
APP_USER="optionsaji"
PY_VER="3.11"

# ─── 1. 系统更新 ──────────────────────────────────────────────────────────────
log "1/8 系统更新..."
apt-get update -qq
apt-get install -y -qq \
  curl wget gnupg2 ca-certificates lsb-release \
  build-essential git software-properties-common \
  python3.11 python3.11-venv python3.11-dev python3-pip \
  libpq-dev gcc

# ─── 2. PostgreSQL 16 ─────────────────────────────────────────────────────────
log "2/8 安装 PostgreSQL 16..."
if ! command -v psql &>/dev/null; then
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /etc/apt/trusted.gpg.d/postgresql.gpg
  echo "deb [signed-by=/etc/apt/trusted.gpg.d/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq
  apt-get install -y -qq postgresql-16 postgresql-client-16
fi
systemctl enable postgresql
systemctl start postgresql
log "PostgreSQL: $(psql --version)"

# ─── 3. Redis 7 ───────────────────────────────────────────────────────────────
log "3/8 安装 Redis 7..."
if ! command -v redis-server &>/dev/null; then
  curl -fsSL https://packages.redis.io/gpg | gpg --dearmor -o /etc/apt/trusted.gpg.d/redis.gpg
  echo "deb [signed-by=/etc/apt/trusted.gpg.d/redis.gpg] https://packages.redis.io/deb $(lsb_release -cs) main" > /etc/apt/sources.list.d/redis.list
  apt-get update -qq
  apt-get install -y -qq redis-server
fi
# 配置 Redis: 启用持久化 + 最大内存 512MB
cat > /etc/redis/redis.conf.d/optionsaji.conf 2>/dev/null || true
redis-cli config set maxmemory 512mb 2>/dev/null || true
redis-cli config set maxmemory-policy allkeys-lru 2>/dev/null || true
redis-cli config set save "900 1 300 10" 2>/dev/null || true
systemctl enable redis-server
systemctl start redis-server
log "Redis: $(redis-server --version)"

# ─── 4. 创建数据库和用户 ───────────────────────────────────────────────────────
log "4/8 创建 PostgreSQL 数据库..."
DB_NAME="optionsaji"
DB_USER="optionsaji"
DB_PASS="$(openssl rand -hex 16)"

# 检查是否已存在
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1; then
  warn "用户 ${DB_USER} 已存在，跳过创建"
else
  sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';"
  sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"
  sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};"
  # 保存密码到文件
  echo "DATABASE_URL=postgresql+psycopg2://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}" > /root/.optionsaji_db_creds
  chmod 600 /root/.optionsaji_db_creds
  log "数据库凭证保存到 /root/.optionsaji_db_creds"
fi

# 读取密码（如果已有）
if [[ -f /root/.optionsaji_db_creds ]]; then
  source /root/.optionsaji_db_creds
  DB_URL="$DATABASE_URL"
else
  DB_URL="postgresql+psycopg2://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}"
fi

# ─── 5. 创建应用用户 ───────────────────────────────────────────────────────────
log "5/8 创建应用用户 ${APP_USER}..."
if ! id -u "${APP_USER}" &>/dev/null; then
  useradd --system --shell /bin/bash --home-dir "${APP_DIR}" --create-home "${APP_USER}"
fi

# ─── 6. 克隆/更新代码 ─────────────────────────────────────────────────────────
log "6/8 部署代码..."
if [[ -d "${APP_DIR}/.git" ]]; then
  log "更新已有代码库..."
  cd "${APP_DIR}"
  git fetch origin
  git checkout claude/redesign-options-platform-bxjbU
  git pull origin claude/redesign-options-platform-bxjbU
else
  log "首次克隆..."
  git clone https://github.com/chengduhaha/options-aji-backend.git "${APP_DIR}"
  cd "${APP_DIR}"
  git checkout claude/redesign-options-platform-bxjbU
fi
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

# ─── 7. Python 虚拟环境 + 依赖安装 ────────────────────────────────────────────
log "7/8 安装 Python 依赖..."
cd "${APP_DIR}"
if [[ ! -d venv ]]; then
  python3.11 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
deactivate
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}/venv"

# ─── 8. systemd 服务 ──────────────────────────────────────────────────────────
log "8/8 配置 systemd 服务..."
cat > /etc/systemd/system/optionsaji.service << EOF
[Unit]
Description=OptionsAji Backend (FastAPI)
After=network.target postgresql.service redis-server.service
Requires=postgresql.service redis-server.service

[Service]
Type=exec
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8787 --workers 2
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=optionsaji

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable optionsaji

# ─── 生成 .env 模板（如果不存在）────────────────────────────────────────────────
if [[ ! -f "${APP_DIR}/.env" ]]; then
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  # 注入数据库 URL
  sed -i "s|DATABASE_URL=.*|DATABASE_URL=${DB_URL}|" "${APP_DIR}/.env"
  sed -i "s|REDIS_URL=.*|REDIS_URL=redis://localhost:6379/0|" "${APP_DIR}/.env"
  chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
  chmod 600 "${APP_DIR}/.env"
  warn "请编辑 ${APP_DIR}/.env 填写 API Keys:"
  warn "  MASSIVE_API_KEY=your_key"
  warn "  FMP_API_KEY=your_key"
  warn "  OPENROUTER_API_KEY=your_key"
fi

# ─── 数据库迁移 ────────────────────────────────────────────────────────────────
log "运行数据库初始化..."
cd "${APP_DIR}"
source venv/bin/activate
DB_URL_ENV=$(grep ^DATABASE_URL .env | cut -d= -f2-)
export DATABASE_URL="$DB_URL_ENV"
python -c "from app.db.bootstrap import init_db; init_db(); print('DB initialized')"
deactivate

log "========================"
log "安装完成!"
log ""
log "下一步:"
log "1. 编辑 /opt/options-aji-backend/.env  填写 API Keys"
log "2. systemctl start optionsaji"
log "3. systemctl status optionsaji"
log "4. journalctl -u optionsaji -f   (查看日志)"
log ""
log "数据库凭证: /root/.optionsaji_db_creds"
