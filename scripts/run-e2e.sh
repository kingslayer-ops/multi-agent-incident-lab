#!/usr/bin/env sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
compose="docker compose -f $root/docker-compose.e2e.yml"
cleanup() {
  cd "$root"
  mkdir -p "$root/e2e/test-results"
  $compose logs --no-color > "$root/e2e/test-results/compose.log" 2>&1 || true
  $compose down -v --remove-orphans || true
}
trap cleanup EXIT

$compose up -d --build --wait
cd "$root/e2e"
npm ci
npx playwright install --with-deps chromium
if [ "${E2E_MODE:-full}" = "smoke" ]; then
  npm run test:smoke
else
  npm test
fi
