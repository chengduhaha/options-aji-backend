#!/usr/bin/env bash
# =============================================================================
# 阿吉美股期权平台 — 服务器一键安装脚本
# 适用: Rocky Linux 9 / AlmaLinux 9 / RHEL 9
# 用法: bash scripts/server-setup.sh
# 可选: GIT_BRANCH=main bash scripts/server-setup.sh
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[SETUP]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()  { echo -e "${RED}[ERR]${NC}   $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请以 root 运行: sudo bash scripts/server-setup.sh"

APP_DIR="/root/workspace/options-aji-backend"
APP_USER="optionsaji"
GIT_BRANCH="${GIT_BRANCH:-claude/redesign-options-platform-bxjbU}"

read_database_url_from_env_file() {
  local f="$1"
  [[ -f "$f" ]] || return 1
  grep '^DATABASE_URL=' "$f" | head -n1 | sed '1s/^DATABASE_URL=//'
}

# ─── 1. 系统更新 ──────────────────────────────────────────────────────────────
log "1/8 系统更新..."
dnf update -y -q
dnf install -y -q \
  curl wget gnupg2 ca-certificates \
  gcc gcc-c++ make git \
  python3.11 python3.11-devel \
  openssl libpq-devel

# ─── 2. PostgreSQL 16 ─────────────────────────────────────────────────────────
log "2/8 安装 PostgreSQL 16..."
if [[ ! -x /usr/pgsql-16/bin/psql ]]; then
  dnf install -y -q https://download.postgresql.org/pub/repos/yum/reporpms/EL-9-x86_64/pgdg-redhat-repo-latest.noarch.rpm || true
  dnf -qy module disable postgresql 2>/dev/null || true
  dnf install -y -q postgresql16-server postgresql16
fi
[[ -x /usr/pgsql-16/bin/psql ]] || die "未安装 PostgreSQL 16（/usr/pgsql-16/bin/psql 不存在）"
PSQL="/usr/pgsql-16/bin/psql"

if [[ ! -f /var/lib/pgsql/16/data/PG_VERSION ]]; then
  log "初始化 PostgreSQL 数据目录..."
  [[ -x /usr/pgsql-16/bin/postgresql-16-setup ]] || die "缺少 postgresql-16-setup"
  /usr/pgsql-16/bin/postgresql-16-setup initdb
fi

systemctl enable postgresql-16
systemctl start postgresql-16
log "PostgreSQL: $($PSQL --version)"

# ─── 3. Redis ─────────────────────────────────────────────────────────────────
log "3/8 安装 Redis..."
if ! command -v redis-server &>/dev/null; then
  dnf install -y -q redis
fi
systemctl enable redis
systemctl start redis
# 运行时配置（重启后恢复默认；长期使用请改 /etc/redis/redis.conf 并重启）
redis-cli config set maxmemory 512mb       2>/dev/null || true
redis-cli config set maxmemory-policy allkeys-lru 2>/dev/null || true
redis-cli config set save "900 1 300 10"  2>/dev/null || true
log "Redis: $(redis-server --version)"

# ─── 4. 创建数据库和用户 ───────────────────────────────────────────────────────
log "4/8 创建 PostgreSQL 数据库..."
DB_NAME="optionsaji"
DB_USER="optionsaji"
DB_PASS_NEW=""
DB_URL=""

ROLE_EXISTS="$(sudo -u postgres "$PSQL" -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | tr -d '[:space:]')"
DB_EXISTS="$(sudo -u postgres "$PSQL" -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | tr -d '[:space:]')"

if [[ "$ROLE_EXISTS" != "1" ]]; then
  DB_PASS_NEW="$(openssl rand -hex 16)"
  sudo -u postgres "$PSQL" -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS_NEW}';"
  echo "DATABASE_URL=postgresql+psycopg2://${DB_USER}:${DB_PASS_NEW}@localhost:5432/${DB_NAME}" > /root/.optionsaji_db_creds
  chmod 600 /root/.optionsaji_db_creds
  log "数据库凭证保存到 /root/.optionsaji_db_creds"
else
  warn "PostgreSQL 角色 ${DB_USER} 已存在，跳过 CREATE USER"
fi

if [[ "$DB_EXISTS" != "1" ]]; then
  sudo -u postgres "$PSQL" -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"
  sudo -u postgres "$PSQL" -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};"
else
  warn "数据库 ${DB_NAME} 已存在，跳过 CREATE DATABASE"
