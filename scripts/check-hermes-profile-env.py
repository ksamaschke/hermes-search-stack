#!/usr/bin/env python3
"""Verify Hermes' named model provider and profile credential wiring."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

MODEL_KEY_ENV = "HERMES_GATEWAY_API_KEY"
MODEL_SECRET_KEY = "model-api-key"
MODEL_PROVIDER = "hermes-search-stack"
MODEL_PROVIDER_ENV = "${HERMES_MODEL_PROVIDER}"
MODEL_BASE_URL_ENV = "${HERMES_MODEL_BASE_URL}"
MODEL_DEFAULT_ENV = "${HERMES_MODEL_DEFAULT}"


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


def mapping_section(document: str, indent: int, name: str) -> str:
    """Return one exact block-mapping section, or empty when ambiguous."""
    lines = document.splitlines()
    padding = " " * indent
    escaped_name = re.escape(name)
    key = rf'(?:{escaped_name}|\'{escaped_name}\'|"{escaped_name}")'
    heading = re.compile(rf"^{padding}{key}[ \t]*:[ \t]*(?P<tail>.*)$")
    occurrences: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = heading.fullmatch(line)
        if match:
            occurrences.append((index, match.group("tail")))
    if len(occurrences) != 1:
        return ""

    index, tail = occurrences[0]
    if tail.strip() and not tail.lstrip().startswith("#"):
        return ""

    section: list[str] = []
    for candidate in lines[index + 1 :]:
        candidate_indent = len(candidate) - len(candidate.lstrip())
        if candidate.strip() and candidate_indent <= indent:
            break
        section.append(candidate)
    return "\n".join(section)


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


def has_unique_exact_scalar(section: str, indent: int, key: str, value: str) -> bool:
    """Require one scalar occurrence with an exact, consistently quoted value."""
    padding = " " * indent
    scalar_line = re.compile(
        rf"^{padding}(?P<key>[^:#][^:]*?)[ \t]*:[ \t]*(?P<scalar>.*)$"
    )
    occurrences: list[tuple[str, str]] = []
    for line in section.splitlines():
        match = scalar_line.fullmatch(line)
        if not match:
            continue
        raw_key = match.group("key").strip()
        # Count malformed quote variants as occurrences too. Otherwise a valid
        # key followed by, for example, `base_url\": ...` could evade the
        # uniqueness check even though the YAML itself is ambiguous/invalid.
        if raw_key.strip("'\"") == key:
            occurrences.append((raw_key, match.group("scalar")))
    if len(occurrences) != 1:
        return False

    raw_key, scalar = occurrences[0]
    if raw_key not in (key, f"'{key}'", f'"{key}"'):
        return False

    forms = (value, f"'{value}'", f'"{value}"')
    for form in forms:
        if scalar.rstrip(" \t") == form:
            return True
        if re.fullmatch(rf"{re.escape(form)}[ \t]+#[^\r\n]*", scalar):
            return True
    return False


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

    runtime_config = resource_document(manifest, "ConfigMap", "hermes-agent-runtime")
    runtime_data = mapping_section(runtime_config, 0, "data")
    if not has_unique_exact_scalar(
        runtime_data, 2, "model-provider", MODEL_PROVIDER
    ):
        failures.append(
            f"hermes-agent-runtime model-provider must occur exactly once with value {MODEL_PROVIDER}"
        )

    partial = args.config_partial.read_text()
    providers = mapping_section(partial, 0, "providers")
    named_provider = mapping_section(providers, 2, MODEL_PROVIDER)
    if not named_provider:
        failures.append(f"config partial must declare providers.{MODEL_PROVIDER}")
    else:
        provider_contract = (
            ("base_url", MODEL_BASE_URL_ENV),
            ("key_env", MODEL_KEY_ENV),
            ("default_model", MODEL_DEFAULT_ENV),
        )
        for key, value in provider_contract:
            if not has_unique_exact_scalar(named_provider, 4, key, value):
                failures.append(
                    f"providers.{MODEL_PROVIDER}.{key} must occur exactly once with value {value}"
                )

    model = mapping_section(partial, 0, "model")
    if not has_unique_exact_scalar(model, 2, "provider", MODEL_PROVIDER_ENV):
        failures.append(
            "model.provider must occur exactly once and stay environment-driven "
            f"via {MODEL_PROVIDER_ENV}"
        )

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1

    print("Hermes named model provider and profile credential wiring are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
