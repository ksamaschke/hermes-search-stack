#!/usr/bin/env python3
"""Require usable general-search fallbacks in a SearXNG settings file."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Dict

REQUIRED_FALLBACKS = ("bing", "mwmbl", "wiby")


class SettingsError(ValueError):
    """Raised when the fallback-engine contract is missing or ambiguous."""


def fallback_states(settings: str) -> Dict[str, bool]:
    lines = settings.splitlines()
    engine_headers = [
        index for index, line in enumerate(lines) if re.fullmatch(r"engines:\s*", line)
    ]
    if len(engine_headers) != 1:
        raise SettingsError(
            f"settings must contain one top-level engines mapping (found {len(engine_headers)})"
        )

    start = engine_headers[0] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        line = lines[index]
        if line and not line.startswith((" ", "#")):
            end = index
            break

    states: Dict[str, bool] = {}
    index = start
    while index < end:
        match = re.fullmatch(r"  - name:\s*([^#\s]+)\s*", lines[index])
        if not match:
            index += 1
            continue
        name = match.group(1)
        item_end = index + 1
        while item_end < end and not re.fullmatch(
            r"  - name:\s*([^#\s]+)\s*", lines[item_end]
        ):
            item_end += 1
        disabled = [
            value == "true"
            for line in lines[index + 1 : item_end]
            if (value_match := re.fullmatch(r"    disabled:\s*(true|false)\s*", line))
            for value in [value_match.group(1)]
        ]
        if len(disabled) != 1:
            raise SettingsError(f"engine {name} must set disabled exactly once")
        if name in states:
            raise SettingsError(f"engine {name} is configured more than once")
        states[name] = disabled[0]
        index = item_end
    return states


def failures(settings: str) -> list[str]:
    try:
        states = fallback_states(settings)
    except SettingsError as error:
        return [str(error)]
    return [
        f"fallback engine {name} must be configured with disabled: false"
        for name in REQUIRED_FALLBACKS
        if states.get(name) is not False
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("settings", nargs="+", type=Path)
    args = parser.parse_args()
    found: list[str] = []
    for path in args.settings:
        for failure in failures(path.read_text()):
            found.append(f"{path}: {failure}")
    if found:
        for failure in found:
            print(f"ERROR: {failure}")
        return 1
    print("SearXNG fallback engines are enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
