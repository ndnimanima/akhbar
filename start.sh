#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Rozaneh · روزنه — one-command installer for a Linux VPS (Ubuntu/Debian).
#
#   curl -fsSL https://raw.githubusercontent.com/USER/Rozaneh/main/start.sh | bash
#
# Override the defaults with environment variables:
#   ROZANEH_DIR=/opt/rozaneh ROZANEH_REPO=https://github.com/USER/Rozaneh.git
#   ROZANEH_PORT=8080 ROZANEH_BRANCH=main
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_DIR="${ROZANEH_DIR:-/opt/rozaneh}"
REPO="${ROZANEH_REPO:-https://github.com/USER/Rozaneh.git}"
BRANCH="${ROZANEH_BRANCH:-main}"
PORT="${ROZANEH_PORT:-8080}"
SERVICE="rozaneh"

blue() { printf "\033[36m%s\033[0m\n" "$*"; }
ok()   { printf "\033[32m✓ %s\033[0m\n" "$*"; }
die()  { printf "\033[31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "این اسکریپت را با sudo یا کاربر root اجرا کنید."

blue "▶ نصب پیش‌نیازها…"
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates
else
  die "این اسکریپت برای Ubuntu/Debian نوشته شده است. با Docker هم می‌توانید اجرا کنید."
fi

blue "▶ دریافت کد پروژه…"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
  rm -rf "$APP_DIR"
  git clone --depth 1 --branch "$BRANCH" "$REPO" "$APP_DIR"
fi

blue "▶ ساخت محیط پایتون…"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  ADMIN_PASSWORD="$(head -c 12 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 12)"
  cat > "$APP_DIR/.env" <<EOF
ADMIN_PASSWORD=$ADMIN_PASSWORD
DATA_DIR=$APP_DIR/data
DEFAULT_TZ=Asia/Tehran
PORT=$PORT
COOKIE_SECURE=0
SCHEDULER_ENABLED=1
EOF
  ok "رمز اولیهٔ پنل: $ADMIN_PASSWORD  (فایل: $APP_DIR/.env)"
fi

mkdir -p "$APP_DIR/data"

blue "▶ ساخت سرویس systemd…"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Rozaneh panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port \${PORT}
Restart=always
RestartSec=4
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE"
sleep 2
systemctl --no-pager --lines=5 status "$SERVICE" || true

ok "روزنه نصب شد."
echo
echo "  آدرس پنل : http://$(hostname -I | awk '{print $1}'):$PORT"
echo "  رمز اولیه: فایل $APP_DIR/.env (کلید ADMIN_PASSWORD)"
echo "  لاگ‌ها    : journalctl -u $SERVICE -f"
echo "  به‌روزرسانی: همین دستور را دوباره اجرا کنید"
echo
