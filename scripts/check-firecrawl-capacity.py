#!/usr/bin/env python3
"""Assert that the rendered Firecrawl API stays within a small-cluster budget."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def deployment_document(manifest: str, name: str) -> str:
    for document in re.split(r"^---\s*$", manifest, flags=re.MULTILINE):
        if not re.search(r"^kind:\s*Deployment\s*$", document, re.MULTILINE):
            continue
        if re.search(rf"^  name:\s*{re.escape(name)}\s*$", document, re.MULTILINE):
            return document
    raise ValueError(f"Deployment/{name} not found in rendered manifest")


def container_block(deployment: str, name: str) -> str:
    blocks: list[list[str]] = []
    current: list[str] | None = None
    container_indent: int | None = None
    item_indent: int | None = None

    for line in deployment.splitlines():
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if container_indent is None:
            if stripped == "containers:":
                container_indent = indent
            continue

        if item_indent is None:
            if stripped and indent <= container_indent and not line.lstrip().startswith("- "):
                break
            if line.lstrip().startswith("- ") and indent >= container_indent:
                item_indent = indent
                current = [line]
            continue

        if stripped and indent < item_indent:
            break
        if stripped and indent == item_indent:
            if line.lstrip().startswith("- "):
                if current:
                    blocks.append(current)
                current = [line]
                continue
            break
        if current is not None:
            current.append(line)

    if current:
        blocks.append(current)

    assert item_indent is not None
    direct_name = re.compile(
        rf"^(?:[ ]{{{item_indent}}}-[ ]+name|[ ]{{{item_indent + 2}}}name):[ ]*"
        rf"{re.escape(name)}[ ]*$"
    )
    for block_lines in blocks:
        if any(direct_name.match(line) for line in block_lines):
            return "\n".join(block_lines)
    raise ValueError(f"container {name!r} not found in Deployment")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--compose-manifest", type=Path)
    args = parser.parse_args()

    firecrawl = deployment_document(args.manifest.read_text(), "firecrawl-api")
    api_container = container_block(firecrawl, "api")
    failures: list[str] = []

    if not re.search(
        r"^\s*- name:\s*NUM_WORKERS_PER_QUEUE\s*$\n^\s+value:\s*[\"']?1[\"']?\s*$",
        api_container,
        re.MULTILINE,
    ):
        failures.append("NUM_WORKERS_PER_QUEUE must render as 1")

    if not re.search(
        r"^\s+requests:\s*$\n(?:^\s+#.*$\n)*^\s+cpu:\s*500m\s*$",
        api_container,
        re.MULTILINE,
    ):
        failures.append("Firecrawl API CPU request must render as 500m")

    if args.compose_manifest:
        compose = args.compose_manifest.read_text()
        service = re.search(
            r"^  firecrawl-api:\s*$\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\s*$|\Z)",
            compose,
            re.MULTILINE | re.DOTALL,
        )
        if not service:
            failures.append("Compose firecrawl-api service is missing")
        elif not re.search(
            r"^\s+NUM_WORKERS_PER_QUEUE:\s*[\"']?1[\"']?\s*$",
            service.group("body"),
            re.MULTILINE,
        ):
            failures.append("Compose NUM_WORKERS_PER_QUEUE must be 1")

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1

    print("Firecrawl worker fan-out and CPU request are bounded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
