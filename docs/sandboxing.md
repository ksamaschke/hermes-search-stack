# Sandboxed container runtimes

Hermes Agent executes model-authored commands, Firecrawl processes untrusted
URLs, and Firecrawl Playwright renders arbitrary web pages. These three
workloads benefit from kernel-boundary isolation.

## Select the installed runtime

Check the cluster before choosing an overlay:

```bash
kubectl get runtimeclass
```

The repository provides explicit, runtime-specific overlays:

```yaml
resources:
  - ../kata       # RuntimeClass name: kata
# - ../gvisor     # RuntimeClass name: gvisor; handler: runsc
# - ../../base    # no sandboxed RuntimeClass
```

`deploy/kubernetes/overlays/sandboxed` remains a backward-compatible alias for
`gvisor`. New deployments should use the named overlays so runtime-specific
logic remains visible.

A RuntimeClass name is the `NAME` column, not the handler. If a cluster exposes
`kata-qemu` or another name instead of `kata`, patch the Kata overlay in the
private environment overlay rather than editing the reusable base.

## Kata Containers

`deploy/kubernetes/overlays/kata` applies `runtimeClassName: kata` to:

- `hermes-agent`;
- `firecrawl-api`;
- `firecrawl-playwright`.

Kata uses a lightweight VM per pod. It provides a full guest kernel and does
not load any gVisor compatibility code. Expect higher memory overhead and a
slower sandbox start than a namespace-only runtime.

## gVisor

`deploy/kubernetes/overlays/gvisor` applies `runtimeClassName: gvisor` to the
same three workloads. gVisor intercepts syscalls in userspace and exposes a
smaller kernel surface.

### Firecrawl CPU admission under runsc

Firecrawl's advisory CPU monitor uses `systeminformation.currentLoad()`. Under
runsc that call can return a non-finite sample, causing every queue worker to
report `WORKER STALLED`. The gVisor overlay therefore preloads
`firecrawl-systeminformation-compat.cjs` into the API harness and its child
workers. Finite CPU samples pass through; only unavailable or non-finite values
fall back to 0% advisory load. Kubernetes CPU limits and Firecrawl's memory gate
remain active.

This preload is intentionally absent from the Kata overlay.

## Verify the rendered manifests

Render before publishing an environment overlay:

```bash
kustomize build deploy/kubernetes/overlays/kata > /tmp/kata.yaml
kustomize build deploy/kubernetes/overlays/gvisor > /tmp/gvisor.yaml
```

Each manifest must contain exactly three `runtimeClassName` fields. The Kata
manifest must contain no `NODE_OPTIONS`, `NODE_PATH`, or
`firecrawl-systeminformation-compat` reference.

## Verify the live rollout

```bash
kubectl -n hermes-search get pods \
  -o custom-columns='NAME:.metadata.name,RUNTIME:.spec.runtimeClassName,NODE:.spec.nodeName,READY:.status.containerStatuses[*].ready'
```

For every sandboxed workload, confirm:

1. `spec.runtimeClassName` matches the selected overlay;
2. the node is eligible under the RuntimeClass scheduler selector;
3. the pod is Ready without restarts caused by sandbox creation;
4. the actual search/extraction path succeeds.

A Ready pod alone is not proof that Firecrawl can render and extract a page.

## When pods stay Pending

A missing or unsupported RuntimeClass produces a scheduling failure or an event
such as:

```text
FailedCreatePodSandBox ... RuntimeHandler "kata" not supported
```

Read the RuntimeClass and its scheduling selector, then count eligible Ready
nodes. Do not replace Kata with gVisor merely to make the pod schedule; repair
the cluster capability or choose the runtime deliberately in the private
overlay.

Hermes WebUI shares the agent's RWO volume and follows it through pod affinity;
it does not need its own RuntimeClass.
