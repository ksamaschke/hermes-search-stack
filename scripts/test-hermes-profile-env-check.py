#!/usr/bin/env python3
"""Behavior tests for the Hermes profile-wiring manifest checker."""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

CHECKER = Path(__file__).with_name("check-hermes-profile-env.py")


def model_binding(indent: str) -> str:
    return textwrap.indent(
        textwrap.dedent(
            """\
            - name: HERMES_GATEWAY_API_KEY
              valueFrom:
                secretKeyRef:
                  key: model-api-key
                  name: hermes-agent-secrets
            """
        ),
        indent,
    ).rstrip()


def manifest(*, init_bindings: int = 1, main_bindings: int = 1, invoke: bool = True) -> str:
    init_env = "\n".join(model_binding("        ") for _ in range(init_bindings))
    main_env = "\n".join(model_binding("        ") for _ in range(main_bindings))
    invocation = (
        "from profile_env import write_default_profile_env\n"
        "write_default_profile_env(home, os.environ)"
        if invoke
        else "print('bootstrap')"
    )
    config_map = (
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: hermes-agent-bootstrap\n"
        "data:\n"
        "  bootstrap.py: |\n"
        + textwrap.indent(invocation, "    ")
        + "\n  profile_env.py: |\n"
        "    def write_default_profile_env(*args): pass\n"
    )
    deployment = (
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: hermes-agent\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      initContainers:\n"
        "      - env:\n"
        + (init_env + "\n" if init_env else "")
        + "        name: render-config\n"
        "        volumeMounts:\n"
        "        - mountPath: /bootstrap\n"
        "          name: bootstrap\n"
        "      containers:\n"
        "      - env:\n"
        + (main_env + "\n" if main_env else "")
        + "        name: hermes-agent\n"
        "      volumes:\n"
        "      - name: bootstrap\n"
        "        projected:\n"
        "          sources:\n"
        "          - configMap:\n"
        "              name: hermes-agent-bootstrap\n"
    )
    return config_map + "---\n" + deployment


class HermesProfileCheckTests(unittest.TestCase):
    def run_checker(self, rendered: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "rendered.yaml"
            partial_path = root / "config.yaml.partial"
            manifest_path.write_text(rendered)
            partial_path.write_text("model:\n  key_env: HERMES_GATEWAY_API_KEY\n")
            return subprocess.run(
                ["python3", str(CHECKER), str(manifest_path), str(partial_path)],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_accepts_complete_profile_wiring(self) -> None:
        result = self.run_checker(manifest())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rejects_two_bindings_in_main_and_none_in_init(self) -> None:
        result = self.run_checker(manifest(init_bindings=0, main_bindings=2))
        self.assertEqual(result.returncode, 1)
        self.assertIn("init container", result.stdout)

    def test_rejects_packaged_but_uninvoked_helper(self) -> None:
        result = self.run_checker(manifest(invoke=False))
        self.assertEqual(result.returncode, 1)
        self.assertIn("invoke write_default_profile_env", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
