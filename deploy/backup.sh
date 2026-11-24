#!/usr/bin/env bash
# Резервна копія бази даних системи.
#
# ЩО САМЕ КОПІЮЄТЬСЯ. Лише база. Навчені моделі в томі models_store не
# копіюються свідомо: вони похідні від історії прогонів і відтворюються
# навчанням, тоді як сама історія — ні. Копіювати десятки мегабайтів
# артефактів щодня, маючи змогу їх перебудувати, немає сенсу.
#
# ПОРЯДОК ДІЙ важливий: спершу вивантаження в тимчасовий файл, потім
# перевірка цілості, лише потім переміщення в сховище й ротація. Інакше
# обірване вивантаження витіснило б із ротації справну копію — найгірший
# можливий наслідок для засобу, що існує саме для надійності.
#
#   ./backup.sh              — зробити копію й прибрати старі
#   ./backup.sh --check      — перевірити найсвіжішу копію відновленням
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$HERE/docker-compose.prod.yml")
DEST="${BACKUP_DIR:-/var/backups/slipstream}"
KEEP="${BACKUP_KEEP:-7}"
MIN_BYTES="${BACKUP_MIN_BYTES:-20000}"   # менший файл означає невдале вивантаження

# Обліковий запис бази береться з того самого файлу, що й склад служб.
set -a; . "$HERE/.env"; set +a

die() { printf 'backup: %s\n' "$*" >&2; exit 1; }

# ─────────────────────────────────────────── перевірка відновленням
if [ "${1:-}" = "--check" ]; then
  latest="$(ls -1t "$DEST"/db-*.sql.gz 2>/dev/null | head -1)" \
    || die "у $DEST немає копій"
  [ -n "$latest" ] || die "у $DEST немає копій"
  scratch="slipstream_restore_check"

  echo "перевірка копії: $latest"
  "${COMPOSE[@]}" exec -T db psql -U "$POSTGRES_USER" -d postgres \
      -c "DROP DATABASE IF EXISTS $scratch" >/dev/null
  "${COMPOSE[@]}" exec -T db psql -U "$POSTGRES_USER" -d postgres \
      -c "CREATE DATABASE $scratch" >/dev/null

  # Копія відновлюється в окрему базу: робочої вона не торкається взагалі.
  if ! gzip -dc "$latest" | "${COMPOSE[@]}" exec -T db \
        psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$scratch" >/dev/null; then
    "${COMPOSE[@]}" exec -T db psql -U "$POSTGRES_USER" -d postgres \
        -c "DROP DATABASE IF EXISTS $scratch" >/dev/null
    die "копія не відновлюється — вона непридатна"
  fi

  echo "  відновлено, вміст:"
  "${COMPOSE[@]}" exec -T db psql -tA -U "$POSTGRES_USER" -d "$scratch" -c "
    SELECT 'projects=' || (SELECT count(*) FROM projects)
        || ' pipelines=' || (SELECT count(*) FROM pipelines)
        || ' test_results=' || (SELECT count(*) FROM test_results)
        || ' users=' || (SELECT count(*) FROM users);" | sed 's/^/    /'

  "${COMPOSE[@]}" exec -T db psql -U "$POSTGRES_USER" -d postgres \
      -c "DROP DATABASE IF EXISTS $scratch" >/dev/null
  echo "  тимчасову базу прибрано"
  exit 0
fi

# ─────────────────────────────────────────── створення копії
mkdir -p "$DEST"
stamp="$(date +%Y%m%d-%H%M)"
tmp="$DEST/.db-$stamp.sql.gz.part"
out="$DEST/db-$stamp.sql.gz"
trap 'rm -f "$tmp"' EXIT

# --clean --if-exists: копія придатна для відновлення в наявну базу.
"${COMPOSE[@]}" exec -T db pg_dump --clean --if-exists \
    -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip -6 > "$tmp"

size="$(stat -c %s "$tmp")"
[ "$size" -ge "$MIN_BYTES" ] || die "копія завмала ($size Б) — вивантаження не вдалося"
gzip -t "$tmp" || die "копія пошкоджена"

mv "$tmp" "$out"
trap - EXIT
echo "копія: $out ($(numfmt --to=iec "$size"))"

# ─────────────────────────────────────────── ротація
# Видаляються лише НАДЛИШКОВІ копії й лише після успішного вивантаження.
mapfile -t stale < <(ls -1t "$DEST"/db-*.sql.gz | tail -n "+$((KEEP + 1))")
for f in "${stale[@]}"; do
  rm -f -- "$f"
  echo "  прибрано давню копію: $(basename "$f")"
done
echo "копій у сховищі: $(ls -1 "$DEST"/db-*.sql.gz | wc -l) (тримаємо $KEEP)"
