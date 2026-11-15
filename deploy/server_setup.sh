#!/usr/bin/env bash
# Настройка чистого Ubuntu-сервера под бота. Идемпотентно — можно перезапускать.
# Так поднят текущий прод (2026-11-15) после потери взломанного старого VPS.
#
# Порядок с нуля:
#   1. С локальной машины положить ключ:   ssh-copy-id -i ~/.ssh/tg_news_bot_de root@<IP>
#   2. Выложить код:                       (VPS_HOST=<IP> в .env) python deploy.py --rebuild
#      — упадёт на "Check .env", это нормально: кода уже на месте
#   3. Положить секреты на сервер:         grep -v '^VPS_' .env | ssh root@<IP> 'umask 077; cat > /opt/tg_news_bot/.env'
#   4. На сервере:                         bash /opt/tg_news_bot/deploy/server_setup.sh <ADMIN_DOMAIN>
#      ADMIN_DOMAIN без своего домена — <ip-через-дефисы>.sslip.io
#   5. БД (первый раз):                    см. README → «Развёртывание с нуля»
#   6. Запуск:                             python deploy.py --rebuild
set -euo pipefail

ADMIN_DOMAIN="${1:?usage: server_setup.sh <ADMIN_DOMAIN>}"
APP_DIR=/opt/tg_news_bot
HERE="$(cd "$(dirname "$0")" && pwd)"
export DEBIAN_FRONTEND=noninteractive

log() { printf '\n==> %s\n' "$*"; }

log "Пакеты"
apt-get update -qq
apt-get install -y -qq docker.io fail2ban ufw caddy sqlite3 curl unattended-upgrades >/dev/null
# BuildKit (legacy builder deprecated); пакета может не быть в старых релизах — не критично
apt-get install -y -qq docker-buildx >/dev/null 2>&1 || echo "docker-buildx недоступен — используется legacy builder"
systemctl enable --now docker fail2ban >/dev/null 2>&1

log "SSH: только по ключу"
if grep -qE '^(ssh-|ecdsa-)' /root/.ssh/authorized_keys 2>/dev/null; then
    cat > /etc/ssh/sshd_config.d/00-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
    sshd -t && systemctl reload ssh
    sshd -T | grep -E '^(passwordauthentication|permitrootlogin)'
else
    echo "!! В /root/.ssh/authorized_keys нет ключа — вход по паролю НЕ отключаю (иначе потеря доступа)"
fi

log "Файрвол: 22, 80, 443"
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
ufw status | sed -n '1,12p'

log "Swap 2G"
if ! swapon --show | grep -q /swapfile; then
    fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
fi
grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf
sysctl -q -p /etc/sysctl.d/99-swappiness.conf

log "Docker: ротация логов"
DAEMON='{ "log-driver": "json-file", "log-opts": { "max-size": "20m", "max-file": "5" } }'
if [ "$(cat /etc/docker/daemon.json 2>/dev/null)" != "$DAEMON" ]; then
    echo "$DAEMON" > /etc/docker/daemon.json
    systemctl restart docker   # контейнеры с --restart unless-stopped поднимутся сами
fi

log "Автообновления безопасности"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

log "Бэкап БД: ежедневно 03:30 UTC"
install -m 700 "$HERE/backup.sh" /usr/local/bin/tg_news_bot_backup.sh
echo '30 3 * * * root /usr/local/bin/tg_news_bot_backup.sh >> /var/log/tg_news_bot_backup.log 2>&1' \
    > /etc/cron.d/tg_news_bot_backup

log "Caddy: HTTPS для админки → https://$ADMIN_DOMAIN"
install -m 644 "$HERE/Caddyfile" /etc/caddy/Caddyfile
echo "ADMIN_DOMAIN=$ADMIN_DOMAIN" > /etc/default/caddy
mkdir -p /etc/systemd/system/caddy.service.d
cat > /etc/systemd/system/caddy.service.d/env.conf <<'EOF'
[Service]
EnvironmentFile=/etc/default/caddy
EOF
systemctl daemon-reload
ADMIN_DOMAIN="$ADMIN_DOMAIN" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 \
    || { echo "!! Caddyfile невалиден"; exit 1; }
systemctl enable caddy >/dev/null 2>&1
systemctl restart caddy

log "Каталог приложения"
mkdir -p "$APP_DIR/data" /opt/backups/tg_news_bot
chmod 700 /opt/backups/tg_news_bot
chmod 600 /opt/backups/tg_news_bot/*.gz 2>/dev/null || true
[ -f "$APP_DIR/.env" ] && chmod 600 "$APP_DIR/.env" || echo "!! $APP_DIR/.env ещё нет — положите секреты (шаг 3)"

log "Готово"