fi

if [[ -f /root/.optionsaji_db_creds ]]; then
  # shellcheck disable=SC1091
  source /root/.optionsaji_db_creds
  DB_URL="$DATABASE_URL"
elif [[ -n "$DB_PASS_NEW" ]]; then
  DB_URL="postgresql+psycopg2://${DB_USER}:${DB_PASS_NEW}@localhost:5432/${DB_NAME}"
else
  warn "无 /root/.optionsaji_db_creds 且未新建用户；将在代码目录就绪后尝试从 ${APP_DIR}/.env 读取 DATABASE_URL"
fi

# ─── 5. 创建应用用户 ───────────────────────────────────────────────────────────
log "5/8 创建应用用户 ${APP_USER}..."
if ! id -u "${APP_USER}" &>/dev/null; then
  mkdir -p "$(dirname "${APP_DIR}")"
  useradd --system --shell /bin/bash --home-dir "${APP_DIR}" --create-home "${APP_USER}"
fi

# ─── 6. 克隆/更新代码 ─────────────────────────────────────────────────────────
log "6/8 部署代码..."
if [[ -d "${APP_DIR}/.git" ]]; then
  log "更新已有代码库..."
  cd "${APP_DIR}"
  git fetch origin
  git checkout "${GIT_BRANCH}"
  git pull origin "${GIT_BRANCH}"
else
  log "首次克隆..."
  mkdir -p "$(dirname "${APP_DIR}")"
  git clone https://github.com/chengduhaha/options-aji-backend.git "${APP_DIR}"
  cd "${APP_DIR}"
  git checkout "${GIT_BRANCH}"
fi
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

# APP_DIR 若在 /root 下，systemd 以 APP_USER 运行须能路径穿越 /root（否则 status=200/CHDIR）
if [[ "${APP_DIR}" == /root/* ]]; then
  root_other_x="$(stat -c %a /root)"
  root_other_x="${root_other_x: -1}"
  if (( root_other_x & 1 )); then
    :
  else
    log "为路径穿越设置 /root 其他用户执行位（不开放读目录列表）..."
    chmod o+x /root
  fi
fi

if [[ -z "$DB_URL" ]]; then
  DB_URL="$(read_database_url_from_env_file "${APP_DIR}/.env" || true)"
  [[ -n "$DB_URL" ]] || die "无法确定 DATABASE_URL：请写入 ${APP_DIR}/.env 或恢复 /root/.optionsaji_db_creds"
fi

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

# ─── 8. systemd 服务（路径与 APP_DIR 一致）──────────────────────────────────────
log "8/8 配置 systemd 服务..."
cat > /etc/systemd/system/optionsaji.service << UNIT
[Unit]
Description=OptionsAji Backend (FastAPI)
After=network.target postgresql-16.service redis.service
Requires=postgresql-16.service redis.service

[Service]
Type=exec
User=optionsaji
Group=optionsaji
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
UNIT

systemctl daemon-reload
systemctl enable optionsaji

if [[ ! -f "${APP_DIR}/.env" ]]; then
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  sed -i "s|DATABASE_URL=.*|DATABASE_URL=${DB_URL}|" "${APP_DIR}/.env"
  sed -i "s|REDIS_URL=.*|REDIS_URL=redis://localhost:6379/0|" "${APP_DIR}/.env"
  chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
  chmod 600 "${APP_DIR}/.env"
  warn "请编辑 ${APP_DIR}/.env 填写 API Keys:"
  warn "  MASSIVE_API_KEY=your_key"
  warn "  FMP_API_KEY=your_key"
  warn "  OPENROUTER_API_KEY=your_key"
fi

log "运行数据库初始化..."
cd "${APP_DIR}"
source venv/bin/activate
DB_URL_ENV="$(read_database_url_from_env_file "${APP_DIR}/.env" || true)"
[[ -n "$DB_URL_ENV" ]] || die ".env 中缺少 DATABASE_URL"
export DATABASE_URL="$DB_URL_ENV"
python -c "from app.db.bootstrap import init_db; init_db(); print('DB initialized')"
deactivate

log "========================"
log "安装完成!"
log ""
log "下一步:"
log "1. 编辑 ${APP_DIR}/.env  填写 API Keys"
log "2. systemctl start optionsaji"
log "3. systemctl status optionsaji"
log "4. journalctl -u optionsaji -f   (查看日志)"
log ""
log "数据库凭证: /root/.optionsaji_db_creds"
