#!/usr/bin/env python3
"""Verify Firecrawl JSON extraction reuses the Hermes model gateway."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple


KUBERNETES_BINDINGS = {
    "OPENAI_API_KEY": ("secretKeyRef", "hermes-agent-secrets", "model-api-key"),
    "OPENAI_BASE_URL": (
        "configMapKeyRef",
        "hermes-agent-runtime",
        "model-base-url",
    ),
    "MODEL_NAME": ("configMapKeyRef", "hermes-agent-runtime", "firecrawl-model"),
}
COMPOSE_BINDINGS = {
    "OPENAI_API_KEY": "HERMES_GATEWAY_API_KEY",
    "OPENAI_BASE_URL": "HERMES_MODEL_BASE_URL",
    "MODEL_NAME": "FIRECRAWL_MODEL_DEFAULT",
}


class ManifestError(ValueError):
    """Raised when the expected direct YAML path cannot be found."""


def indentation(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def significant(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def mapping_entries(
    lines: Sequence[str],
    start: int,
    end: int,
    direct_indent: int,
    key: str,
) -> List[Tuple[int, str]]:
    pattern = re.compile(rf"^[ ]{{{direct_indent}}}{re.escape(key)}:\s*(.*?)\s*$")
    entries: List[Tuple[int, str]] = []
    for index in range(start, end):
        match = pattern.match(lines[index])
        if match:
            entries.append((index, match.group(1)))
    return entries


def only_mapping_entry(
    lines: Sequence[str],
    start: int,
    end: int,
    direct_indent: int,
    key: str,
    context: str,
) -> Tuple[int, str]:
    entries = mapping_entries(lines, start, end, direct_indent, key)
    if len(entries) != 1:
        raise ManifestError(
            f"{context} must contain {key} exactly once (found {len(entries)})"
        )
    return entries[0]


def block_end(lines: Sequence[str], key_index: int, enclosing_end: int) -> int:
    key_indent = indentation(lines[key_index])
    for index in range(key_index + 1, enclosing_end):
        if significant(lines[index]) and indentation(lines[index]) <= key_indent:
            return index
    return enclosing_end


def child_indent(
    lines: Sequence[str],
    key_index: int,
    enclosing_end: int,
    context: str,
) -> int:
    end = block_end(lines, key_index, enclosing_end)
    candidates = [
        indentation(lines[index])
        for index in range(key_index + 1, end)
        if significant(lines[index]) and indentation(lines[index]) > indentation(lines[key_index])
    ]
    if not candidates:
        raise ManifestError(f"{context} is empty")
    return min(candidates)


def direct_child(
    lines: Sequence[str],
    parent_index: int,
    enclosing_end: int,
    key: str,
    context: str,
) -> Tuple[int, str, int]:
    end = block_end(lines, parent_index, enclosing_end)
    direct_indent = child_indent(lines, parent_index, enclosing_end, context)
    index, value = only_mapping_entry(
        lines,
        parent_index + 1,
        end,
        direct_indent,
        key,
        context,
    )
    return index, value, end


def top_level_entry(lines: Sequence[str], key: str, context: str) -> Tuple[int, str]:
    return only_mapping_entry(lines, 0, len(lines), 0, key, context)


def resource_document(manifest: str, kind: str, name: str) -> List[str]:
    for document in re.split(r"^---\s*$", manifest, flags=re.MULTILINE):
        lines = document.splitlines()
        kind_entries = mapping_entries(lines, 0, len(lines), 0, "kind")
        if len(kind_entries) != 1 or unquote(kind_entries[0][1]) != kind:
            continue
        metadata_entries = mapping_entries(lines, 0, len(lines), 0, "metadata")
        if len(metadata_entries) != 1:
            continue
        metadata_index, metadata_value = metadata_entries[0]
        if metadata_value:
            continue
        try:
            metadata_end = block_end(lines, metadata_index, len(lines))
            metadata_indent = child_indent(
                lines,
                metadata_index,
                len(lines),
                f"{kind} metadata",
            )
            names = mapping_entries(
                lines,
                metadata_index + 1,
                metadata_end,
                metadata_indent,
                "name",
            )
        except ManifestError:
            continue
        if len(names) == 1 and unquote(names[0][1]) == name:
            return lines
    raise ManifestError(f"{kind}/{name} not found in rendered manifest")


def sequence_items(
    lines: Sequence[str],
    key_index: int,
    enclosing_end: int,
    context: str,
) -> List[Tuple[List[str], int]]:
    key_indent = indentation(lines[key_index])
    first_index = None
    for index in range(key_index + 1, enclosing_end):
        if not significant(lines[index]):
            continue
        candidate_indent = indentation(lines[index])
        if candidate_indent < key_indent:
            break
        if lines[index].lstrip().startswith("- "):
            first_index = index
            break
        raise ManifestError(f"{context} must be a YAML sequence")
    if first_index is None:
        raise ManifestError(f"{context} must be a non-empty YAML sequence")

    item_indent = indentation(lines[first_index])
    items: List[List[str]] = []
    current: List[str] = []
    for index in range(first_index, enclosing_end):
        line = lines[index]
        if significant(line):
            line_indent = indentation(line)
            if line_indent < item_indent:
                break
            if line_indent == item_indent:
                if line.lstrip().startswith("- "):
                    if current:
                        items.append(current)
                    current = [line]
                    continue
                break
        if current:
            current.append(line)
    if current:
        items.append(current)

    normalized: List[Tuple[List[str], int]] = []
    direct_indent = item_indent + 2
    for item in items:
        payload = item[0].lstrip()[2:]
        if not payload:
            raise ManifestError(f"{context} contains an empty sequence item")
        normalized.append(([" " * direct_indent + payload] + item[1:], direct_indent))
    return normalized


def mapping_scalar_path(
    lines: Sequence[str],
    direct_indent: int,
    path: Iterable[str],
    context: str,
) -> str:
    start = 0
    end = len(lines)
    current_indent = direct_indent
    parts = list(path)
    for position, key in enumerate(parts):
        index, value = only_mapping_entry(
            lines,
            start,
            end,
            current_indent,
            key,
            context,
        )
        if position == len(parts) - 1:
            if not value:
                raise ManifestError(f"{context} {'.'.join(parts)} must be a scalar")
            return unquote(value)
        if value:
            raise ManifestError(f"{context} {key} must be a mapping")
        nested_end = block_end(lines, index, end)
        current_indent = child_indent(lines, index, end, f"{context} {key}")
        start = index + 1
        end = nested_end
    raise AssertionError("path must not be empty")


def api_container(lines: Sequence[str]) -> Tuple[List[str], int]:
    spec_index, spec_value = top_level_entry(lines, "spec", "Deployment/firecrawl-api")
    if spec_value:
        raise ManifestError("Deployment/firecrawl-api spec must be a mapping")
    template_index, template_value, spec_end = direct_child(
        lines,
        spec_index,
        len(lines),
        "template",
        "Deployment/firecrawl-api spec",
    )
    if template_value:
        raise ManifestError("Deployment/firecrawl-api spec.template must be a mapping")
    pod_spec_index, pod_spec_value, template_end = direct_child(
        lines,
        template_index,
        spec_end,
        "spec",
        "Deployment/firecrawl-api template",
    )
    if pod_spec_value:
        raise ManifestError("Deployment/firecrawl-api spec.template.spec must be a mapping")
    containers_index, containers_value, pod_spec_end = direct_child(
        lines,
        pod_spec_index,
        template_end,
        "containers",
        "Deployment/firecrawl-api pod spec",
    )
    if containers_value:
        raise ManifestError("Deployment/firecrawl-api containers must be a sequence")

    matches: List[Tuple[List[str], int]] = []
    for item_lines, direct_indent in sequence_items(
        lines,
        containers_index,
        pod_spec_end,
        "Deployment/firecrawl-api containers",
    ):
        names = mapping_entries(
            item_lines,
            0,
            len(item_lines),
            direct_indent,
            "name",
        )
        if len(names) == 1 and unquote(names[0][1]) == "api":
            matches.append((item_lines, direct_indent))
    if len(matches) != 1:
        raise ManifestError(
            "Deployment/firecrawl-api must contain direct container api exactly once "
            f"(found {len(matches)})"
        )
    return matches[0]


def kubernetes_failures(manifest: str) -> List[str]:
    try:
        deployment = resource_document(manifest, "Deployment", "firecrawl-api")
        container_lines, container_indent = api_container(deployment)
        env_entries = mapping_entries(
            container_lines,
            0,
            len(container_lines),
            container_indent,
            "env",
        )
        if len(env_entries) != 1 or env_entries[0][1]:
            raise ManifestError(
                "Deployment/firecrawl-api container api must contain direct env sequence exactly once"
            )
        env_index = env_entries[0][0]
        env_items = sequence_items(
            container_lines,
            env_index,
            len(container_lines),
            "Deployment/firecrawl-api container api env",
        )
    except ManifestError as error:
        return [str(error)]

    failures: List[str] = []
    for env_name, (ref_kind, ref_name, ref_key) in KUBERNETES_BINDINGS.items():
        matches: List[Tuple[List[str], int]] = []
        for item_lines, direct_indent in env_items:
            names = mapping_entries(
                item_lines,
                0,
                len(item_lines),
                direct_indent,
                "name",
            )
            if len(names) == 1 and unquote(names[0][1]) == env_name:
                matches.append((item_lines, direct_indent))

        reference_label = "Secret" if ref_kind == "secretKeyRef" else "ConfigMap"
        expected = (
            f"Deployment/firecrawl-api container api {env_name} must use "
            f"{reference_label} {ref_name} key {ref_key}"
        )
        if len(matches) != 1:
            failures.append(f"{expected} exactly once (found {len(matches)})")
            continue
        item_lines, direct_indent = matches[0]
        context = f"Deployment/firecrawl-api container api {env_name}"
        try:
            actual_name = mapping_scalar_path(
                item_lines,
                direct_indent,
                ("valueFrom", ref_kind, "name"),
                context,
            )
            actual_key = mapping_scalar_path(
                item_lines,
                direct_indent,
                ("valueFrom", ref_kind, "key"),
                context,
            )
        except ManifestError:
            failures.append(expected)
            continue
        if (actual_name, actual_key) != (ref_name, ref_key):
            failures.append(expected)
    return failures


def compose_variable_reference(value: str, variable: str) -> bool:
    value = unquote(value)
    return bool(
        re.fullmatch(
            rf"\$\{{{re.escape(variable)}:\?[^}}]*\}}",
            value,
        )
    )


def compose_failures(compose: str) -> List[str]:
    lines = compose.splitlines()
    try:
        services_index, services_value = top_level_entry(lines, "services", "Compose")
        if services_value:
            raise ManifestError("Compose services must be a mapping")
        services_end = block_end(lines, services_index, len(lines))
        services_indent = child_indent(lines, services_index, len(lines), "Compose services")
        service_index, service_value = only_mapping_entry(
            lines,
            services_index + 1,
            services_end,
            services_indent,
            "firecrawl-api",
            "Compose services",
        )
        if service_value:
            raise ManifestError("Compose service firecrawl-api must be a mapping")
        service_end = block_end(lines, service_index, services_end)
        environment_index, environment_value, _ = direct_child(
            lines,
            service_index,
            services_end,
            "environment",
            "Compose service firecrawl-api",
        )
        if environment_value:
            raise ManifestError(
                "Compose service firecrawl-api environment must be a mapping"
            )
        environment_end = block_end(lines, environment_index, service_end)
        environment_indent = child_indent(
            lines,
            environment_index,
            service_end,
            "Compose service firecrawl-api environment",
        )
    except ManifestError as error:
        return [str(error)]

    failures: List[str] = []
    for env_name, variable in COMPOSE_BINDINGS.items():
        expected = (
            f"Compose service firecrawl-api {env_name} must reference {variable}"
        )
        entries = mapping_entries(
            lines,
            environment_index + 1,
            environment_end,
            environment_indent,
            env_name,
        )
        if len(entries) != 1 or not compose_variable_reference(entries[0][1], variable):
            failures.append(expected)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--compose-manifest", required=True, type=Path)
    args = parser.parse_args()

    failures = kubernetes_failures(args.manifest.read_text())
    failures.extend(compose_failures(args.compose_manifest.read_text()))
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1

    print("Firecrawl extraction model gateway wiring is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
