#!/usr/bin/env bash
# Оновлення розгорнутої системи до поточного стану коду.
#
# Запускається з машини адміністратора, з кореня репозиторію або з deploy/:
#
#     ./deploy/update.sh             # оновити
#     ./deploy/update.sh --dry-run   # лише показати, які файли зміняться
#
# Порядок дій має значення:
#   1. резервна копія бази - до будь-яких змін, бо оновлення може містити
#      міграцію схеми;
#   2. доставка коду й зібраного інтерфейсу; файл .env на сервері не
#      передається й не чіпається, у ньому таємниці;
#   3. перескладання образу застосунку і перезапуск лише його: база та
#      проксі працюють далі;
#   4. перевірка, що застосунок відповідає і схема на останній ревізії.
#
# Міграції застосовує сам контейнер під час запуску (alembic upgrade head).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
HOST="${SLIPSTREAM_SSH:-azureuser@slipstream-ci.duckdns.org}"
REMOTE="${SLIPSTREAM_DIR:-/opt/slipstream}"
URL="${SLIPSTREAM_URL:-https://slipstream-ci.duckdns.org}"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15)
DRY=()
[[ "${1:-}" == "--dry-run" ]] && DRY=(--dry-run --itemize-changes)

[[ -f "$ROOT/web/dist/index.html" ]] || { echo "інтерфейс не зібрано: cd web && npm run build" >&2; exit 1; }

if [[ ${#DRY[@]} -eq 0 ]]; then
  echo "== 1. резервна копія бази"
  "${SSH[@]}" "$HOST" "cd $REMOTE/deploy && (sudo -n ./backup.sh || ./backup.sh)" | tail -3
fi

echo "== 2. доставка коду"
sync() { rsync -az "${DRY[@]}" -e "${SSH[*]}" "$@"; }
for dir in app alembic cli scripts tests; do
  sync --delete --exclude '__pycache__' "$ROOT/$dir/" "$HOST:$REMOTE/$dir/"
done
sync --delete "$ROOT/web/dist/" "$HOST:$REMOTE/web/dist/"
sync "$ROOT/Dockerfile" "$ROOT/requirements.txt" "$ROOT/README.md" "$ROOT/alembic.ini" "$HOST:$REMOTE/"
sync "$HERE/docker-compose.prod.yml" "$HERE/README.md" "$HERE/update.sh" "$HOST:$REMOTE/deploy/"

if [[ ${#DRY[@]} -gt 0 ]]; then
  echo "пробний запуск: на сервері нічого не змінено"
  exit 0
fi

echo "== 3. перескладання і перезапуск застосунку"
"${SSH[@]}" "$HOST" "cd $REMOTE/deploy \
  && docker compose -f docker-compose.prod.yml build api </dev/null \
  && docker compose -f docker-compose.prod.yml up -d api </dev/null"

echo "== 4. перевірка"
for attempt in $(seq 1 30); do
  if curl -fsS -m 5 "$URL/health" >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS -m 10 "$URL/health"; echo
"${SSH[@]}" "$HOST" "cd $REMOTE/deploy \
  && docker compose -f docker-compose.prod.yml exec -T api alembic current </dev/null 2>/dev/null | tail -1"
paths=$(curl -fsS -m 10 "$URL/openapi.json" | grep -o '"/api/v1/[^"]*"' | sort -u | wc -l)
echo "шляхів програмного інтерфейсу: $paths"
echo "готово: $URL"
