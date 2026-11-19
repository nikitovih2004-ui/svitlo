#!/usr/bin/env bash
# Обновить код бота на сервере: rsync без .env, data/, .venv, затем перезапуск.
# Сервер уже настроен по deploy/svitlo.service (проект в /opt/svitlo).
#   SVITLO_HOST=root@1.2.3.4 deploy/deploy.sh
#   SVITLO_HOST=root@1.2.3.4 SVITLO_KEY=~/.ssh/other deploy/deploy.sh
# Зависимости (requirements.txt) и юнит (deploy/svitlo.service) обновляются, только если файл изменился.
set -euo pipefail

HOST="${SVITLO_HOST:?задайте SVITLO_HOST=user@host}"
DEST="/opt/svitlo"
SSH="ssh"
[ -n "${SVITLO_KEY:-}" ] && SSH="ssh -i $SVITLO_KEY -o IdentitiesOnly=yes"
cd "$(dirname "$0")/.."

# Exclude защищает эти пути на сервере и от удаления (--delete не трогает исключённое).
rsync -rlptz --delete \
  --exclude '/.env' --exclude '/data/' --exclude '/.venv/' \
  --exclude '/prototypes/' --exclude '.pytest_cache/' --exclude '__pycache__/' \
  --exclude '*.pyc' --exclude '.DS_Store' --exclude '.git/' \
  -e "$SSH" ./ "$HOST:$DEST/"

$SSH "$HOST" "set -e
  cd $DEST
  if ! cmp -s requirements.txt .venv/.requirements.txt; then
    .venv/bin/pip install -q -r requirements.txt && cp requirements.txt .venv/.requirements.txt
    echo 'зависимости обновлены'
  fi
  if ! cmp -s deploy/svitlo.service /etc/systemd/system/svitlo.service; then
    cp deploy/svitlo.service /etc/systemd/system/svitlo.service && systemctl daemon-reload
    echo 'юнит обновлён'
  fi
  systemctl restart svitlo
  sleep 3
  systemctl is-active svitlo
  journalctl -u svitlo -n 8 --no-pager -o cat"
