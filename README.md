# Hermes Search Stack

A reusable, self-hosted **private search + agent stack** you can roll out with
Argo CD on Kubernetes — or with Docker Compose on a plain VM.

Four components, wired together so search and scraping never leave your
infrastructure:

- **[Open WebUI](https://github.com/open-webui/open-webui)** — the chat frontend. Talks to Hermes Agent as an
  OpenAI-compatible model, and uses SearXNG for its own web search.
- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** — the autonomous agent (tools, memory, skills),
  exposed over an OpenAI-compatible API.
- **[Hermes WebUI](https://github.com/nesquena/hermes-webui)** — an admin interface for the agent, so the whole
  thing can be configured from a browser instead of a terminal.
- **[SearXNG](https://github.com/searxng/searxng)** — privacy-respecting metasearch across 70+ engines. The
  default (and only) search endpoint for both the agent and Open WebUI.

Plus an optional fifth:

- **[Firecrawl](https://github.com/firecrawl/firecrawl)** (self-hosted) — the website scraper. Renders pages in a
  sandboxed Playwright browser and returns clean markdown or structured JSON.

## Why these choices

**SearXNG is search-only.** Hermes Agent supports it natively as a
`search_backend`, but it cannot extract page content. That is a deliberate
split in Hermes: `web_search` finds URLs, `web_extract` reads them.

**Firecrawl is the scraper because it is the only self-hostable one.** Hermes
accepts `firecrawl`, `tavily`, `exa`, or `parallel` for extraction. The other
three are SaaS-only. Self-hosted Firecrawl is AGPL-3.0, runs with
`USE_DB_AUTHENTICATION=false`, and has **no credit metering** — the 500/month
figure people quote is a property of Firecrawl *Cloud*, not the software. Your
ceiling is worker concurrency and CPU.

It is also the heaviest component here (API + workers + Playwright + Redis +
RabbitMQ + Postgres). If you would rather not run it, the stack degrades
cleanly to search-only — see [Running without the scraper](#running-without-the-scraper).

**One explicit configuration detail worth knowing:** Hermes' environment
auto-detection ranks `FIRECRAWL_API_URL` *above* `SEARXNG_URL`. If both are
set and you let it auto-detect, Firecrawl would quietly take over search too.
This stack therefore writes `web.search_backend: searxng` and
`web.extract_backend: firecrawl` explicitly, and the agent refuses to start if
that routing does not survive config rendering.

**The OpenAI-compatible model gateway uses a named custom provider.** With the
pinned Hermes image, selecting bare `custom` while setting `model.base_url`
bypasses the custom-provider `key_env` lookup and can send the literal fallback
credential `no-key-required`. Authenticated gateways then reject the request.
The stack therefore declares `providers.hermes-search-stack` with the gateway
URL, model, and `HERMES_GATEWAY_API_KEY` reference, and selects that provider by
default. `HERMES_MODEL_PROVIDER` remains overridable, but native providers have
their own authentication contracts; this stack key is not a universal native-
provider credential.

## Architecture

```
                    ┌──────────────┐
   users ──HTTPS──▶ │  Open WebUI  │ ──── chat ────┐
                    └──────┬───────┘               │
                           │ web search            ▼
                           │              ┌──────────────────┐
                           ├─────────────▶│   Hermes Agent   │
                           │              │  (OpenAI API)    │
                           ▼              └────────┬─────────┘
                    ┌──────────────┐               │ search / extract
   admins ─HTTPS──▶ │ Hermes WebUI │──── admin ────┤
                    └──────────────┘               │
                                          ┌────────┴────────┐
                                          ▼                 ▼
                                   ┌────────────┐   ┌──────────────┐
                                   │  SearXNG   │   │  Firecrawl   │
                                   │  (search)  │◀──│  (scrape)    │
                                   └────────────┘   └──────────────┘

   Only the two UIs are exposed. SearXNG and Firecrawl are cluster-internal.
```

## Quick start — Kubernetes + Argo CD

```bash
# 1. Copy the example overlay and point it at your environment
cp -r deploy/kubernetes/overlays/example deploy/kubernetes/overlays/mine
$EDITOR deploy/kubernetes/overlays/mine/kustomization.yaml   # model gateway
$EDITOR deploy/kubernetes/overlays/mine/ingress.yaml         # hostnames

# 2. Create the secrets (they are never stored in Git)
./scripts/create-secrets.sh hermes-search

# 3. Point the Argo CD Application at your overlay, then apply it
$EDITOR deploy/kubernetes/argocd/application.yaml
kubectl apply -f deploy/kubernetes/argocd/application.yaml
```

Verify:

```bash
./scripts/verify.sh hermes-search
```

## Quick start — Docker Compose (no Kubernetes)

For a VM, a NUC, or anything without a cluster:

```bash
cd deploy/compose
cp .env.example .env
./init.sh                 # generates every secret
$EDITOR .env              # set your model gateway and extraction model
docker compose up -d
```

Open WebUI lands on <http://localhost:3000>, the admin UI on
<http://localhost:8787>. Both bind to loopback by default — put a reverse
proxy with TLS in front before exposing either one.

## Configuration

Everything environment-specific lives in **one ConfigMap** plus **five
Secrets**. The manifests themselves are environment-agnostic.

**Model gateway** (`hermes-agent-runtime` ConfigMap):

- `model-provider` — defaults to `hermes-search-stack` for the configured
  OpenAI-compatible endpoint; advanced deployments may select another provider
- `model-default` — the model id
- `model-base-url` — your OpenAI-compatible endpoint
- `firecrawl-model` — a structured-output model available through the same
  gateway; it may differ from the conversational model
- `extract-backend` — `firecrawl`, or empty for search-only

The API key itself is **never** in a ConfigMap. The default named provider's
`key_env` points to `HERMES_GATEWAY_API_KEY`, populated from the
`hermes-agent-secrets` Secret. If you override `model-provider` with a native
provider, supply the credential that provider requires instead of assuming it
uses `HERMES_GATEWAY_API_KEY`.
Firecrawl's structured JSON extraction reuses that key and base URL while using
the independently configured `firecrawl-model`: its API container receives
them as `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `MODEL_NAME` through
Secret/ConfigMap environment bindings. Compose maps
`FIRECRAWL_MODEL_DEFAULT` into `MODEL_NAME`. The pinned Firecrawl image calls
the gateway's OpenAI Responses API for structured extraction, so select a model
and route that support that endpoint and strict JSON output.

See [docs/secrets.md](docs/secrets.md) for the full contract,
[docs/sso.md](docs/sso.md) for OIDC/Keycloak, and
[docs/sandboxing.md](docs/sandboxing.md) for gVisor/Kata.

## Storage

Hermes Agent gets a **10 GiB `ReadWriteOnce` PVC** for its home directory —
config, sessions, memory, skills, and workspace. The default StorageClass is
`longhorn`; override it in your overlay for any other RWO-capable provisioner.

Hermes WebUI mounts the *same* volume (it is an admin surface over the same
state), so it is pinned to the agent's node with a `podAffinity` rule.

## Sandboxed runtimes

The agent executes model-authored shell commands and Firecrawl renders
untrusted web pages, so both are worth isolating at the kernel boundary. Select
the overlay matching the RuntimeClass installed in your cluster:

```yaml
resources:
  - ../kata       # RuntimeClass name: kata
# - ../gvisor     # RuntimeClass name: gvisor / handler: runsc
# - ../../base    # no sandboxed runtime
```

The named overlays are deliberately separate: the gVisor path includes a
runsc-specific Firecrawl CPU compatibility shim; the Kata path does not. The
legacy `sandboxed` path remains as a backward-compatible alias for `gvisor`.

Check available names with `kubectl get runtimeclass`. Applying a RuntimeClass
your cluster does not have leaves pods `Pending` with a
`FailedCreatePodSandBox` event.

## Running without the scraper

Search-only, no Firecrawl:

```yaml
# in your overlay
configMapGenerator:
  - name: hermes-agent-runtime
    behavior: merge
    literals:
      - extract-backend=""       # Hermes: search only
  - name: open-webui-runtime
    behavior: merge
    literals:
      - web-loader-engine=""     # Open WebUI: built-in loader
```

…and drop `- firecrawl` from `deploy/kubernetes/base/kustomization.yaml` (or
patch it out). For Compose: `docker compose up -d --scale firecrawl-api=0`, or
clear `HERMES_EXTRACT_BACKEND` and `WEB_LOADER_ENGINE` in `.env`.

## Security notes

- **Hermes WebUI is a remote shell.** It can run arbitrary commands as the
  agent. It requires a password, should be on its own hostname, and is best
  restricted to a VPN or internal network.
- **Firecrawl's API is unauthenticated** in self-hosted mode. It is a
  `ClusterIP` service behind a `NetworkPolicy` that admits only Hermes Agent
  and Open WebUI. Do not expose it.
- **Every image is pinned by digest**, so a `latest` tag moving upstream cannot
  silently change what you run.
- **No secrets in this repository.** CI enforces it (see
  [.github/workflows/ci.yml](.github/workflows/ci.yml)).

## Repository layout

```
deploy/
  kubernetes/
    base/            # environment-agnostic manifests (all 5 components)
    overlays/
      sandboxed/     # + gVisor/Kata RuntimeClass
      example/       # template: copy, edit, deploy
    argocd/          # Argo CD Application
  compose/           # VM / bare-metal fallback
scripts/             # secret creation + verification
docs/                # secrets, SSO, sandboxing, troubleshooting
```

## License

MIT — see [LICENSE](LICENSE). The deployed components keep their own licenses
(Firecrawl is AGPL-3.0; SearXNG is AGPL-3.0; Open WebUI, Hermes Agent, and
Hermes WebUI are MIT).
