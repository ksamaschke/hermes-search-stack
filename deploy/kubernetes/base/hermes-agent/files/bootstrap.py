#!/usr/bin/env python3
"""Merge stack-owned config keys into $HERMES_HOME/config.yaml.

Runs as an init container on every pod start. Idempotent by design:
- Keys this stack owns are enforced (search backend, model routing).
- Any other key the user set via the WebUI / CLI is preserved.
- Secrets are never written here; only the NAME of an env var is stored.
"""
import os
import pathlib
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML unavailable in the image - cannot render config")

home = pathlib.Path(os.environ.get("HERMES_HOME", "/opt/data"))
home.mkdir(parents=True, exist_ok=True)
(home / "workspace").mkdir(parents=True, exist_ok=True)

cfg_path = home / "config.yaml"
partial_raw = pathlib.Path("/bootstrap/config.yaml.partial").read_text()

# Expand ${VAR} against the environment so overlays can drive values
# without templating the whole manifest.
for key, value in os.environ.items():
    partial_raw = partial_raw.replace("${" + key + "}", value)

desired = yaml.safe_load(partial_raw) or {}

current = {}
if cfg_path.exists():
    try:
        current = yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError:
        backup = cfg_path.with_suffix(".yaml.corrupt")
        cfg_path.rename(backup)
        print(f"existing config unparseable, moved to {backup}", flush=True)
        current = {}

def deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out

merged = deep_merge(current, desired)

# Drop keys whose value is still an unexpanded placeholder, so a missing
# overlay variable never lands in config as a literal "${...}" string.
def prune(node):
    if isinstance(node, dict):
        return {
            k: prune(v)
            for k, v in node.items()
            if not (isinstance(v, str) and v.startswith("${") and v.endswith("}"))
        }
    return node

merged = prune(merged)

cfg_path.write_text(yaml.safe_dump(merged, sort_keys=False))
print(f"rendered {cfg_path}", flush=True)

# Sanity: fail loudly if search routing did not survive the merge.
if merged.get("web", {}).get("search_backend") != "searxng":
    sys.exit("web.search_backend is not 'searxng' after merge - refusing to start")
