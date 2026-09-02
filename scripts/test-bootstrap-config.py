#!/usr/bin/env python3
"""Behavior tests for the Hermes persistent-config bootstrap."""

from __future__ import annotations

import io
import json
import os
import runpy
import sys
import tempfile
import textwrap
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

BOOTSTRAP = (
    Path(__file__).resolve().parents[1]
    / "deploy/kubernetes/base/hermes-agent/files/bootstrap.py"
)
PARTIAL_PATH = Path("/bootstrap/config.yaml.partial")

DESIRED_CONFIG = textwrap.dedent(
    """\
    {
      "providers": {
        "hermes-search-stack": {
          "base_url": "https://gateway.invalid/v1",
          "key_env": "HERMES_GATEWAY_API_KEY",
          "default_model": "test/model"
        }
      },
      "model": {
        "provider": "hermes-search-stack",
        "default": "test/model"
      },
      "web": {
        "search_backend": "searxng"
      }
    }
    """
)

CURRENT_CONFIG = textwrap.dedent(
    """\
    {
      "model": {
        "provider": "custom",
        "default": "legacy/model",
        "base_url": "https://legacy.invalid/v1",
        "key_env": "LEGACY_MODEL_API_KEY",
        "temperature": 0.25,
        "user_option": "preserved"
      },
      "providers": {
        "user-provider": {
          "base_url": "https://user-provider.invalid/v1"
        }
      },
      "web": {
        "search_backend": "legacy-search"
      }
    }
    """
)

TEST_API_KEY = "aaaaaaaa"
TEST_MODEL_KEY = "bbbbbbbb"


class FakeYamlError(Exception):
    """Test replacement for yaml.YAMLError."""


def fake_yaml_module() -> types.ModuleType:
    module = types.ModuleType("yaml")

    def safe_load(raw: str) -> Any:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise FakeYamlError(str(exc)) from exc

    def safe_dump(value: Any, *, sort_keys: bool = True) -> str:
        return json.dumps(value, indent=2, sort_keys=sort_keys) + "\n"

    setattr(module, "YAMLError", FakeYamlError)
    setattr(module, "safe_load", safe_load)
    setattr(module, "safe_dump", safe_dump)
    return module


class BootstrapConfigTests(unittest.TestCase):
    def run_bootstrap(self, home: Path) -> tuple[dict[str, Any], str]:
        original_read_text = Path.read_text
        yaml_before = sys.modules.get("yaml")
        yaml_was_loaded = "yaml" in sys.modules
        fake_yaml = fake_yaml_module()

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            if path == PARTIAL_PATH:
                return DESIRED_CONFIG
            return original_read_text(path, *args, **kwargs)

        environ = {
            "HERMES_HOME": str(home),
            "API_SERVER_KEY": TEST_API_KEY,
            "HERMES_GATEWAY_API_KEY": TEST_MODEL_KEY,
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, environ, clear=True),
            mock.patch.object(Path, "read_text", new=read_text),
            mock.patch.object(sys, "path", [str(BOOTSTRAP.parent), *sys.path]),
            mock.patch.dict(sys.modules, {"yaml": fake_yaml}),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            namespace = runpy.run_path(str(BOOTSTRAP), run_name="bootstrap_under_test")
        if yaml_was_loaded:
            self.assertIs(sys.modules.get("yaml"), yaml_before)
        else:
            self.assertNotIn("yaml", sys.modules)
        self.assertIs(namespace["yaml"], fake_yaml)
        return namespace, stdout.getvalue() + stderr.getvalue()

    def assert_output_has_no_values(self, output: str) -> None:
        for value in (
            "https://legacy.invalid/v1",
            "LEGACY_MODEL_API_KEY",
            TEST_API_KEY,
            TEST_MODEL_KEY,
        ):
            with self.subTest(value=value):
                self.assertNotIn(value, output)

    def test_named_provider_migration_retires_legacy_model_auth_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.yaml").write_text(CURRENT_CONFIG)

            first, first_output = self.run_bootstrap(home)
            merged = first["merged"]
            self.assertIsInstance(merged, dict)
            persisted = json.loads((home / "config.yaml").read_text())
            self.assertEqual(persisted, merged)
            model = merged["model"]
            self.assertNotIn("base_url", model)
            self.assertNotIn("key_env", model)
            self.assertEqual(model["temperature"], 0.25)
            self.assertEqual(model["user_option"], "preserved")
            self.assertIn("hermes-search-stack", merged["providers"])
            self.assertIn("user-provider", merged["providers"])
            migration_lines = [
                line
                for line in first_output.splitlines()
                if line.startswith("dropped legacy model.")
            ]
            self.assertCountEqual(
                migration_lines,
                [
                    "dropped legacy model.base_url",
                    "dropped legacy model.key_env",
                ],
            )
            self.assert_output_has_no_values(first_output)
            first_persisted = (home / "config.yaml").read_text()

            second, second_output = self.run_bootstrap(home)
            self.assertEqual(second["merged"], merged)
            self.assertEqual((home / "config.yaml").read_text(), first_persisted)
            self.assertNotIn("dropped legacy model.", second_output)
            self.assert_output_has_no_values(second_output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
