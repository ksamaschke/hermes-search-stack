#!/usr/bin/env python3
"""Behavior tests for the profile-scoped Hermes credential writer."""

from __future__ import annotations

import importlib.util
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HELPER = (
    Path(__file__).resolve().parents[1]
    / "deploy/kubernetes/base/hermes-agent/files/profile_env.py"
)
SPEC = importlib.util.spec_from_file_location("hermes_profile_env", HELPER)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {HELPER}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def credentials(api_key: str = "api-key-12345678", model_key: str = "model-key") -> dict[str, str]:
    return {
        "API_SERVER_ENABLED": "true",
        "API_SERVER_HOST": "0.0.0.0",
        "API_SERVER_PORT": "8642",
        "API_SERVER_KEY": api_key,
        "HERMES_GATEWAY_API_KEY": model_key,
    }


class ProfileEnvTests(unittest.TestCase):
    def test_writes_managed_credentials_and_removes_plain_or_exported_stale_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            profile_env = home / "profiles/default/.env"
            profile_env.parent.mkdir(parents=True)
            profile_env.write_text(
                "# existing\n"
                "API_SERVER_KEY=stale-api\n"
                "export HERMES_GATEWAY_API_KEY=stale-model\n"
                "export\tHERMES_GATEWAY_API_KEY=stale-tab-model\n"
                "UNRELATED=value\n"
            )
            profile_env.chmod(0o644)

            env = credentials()
            MODULE.write_default_profile_env(home, env)
            text = profile_env.read_text()

            self.assertIn(f"API_SERVER_KEY={env['API_SERVER_KEY']}\n", text)
            self.assertIn(
                f"HERMES_GATEWAY_API_KEY={env['HERMES_GATEWAY_API_KEY']}\n", text
            )
            self.assertIn("UNRELATED=value\n", text)
            self.assertNotIn("stale-api", text)
            self.assertNotIn("stale-model", text)
            self.assertNotIn("stale-tab-model", text)
            self.assertEqual(text.count("API_SERVER_KEY="), 1)
            self.assertEqual(text.count("HERMES_GATEWAY_API_KEY="), 1)
            self.assertEqual(stat.S_IMODE(profile_env.stat().st_mode), 0o600)

    def test_rotation_replaces_both_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            MODULE.write_default_profile_env(home, credentials())
            MODULE.write_default_profile_env(home, credentials("new-api-key", "new-model-key"))
            text = (home / "profiles/default/.env").read_text()
            self.assertIn("API_SERVER_KEY=new-api-key\n", text)
            self.assertIn("HERMES_GATEWAY_API_KEY=new-model-key\n", text)
            self.assertNotIn("api-key-12345678", text)
            self.assertNotIn("HERMES_GATEWAY_API_KEY=model-key\n", text)

    def test_missing_model_key_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = credentials()
            env["HERMES_GATEWAY_API_KEY"] = ""
            with self.assertRaises(MODULE.ProfileEnvError):
                MODULE.write_default_profile_env(Path(tmp), env)

    def test_multiline_credentials_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = credentials(model_key="first\nsecond")
            with self.assertRaises(MODULE.ProfileEnvError):
                MODULE.write_default_profile_env(Path(tmp), env)

    def test_permission_hardening_failure_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(Path, "chmod", side_effect=OSError("denied")):
                with self.assertRaises(MODULE.ProfileEnvError):
                    MODULE.write_default_profile_env(Path(tmp), credentials())


if __name__ == "__main__":
    unittest.main(verbosity=2)
