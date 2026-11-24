#!/usr/bin/env bash
# Налаштування стенда: GitLab CE + виконавець + демонстраційний проєкт.
#
# Сценарій ідемпотентний: повторний запуск не створює дублікатів.
# Передумова: контейнер gitlab уже запущено і він відповідає на 8929.
#
#   podman run --detach --name gitlab ... gitlab/gitlab-ce:17.5.5-ce.0
#
# Після завершення конвеєр демонстраційного репозиторію запитує план у
# Slipstream, виконує саме його і повертає звіти JUnit.
set -euo pipefail

GITLAB_URL="${GITLAB_URL:-http://127.0.0.1:8929}"
SLIPSTREAM_API="${SLIPSTREAM_API:-http://127.0.0.1:8000/api/v1}"
# Адреса системи, видима ЗСЕРЕДИНИ контейнера GitLab. Вона інша, ніж з хоста:
# для контейнера 127.0.0.1 — це він сам. Задачі конвеєра виконуються на хості
# й користуються SLIPSTREAM_API, а вебхуки надсилає сам GitLab і йому потрібна
# ця адреса. Перевірити: podman exec gitlab curl http://172.17.0.1:8000/health
SLIPSTREAM_API_FROM_GITLAB="${SLIPSTREAM_API_FROM_GITLAB:-http://172.17.0.1:8000/api/v1}"
ROOT_TOKEN="${ROOT_TOKEN:-standtoken000000000001}"
PROJECT_PATH="demo-orders"
STAND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$STAND_DIR/demo-repo"
RUNNER_DIR="${RUNNER_DIR:-/home/yuliia/gitlab-stand/runner}"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

# ─────────────────────────────────────────────── 1. маркер доступу GitLab
say "Маркер доступу GitLab"
podman exec gitlab gitlab-rails runner "
  u = User.find_by(username: 'root')
  t = u.personal_access_tokens.find_by(name: 'stand')
  unless t
    t = u.personal_access_tokens.create!(
      scopes: ['api', 'write_repository'], name: 'stand',
      expires_at: 365.days.from_now)
    t.set_token('${ROOT_TOKEN}')
    t.save!
  end
" >/dev/null
echo "  маркер готовий"

api() { curl -sS --header "PRIVATE-TOKEN: ${ROOT_TOKEN}" "$@"; }

# ─────────────────────────────────────────────── 2. проєкт у GitLab
say "Проєкт у GitLab"
PID=$(api "${GITLAB_URL}/api/v4/projects?search=${PROJECT_PATH}" \
      | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['id'] if d else '')")
