#!/usr/bin/env bash
# Verify a deployed Hermes Search Stack from the data plane, not just from
# Kubernetes object status. "Running" is not "working".
#
#   ./scripts/verify.sh <namespace>
set -uo pipefail

NS="${1:-hermes-search}"
FAIL=0

ok()   { echo "  ✓ $*"; }
bad()  { echo "  ✗ $*"; FAIL=1; }
info() { echo "── $*"; }

info "workload readiness in $NS"
while read -r name ready; do
  [[ -z "$name" ]] && continue
  if [[ "$ready" == "True" ]]; then ok "$name"; else bad "$name not available"; fi
done < <(kubectl -n "$NS" get deploy -o \
  'jsonpath={range .items[*]}{.metadata.name} {.status.conditions[?(@.type=="Available")].status}{"\n"}{end}' 2>/dev/null)

info "hermes-agent storage"
PVC_SIZE=$(kubectl -n "$NS" get pvc hermes-agent-data -o jsonpath='{.status.capacity.storage}' 2>/dev/null)
PVC_PHASE=$(kubectl -n "$NS" get pvc hermes-agent-data -o jsonpath='{.status.phase}' 2>/dev/null)
if [[ "$PVC_PHASE" == "Bound" ]]; then
  ok "PVC Bound at ${PVC_SIZE}"
else
  bad "PVC not bound (phase=${PVC_PHASE:-missing})"
fi

info "sandboxed runtime"
RC=$(kubectl -n "$NS" get deploy hermes-agent -o jsonpath='{.spec.template.spec.runtimeClassName}' 2>/dev/null)
if [[ -n "$RC" ]]; then ok "hermes-agent runtimeClassName=$RC"; else
  echo "  · no RuntimeClass set (base install)"; fi

# --- data plane -------------------------------------------------------------
info "SearXNG JSON API (the contract both consumers depend on)"
SX=$(kubectl -n "$NS" exec deploy/searxng -- \
  python3 -c "import urllib.request,json;d=json.load(urllib.request.urlopen('http://localhost:8080/search?q=kubernetes&format=json'));print(len(d['results']))" 2>/dev/null)
if [[ "${SX:-0}" =~ ^[0-9]+$ ]] && [[ "${SX:-0}" -gt 0 ]]; then
  ok "SearXNG returned $SX results as JSON"
else
  bad "SearXNG JSON API returned nothing - check 'formats: [html, json]' in settings.yml"
fi

info "Hermes agent config (search must route to SearXNG, not Firecrawl)"
BACKEND=$(kubectl -n "$NS" exec deploy/hermes-agent -- \
  python3 -c "import yaml;print(yaml.safe_load(open('/opt/data/config.yaml'))['web']['search_backend'])" 2>/dev/null)
if [[ "$BACKEND" == "searxng" ]]; then
  ok "web.search_backend = searxng"
else
  bad "web.search_backend = '${BACKEND:-unset}' (expected searxng)"
fi

info "Hermes gateway API"
HEALTH=$(kubectl -n "$NS" exec deploy/hermes-agent -- \
  curl -fsS -m 10 http://localhost:8642/health 2>/dev/null)
if echo "$HEALTH" | grep -q '"ok"'; then
  ok "gateway /health ok"
else
  bad "gateway /health failed - API_SERVER_KEY must be >= 16 chars for the listener to bind"
fi

info "Hermes model chat through the OpenAI-compatible API"
# Keep both credentials inside the pod: API_SERVER_KEY authenticates the local
# request, while HERMES_GATEWAY_API_KEY is resolved by Hermes from the profile
# secret scope for the upstream model call.
# Expansion is intentionally deferred to the shell inside the Hermes pod.
# shellcheck disable=SC2016
CHAT=$(kubectl -n "$NS" exec deploy/hermes-agent -- sh -c \
  'curl -fsS -m 120 -H "Authorization: Bearer $API_SERVER_KEY" \
    -H "Content-Type: application/json" \
    -X POST http://localhost:8642/v1/chat/completions \
    -d '\''{"model":"hermes-agent","messages":[{"role":"user","content":"Reply with OK."}],"stream":false}'\''' \
  2>/dev/null)
if printf '%s' "$CHAT" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
choice = (payload.get("choices") or [{}])[0]
content = ((choice.get("message") or {}).get("content") or "").strip()
raise SystemExit(0 if content and choice.get("finish_reason") != "error" else 1)
' 2>/dev/null; then
  ok "model chat returned a completion"
else
  bad "model chat failed - verify model credentials in the default profile secret scope"
fi

info "Open WebUI can discover the agent as a model"
# Expansion is intentionally deferred to the shell inside the Open WebUI pod.
# shellcheck disable=SC2016
MODELS=$(kubectl -n "$NS" exec deploy/open-webui -- \
  sh -c 'curl -fsS -m 10 -H "Authorization: Bearer $OPENAI_API_KEY" "$OPENAI_API_BASE_URL/models"' 2>/dev/null)
if echo "$MODELS" | grep -q 'hermes'; then
  ok "Open WebUI sees the hermes-agent model"
else
  bad "model discovery failed - check the /v1 suffix on OPENAI_API_BASE_URL"
fi

if kubectl -n "$NS" get deploy firecrawl-api >/dev/null 2>&1; then
  info "Firecrawl scrape"
  SCRAPE=$(kubectl -n "$NS" exec deploy/firecrawl-api -- \
    curl -fsS -m 60 -X POST http://localhost:3002/v1/scrape \
      -H 'Content-Type: application/json' \
      -d '{"url":"https://example.com","formats":["markdown"]}' 2>/dev/null)
  if echo "$SCRAPE" | grep -qi 'example domain'; then
    ok "Firecrawl returned page content"
  else
    bad "Firecrawl scrape failed (playwright/queue may still be warming up)"
  fi

  info "Firecrawl v2 JSON extraction through the model gateway"
  EXTRACT=$(kubectl -n "$NS" exec deploy/firecrawl-api -- \
    curl -fsS -m 120 -X POST http://localhost:3002/v2/scrape \
      -H 'Content-Type: application/json' \
      -d '{"url":"https://example.com","formats":[{"type":"json","prompt":"Extract the page title exactly as shown.","schema":{"type":"object","properties":{"title":{"type":"string"}},"required":["title"],"additionalProperties":false}}]}' \
    2>/dev/null)
  if printf '%s' "$EXTRACT" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
raise SystemExit(0 if payload["data"]["json"]["title"] == "Example Domain" else 1)
' 2>/dev/null; then
    ok "Firecrawl extracted data.json.title = Example Domain"
  else
    bad "Firecrawl JSON extraction failed - check model gateway env wiring"
  fi
fi

echo
if [[ "$FAIL" -eq 0 ]]; then
  echo "all checks passed"
  exit 0
fi
echo "VERIFICATION FAILED"
exit 1
