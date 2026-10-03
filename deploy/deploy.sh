#!/usr/bin/env bash
# Деплой live-таймингов на свой сервер (Debian/Ubuntu, Python 3.10+).
#   ./deploy/deploy.sh root@1.2.3.4          — порт 8765
#   ./deploy/deploy.sh user@host 9000        — свой порт
# Повторный запуск обновляет код; токен доступа сохраняется.
set -euo pipefail
HOST=${1:?использование: deploy/deploy.sh user@host [порт]}
PORT=${2:-8765}
cd "$(dirname "$0")/.."

echo "→ копирую код на $HOST"
tar czf - --exclude __pycache__ live requirements-live.txt deploy \
  | ssh "$HOST" 'rm -rf /tmp/f1-live-src && mkdir -p /tmp/f1-live-src && tar xzf - -C /tmp/f1-live-src'

echo "→ ставлю на сервере"
ssh "$HOST" "sudo PORT=$PORT bash -s" <<'REMOTE'
set -euo pipefail
DIR=/opt/f1-live
SRC=/tmp/f1-live-src
export DEBIAN_FRONTEND=noninteractive
if ! python3 -m venv --help >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq python3 python3-venv
fi
python3 -c 'import sys; assert sys.version_info >= (3, 10), "нужен Python 3.10+"'
id f1live >/dev/null 2>&1 || useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin f1live
mkdir -p "$DIR"
rm -rf "$DIR/live"
cp -r "$SRC/live" "$SRC/requirements-live.txt" "$DIR/"
[ -x "$DIR/venv/bin/python" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements-live.txt"
chown -R f1live:f1live "$DIR"

ENV=/etc/f1-live.env
if [ ! -f "$ENV" ]; then
  echo "F1_LIVE_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')" > "$ENV"
fi
sed -i '/^F1_LIVE_PORT=/d' "$ENV"; echo "F1_LIVE_PORT=$PORT" >> "$ENV"
chmod 600 "$ENV"

cp "$SRC/deploy/f1-live.service" /etc/systemd/system/f1-live.service
systemctl daemon-reload
systemctl enable -q f1-live
systemctl restart f1-live
if command -v ufw >/dev/null && ufw status | grep -q active; then ufw allow "$PORT/tcp" >/dev/null; fi
rm -rf "$SRC"
sleep 2
systemctl is-active --quiet f1-live || { journalctl -u f1-live -n 30 --no-pager; exit 1; }
TOKEN=$(grep ^F1_LIVE_TOKEN "$ENV" | cut -d= -f2)
IP=$(hostname -I | awk '{print $1}')
echo
echo "✓ работает. Открыть: http://$IP:$PORT/?token=$TOKEN"
echo "  логи: journalctl -u f1-live -f    память: systemctl status f1-live"
REMOTE
