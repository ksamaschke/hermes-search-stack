#!/usr/bin/env python3
"""Behavior tests for the rendered Firecrawl capacity contract."""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

CHECKER = Path(__file__).with_name("check-firecrawl-capacity.py")
SOURCE_DEPLOYMENT = (
    Path(__file__).resolve().parents[1]
    / "deploy/kubernetes/base/firecrawl/deployment.yaml"
)


def deployment(api_cpu: str = "500m", workers: str = "1") -> str:
    return textwrap.dedent(
        f"""\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: firecrawl-api
        spec:
          template:
            spec:
              containers:
              - env:
                - name: NUM_WORKERS_PER_QUEUE
                  value: "{workers}"
                name: api
                resources:
                  requests:
                    cpu: {api_cpu}
              - image: busybox:stable
                name: unrelated
                resources:
                  requests:
                    cpu: 500m
        """
    )


COMPOSE = textwrap.dedent(
    """\
    services:
      firecrawl-api:
        environment:
          NUM_WORKERS_PER_QUEUE: "1"
      redis:
        image: redis:latest
    """
)


class FirecrawlCapacityTests(unittest.TestCase):
    def run_checker(self, manifest: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rendered = root / "rendered.yaml"
            compose = root / "compose.yaml"
            rendered.write_text(manifest)
            compose.write_text(COMPOSE)
            return subprocess.run(
                [
                    "python3",
                    str(CHECKER),
                    str(rendered),
                    "--compose-manifest",
                    str(compose),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_accepts_bounded_api_container(self) -> None:
        result = self.run_checker(deployment())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_accepts_source_style_sequence_indentation(self) -> None:
        rendered = deployment()
        # Indent the remainder of both synthetic container entries as source
        # YAML commonly does; rendered Kustomize YAML aligns the list dash.
        lines = rendered.splitlines()
        start = next(i for i, line in enumerate(lines) if line.strip() == "containers:") + 1
        rendered = "\n".join(lines[:start] + ["  " + line for line in lines[start:]]) + "\n"
        result = self.run_checker(rendered)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_accepts_production_source_manifest(self) -> None:
        result = self.run_checker(SOURCE_DEPLOYMENT.read_text())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_nested_name_cannot_impersonate_api_container(self) -> None:
        rendered = deployment().replace("        name: api\n", "        name: worker\n", 1)
        rendered = rendered.replace(
            "      - image: busybox:stable\n",
            "      - env:\n"
            "        - name: api\n"
            "          value: misleading\n"
            "        - name: NUM_WORKERS_PER_QUEUE\n"
            "          value: \"1\"\n"
            "        image: busybox:stable\n",
            1,
        )
        result = self.run_checker(rendered)
        self.assertEqual(result.returncode, 1)
        self.assertIn("container 'api' not found", result.stderr)

    def test_unrelated_container_cpu_cannot_mask_underrequested_api(self) -> None:
        result = self.run_checker(deployment(api_cpu="250m"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Firecrawl API CPU request must render as 500m", result.stdout)

    def test_rejects_worker_fanout_regression(self) -> None:
        result = self.run_checker(deployment(workers="4"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("NUM_WORKERS_PER_QUEUE must render as 1", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
