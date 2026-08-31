#!/usr/bin/env bash
# Create the five Secrets the Hermes Search Stack needs.
#
#   ./scripts/create-secrets.sh <namespace> [--rotate]
#
# Idempotent: existing Secrets are left alone unless --rotate is passed.
# Generated values never touch disk or the shell history.
set -euo pipefail

NS="${1:-hermes-search}"
ROTATE="${2:-}"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "error: kubectl is required" >&2
  exit 1
fi
if ! command -v openssl >/dev/null 2>&1; then
  echo "error: openssl is required" >&2
  exit 1
fi

kubectl get namespace "$NS" >/dev/null 2>&1 || {
  echo "creating namespace $NS"
  kubectl create namespace "$NS"
}

exists() { kubectl -n "$NS" get secret "$1" >/dev/null 2>&1; }

skip_or_rotate() {
  local name="$1"
  if exists "$name"; then
    if [[ "$ROTATE" == "--rotate" ]]; then
      echo "  rotating $name"
      kubectl -n "$NS" delete secret "$name" >/dev/null
      return 0
    fi
    echo "  $name exists - skipping (pass --rotate to replace)"
    return 1
  fi
  return 0
}

echo "creating secrets in namespace $NS"

# --- hermes-agent-secrets ----------------------------------------------------
if skip_or_rotate hermes-agent-secrets; then
  # Prompt for the upstream model gateway key without echoing it.
  if [[ -n "${HERMES_MODEL_API_KEY:-}" ]]; then
    MODEL_KEY="$HERMES_MODEL_API_KEY"
    echo "  using model key from \$HERMES_MODEL_API_KEY"
  else
    read -rsp "  model gateway API key (input hidden, empty for none): " MODEL_KEY
    echo
  fi
  # >= 16 chars or the gateway never binds its listener.
  kubectl -n "$NS" create secret generic hermes-agent-secrets \
    --from-literal=api-server-key="$(openssl rand -hex 32)" \
    --from-literal=model-api-key="${MODEL_KEY}" >/dev/null
  unset MODEL_KEY
  echo "  hermes-agent-secrets created"
fi

# --- hermes-webui-secrets ----------------------------------------------------
if skip_or_rotate hermes-webui-secrets; then
  WEBUI_PW="$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)"
  kubectl -n "$NS" create secret generic hermes-webui-secrets \
    --from-literal=password="$WEBUI_PW" >/dev/null
  echo "  hermes-webui-secrets created"
  echo "  >>> admin UI password: $WEBUI_PW"
  echo "  >>> store it now; it is not printed again"
  unset WEBUI_PW
fi

# --- open-webui-secrets ------------------------------------------------------
if skip_or_rotate open-webui-secrets; then
  kubectl -n "$NS" create secret generic open-webui-secrets \
    --from-literal=secret-key="$(openssl rand -hex 32)" >/dev/null
  echo "  open-webui-secrets created"
fi

# --- searxng-secret ----------------------------------------------------------
if skip_or_rotate searxng-secret; then
  kubectl -n "$NS" create secret generic searxng-secret \
    --from-literal=secret-key="$(openssl rand -hex 32)" >/dev/null
  echo "  searxng-secret created"
fi

# --- firecrawl-secrets -------------------------------------------------------
if skip_or_rotate firecrawl-secrets; then
  kubectl -n "$NS" create secret generic firecrawl-secrets \
    --from-literal=postgres-password="$(openssl rand -hex 24)" >/dev/null
  echo "  firecrawl-secrets created"
fi

echo
echo "done. Secrets in $NS:"
kubectl -n "$NS" get secrets \
  hermes-agent-secrets hermes-webui-secrets open-webui-secrets \
  searxng-secret firecrawl-secrets \
  -o custom-columns=NAME:.metadata.name,KEYS:.data --no-headers 2>/dev/null \
  | sed 's/map\[/ /; s/\]//' || true
