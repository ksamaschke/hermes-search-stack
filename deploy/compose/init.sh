#!/usr/bin/env bash
# Generate the stack's secrets into .env. Idempotent: existing non-empty
# values are left alone, so re-running never rotates a live credential.
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    echo "created .env from .env.example"
  else
    echo "error: no .env.example found" >&2
    exit 1
  fi
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "error: openssl is required" >&2
  exit 1
fi

set_if_empty() {
  local key="$1" value="$2"
  local current
  current="$(grep -E "^${key}=" .env | head -1 | cut -d= -f2- || true)"
  if [[ -n "${current}" ]]; then
    echo "  ${key} already set - leaving unchanged"
    return
  fi
  # Portable in-place edit (GNU sed and BSD/macOS sed disagree on -i).
  local tmp
  tmp="$(mktemp)"
  if grep -qE "^${key}=" .env; then
    awk -v k="${key}" -v v="${value}" \
      'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' .env > "${tmp}"
  else
    cat .env > "${tmp}"
    printf '%s=%s\n' "${key}" "${value}" >> "${tmp}"
  fi
  mv "${tmp}" .env
  echo "  ${key} generated"
}

echo "generating secrets in .env"
# API_SERVER_KEY must be >= 16 chars or the gateway silently skips binding
# the listener, and Open WebUI then finds no models.
set_if_empty HERMES_API_SERVER_KEY        "$(openssl rand -hex 32)"
set_if_empty HERMES_WEBUI_PASSWORD        "$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)"
set_if_empty OPEN_WEBUI_SECRET_KEY        "$(openssl rand -hex 32)"
set_if_empty SEARXNG_SECRET               "$(openssl rand -hex 32)"
set_if_empty FIRECRAWL_POSTGRES_PASSWORD  "$(openssl rand -hex 24)"
set_if_empty KOKORO_API_KEY               "$(openssl rand -hex 32)"

chmod 600 .env

echo
echo "done. Remaining manual step: set your model gateway in .env"
echo "  HERMES_MODEL_DEFAULT / FIRECRAWL_MODEL_DEFAULT"
echo "  HERMES_MODEL_BASE_URL / HERMES_GATEWAY_API_KEY"
echo
echo "then:  docker compose up -d"
echo "admin UI password:"
grep -E '^HERMES_WEBUI_PASSWORD=' .env | cut -d= -f2-
