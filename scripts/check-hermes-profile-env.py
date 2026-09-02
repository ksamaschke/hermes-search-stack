#!/usr/bin/env python3
"""Verify that the model credential reaches Hermes' profile secret scope."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

MODEL_KEY_ENV = "HERMES_GATEWAY_API_KEY"
MODEL_SECRET_KEY = "model-api-key"


def resource_document(manifest: str, kind: str, name: str) -> str:
    for document in re.split(r"^---\s*$", manifest, flags=re.MULTILINE):
        if not re.search(rf"^kind:\s*{re.escape(kind)}\s*$", document, re.MULTILINE):
            continue
        if re.search(rf"^  name:\s*{re.escape(name)}\s*$", document, re.MULTILINE):
            return document
    raise ValueError(f"{kind}/{name} not found in rendered manifest")


def sequence_section(document: str, name: str) -> str:
    lines = document.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != f"{name}:":
            continue
        base_indent = len(line) - len(line.lstrip())
        section: list[str] = []
        for candidate in lines[index + 1 :]:
            indent = len(candidate) - len(candidate.lstrip())
            if candidate.strip() and indent <= base_indent and not candidate.lstrip().startswith("- "):
                break
            section.append(candidate)
        return "\n".join(section)
    return ""


def model_secret_binding_count(section: str) -> int:
    lines = section.splitlines()
    count = 0
    for index, line in enumerate(lines):
        if not re.match(rf"^\s*- name:\s*{MODEL_KEY_ENV}\s*$", line):
            continue
        binding = "\n".join(lines[index : index + 8])
        if (
            re.search(r"^\s+secretKeyRef:\s*$", binding, re.MULTILINE)
            and re.search(rf"^\s+key:\s*{MODEL_SECRET_KEY}\s*$", binding, re.MULTILINE)
            and re.search(r"^\s+name:\s*hermes-agent-secrets\s*$", binding, re.MULTILINE)
        ):
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("config_partial", type=Path)
    args = parser.parse_args()

    failures: list[str] = []
    manifest = args.manifest.read_text()
    deployment = resource_document(manifest, "Deployment", "hermes-agent")

    init_section = sequence_section(deployment, "initContainers")
    main_section = sequence_section(deployment, "containers")
    init_bindings = model_secret_binding_count(init_section)
    main_bindings = model_secret_binding_count(main_section)
    if init_bindings != 1:
        failures.append(
            f"init container must bind {MODEL_KEY_ENV} from model-api-key exactly once "
            f"(found {init_bindings})"
        )
    if main_bindings != 1:
        failures.append(
            f"main container must bind {MODEL_KEY_ENV} from model-api-key exactly once "
            f"(found {main_bindings})"
        )

    if not (
        re.search(r"^\s*- mountPath:\s*/bootstrap\s*$", init_section, re.MULTILINE)
        and re.search(r"^\s+name:\s*bootstrap\s*$", init_section, re.MULTILINE)
    ):
        failures.append("init container must mount the bootstrap ConfigMap at /bootstrap")

    volume_section = sequence_section(deployment, "volumes")
    if not (
        re.search(r"^\s*- name:\s*bootstrap\s*$", volume_section, re.MULTILINE)
        and re.search(
            r"^\s+name:\s*hermes-agent-bootstrap\s*$", volume_section, re.MULTILINE
        )
    ):
        failures.append("bootstrap volume must project hermes-agent-bootstrap")

    bootstrap_config = resource_document(manifest, "ConfigMap", "hermes-agent-bootstrap")
    if not re.search(r"^  profile_env\.py:\s*\|", bootstrap_config, re.MULTILINE):
        failures.append("hermes-agent-bootstrap ConfigMap must package profile_env.py")
    if not re.search(
        r"from profile_env import [^\n]*write_default_profile_env", bootstrap_config
    ):
        failures.append("bootstrap.py must import write_default_profile_env")
    if "write_default_profile_env(home, os.environ)" not in bootstrap_config:
        failures.append("bootstrap.py must invoke write_default_profile_env")

    partial = args.config_partial.read_text()
    if not re.search(rf"^\s*key_env:\s*{MODEL_KEY_ENV}\s*$", partial, re.MULTILINE):
        failures.append(f"model.key_env must reference {MODEL_KEY_ENV}")

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1

    print("Hermes model credential reaches the default profile secret scope")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
