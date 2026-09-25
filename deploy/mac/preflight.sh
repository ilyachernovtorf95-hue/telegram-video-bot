#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "Ошибка: нужен Mac с Apple Silicon (arm64)." >&2
  exit 1
fi

if [ ! -f .env ]; then
  echo "Ошибка: создай .env из .env.example и заполни свои ключи на Mac." >&2
  exit 1
fi

if ! grep -Eq '^TELEGRAM_BOT_TOKEN=[^[:space:]]+:[^[:space:]]+' .env || grep -Eq '^TELEGRAM_BOT_TOKEN=put_your_' .env; then
  echo "Ошибка: в .env нет действующего TELEGRAM_BOT_TOKEN." >&2
  exit 1
fi

if ! grep -Eq '^GEMINI_API_KEY=[^[:space:]]+' .env || grep -Eq '^GEMINI_API_KEY=put_your_' .env; then
  echo "Ошибка: в .env нет GEMINI_API_KEY для AI-разбора." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Ошибка: установи и открой Docker Desktop для Mac (Apple Silicon)." >&2
  exit 1
fi

docker compose config -q
echo "Проверка пройдена. Можно запускать бота."
