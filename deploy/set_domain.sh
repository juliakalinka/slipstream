#!/usr/bin/env bash
# Перемикання стенда на власне доменне ім'я DuckDNS.
#
# Ім'я, видане хмарною платформою, працює, але воно довге й розкриває і
# регіон, і постачальника. DuckDNS дає коротке ім'я безкоштовно й без
# верифікації, а для демонстрації цього досить.
#
# Сценарій виконується на машині адміністратора: сам він лише звертається
# до DuckDNS, а зміни на сервері робить через SSH.
#
# ПЕРЕДУМОВА. Файл ~/.config/duckdns.env із двома рядками:
#     DUCKDNS_DOMAIN=<піддомен без .duckdns.org>
#     DUCKDNS_TOKEN=<маркер зі сторінки duckdns.org>
# Маркер читається лише цим сценарієм і нікуди не виводиться.
#
#     ./set_domain.sh            # перемкнути
#     ./set_domain.sh --dry-run  # показати, що буде зроблено
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${DUCKDNS_ENV:-$HOME/.config/duckdns.env}"
SSH_HOST="${SSH_HOST:-azureuser@slipstream-demo-plc.polandcentral.cloudapp.azure.com}"
LEGACY="${LEGACY_DOMAIN:-slipstream-demo-plc.polandcentral.cloudapp.azure.com}"
REMOTE="/opt/slipstream/deploy"
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY=1

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf 'set_domain: %s\n' "$*" >&2; exit 1; }

[ -r "$CFG" ] || die "немає файлу $CFG — створіть його з DUCKDNS_DOMAIN і DUCKDNS_TOKEN"
set -a; . "$CFG"; set +a
[ -n "${DUCKDNS_DOMAIN:-}" ] || die "у $CFG не задано DUCKDNS_DOMAIN"
[ -n "${DUCKDNS_TOKEN:-}" ] || die "у $CFG не задано DUCKDNS_TOKEN"

NEW="${DUCKDNS_DOMAIN}.duckdns.org"
[ "$NEW" != "$LEGACY" ] || die "нове й попереднє імена збігаються"

# ─────────────────────────────────────────────── 1. адреса сервера
say "Адреса сервера"
IP="$(dig +short "$LEGACY" | tail -1)"
[ -n "$IP" ] || die "не вдалося визначити адресу за іменем $LEGACY"
echo "  $LEGACY -> $IP"

if [ -n "$DRY" ]; then
  echo
  echo "  буде створено запис: $NEW -> $IP"
  echo "  на сервері буде задано DOMAIN=$NEW, DOMAIN_LEGACY=$LEGACY"
  echo "  потім перезапуск проксі й очікування сертифіката"
  exit 0
fi

# ─────────────────────────────────────────────── 2. запис у DuckDNS
say "Запис у DuckDNS"
# Маркер передається лише тут і не потрапляє ні в журнал, ні у вивід.
ANSWER="$(curl -sS --get "https://www.duckdns.org/update" \
          --data-urlencode "domains=${DUCKDNS_DOMAIN}" \
          --data-urlencode "token=${DUCKDNS_TOKEN}" \
          --data-urlencode "ip=${IP}")"
[ "$ANSWER" = "OK" ] || die "DuckDNS відмовив (відповідь «$ANSWER») — перевірте ім'я й маркер"
echo "  $NEW -> $IP створено"

# ─────────────────────────────────────────────── 3. очікування DNS
say "Поширення запису"
for i in $(seq 1 30); do
  GOT="$(dig +short "$NEW" @1.1.1.1 | tail -1 || true)"
  [ "$GOT" = "$IP" ] && break
  sleep 4
done
[ "${GOT:-}" = "$IP" ] || die "ім'я $NEW досі не вказує на $IP"
echo "  розв'язується правильно"

# ─────────────────────────────────────────────── 4. налаштування сервера
# Опис проксі й склад служб доставляються разом зі зміною імені: нова
# конфігурація вимагає ДВОХ різних імен, тому доти, доки другого імені не
# існує, надсилати її на сервер не можна — склад служб просто не запуститься.
say "Доставлення конфігурації"
rsync -az "$HERE/Caddyfile" "$HERE/docker-compose.prod.yml" "$SSH_HOST:$REMOTE/"
echo "  Caddyfile і docker-compose.prod.yml надіслано"

say "Налаштування сервера"
ssh "$SSH_HOST" DOMAIN="$NEW" LEGACY="$LEGACY" REMOTE="$REMOTE" 'bash -s' <<'EOS'
set -euo pipefail
cd "$REMOTE"
cp .env .env.bak

# Два однакові імені Caddy вважає неоднозначним описом і не запускається,
# тому попереднє ім'я задається окремим рядком і має відрізнятися.
python3 - "$DOMAIN" "$LEGACY" <<'PY'
import sys, pathlib
domain, legacy = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
lines = [l for l in p.read_text().splitlines()
         if not l.startswith(("DOMAIN=", "DOMAIN_LEGACY="))]
lines.insert(0, f"DOMAIN={domain}")
lines.insert(1, f"DOMAIN_LEGACY={legacy}")
p.write_text("\n".join(lines) + "\n")
print(f"  DOMAIN={domain}")
print(f"  DOMAIN_LEGACY={legacy}")
PY

# Перевірка опису ДО перезапуску: хибний опис зупинив би проксі, а з ним і
# весь доступ до системи ззовні.
# Прапорець -T і перенаправлення входу обов'язкові: сценарій надходить
# на віддалену машину через стандартний вхід, а docker compose без них
# цей самий вхід вичитує — і решта сценарію просто зникає. Саме так
# перший запуск змінив .env, але не перезапустив проксі.
if ! docker compose -f docker-compose.prod.yml run --rm --no-deps -T --entrypoint caddy \
     caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile </dev/null >/dev/null 2>&1; then
  echo "  опис проксі хибний — повертаю попередній .env" >&2
  mv .env.bak .env
  exit 1
fi
echo "  опис проксі коректний"

docker compose -f docker-compose.prod.yml up -d caddy </dev/null
rm -f .env.bak
EOS

# ─────────────────────────────────────────────── 5. сертифікат
say "Сертифікат"
for i in $(seq 1 45); do
  if curl -sf -m 8 -o /dev/null "https://${NEW}/health"; then
    echo "  видано, система відповідає"
    break
  fi
  sleep 4
done
curl -sf -m 10 -o /dev/null "https://${NEW}/health" \
  || die "за ${NEW} система не відповідає; подивіться docker compose logs caddy"

ISSUER="$(echo | openssl s_client -connect "${NEW}:443" -servername "$NEW" 2>/dev/null \
          | openssl x509 -noout -issuer 2>/dev/null || true)"
echo "  $ISSUER"

say "Перенаправлення з попереднього імені"
CODE="$(curl -s -o /dev/null -w '%{http_code}' "https://${LEGACY}/")"
LOC="$(curl -s -o /dev/null -w '%{redirect_url}' "https://${LEGACY}/")"
echo "  https://${LEGACY}/ -> HTTP $CODE $LOC"

say "Готово"
echo "  https://${NEW}"
