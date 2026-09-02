"""Synchronize credentials into Hermes' owner-only default profile .env."""

from __future__ import annotations

import stat
from collections.abc import Mapping
from pathlib import Path


class ProfileEnvError(RuntimeError):
    """The profile credential file could not be made safe and current."""


def _credential(environ: Mapping[str, str], name: str, *, min_length: int = 1) -> str:
    value = environ.get(name, "")
    if not value:
        raise ProfileEnvError(f"{name} is empty - refusing to start unauthenticated")
    if len(value) < min_length:
        raise ProfileEnvError(f"{name} must be at least {min_length} characters")
    if "\n" in value or "\r" in value:
        raise ProfileEnvError(f"{name} contains a newline - refusing an unsafe .env value")
    return value


def _assignment_name(line: str) -> str:
    candidate = line.lstrip()
    fields = candidate.split(maxsplit=1)
    if len(fields) == 2 and fields[0] == "export":
        candidate = fields[1]
    return candidate.split("=", 1)[0].strip()


def write_default_profile_env(home: Path, environ: Mapping[str, str]) -> Path:
    """Write managed credentials, preserving unrelated profile variables."""
    api_key = _credential(environ, "API_SERVER_KEY", min_length=8)
    model_api_key = _credential(environ, "HERMES_GATEWAY_API_KEY")
    managed = {
        "API_SERVER_ENABLED": environ.get("API_SERVER_ENABLED", "true"),
        "API_SERVER_HOST": environ.get("API_SERVER_HOST", "0.0.0.0"),
        "API_SERVER_PORT": environ.get("API_SERVER_PORT", "8642"),
        "API_SERVER_KEY": api_key,
        "HERMES_GATEWAY_API_KEY": model_api_key,
    }
    for name, value in managed.items():
        if "\n" in value or "\r" in value:
            raise ProfileEnvError(f"{name} contains a newline - refusing an unsafe .env value")

    profile_env = home / "profiles" / "default" / ".env"
    profile_env.parent.mkdir(parents=True, exist_ok=True)

    preserved: list[str] = []
    if profile_env.exists():
        for line in profile_env.read_text().splitlines():
            name = _assignment_name(line)
            if line.strip() and not line.lstrip().startswith("#") and name not in managed:
                preserved.append(line)

    lines = [
        "# Managed by hermes-search-stack; API server and model credentials are overwritten on boot."
    ]
    lines.extend(f"{key}={value}" for key, value in managed.items())
    lines.extend(preserved)
    body = "\n".join(lines) + "\n"

    # Harden the inode before writing any credential bytes. An old PVC may
    # contain a file created by another uid; in that case fail closed instead of
    # writing a model key into a group/world-readable file.
    try:
        profile_env.touch(mode=0o600, exist_ok=True)
        profile_env.chmod(0o600)
        if stat.S_IMODE(profile_env.stat().st_mode) != 0o600:
            raise OSError("mode did not converge to 0600")
        with profile_env.open("w", encoding="utf-8") as handle:
            handle.write(body)
    except OSError as exc:
        raise ProfileEnvError(f"cannot securely write {profile_env}: {exc}") from exc

    return profile_env
