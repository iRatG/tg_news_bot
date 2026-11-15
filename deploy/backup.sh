#!/bin/sh
# Ежедневный бэкап БД бота (cron: /etc/cron.d/tg_news_bot_backup).
#
# 1. Локально: консистентная копия через sqlite3 .backup (безопасно при работающем
#    боте и WAL) → /opt/backups/tg_news_bot/newsbot-YYYY-MM-DD.db.gz, 14 дней.
# 2. Вне сервера: тот же файл уходит документом в личку админу через бота
#    (если в .env задан TELEGRAM_ADMIN_CHAT_ID). Локальная копия не спасёт при
#    потере сервера — так уже было со старым VPS в 2026-11.
set -eu

APP_DIR=/opt/tg_news_bot
DB="$APP_DIR/data/newsbot.db"
DEST=/opt/backups/tg_news_bot
KEEP_DAYS=14

[ -f "$DB" ] || { echo "no db at $DB"; exit 0; }
mkdir -p "$DEST"

OUT="$DEST/newsbot-$(date +%F).db"
sqlite3 "$DB" ".backup '$OUT'"
gzip -f "$OUT"
find "$DEST" -name 'newsbot-*.db.gz' -mtime +"$KEEP_DAYS" -delete

# Offsite → Telegram (токен и chat id читаем из .env, не светим в ps/cron)
TOKEN=$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$APP_DIR/.env" | tr -d '\r')
CHAT=$(sed -n 's/^TELEGRAM_ADMIN_CHAT_ID=//p' "$APP_DIR/.env" | tr -d '\r')
if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
    curl -fsS -m 120 -o /dev/null \
        -F chat_id="$CHAT" \
        -F caption="🗄 Бэкап БД NewsBot $(date +%F)" \
        -F document=@"$OUT.gz" \
        "https://api.telegram.org/bot$TOKEN/sendDocument" \
        && echo "sent to telegram" \
        || echo "telegram upload failed (local backup is fine)"
fi
echo "backup ok: $OUT.gz"
