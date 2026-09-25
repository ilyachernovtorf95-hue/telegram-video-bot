#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"

sh deploy/mac/preflight.sh
docker compose up -d --build
docker compose ps
docker compose logs --tail=40 bot
