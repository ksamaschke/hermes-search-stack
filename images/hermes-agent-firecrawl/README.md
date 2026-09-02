# Hermes Agent Firecrawl image

The production stack disables Hermes runtime package installation. This derived
image therefore adds the optional `firecrawl-py` SDK at image-build time instead
of installing it on the persistent volume during gateway startup.

The base image, SDK, missing transitive dependency, and both exact wheel URLs
and hashes are pinned. `--no-deps` is intentional: the base image already
contains every other Firecrawl requirement, and the workflow smoke test imports
the finished SDK.

Build locally for the cluster architecture:

```sh
docker buildx build \
  --platform linux/amd64 \
  --load \
  --build-arg BUILD_REVISION="$(git rev-parse HEAD)" \
  --tag hermes-agent-firecrawl:test \
  images/hermes-agent-firecrawl

docker run --rm \
  --entrypoint /opt/hermes/.venv/bin/python3 \
  hermes-agent-firecrawl:test \
  -c "from firecrawl import Firecrawl; print(Firecrawl.__name__)"
```

Published images use the immutable `sha-<commit>` tag. Kubernetes manifests
must still pin the resulting registry digest, never the tag.
