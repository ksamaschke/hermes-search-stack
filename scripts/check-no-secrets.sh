#!/usr/bin/env bash
# Fail if anything secret-shaped is about to be committed.
# Used by CI and safe to run locally before a push.
set -uo pipefail

cd "$(dirname "$0")/.."

FAIL=0
note() { echo "  ✗ $*"; FAIL=1; }

echo "checking for committed secrets..."

# 1. Any .env that is not a template.
while IFS= read -r f; do
  case "$f" in
    *.example|*.sample|*.template) ;;
    *) note "environment file tracked in git: $f" ;;
  esac
done < <(git ls-files | grep -E '(^|/)\.env' || true)

# 2. Private keys.
if git grep -InE -- '-----BEGIN [A-Z ]*PRIVATE KEY-----' -- . >/dev/null 2>&1; then
  note "private key block found:"
  git grep -InE -- '-----BEGIN [A-Z ]*PRIVATE KEY-----' -- .
fi

# 3. Provider API key shapes. Excludes docs/examples that intentionally
#    show the *shape* of a key.
PATTERN='(sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})'
if git grep -InE "$PATTERN" -- . ':!docs/*' ':!*.md' >/dev/null 2>&1; then
  note "possible API key:"
  git grep -InE "$PATTERN" -- . ':!docs/*' ':!*.md'
fi

# 4. Kubernetes Secrets with literal data. The stack creates Secrets out of
#    band; a committed `kind: Secret` with a data block is a mistake.
while IFS= read -r f; do
  if grep -qE '^kind:[[:space:]]*Secret' "$f" 2>/dev/null; then
    if grep -qE '^[[:space:]]*(data|stringData):' "$f" 2>/dev/null; then
      note "manifest with inline Secret data: $f"
    fi
  fi
done < <(git ls-files '*.yaml' '*.yml' || true)

# 5. Internal hostnames must not leak into the public repo. Assembled at
#    runtime so this script does not match its own pattern definition.
INTERNAL="$(printf 'homelab\.samaschke\.de|%s|%s' 'rackt''aq' 'vanilla''core')"
if git grep -InE "$INTERNAL" -- . ':!scripts/check-no-secrets.sh' >/dev/null 2>&1; then
  note "internal hostname leaked into public repo:"
  git grep -InE "$INTERNAL" -- . ':!scripts/check-no-secrets.sh'
fi

if [[ "$FAIL" -eq 0 ]]; then
  echo "  ✓ clean"
  exit 0
fi
echo
echo "secret check FAILED"
exit 1
