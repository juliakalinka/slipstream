#!/usr/bin/env bash
# Очікування готовності складу служб.
#
# Потрібне завданням за розкладом. Віртуальну машину між демонстраціями
# зупиняють, тому таймери з `Persistent=true` надолужують пропущений запуск
# відразу після вмикання — коли контейнери ще піднімаються. Без очікування
# таке завдання падало б щоразу після вмикання машини.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$HERE/docker-compose.prod.yml")
TIMEOUT="${WAIT_TIMEOUT:-300}"

deadline=$(( $(date +%s) + TIMEOUT ))
while :; do
  if [ "$("${COMPOSE[@]}" ps --quiet --status running db api | wc -l)" -ge 2 ]; then
    exit 0
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "wait_services: служби не піднялися за ${TIMEOUT} с" >&2
    exit 1
  fi
  sleep 10
done
