#!/usr/bin/env python3
"""Behavior tests for the Hermes profile-wiring manifest checker."""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

CHECKER = Path(__file__).with_name("check-hermes-profile-env.py")
NAMED_PROVIDER = "hermes-search-stack"


def config_partial() -> str:
    return textwrap.dedent(
        """\
        providers:
          hermes-search-stack:
            base_url: ${HERMES_MODEL_BASE_URL}
            key_env: HERMES_GATEWAY_API_KEY
            default_model: ${HERMES_MODEL_DEFAULT}

        model:
          provider: ${HERMES_MODEL_PROVIDER}
          default: ${HERMES_MODEL_DEFAULT}
        """
    )


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


def manifest(
    *,
    init_bindings: int = 1,
    main_bindings: int = 1,
    invoke: bool = True,
    runtime_provider: str = NAMED_PROVIDER,
) -> str:
    init_env = "\n".join(model_binding("        ") for _ in range(init_bindings))
    main_env = "\n".join(model_binding("        ") for _ in range(main_bindings))
    invocation = (
        "from profile_env import write_default_profile_env\n"
        "write_default_profile_env(home, os.environ)"
        if invoke
        else "print('bootstrap')"
    )
    runtime_config_map = (
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: hermes-agent-runtime\n"
        "data:\n"
        f'  model-provider: "{runtime_provider}"\n'
    )
    bootstrap_config_map = (
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
    return runtime_config_map + "---\n" + bootstrap_config_map + "---\n" + deployment


class HermesProfileCheckTests(unittest.TestCase):
    def run_checker(
        self, rendered: str, partial: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "rendered.yaml"
            partial_path = root / "config.yaml.partial"
            manifest_path.write_text(rendered)
            partial_path.write_text(config_partial() if partial is None else partial)
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

    def test_rejects_bare_custom_runtime_default(self) -> None:
        result = self.run_checker(manifest(runtime_provider="custom"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("model-provider", result.stdout)

    def test_rejects_duplicate_runtime_provider_with_effective_custom(self) -> None:
        rendered = manifest().replace(
            '  model-provider: "hermes-search-stack"\n',
            '  model-provider: "hermes-search-stack"\n  model-provider: "custom"\n',
        )
        result = self.run_checker(rendered)
        self.assertEqual(result.returncode, 1)
        self.assertIn("model-provider", result.stdout)

    def test_rejects_missing_named_provider_contract(self) -> None:
        partial = textwrap.dedent(
            """\
            model:
              provider: ${HERMES_MODEL_PROVIDER}
              default: ${HERMES_MODEL_DEFAULT}
              key_env: HERMES_GATEWAY_API_KEY
            """
        )
        result = self.run_checker(manifest(), partial)
        self.assertEqual(result.returncode, 1)
        self.assertIn(NAMED_PROVIDER, result.stdout)

    def test_rejects_wrong_named_provider_base_url(self) -> None:
        partial = config_partial().replace(
            "base_url: ${HERMES_MODEL_BASE_URL}",
            "base_url: https://wrong.example/v1",
        )
        result = self.run_checker(manifest(), partial)
        self.assertEqual(result.returncode, 1)
        self.assertIn("base_url", result.stdout)

    def test_rejects_duplicate_named_provider_base_url_with_effective_wrong_value(self) -> None:
        partial = config_partial().replace(
            "    base_url: ${HERMES_MODEL_BASE_URL}\n",
            "    base_url: ${HERMES_MODEL_BASE_URL}\n"
            "    base_url: https://wrong.example/v1\n",
        )
        result = self.run_checker(manifest(), partial)
        self.assertEqual(result.returncode, 1)
        self.assertIn("base_url", result.stdout)

    def test_rejects_duplicate_each_other_named_provider_contract_scalar(self) -> None:
        cases = (
            ("key_env", "HERMES_GATEWAY_API_KEY", "OTHER_API_KEY"),
            ("default_model", "${HERMES_MODEL_DEFAULT}", "wrong/model"),
        )
        for key, expected, wrong in cases:
            with self.subTest(key=key):
                partial = config_partial().replace(
                    f"    {key}: {expected}\n",
                    f"    {key}: {expected}\n    {key}: {wrong}\n",
                )
                result = self.run_checker(manifest(), partial)
                self.assertEqual(result.returncode, 1)
                self.assertIn(key, result.stdout)

    def test_rejects_mismatched_scalar_quotes(self) -> None:
        partial = config_partial().replace(
            "base_url: ${HERMES_MODEL_BASE_URL}",
            'base_url: "${HERMES_MODEL_BASE_URL}\'',
        )
        result = self.run_checker(manifest(), partial)
        self.assertEqual(result.returncode, 1)
        self.assertIn("base_url", result.stdout)

    def test_rejects_wrong_named_provider_key_env_even_if_model_key_env_matches(self) -> None:
        partial = config_partial().replace(
            "key_env: HERMES_GATEWAY_API_KEY",
            "key_env: OTHER_API_KEY",
        )
        partial += "  key_env: HERMES_GATEWAY_API_KEY\n"
        result = self.run_checker(manifest(), partial)
        self.assertEqual(result.returncode, 1)
        self.assertIn("key_env", result.stdout)

    def test_rejects_wrong_named_provider_default_model(self) -> None:
        partial = config_partial().replace(
            "default_model: ${HERMES_MODEL_DEFAULT}",
            "default_model: wrong/model",
        )
        result = self.run_checker(manifest(), partial)
        self.assertEqual(result.returncode, 1)
        self.assertIn("default_model", result.stdout)

    def test_rejects_hard_coded_model_provider(self) -> None:
        partial = config_partial().replace(
            "provider: ${HERMES_MODEL_PROVIDER}",
            f"provider: {NAMED_PROVIDER}",
        )
        result = self.run_checker(manifest(), partial)
        self.assertEqual(result.returncode, 1)
        self.assertIn("environment-driven", result.stdout)

    def test_rejects_duplicate_model_provider_with_effective_wrong_value(self) -> None:
        partial = config_partial().replace(
            "  provider: ${HERMES_MODEL_PROVIDER}\n",
            "  provider: ${HERMES_MODEL_PROVIDER}\n  provider: custom\n",
        )
        result = self.run_checker(manifest(), partial)
        self.assertEqual(result.returncode, 1)
        self.assertIn("model.provider", result.stdout)

    def test_rejects_mismatched_model_provider_quotes(self) -> None:
        partial = config_partial().replace(
            "provider: ${HERMES_MODEL_PROVIDER}",
            'provider: "${HERMES_MODEL_PROVIDER}\'',
        )
        result = self.run_checker(manifest(), partial)
        self.assertEqual(result.returncode, 1)
        self.assertIn("model.provider", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
