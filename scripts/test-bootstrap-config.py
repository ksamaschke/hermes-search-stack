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

import yaml

BOOTSTRAP = (
    Path(__file__).resolve().parents[1]
    / "deploy/kubernetes/base/hermes-agent/files/bootstrap.py"
)
PARTIAL_PATHS = {
    Path("/bootstrap/config.yaml.partial"),
    Path("/bootstrap/hermes-config.partial.yaml"),
}

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
      },
      "gateway": {
        "platforms": {
          "api_server": {
            "extra": {
              "model_routes": ${HERMES_MODEL_ROUTES_JSON}
            }
          }
        }
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
    """Provide a probeable YAML facade while delegating parsing to PyYAML."""
    real_yaml = yaml
    module = types.ModuleType("yaml")

    def safe_load(raw: str) -> Any:
        try:
            return real_yaml.safe_load(raw)
        except real_yaml.YAMLError as exc:
            raise FakeYamlError(str(exc)) from exc

    def safe_dump(
        value: Any,
        *,
        default_flow_style: bool = False,
        sort_keys: bool = True,
    ) -> str:
        return real_yaml.safe_dump(
            value,
            default_flow_style=default_flow_style,
            sort_keys=sort_keys,
        )

    setattr(module, "YAMLError", FakeYamlError)
    setattr(module, "safe_load", safe_load)
    setattr(module, "safe_dump", safe_dump)
    return module


class BootstrapConfigTests(unittest.TestCase):
    def run_bootstrap(
        self,
        home: Path,
        *,
        routes_json: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        original_read_text = Path.read_text
        yaml_before = sys.modules.get("yaml")
        yaml_was_loaded = "yaml" in sys.modules
        fake_yaml = fake_yaml_module()

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            if path in PARTIAL_PATHS:
                return DESIRED_CONFIG
            return original_read_text(path, *args, **kwargs)

        environ = {
            "HERMES_HOME": str(home),
            "API_SERVER_KEY": TEST_API_KEY,
            "HERMES_GATEWAY_API_KEY": TEST_MODEL_KEY,
            "HERMES_MODEL_ROUTES_JSON": (
                routes_json
                if routes_json is not None
                else json.dumps(
                    {
                        "example-alias": {
                            "model": "example-backend/model",
                            "provider": "hermes-search-stack",
                        }
                    }
                )
            ),
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

    def test_invalid_model_routes_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                SystemExit,
                "HERMES_MODEL_ROUTES_JSON is not valid JSON",
            ):
                self.run_bootstrap(Path(tmp), routes_json="not-json")

    def test_model_routes_are_rendered_as_a_mapping(self) -> None:
        routes_json = json.dumps(
            {
                "example-alias": {
                    "model": "example-backend/model",
                    "provider": "hermes-search-stack",
                }
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            namespace, _ = self.run_bootstrap(home, routes_json=routes_json)
            routes = namespace["merged"]["gateway"]["platforms"]["api_server"]["extra"]["model_routes"]
            self.assertEqual(routes["example-alias"], {
                "model": "example-backend/model",
                "provider": "hermes-search-stack",
            })
            self.assertIsInstance(routes, dict)
            self.assertNotIsInstance(routes, str)

            persisted = yaml.safe_load((home / "config.yaml").read_text())
            persisted_routes = persisted["gateway"]["platforms"]["api_server"]["extra"]["model_routes"]
            self.assertIsInstance(persisted_routes, dict)
            self.assertEqual(persisted_routes, routes)

    def test_non_object_model_routes_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                SystemExit,
                "HERMES_MODEL_ROUTES_JSON must decode to a JSON object",
            ):
                self.run_bootstrap(Path(tmp), routes_json="[]")

    def test_named_provider_migration_retires_legacy_model_auth_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.yaml").write_text(CURRENT_CONFIG)

            first, first_output = self.run_bootstrap(home, routes_json="{}")
            merged = first["merged"]
            self.assertIsInstance(merged, dict)
            persisted = yaml.safe_load((home / "config.yaml").read_text())
            self.assertEqual(persisted, merged)
            model = merged["model"]
            self.assertNotIn("base_url", model)
            self.assertNotIn("key_env", model)
            self.assertEqual(model["temperature"], 0.25)
            self.assertEqual(model["user_option"], "preserved")
            self.assertIn("hermes-search-stack", merged["providers"])
            self.assertIn("user-provider", merged["providers"])
            routes = merged["gateway"]["platforms"]["api_server"]["extra"]["model_routes"]
            self.assertEqual(routes, {})
            self.assertIsInstance(routes, dict)
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

            second, second_output = self.run_bootstrap(home, routes_json="{}")
            self.assertEqual(second["merged"], merged)
            self.assertEqual((home / "config.yaml").read_text(), first_persisted)
            self.assertNotIn("dropped legacy model.", second_output)
            self.assert_output_has_no_values(second_output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
