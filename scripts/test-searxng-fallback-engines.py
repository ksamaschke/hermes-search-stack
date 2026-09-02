#!/usr/bin/env python3
"""Behavior tests for the SearXNG fallback-engine contract."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

CHECKER = Path(__file__).with_name("check-searxng-fallback-engines.py")
SPEC = importlib.util.spec_from_file_location("searxng_fallback_check", CHECKER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

VALID = """\
use_default_settings: true
engines:
  - name: bing
    disabled: false
  - name: mwmbl
    disabled: false
  - name: wiby
    disabled: false
search:
  formats:
    - json
"""


class SearXNGFallbackEngineTests(unittest.TestCase):
    def test_accepts_required_enabled_fallbacks(self) -> None:
        self.assertEqual(MODULE.failures(VALID), [])

    def test_rejects_missing_fallback(self) -> None:
        failures = MODULE.failures(VALID.replace("  - name: wiby\n    disabled: false\n", ""))
        self.assertIn("fallback engine wiby", failures[0])

    def test_rejects_disabled_fallback(self) -> None:
        failures = MODULE.failures(
            VALID.replace("  - name: bing\n    disabled: false", "  - name: bing\n    disabled: true")
        )
        self.assertIn("fallback engine bing", failures[0])

    def test_comment_decoy_does_not_satisfy_contract(self) -> None:
        fixture = VALID.replace("  - name: mwmbl\n    disabled: false\n", "#  - name: mwmbl\n#    disabled: false\n")
        failures = MODULE.failures(fixture)
        self.assertIn("fallback engine mwmbl", failures[0])

    def test_rejects_duplicate_top_level_engines_mapping(self) -> None:
        failures = MODULE.failures(VALID + "\nengines:\n  - name: bing\n    disabled: false\n")
        self.assertIn("one top-level engines mapping", failures[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
