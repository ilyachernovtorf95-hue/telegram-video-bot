#!/bin/sh
set -eu

PRIMARY_PROJECT_ID="0782ee62-74b0-447a-94e3-e88cd24c2e01"
PRIMARY_PROJECT_NAME="adventurous-perception"

PROJECT_ID="${RAILWAY_PROJECT_ID:-}"
PROJECT_NAME="${RAILWAY_PROJECT_NAME:-}"

# Fail closed on every known non-primary Railway deployment. This guard lives in
# ENTRYPOINT so it still applies even if Railway has an old/custom Start Command.
if [ -n "$PROJECT_ID" ]; then
  if [ "$PROJECT_ID" != "$PRIMARY_PROJECT_ID" ]; then
    echo "STANDBY: Telegram polling disabled for Railway project ${PROJECT_NAME:-unknown} ($PROJECT_ID)"
    exec tail -f /dev/null
  fi
elif [ -n "$PROJECT_NAME" ] && [ "$PROJECT_NAME" != "$PRIMARY_PROJECT_NAME" ]; then
  echo "STANDBY: Telegram polling disabled for Railway project $PROJECT_NAME (project ID unavailable)"
  exec tail -f /dev/null
fi

echo "PRIMARY: starting Telegram bot in ${PROJECT_NAME:-local} (${PROJECT_ID:-no-project-id})"

# YouTube PO-token helper. A failure here must not prevent Telegram from starting.
node /opt/bgutil-ytdlp-pot-provider/server/build/main.js >/tmp/pot-provider.log 2>&1 &

# Always run the responsive dispatcher. Intentionally ignore any stale Railway
# Start Command arguments so an old `python bot.py` override cannot re-enable the
# blocking legacy poller.
exec xvfb-run -a -s '-screen 0 1280x720x24' python /app/bot_runner.py
