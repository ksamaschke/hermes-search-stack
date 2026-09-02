#!/usr/bin/env python3
"""Merge stack-owned config keys into $HERMES_HOME/config.yaml.

Runs as an init container on every pod start. Idempotent by design:
- Keys this stack owns are enforced (search backend, model routing).
- Any other key the user set via the WebUI / CLI is preserved.
- The model key is stored only in the owner-only default-profile .env; the named
  provider config contains its env-var name. The API-server key is synchronized
  to both owner-only files because the platform enrolment contract requires it.
"""
import os
import pathlib
import sys

from profile_env import ProfileEnvError, write_default_profile_env

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

# Guard against duplicate top-level keys in the partial. PyYAML silently keeps
# the LAST occurrence, so a second `security:` block further down the file
# quietly discarded allow_lazy_installs and the gateway kept pip-installing at
# boot. Fail loudly instead of shipping a config that lost half its keys.
_seen, _dupes = set(), set()
for _line in partial_raw.splitlines():
    if _line[:1].isalpha() and _line.rstrip().endswith(":"):
        _key = _line.split(":", 1)[0].strip()
        if _key in _seen:
            _dupes.add(_key)
        _seen.add(_key)
if _dupes:
    sys.exit(f"duplicate top-level key(s) in config.yaml.partial: {sorted(_dupes)}")

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

# Retire model-level custom-provider fields after switching to this stack's
# named provider. Deep merge preserves absent PVC keys, so removing these from
# the partial alone would leave stale endpoint/auth routing active indefinitely.
desired_providers = desired.get("providers")
desired_model = desired.get("model")
uses_named_provider = (
    isinstance(desired_providers, dict)
    and "hermes-search-stack" in desired_providers
) or (
    isinstance(desired_model, dict)
    and desired_model.get("provider") == "hermes-search-stack"
)
merged_model = merged.get("model")
if uses_named_provider and isinstance(merged_model, dict):
    for legacy_key in ("base_url", "key_env"):
        if legacy_key in merged_model:
            merged_model.pop(legacy_key)
            print(f"dropped legacy model.{legacy_key}", flush=True)

# --- Managed paths: the ConfigMap wins, always -------------------------------
#
# config.yaml lives on the PVC and is deep-merged, which means pre-existing
# state SHADOWS anything this stack introduces. That bit us twice:
#
#   * a legacy `gateway.api_server` block survived upgrades and shadowed the
#     supported `gateway.platforms.api_server` shape, so the platform never
#     enrolled and 8642 stayed closed;
#   * `security:` already existed on the PVC (allow_private_urls), so a newly
#     added `security.allow_lazy_installs: false` was silently dropped and the
#     gateway kept pip-installing at boot.
#
# Both were the same bug: deep_merge cannot introduce a key into a block the
# PVC already owns. So rather than special-casing each incident, enforce the
# leaf paths this stack owns. Everything else the user set is still preserved.
MANAGED_PATHS = [
    ("web", "search_backend"),
    ("web", "extract_backend"),
    ("security", "allow_lazy_installs"),
    ("gateway", "platforms", "api_server"),
]

def get_path(node, path):
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None, False
        node = node[key]
    return node, True

def set_path(node, path, value):
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value

for path in MANAGED_PATHS:
    want, present = get_path(desired, path)
    if not present:
        continue
    have, _ = get_path(merged, path)
    if have != want:
        set_path(merged, path, want)
        print(f"enforced managed path {'.'.join(path)}", flush=True)

# Retire the legacy API-server location once the supported one is in place.
gw = merged.get("gateway")
if isinstance(gw, dict) and "api_server" in gw:
    platforms_block = gw.get("platforms")
    if isinstance(platforms_block, dict) and "api_server" in platforms_block:
        gw.pop("api_server", None)
        print("dropped legacy gateway.api_server block", flush=True)

cfg_path.write_text(yaml.safe_dump(merged, sort_keys=False))
# config.yaml now carries the API server key (the gateway's platform checker
# reads it from platforms.api_server.extra.key), so keep it owner-only.
try:
    cfg_path.chmod(0o600)
except OSError:
    pass
print(f"rendered {cfg_path}", flush=True)

# The OpenAI-compatible API server and model provider credentials are resolved
# per PROFILE, from that profile's own .env - not from container-wide
# environment variables and not from config.yaml. Without this, the gateway
# can bind successfully while model calls use "no-key-required" and get 401.
#
# Values come from the init-container environment (both credentials are
# injected from Secrets); rewrite them on every start so the Kubernetes Secret
# stays the single source of truth.
try:
    profile_env = write_default_profile_env(home, os.environ)
except ProfileEnvError as exc:
    sys.exit(str(exc))
print(
    f"wrote {profile_env} (API server and model credentials for the default profile)",
    flush=True,
)

# Sanity: fail loudly if search routing did not survive the merge.
if merged.get("web", {}).get("search_backend") != "searxng":
    sys.exit("web.search_backend is not 'searxng' after merge - refusing to start")
