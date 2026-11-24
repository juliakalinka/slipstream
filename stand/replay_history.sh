#!/usr/bin/env bash
# Відтворення історії демонстраційного репозиторію як послідовності гілок.
#
# ЩО САМЕ ВІДТВОРЮЄТЬСЯ. Беремо стан коду на кожному історичному коміті, але
# конфігурацію конвеєра накладаємо ПОТОЧНУ. Інакше кожна гілка тягнула б за
# собою ту версію .gitlab-ci.yml, яка існувала на той момент, і більшість
# прогонів зламалася б не через код, а через несумісну конфігурацію.
#
# Кожна гілка породжує конвеєр, конвеєр запитує план, виконує його і повертає
# звіт JUnit. Саме так система накопичує історію прогонів і результатів
# окремих перевірок — без неї моделі нема на чому навчати.
set -euo pipefail

STAND="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$STAND/demo-repo"
WORK="${WORK:-/tmp/claude-1000/-home-yuliia/573a2f28-4d79-4020-b7c9-caecee5cd0c7/scratchpad/replay-wt}"
COUNT="${1:-20}"
BATCH="${2:-3}"
PAUSE="${3:-50}"

# Файли, які завжди беруться з поточного стану гілки main.
CI_FILES=(.gitlab-ci.yml Makefile pytest.ini ci)

cd "$REPO"
git worktree prune
rm -rf "$WORK"
git worktree add --detach "$WORK" main >/dev/null 2>&1

mapfile -t SHAS < <(git rev-list --reverse main | head -n -3 | tail -n "$COUNT")
echo "відтворюється комітів: ${#SHAS[@]} (партіями по $BATCH, пауза ${PAUSE}с)"

i=0
for sha in "${SHAS[@]}"; do
  i=$((i + 1))
  br="feature/replay-$(printf '%03d' "$i")"

  git -C "$WORK" checkout -q --detach "$sha"
  git -C "$WORK" checkout -q main -- "${CI_FILES[@]}"
  git -C "$WORK" add -A >/dev/null
  git -C "$WORK" -c user.name=stand -c user.email=stand@example.com \
      commit -q -m "ci: pipeline configuration for replay" --allow-empty

  git -C "$WORK" push -q --force origin "HEAD:refs/heads/${br}" 2>/dev/null \
    && printf '  %s -> %s\n' "$(echo "$sha" | cut -c1-8)" "$br" \
    || printf '  %s -> %s (не вдалося)\n' "$(echo "$sha" | cut -c1-8)" "$br"

  if [ $((i % BATCH)) -eq 0 ]; then
    sleep "$PAUSE"
  fi
done

git -C "$REPO" worktree remove --force "$WORK" 2>/dev/null || true
echo "усі гілки виштовхнуто"
