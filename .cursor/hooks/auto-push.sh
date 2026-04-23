#!/bin/bash
set -u

if ! command -v git >/dev/null 2>&1; then
  exit 0
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  exit 0
fi

if [[ -n "${CURSOR_AUTO_PUSH_DRY_RUN:-}" ]]; then
  echo "dry-run: auto-push hook checked" >&2
  exit 0
fi

if [[ -z "$(git status --porcelain)" ]]; then
  exit 0
fi

git add -A

msg="chore: auto sync $(date '+%Y-%m-%d %H:%M:%S')"
git commit -m "$msg" >/dev/null 2>&1 || exit 0

if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  git push >/dev/null 2>&1 || exit 0
else
  git push -u origin HEAD >/dev/null 2>&1 || exit 0
fi

exit 0
