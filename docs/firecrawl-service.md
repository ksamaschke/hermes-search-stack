# Shared Firecrawl service contract

This opt-in interface is separate from the full search-stack overlay and does
not change it. A shared service belongs to one infrastructure owner, never to
an individual agent. Rendering never probes or applies to a cluster. An
unreachable endpoint is a routing/auth/dependency failure, NOT permission to
install a second instance. There is deliberately no default endpoint or auto mode.

## Existing service (preferred when already provisioned)

Create a non-secret JSON input in the consuming private GitOps repository:

```json
{"mode":"existing-service","endpoint":"https://crawl.example.net","auth":"network-restricted"}
```

Run `python3 scripts/firecrawl-service.py render INPUT.json`. It emits a JSON
contract with an empty `manifests` list. This remains true if the endpoint is
unreachable. No namespace, pod, rights or secrets are created.

## Managed installation (explicit new shared service only)

Use a dedicated, already-owned namespace. Do not point this at an incumbent
Firecrawl namespace to attempt adoption: names/versions may differ. Existing
workloads and PVCs must remain untouched. Deployment/adoption requires a separate
reviewed private GitOps change by the infrastructure owner.

```json
{
  "mode":"managed-install",
  "endpoint":"https://crawl.example.net",
  "auth":"bearer-proxy",
  "namespace":"shared-web",
  "postgres_secret":"existing-firecrawl-queue",
  "searxng_endpoint":"http://search.search.svc.cluster.local:8080",
  "ingress_namespace":"authorized-proxy"
}
```

Run `python3 scripts/firecrawl-service.py render INPUT.json > service.json`.
The output's `manifests` array contains deterministic Kubernetes objects; JSON
objects are valid YAML. The consuming GitOps build can serialize each object
as a YAML document (using `yaml.safe_dump_all`) into a tracked manifest, and
reference it from its Kustomization. Do not apply this output manually.

The renderer reuses this repository's digest-pinned Firecrawl base: one API
harness (including queue workers), Playwright, Valkey, RabbitMQ, and NuQ-enabled
Postgres. These queues are ephemeral, matching the source base; do not promise
durable crawl jobs across restarts. The pre-existing named Secret must contain
`postgres-password`. No Secret values are generated/read and no RBAC is created.
Model extraction is outside this basic search/scrape interface: Hermes/model
ConfigMaps and credentials are deliberately removed from the rendered API.

SearXNG is a REQUIRED separately managed dependency, not silently installed.
Its `/search?q=...&format=json` endpoint must return nonempty results from the
Firecrawl network. Enable JSON output and functioning engines; HTTP health
alone does not establish search readiness. Namespace egress policy must allow
SearXNG and the requested public scrape targets. API readiness uses
`/v0/health/readiness`; the functional check below is the acceptance gate.

## External routing and authentication

The returned `endpoint` is the client-facing base URL, without `/v2`. Miles,
HEX and Känguru need an authorized route to that URL from their actual runtimes;
Kubernetes Service DNS alone is insufficient. The managed renderer exposes
only ClusterIP `firecrawl-api:3002`. Its ingress NetworkPolicy permits intra-
namespace dependencies and API port 3002 from the explicitly named proxy
namespace. It creates no public Ingress, TLS issuer, identity or credential.

The Firecrawl self-hosted base has DB authentication disabled. A bearer token
sent directly to it does NOT implement authentication. `bearer-proxy` requires
an existing authorized HTTPS proxy that validates existing agent credentials
before forwarding; `network-restricted` requires an existing trusted routing/
network boundary limiting callers to authorized agents. These are declared
operator prerequisites, not assertions that a route has been installed. Do
not expose unauthenticated Firecrawl to the Internet. The private infrastructure
owner wires/reviews its existing ingress, TLS and auth policy before activation.
No new rights or secret values are part of this change.

## Bounded functional acceptance

Run from each authorized external runtime:

```
python3 scripts/firecrawl-service.py check --endpoint https://crawl.example.net
```

For an existing bearer-auth proxy, add `--token-env EXISTING_CRAWL_TOKEN`; the
value stays in the environment, is not logged, is only sent over HTTPS and is
never forwarded across redirects. TLS verification remains enabled. Each of
three requests has a 45-second socket timeout and a two-MB response limit;
there are no retries, auto installs, or unbounded crawl jobs. Search requests
two URLs; scrape requests only `https://example.com`. A successful report
contains readiness HTTP 200, nonempty search URLs and actual Example Domain
markdown. Error output excludes response bodies, credentials and raw exception
text. Failure is a nonzero exit code, never evidence the service is absent.

The renderer requires Python, PyYAML and `kubectl kustomize` (offline build
only). The live check uses Python's standard library only. Test with
`python3 scripts/test-firecrawl-service.py`.

## Delivery boundary

This source component does not roll out agent runtime configuration. Avi owns
that work and end-to-end delivery: enable the web toolset, set
`web.search_backend` and `web.extract_backend` to `firecrawl`, and provide
`FIRECRAWL_API_URL` from this contract through each runtime's managed config.
Auth environment projection must use existing authorized secret custody.
Source tests, a merged component, infrastructure readiness and per-agent
functional acceptance are distinct evidence; none substitutes for another.
