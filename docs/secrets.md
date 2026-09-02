# Secret contract

This repository contains **no secrets** and no environment-specific
credentials. Every sensitive value is supplied at deploy time through one of
five Kubernetes Secrets (or the Compose `.env`).

Create them with [`scripts/create-secrets.sh`](../scripts/create-secrets.sh),
or manage them with External Secrets, Sealed Secrets, SOPS, or Vault/OpenBao.

## The five Secrets

### `hermes-agent-secrets`

- **`api-server-key`** — bearer token for the agent's OpenAI-compatible API.
  Open WebUI authenticates with this exact value.

  Must be **at least 16 characters**. Below that the gateway starts but never
  binds the listener, and Open WebUI shows an empty model dropdown with no
  error anywhere.

- **`model-api-key`** — the credential for your upstream model gateway.
  Referenced indirectly: the default `providers.hermes-search-stack` contract
  stores only `key_env: HERMES_GATEWAY_API_KEY`, and the deployment populates
  that env var from this key. The value never appears in a ConfigMap, a
  manifest, or Git. Native provider overrides use their own auth contracts.

### `hermes-webui-secrets`

- **`password`** — login password for the admin UI.

  This UI can execute arbitrary commands as the agent. Treat this password
  like a root password, and do not expose the UI to the public internet
  without an additional auth layer in front.

### `open-webui-secrets`

- **`secret-key`** — session signing key (`WEBUI_SECRET_KEY`). Rotating it
  invalidates every existing login session.

### `searxng-secret`

- **`secret-key`** — SearXNG's `server.secret_key`, injected as
  `SEARXNG_SECRET`.

### `firecrawl-secrets`

- **`postgres-password`** — password for Firecrawl's NuQ queue database.
  Internal to the stack; not reachable outside the namespace.

### `open-webui-oidc` (optional)

Only needed for SSO. Consumed via `envFrom` with `optional: true`, so the
stack runs fine without it. See [sso.md](sso.md).

## Creating them

```bash
./scripts/create-secrets.sh <namespace>
```

The script generates strong random values for everything except
`model-api-key`, which it prompts for (and reads without echoing). It is
**idempotent**: existing Secrets are left untouched unless you pass
`--rotate`.

Manual equivalent:

```bash
kubectl -n hermes-search create secret generic hermes-agent-secrets \
  --from-literal=api-server-key="$(openssl rand -hex 32)" \
  --from-literal=model-api-key="$YOUR_KEY"
```

## Verifying nothing leaked

CI runs [gitleaks](https://github.com/gitleaks/gitleaks) on every push and
blocks the merge on a finding. To check locally before committing:

```bash
./scripts/check-no-secrets.sh
```

That script greps for the specific shapes this stack could plausibly leak —
`sk-` keys, bearer tokens, private key blocks, and any `.env` that is not the
`.env.example` template.

## What is deliberately *not* a secret

- Image digests, ports, service names — all public by design.
- `FIRECRAWL_API_KEY: "self-hosted-no-auth"` in the Open WebUI deployment.
  Self-hosted Firecrawl runs with `USE_DB_AUTHENTICATION=false` and ignores
  the value, but Open WebUI requires the variable to be non-empty before it
  will route through the Firecrawl loader. It is a placeholder, not a
  credential.