if [ -z "$PID" ]; then
  PID=$(api --request POST "${GITLAB_URL}/api/v4/projects" \
        --data "name=${PROJECT_PATH}&path=${PROJECT_PATH}&visibility=private&initialize_with_readme=false" \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
  echo "  створено проєкт id=$PID"
else
  echo "  проєкт уже існує, id=$PID"
fi

# ─────────────────────────────────────────────── 3. вивантаження коду
say "Вивантаження демонстраційного репозиторію"
PUSH_URL="http://root:${ROOT_TOKEN}@127.0.0.1:8929/root/${PROJECT_PATH}.git"
git -C "$REPO_DIR" remote remove origin 2>/dev/null || true
git -C "$REPO_DIR" remote add origin "$PUSH_URL"
git -C "$REPO_DIR" push --force --quiet origin main
echo "  вивантажено комітів: $(git -C "$REPO_DIR" rev-list --count HEAD)"

# ─────────────────────────────────────────────── 4. проєкт у Slipstream
say "Проєкт у Slipstream"
TOKEN=$(curl -sS -X POST "${SLIPSTREAM_API}/auth/login" \
        -H 'Content-Type: application/json' \
        -d '{"email":"devops@example.com","password":"devops123"}' \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

SP_ID=$(curl -sS "${SLIPSTREAM_API}/projects" -H "Authorization: Bearer $TOKEN" \
        | python3 -c "
import sys,json
d=json.load(sys.stdin)
m=[p for p in d if p['name']=='${PROJECT_PATH}']
print(m[0]['id'] if m else '')")
if [ -z "$SP_ID" ]; then
  SP_ID=$(curl -sS -X POST "${SLIPSTREAM_API}/projects" -H "Authorization: Bearer $TOKEN" \
          -H 'Content-Type: application/json' \
          -d "{\"name\":\"${PROJECT_PATH}\",\"repo_url\":\"${GITLAB_URL}/root/${PROJECT_PATH}\",\"ci_provider\":\"gitlab\",\"external_id\":\"${PID}\",\"default_branch\":\"main\"}" \
          | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
  echo "  створено проєкт id=$SP_ID"
else
  echo "  проєкт уже існує, id=$SP_ID"
fi

SECRET=$(curl -sS -X POST "${SLIPSTREAM_API}/projects/${SP_ID}/webhook-secret" \
         -H "Authorization: Bearer $TOKEN" \
         | python3 -c "import sys,json; print(json.load(sys.stdin)['secret'])")
echo "  секрет вебхука створено"

# ─────────────────────────────────────────────── 5. змінні конвеєра
say "Змінні конвеєра"
set_var() {
  api --request DELETE "${GITLAB_URL}/api/v4/projects/${PID}/variables/$1" >/dev/null 2>&1 || true
  api --request POST "${GITLAB_URL}/api/v4/projects/${PID}/variables" \
      --form "key=$1" --form "value=$2" --form "masked=$3" --form "protected=false" >/dev/null
  echo "  $1"
}
set_var CICD_OPT_API    "$SLIPSTREAM_API" false
set_var CICD_OPT_PROJECT "$SP_ID"         false
set_var CICD_OPT_SECRET  "$SECRET"        true

# ─────────────────────────────────────────────── 6. вебхук GitLab -> Slipstream
say "Вебхук GitLab"
# GitLab типово блокує звернення до локальної мережі з вебхуків.
podman exec gitlab gitlab-rails runner "
  s = ApplicationSetting.current
  s.allow_local_requests_from_web_hooks_and_services = true
  s.save!" >/dev/null
echo "  звернення до локальної мережі дозволено"

HOOK_URL="${SLIPSTREAM_API_FROM_GITLAB}/webhooks/gitlab/${SP_ID}"
# Перевіряємо досяжність ДО створення вебхука: інакше він мовчки не працюватиме,
# а причину довелося б шукати за відсутністю тривалостей у моделях.
if ! podman exec gitlab curl -sf -m 5 "${SLIPSTREAM_API_FROM_GITLAB%/api/v1}/health" >/dev/null; then
  echo "  УВАГА: з контейнера GitLab адреса ${SLIPSTREAM_API_FROM_GITLAB} недоступна."
  echo "  Події конвеєра не надходитимуть, а отже не навчаться моделі тривалості"
  echo "  та ризику збою. Задайте SLIPSTREAM_API_FROM_GITLAB правильною адресою"
  echo "  і переконайтеся, що сервер слухає не лише на 127.0.0.1."
else
  api --request DELETE "${GITLAB_URL}/api/v4/projects/${PID}/hooks/1" >/dev/null 2>&1 || true
  api --request POST "${GITLAB_URL}/api/v4/projects/${PID}/hooks" \
      --form "url=${HOOK_URL}" --form "token=${SECRET}" \
      --form "pipeline_events=true" --form "push_events=false" \
      --form "enable_ssl_verification=false" >/dev/null
  echo "  вебхук pipeline_events -> ${HOOK_URL}"
fi

# ─────────────────────────────────────────────── 7. виконавець
say "Виконавець"
RTOKEN=$(api --request POST "${GITLAB_URL}/api/v4/user/runners" \
         --form "runner_type=project_type" --form "project_id=${PID}" \
         --form "description=stand-shell" --form "tag_list=stand" \
         --form "run_untagged=true" \
         | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))")
if [ -n "$RTOKEN" ]; then
  "$RUNNER_DIR/gitlab-runner" register --non-interactive \
    --config "$RUNNER_DIR/config.toml" \
    --url "$GITLAB_URL" --token "$RTOKEN" \
    --executor shell --shell bash \
    --name stand-shell >/dev/null
  echo "  виконавця зареєстровано (shell)"
else
  echo "  маркер виконавця не одержано — можливо, його вже зареєстровано"
fi

say "Готово"
echo "  GitLab:     ${GITLAB_URL}  (root / пароль, заданий у GITLAB_ROOT_PASSWORD)"
echo "  проєкт CI:  ${GITLAB_URL}/root/${PROJECT_PATH}"
echo "  Slipstream: проєкт id=${SP_ID}"
echo
echo "  Запустити виконавця:"
echo "    $RUNNER_DIR/gitlab-runner run --config $RUNNER_DIR/config.toml"
