#!/usr/bin/env python3
"""Behavior tests for Firecrawl's model-gateway environment contract."""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

CHECKER = Path(__file__).with_name("check-firecrawl-extraction-env.py")
ROOT = Path(__file__).resolve().parents[1]
SOURCE_DEPLOYMENT = ROOT / "deploy/kubernetes/base/firecrawl/deployment.yaml"
SOURCE_COMPOSE = ROOT / "deploy/compose/docker-compose.yml"


def kubernetes_binding(
    env_name: str,
    ref_kind: str,
    ref_name: str,
    ref_key: str,
) -> str:
    return textwrap.dedent(
        f"""\
        - name: {env_name}
          valueFrom:
            {ref_kind}:
              name: {ref_name}
              key: {ref_key}
        """
    ).rstrip()


def deployment(
    *,
    api_bindings: str | None = None,
    sidecar_bindings: str = "",
) -> str:
    if api_bindings is None:
        api_bindings = "\n".join(
            (
                kubernetes_binding(
                    "OPENAI_API_KEY",
                    "secretKeyRef",
                    "hermes-agent-secrets",
                    "model-api-key",
                ),
                kubernetes_binding(
                    "OPENAI_BASE_URL",
                    "configMapKeyRef",
                    "hermes-agent-runtime",
                    "model-base-url",
                ),
                kubernetes_binding(
                    "MODEL_NAME",
                    "configMapKeyRef",
                    "hermes-agent-runtime",
                    "model-default",
                ),
            )
        )

    api_env = textwrap.indent(api_bindings, "        ") if api_bindings else ""
    sidecar_env = (
        "      - env:\n" + textwrap.indent(sidecar_bindings, "        ") + "\n"
        if sidecar_bindings
        else "      - "
    )
    return (
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: firecrawl-api\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "      - env:\n"
        + (api_env + "\n" if api_env else "")
        + "        name: api\n"
        + sidecar_env
        + "name: observer\n"
    )


COMPOSE = textwrap.dedent(
    """\
    services:
      firecrawl-api:
        image: firecrawl:contract-test
        environment:
          OPENAI_API_KEY: "${HERMES_GATEWAY_API_KEY:?set the gateway key}"
          OPENAI_BASE_URL: "${HERMES_MODEL_BASE_URL:?set the gateway URL}"
          MODEL_NAME: "${HERMES_MODEL_DEFAULT:?set the model}"
      firecrawl-api-helper:
        environment:
          OPENAI_API_KEY: "${HERMES_GATEWAY_API_KEY}"
          OPENAI_BASE_URL: "${HERMES_MODEL_BASE_URL}"
          MODEL_NAME: "${HERMES_MODEL_DEFAULT}"
    """
)


class FirecrawlExtractionEnvironmentTests(unittest.TestCase):
    def run_checker(
        self,
        manifest: str,
        compose: str = COMPOSE,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "rendered.yaml"
            compose_path = root / "compose.yaml"
            manifest_path.write_text(manifest)
            compose_path.write_text(compose)
            return subprocess.run(
                [
                    "python3",
                    str(CHECKER),
                    str(manifest_path),
                    "--compose-manifest",
                    str(compose_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_accepts_complete_direct_wiring(self) -> None:
        result = self.run_checker(deployment())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_production_manifests_wire_extraction_gateway(self) -> None:
        result = self.run_checker(
            SOURCE_DEPLOYMENT.read_text(),
            SOURCE_COMPOSE.read_text(),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sidecar_cannot_mask_missing_api_container_binding(self) -> None:
        api_bindings = "\n".join(
            (
                kubernetes_binding(
                    "OPENAI_BASE_URL",
                    "configMapKeyRef",
                    "hermes-agent-runtime",
                    "model-base-url",
                ),
                kubernetes_binding(
                    "MODEL_NAME",
                    "configMapKeyRef",
                    "hermes-agent-runtime",
                    "model-default",
                ),
            )
        )
        sidecar_binding = kubernetes_binding(
            "OPENAI_API_KEY",
            "secretKeyRef",
            "hermes-agent-secrets",
            "model-api-key",
        )
        result = self.run_checker(
            deployment(api_bindings=api_bindings, sidecar_bindings=sidecar_binding)
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("container api OPENAI_API_KEY", result.stdout)

    def test_wrong_kubernetes_secret_reference_is_rejected(self) -> None:
        rendered = deployment().replace(
            "key: model-api-key",
            "key: unrelated-key",
            1,
        )
        result = self.run_checker(rendered)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Secret hermes-agent-secrets key model-api-key", result.stdout)

    def test_wrong_kubernetes_configmap_reference_is_rejected(self) -> None:
        rendered = deployment().replace(
            "key: model-base-url",
            "key: model-provider",
            1,
        )
        result = self.run_checker(rendered)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ConfigMap hermes-agent-runtime key model-base-url", result.stdout)

    def test_similarly_named_compose_service_cannot_mask_target(self) -> None:
        compose = COMPOSE.replace(
            '      OPENAI_API_KEY: "${HERMES_GATEWAY_API_KEY:?set the gateway key}"\n',
            "",
            1,
        )
        result = self.run_checker(deployment(), compose)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "Compose service firecrawl-api OPENAI_API_KEY",
            result.stdout,
        )

    def test_compose_values_must_use_required_expansion(self) -> None:
        bindings = {
            "OPENAI_API_KEY": (
                "HERMES_GATEWAY_API_KEY",
                "set the gateway key",
            ),
            "OPENAI_BASE_URL": (
                "HERMES_MODEL_BASE_URL",
                "set the gateway URL",
            ),
            "MODEL_NAME": ("HERMES_MODEL_DEFAULT", "set the model"),
        }
        for env_name, (variable, message) in bindings.items():
            with self.subTest(env_name=env_name):
                required = f'      {env_name}: "${{{variable}:?{message}}}"\n'
                permissive = f'      {env_name}: "${{{variable}}}"\n'
                self.assertEqual(COMPOSE.count(required), 1)
                result = self.run_checker(
                    deployment(),
                    COMPOSE.replace(required, permissive, 1),
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    f"Compose service firecrawl-api {env_name}",
                    result.stdout,
                )

    def test_compose_values_must_not_use_default_expansion(self) -> None:
        bindings = {
            "OPENAI_API_KEY": (
                "HERMES_GATEWAY_API_KEY",
                "set the gateway key",
            ),
            "OPENAI_BASE_URL": (
                "HERMES_MODEL_BASE_URL",
                "set the gateway URL",
            ),
            "MODEL_NAME": ("HERMES_MODEL_DEFAULT", "set the model"),
        }
        for env_name, (variable, message) in bindings.items():
            with self.subTest(env_name=env_name):
                required = f'      {env_name}: "${{{variable}:?{message}}}"\n'
                fallback = f'      {env_name}: "${{{variable}:-fallback}}"\n'
                self.assertEqual(COMPOSE.count(required), 1)
                result = self.run_checker(
                    deployment(),
                    COMPOSE.replace(required, fallback, 1),
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    f"Compose service firecrawl-api {env_name}",
                    result.stdout,
                )

    def test_compose_values_must_reference_matching_gateway_variables(self) -> None:
        replacements = {
            "HERMES_GATEWAY_API_KEY": "OTHER_API_KEY",
            "HERMES_MODEL_BASE_URL": "OTHER_BASE_URL",
            "HERMES_MODEL_DEFAULT": "OTHER_MODEL",
        }
        for required, wrong in replacements.items():
            with self.subTest(required=required):
                result = self.run_checker(
                    deployment(),
                    COMPOSE.replace(required, wrong, 1),
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn(required, result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
