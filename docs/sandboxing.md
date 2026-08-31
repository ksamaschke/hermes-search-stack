# Sandboxed container runtimes

Two workloads in this stack process untrusted input:

- **Hermes Agent** executes shell commands the model writes.
- **Firecrawl's Playwright service** renders arbitrary web pages in a browser.

Both are worth isolating at the kernel boundary rather than relying on
namespaces alone. The `sandboxed` overlay does that with a `RuntimeClass`.

## Check what your cluster has

```bash
kubectl get runtimeclass
```

Typical output on a K3s cluster with the sandboxing addons installed:

```
NAME      HANDLER
crun      crun
gvisor    runsc
kata      kata
```

If this returns nothing, your cluster has no sandboxed runtime — use
`deploy/kubernetes/base` directly and skip this overlay.

## Applying it

The overlay defaults to **gVisor**:

```yaml
resources:
  - ../sandboxed      # instead of ../../base
```

## Switching to Kata Containers

Edit
[`deploy/kubernetes/overlays/sandboxed/kustomization.yaml`](../deploy/kubernetes/overlays/sandboxed/kustomization.yaml)
and change each `value:` from `gvisor` to your handler's **RuntimeClass name**
(the `NAME` column above, not the `HANDLER` column):

```yaml
      - op: add
        path: /spec/template/spec/runtimeClassName
        value: kata          # or kata-qemu, kata-fc, kata-clh
```

## gVisor vs Kata for this stack

**gVisor (`runsc`)** intercepts syscalls in userspace. Lower overhead, starts
faster, and is usually already present on managed clusters. The tradeoff is an
incomplete syscall surface — which matters here, because Playwright drives a
full Chromium.

**Kata** boots a lightweight VM per pod. Stronger isolation and full kernel
compatibility, at the cost of ~100-200 MB extra memory per pod and slower
starts. If Chromium misbehaves under gVisor, Kata is the fix.

A reasonable middle ground is Kata for `firecrawl-playwright` (the browser)
and gVisor for the agent. Nothing stops you mixing them — the patches are
independent.

## Verifying it took effect

```bash
kubectl -n hermes-search get pod -l app.kubernetes.io/name=hermes-agent \
  -o jsonpath='{.items[0].spec.runtimeClassName}{"\n"}'
```

Then confirm from inside the sandbox. Under gVisor the kernel identifies
itself:

```bash
kubectl -n hermes-search exec deploy/hermes-agent -- uname -a
# gVisor reports a synthetic kernel version, e.g. "4.4.0" with gVisor in the
# string, rather than your node's real kernel.
```

## When pods stay Pending

A `RuntimeClass` that does not exist on the cluster produces:

```
Warning  FailedCreatePodSandBox  ...  RuntimeHandler "runsc" not supported
```

or the pod is never scheduled at all. Check with:

```bash
kubectl -n hermes-search describe pod -l app.kubernetes.io/name=hermes-agent | tail -20
```

Fix by pointing the overlay at a handler you actually have, or by deploying
`base` without the overlay.

## Node placement

Sandboxed runtimes are often installed on a subset of nodes. If so, the
`RuntimeClass` normally carries a `scheduling.nodeSelector` that handles
placement automatically. If yours does not, add a nodeSelector in your overlay:

```yaml
patches:
  - target:
      kind: Deployment
      name: hermes-agent
    patch: |-
      - op: add
        path: /spec/template/spec/nodeSelector
        value:
          my.cluster/sandboxed: "true"
```

Remember that `hermes-webui` uses `podAffinity` to co-locate with the agent
(they share an RWO volume), so it follows the agent automatically.
