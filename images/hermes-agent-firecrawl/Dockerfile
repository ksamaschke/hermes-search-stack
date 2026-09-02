ARG HERMES_BASE_IMAGE=docker.io/nousresearch/hermes-agent:v2026.8.27@sha256:e0df6adebddf29b91112aefc999d4aaf6846c9eb544faca5672a16a13590ff79
FROM ${HERMES_BASE_IMAGE}

ARG BUILD_REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/ksamaschke/hermes-search-stack"
LABEL org.opencontainers.image.description="Hermes Agent with the pinned Firecrawl extraction SDK"
LABEL org.opencontainers.image.revision="${BUILD_REVISION}"

USER root
COPY requirements.txt /tmp/hermes-firecrawl-requirements.txt
RUN uv pip install \
      --python /opt/hermes/.venv/bin/python3 \
      --no-cache-dir \
      --no-deps \
      --requirement /tmp/hermes-firecrawl-requirements.txt \
    && rm /tmp/hermes-firecrawl-requirements.txt \
    && /opt/hermes/.venv/bin/python3 -c "import firecrawl, nest_asyncio; from importlib.metadata import version; assert version('firecrawl-py') == '4.17.0'; assert version('nest-asyncio') == '1.6.0'"
