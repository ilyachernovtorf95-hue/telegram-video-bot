#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"

if [ "$(uname -m)" != "aarch64" ]; then
  echo "ERROR: This deployment requires an arm64/aarch64 host (OCI Ampere A1)." >&2
  exit 1
fi

if [ ! -f .env ]; then
  echo "ERROR: .env is missing. Copy .env.example to .env and fill credentials on the server." >&2
  exit 1
fi

if ! grep -Eq '^TELEGRAM_BOT_TOKEN=[^[:space:]]+:[^[:space:]]+' .env || grep -Eq '^TELEGRAM_BOT_TOKEN=put_your_' .env; then
  echo "ERROR: Set a real TELEGRAM_BOT_TOKEN in .env; do not paste it into chat or git." >&2
  exit 1
fi

if ! grep -Eq '^GEMINI_API_KEY=[^[:space:]]+' .env || grep -Eq '^GEMINI_API_KEY=put_your_' .env; then
  echo "ERROR: Set GEMINI_API_KEY in .env to retain the Gemini analysis path." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: Docker Engine and the Compose plugin must be installed and accessible." >&2
  exit 1
fi

docker compose config -q
echo "Ready for: docker compose up -d --build"

