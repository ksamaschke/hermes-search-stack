#!/usr/bin/env python3
"""Regression tests for the Kokoro TTS manifest contract."""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKER_PATH = ROOT / "scripts" / "check-kokoro-tts.py"
SPEC = importlib.util.spec_from_file_location("check_kokoro_tts", CHECKER_PATH)
assert SPEC and SPEC.loader
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class KokoroTTSContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        kustomize = shutil.which("kustomize")
        command = [kustomize, "build"] if kustomize else ["kubectl", "kustomize"]
        cls.kubernetes = subprocess.check_output(
            [*command, str(ROOT / "deploy/kubernetes/base")],
            text=True,
        )
        cls.compose = (ROOT / "deploy/compose/docker-compose.yml").read_text()

    def check(self, kubernetes: str | None = None, compose: str | None = None) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            kubernetes_path = base / "kubernetes.yaml"
            compose_path = base / "compose.yaml"
            kubernetes_path.write_text(kubernetes if kubernetes is not None else self.kubernetes)
            compose_path.write_text(compose if compose is not None else self.compose)
            CHECKER.check_kubernetes(kubernetes_path)
            CHECKER.check_compose(compose_path)

    def mutate_kubernetes(self, kind: str, name: str, mutation) -> str:
        documents = list(CHECKER.yaml.safe_load_all(self.kubernetes))
        target = next(
            document
            for document in documents
            if document
            and document.get("kind") == kind
            and document.get("metadata", {}).get("name") == name
        )
        mutation(target)
        return CHECKER.yaml.safe_dump_all(documents, sort_keys=False)

    def mutate_compose_service(self, name: str, mutation) -> str:
        document = CHECKER.yaml.safe_load(self.compose)
        mutation(document["services"][name])
        return CHECKER.yaml.safe_dump(document, sort_keys=False)

    def test_accepts_production_manifests(self) -> None:
        self.check()

    def test_rejects_wrong_api_prefix(self) -> None:
        with self.assertRaisesRegex(AssertionError, "base URL is wrong"):
            self.check(kubernetes=self.kubernetes.replace("http://kokoro-web:3000/api/v1", "http://kokoro-web:3000/v1"))

    def test_rejects_unreviewed_image_digest(self) -> None:
        replacement = CHECKER.KOKORO_IMAGE[:-1] + ("0" if CHECKER.KOKORO_IMAGE[-1] != "0" else "1")
        with self.assertRaisesRegex(AssertionError, "reviewed digest"):
            self.check(kubernetes=self.kubernetes.replace(CHECKER.KOKORO_IMAGE, replacement))

    def test_rejects_missing_shared_secret(self) -> None:
        with self.assertRaisesRegex(AssertionError, "API key Secret reference"):
            self.check(kubernetes=self.kubernetes.replace("name: kokoro-secrets", "name: unrelated-secret", 1))

    def test_rejects_compose_missing_cache_volume(self) -> None:
        mutated = self.mutate_compose_service(
            "kokoro-web",
            lambda service: service["volumes"].remove("kokoro-cache:/kokoro/cache"),
        )
        with self.assertRaisesRegex(AssertionError, "cache volume"):
            self.check(compose=mutated)

    def test_rejects_kubernetes_disconnected_cache_pvc(self) -> None:
        def disconnect_cache(deployment) -> None:
            volume = next(item for item in deployment["spec"]["template"]["spec"]["volumes"] if item["name"] == "cache")
            volume["persistentVolumeClaim"]["claimName"] = "unrelated-cache"

        mutated = self.mutate_kubernetes("Deployment", "kokoro-web", disconnect_cache)
        with self.assertRaisesRegex(AssertionError, "kokoro-cache PVC"):
            self.check(kubernetes=mutated)

    def test_rejects_wrong_service_port(self) -> None:
        service_route = "port: 3000\n    targetPort: http\n  selector:\n    app.kubernetes.io/name: kokoro-web"
        with self.assertRaisesRegex(AssertionError, "Service port or targetPort"):
            self.check(kubernetes=self.kubernetes.replace(service_route, service_route.replace("port: 3000", "port: 3001")))

    def test_rejects_wrong_deployment_selector(self) -> None:
        selector = "  selector:\n    matchLabels:\n      app.kubernetes.io/name: kokoro-web\n  strategy:"
        with self.assertRaisesRegex(AssertionError, "Deployment selector"):
            self.check(kubernetes=self.kubernetes.replace(selector, selector.replace("kokoro-web", "unrelated"), 1))

    def test_rejects_wrong_deployment_template_label(self) -> None:
        template_label = "        app.kubernetes.io/name: kokoro-web\n        app.kubernetes.io/part-of:"
        self.assertEqual(self.kubernetes.count(template_label), 1)
        with self.assertRaisesRegex(AssertionError, "pod-template labels"):
            self.check(
                kubernetes=self.kubernetes.replace(
                    template_label,
                    template_label.replace("kokoro-web", "unrelated"),
                )
            )

    def test_rejects_wrong_network_policy_target(self) -> None:
        marker = "name: kokoro-allow-open-webui"
        before, after = self.kubernetes.split(marker, 1)
        mutated = before + marker + after.replace("app.kubernetes.io/name: kokoro-web", "app.kubernetes.io/name: unrelated", 1)
        with self.assertRaisesRegex(AssertionError, "NetworkPolicy selector"):
            self.check(kubernetes=mutated)

    def test_rejects_nonmatching_required_network_policy_expression(self) -> None:
        def exclude_kokoro(policy) -> None:
            policy["spec"]["podSelector"]["matchExpressions"] = [
                {"key": "app.kubernetes.io/name", "operator": "DoesNotExist"}
            ]

        mutated = self.mutate_kubernetes("NetworkPolicy", "kokoro-allow-open-webui", exclude_kokoro)
        with self.assertRaisesRegex(AssertionError, "NetworkPolicy selector"):
            self.check(kubernetes=mutated)

    def test_rejects_additional_unrestricted_network_policy_source(self) -> None:
        def add_unrestricted_source(policy) -> None:
            policy["spec"]["ingress"][0]["from"].append({"podSelector": {}})

        mutated = self.mutate_kubernetes("NetworkPolicy", "kokoro-allow-open-webui", add_unrestricted_source)
        with self.assertRaisesRegex(AssertionError, "only Open WebUI"):
            self.check(kubernetes=mutated)

    def test_rejects_additional_network_policy_allowing_kokoro(self) -> None:
        documents = list(CHECKER.yaml.safe_load_all(self.kubernetes))
        documents.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "kokoro-unrestricted"},
                "spec": {
                    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "kokoro-web"}},
                    "policyTypes": ["Ingress"],
                    "ingress": [{}],
                },
            }
        )
        mutated = CHECKER.yaml.safe_dump_all(documents, sort_keys=False)
        with self.assertRaisesRegex(AssertionError, "additional ingress NetworkPolicy"):
            self.check(kubernetes=mutated)

    def test_rejects_unmatched_network_policy_source_label(self) -> None:
        def add_unmatched_label(policy) -> None:
            source = policy["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]
            source["example.invalid/nonexistent"] = "true"

        mutated = self.mutate_kubernetes("NetworkPolicy", "kokoro-allow-open-webui", add_unmatched_label)
        with self.assertRaisesRegex(AssertionError, "Open WebUI pod-template labels"):
            self.check(kubernetes=mutated)

    def test_rejects_wrong_container_port(self) -> None:
        with self.assertRaisesRegex(AssertionError, "Deployment HTTP container port"):
            self.check(kubernetes=self.kubernetes.replace("containerPort: 3000", "containerPort: 3001"))

    def test_rejects_wrong_network_policy_port(self) -> None:
        marker = "name: kokoro-allow-open-webui"
        before, after = self.kubernetes.split(marker, 1)
        mutated = before + marker + after.replace("port: 3000", "port: 3001", 1)
        with self.assertRaisesRegex(AssertionError, "NetworkPolicy"):
            self.check(kubernetes=mutated)

    def test_rejects_persistent_configuration(self) -> None:
        with self.assertRaisesRegex(AssertionError, "persistent configuration"):
            self.check(kubernetes=self.kubernetes.replace('name: ENABLE_PERSISTENT_CONFIG\n          value: "false"', 'name: ENABLE_PERSISTENT_CONFIG\n          value: "true"'))

    def test_rejects_wrong_model_default(self) -> None:
        with self.assertRaisesRegex(AssertionError, "model or voice default"):
            self.check(kubernetes=self.kubernetes.replace("tts-model: model_fp16", "tts-model: model_q8f16"))

    def test_rejects_compose_without_rootless_user(self) -> None:
        with self.assertRaisesRegex(AssertionError, "not rootless"):
            self.check(compose=self.compose.replace('    user: "${HERMES_UID:-1000}:${HERMES_GID:-1000}"\n', ""))

    def test_rejects_compose_without_kokoro_cap_drop(self) -> None:
        mutated = self.mutate_compose_service("kokoro-web", lambda service: service.pop("cap_drop"))
        with self.assertRaisesRegex(AssertionError, "capabilities"):
            self.check(compose=mutated)

    def test_rejects_compose_without_kokoro_no_new_privileges(self) -> None:
        mutated = self.mutate_compose_service("kokoro-web", lambda service: service.pop("security_opt"))
        with self.assertRaisesRegex(AssertionError, "no-new-privileges"):
            self.check(compose=mutated)

    def test_rejects_compose_extra_kokoro_volume(self) -> None:
        mutated = self.mutate_compose_service(
            "kokoro-web", lambda service: service["volumes"].append("unrelated:/tmp/unrelated")
        )
        with self.assertRaisesRegex(AssertionError, "cache volume"):
            self.check(compose=mutated)

    def test_rejects_compose_extra_kokoro_network(self) -> None:
        mutated = self.mutate_compose_service(
            "kokoro-web", lambda service: service["networks"].append("unrelated")
        )
        with self.assertRaisesRegex(AssertionError, "stack network"):
            self.check(compose=mutated)

    def test_rejects_compose_missing_no_track(self) -> None:
        mutated = self.mutate_compose_service(
            "kokoro-web", lambda service: service["environment"].pop("KW_PUBLIC_NO_TRACK")
        )
        with self.assertRaisesRegex(AssertionError, "environment"):
            self.check(compose=mutated)

    def test_rejects_compose_extra_environment(self) -> None:
        mutated = self.mutate_compose_service(
            "kokoro-web", lambda service: service["environment"].update({"UNEXPECTED": "true"})
        )
        with self.assertRaisesRegex(AssertionError, "environment"):
            self.check(compose=mutated)

    def test_rejects_compose_malformed_api_key_interpolation(self) -> None:
        mutated = self.mutate_compose_service(
            "kokoro-web",
            lambda service: service["environment"].update(
                {"KW_SECRET_API_KEY": "${KOKORO_API_KEY_UNRELATED:-unsafe}"}
            ),
        )
        with self.assertRaisesRegex(AssertionError, "environment"):
            self.check(compose=mutated)

    def test_rejects_compose_openwebui_malformed_api_key_interpolation(self) -> None:
        mutated = self.mutate_compose_service(
            "open-webui",
            lambda service: service["environment"].update(
                {"AUDIO_TTS_OPENAI_API_KEY": "${KOKORO_API_KEY_UNRELATED:-unsafe}"}
            ),
        )
        with self.assertRaisesRegex(AssertionError, "share Kokoro API key"):
            self.check(compose=mutated)


if __name__ == "__main__":
    unittest.main(verbosity=2)
