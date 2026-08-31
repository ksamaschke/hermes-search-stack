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
# config.yaml now carries the API server key (the gateway's platform checker
# reads it from platforms.api_server.extra.key), so keep it owner-only.
try:
    cfg_path.chmod(0o600)
except OSError:
    pass
print(f"rendered {cfg_path}", flush=True)

# The OpenAI-compatible API server is resolved per PROFILE, from that
# profile's own .env - not from container-wide environment variables and not
# from config.yaml. Without this the gateway starts happily, logs nothing
# about a platform, and port 8642 is never bound.
#
# Values come from the container environment (API_SERVER_KEY is injected from
# a Secret); we write them into the default profile's .env so the gateway
# actually resolves them. The file is rewritten on every start so the Secret
# stays the single source of truth.
profile_env = home / "profiles" / "default" / ".env"
profile_env.parent.mkdir(parents=True, exist_ok=True)

api_key = os.environ.get("API_SERVER_KEY", "")
if not api_key:
    sys.exit("API_SERVER_KEY is empty - the gateway would start unauthenticated")
if len(api_key) < 8:
    sys.exit("API_SERVER_KEY must be at least 8 characters")

managed = {
    "API_SERVER_ENABLED": os.environ.get("API_SERVER_ENABLED", "true"),
    "API_SERVER_HOST": os.environ.get("API_SERVER_HOST", "0.0.0.0"),
    "API_SERVER_PORT": os.environ.get("API_SERVER_PORT", "8642"),
    "API_SERVER_KEY": api_key,
}

# Preserve any unrelated keys a user set in the profile .env.
preserved = []
if profile_env.exists():
    for line in profile_env.read_text().splitlines():
        name = line.split("=", 1)[0].strip()
        if line.strip() and not line.lstrip().startswith("#") and name not in managed:
            preserved.append(line)

lines = ["# Managed by hermes-search-stack; API_SERVER_* are overwritten on boot."]
lines += [f"{k}={v}" for k, v in managed.items()]
lines += preserved
body = "\n".join(lines) + "\n"

# The PVC survives restarts, so an .env may already exist from an earlier run
# (possibly written by a different uid before fsGroup was in play). Rewriting
# in place keeps the existing inode and avoids EPERM on a file we do not own;
# chmod is best-effort for the same reason.
try:
    with open(profile_env, "w", encoding="utf-8") as fh:
        fh.write(body)
except OSError as exc:
    sys.exit(f"cannot write {profile_env}: {exc}")
try:
    profile_env.chmod(0o600)
except OSError:
    pass
print(f"wrote {profile_env} (API_SERVER_* for the default profile)", flush=True)

# Sanity: fail loudly if search routing did not survive the merge.
if merged.get("web", {}).get("search_backend") != "searxng":
    sys.exit("web.search_backend is not 'searxng' after merge - refusing to start")
