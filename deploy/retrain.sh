#!/usr/bin/env bash
# Перенавчання моделей усередині контейнера застосунку.
#
# Виконується в тому самому контейнері, що й застосунок: там уже є і
# підключення до бази, і том із моделями, тому окремого налаштування
# середовища для завдання за розкладом не потрібно.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec docker compose -f "$HERE/docker-compose.prod.yml" exec -T api \
     python scripts/retrain_all.py "$@"
