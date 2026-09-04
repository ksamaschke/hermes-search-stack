#!/usr/bin/env python3
"""Validate Kokoro TTS wiring in rendered Kubernetes and Compose manifests."""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised by the CI environment
    raise SystemExit("PyYAML is required: python3 -m pip install PyYAML==6.0.3") from exc

KOKORO_IMAGE = "ghcr.io/eduardolat/kokoro-web:v0.1.3@sha256:58202493d16e2b116a9593fc02adf8ceaca6e8a95ad72369733059707a7d3d17"
KOKORO_LABEL = {"app.kubernetes.io/name": "kokoro-web"}
OPEN_WEBUI_LABEL = {"app.kubernetes.io/name": "open-webui"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def object_doc(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    for doc in docs:
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    raise AssertionError(f"Kubernetes {kind}/{name} missing")


def named_container(deployment: dict[str, Any], name: str) -> dict[str, Any]:
    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    for container in containers:
        if container.get("name") == name:
            return container
    raise AssertionError(f"Kubernetes container {name} missing")


def named_env(container: dict[str, Any], name: str) -> dict[str, Any]:
    for entry in container.get("env", []):
        if entry.get("name") == name:
            return entry
    raise AssertionError(f"Kubernetes environment variable {name} missing")


def labels_include(actual: dict[str, Any], expected: dict[str, str]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


def selector_matches_labels(selector: dict[str, Any], labels: dict[str, Any]) -> bool:
    if not labels_include(labels, selector.get("matchLabels", {})):
        return False
    for expression in selector.get("matchExpressions", []):
        key = expression.get("key")
        operator = expression.get("operator")
        values = expression.get("values", [])
        if operator == "In" and labels.get(key) not in values:
            return False
        if operator == "NotIn" and (key not in labels or labels.get(key) in values):
            return False
        if operator == "Exists" and key not in labels:
            return False
        if operator == "DoesNotExist" and key in labels:
            return False
        if operator not in {"In", "NotIn", "Exists", "DoesNotExist"}:
            return False
    return True


def check_kubernetes(path: pathlib.Path) -> None:
    loaded = list(yaml.safe_load_all(path.read_text()))
    docs = [doc for doc in loaded if isinstance(doc, dict)]
    kokoro = object_doc(docs, "Deployment", "kokoro-web")
    service = object_doc(docs, "Service", "kokoro-web")
    object_doc(docs, "PersistentVolumeClaim", "kokoro-cache")
    openwebui = object_doc(docs, "Deployment", "open-webui")
    runtime = object_doc(docs, "ConfigMap", "open-webui-runtime")
    policy = object_doc(docs, "NetworkPolicy", "kokoro-allow-open-webui")

    kokoro_spec = kokoro.get("spec", {})
    deployment_selector = kokoro_spec.get("selector", {}).get("matchLabels", {})
    template_labels = kokoro_spec.get("template", {}).get("metadata", {}).get("labels", {})
    require(labels_include(deployment_selector, KOKORO_LABEL), "Kokoro Deployment selector does not target kokoro-web")
    require(
        bool(deployment_selector) and labels_include(template_labels, deployment_selector),
        "Kokoro Deployment selector does not match its pod-template labels",
    )

    service_selector = service.get("spec", {}).get("selector", {})
    require(labels_include(service_selector, KOKORO_LABEL), "Kokoro Service selector does not target kokoro-web")
    require(
        bool(service_selector) and labels_include(template_labels, service_selector),
        "Kokoro Service selector does not match the Kokoro pod-template labels",
    )

    policy_pod_selector = policy.get("spec", {}).get("podSelector", {})
    policy_selector = policy_pod_selector.get("matchLabels", {})
    require(
        policy_pod_selector == {"matchLabels": KOKORO_LABEL},
        "Kokoro NetworkPolicy selector does not target exactly kokoro-web",
    )
    require(
        bool(policy_selector) and labels_include(template_labels, policy_selector),
        "Kokoro NetworkPolicy selector does not match the Kokoro pod-template labels",
    )

    container = named_container(kokoro, "kokoro-web")
    require(container.get("image") == KOKORO_IMAGE, "Kokoro image is not pinned to the reviewed digest")
    require(
        any(port.get("name") == "http" and port.get("containerPort") == 3000 for port in container.get("ports", [])),
        "Kokoro Deployment HTTP container port is wrong",
    )
    require(
        any(port.get("name") == "http" and port.get("port") == 3000 and port.get("targetPort") == "http" for port in service.get("spec", {}).get("ports", [])),
        "Kokoro Service port or targetPort is wrong",
    )
    cache_mounts = [
        mount
        for mount in container.get("volumeMounts", [])
        if mount.get("name") == "cache" and mount.get("mountPath") == "/kokoro/cache"
    ]
    cache_volumes = [
        volume
        for volume in kokoro_spec.get("template", {}).get("spec", {}).get("volumes", [])
        if volume.get("name") == "cache"
        and volume.get("persistentVolumeClaim", {}).get("claimName") == "kokoro-cache"
    ]
    require(
        len(cache_mounts) == 1 and len(cache_volumes) == 1,
        "Kokoro cache mount must resolve to the kokoro-cache PVC",
    )

    secret_ref = named_env(container, "KW_SECRET_API_KEY").get("valueFrom", {}).get("secretKeyRef", {})
    require(secret_ref == {"key": "api-key", "name": "kokoro-secrets"}, "Kokoro API key Secret reference is wrong")
    require(named_env(container, "KW_PUBLIC_NO_TRACK").get("value") == "true", "Kokoro analytics opt-out missing")

    openwebui_container = named_container(openwebui, "open-webui")
    require(named_env(openwebui_container, "AUDIO_TTS_ENGINE").get("value") == "openai", "Open WebUI TTS engine is not OpenAI")
    require(
        named_env(openwebui_container, "ENABLE_PERSISTENT_CONFIG").get("value") == "false",
        "Open WebUI persistent configuration must be disabled",
    )
    require(
        named_env(openwebui_container, "AUDIO_TTS_OPENAI_API_BASE_URL").get("value") == "http://kokoro-web:3000/api/v1",
        "Open WebUI Kokoro base URL is wrong",
    )
    openwebui_secret_ref = named_env(openwebui_container, "AUDIO_TTS_OPENAI_API_KEY").get("valueFrom", {}).get("secretKeyRef", {})
    require(openwebui_secret_ref == {"key": "api-key", "name": "kokoro-secrets"}, "Open WebUI TTS key does not share Kokoro Secret")
    for name, key in (("AUDIO_TTS_MODEL", "tts-model"), ("AUDIO_TTS_VOICE", "tts-voice")):
        config_ref = named_env(openwebui_container, name).get("valueFrom", {}).get("configMapKeyRef", {})
        require(config_ref == {"key": key, "name": "open-webui-runtime"}, f"{name} is not configurable through open-webui-runtime")

    runtime_data = runtime.get("data", {})
    require(runtime_data.get("tts-model") == "model_fp16" and runtime_data.get("tts-voice") == "af_heart", "Kubernetes Kokoro model or voice default is wrong")

    openwebui_labels = openwebui.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
    ingress_rules = policy.get("spec", {}).get("ingress", [])
    require(len(ingress_rules) == 1, "Kokoro NetworkPolicy must contain exactly one ingress rule")
    sources = ingress_rules[0].get("from", [])
    ports = ingress_rules[0].get("ports", [])
    require(len(sources) == 1, "Kokoro NetworkPolicy must allow only Open WebUI")
    source = sources[0]
    source_selector = source.get("podSelector", {}).get("matchLabels", {})
    require(
        set(source) == {"podSelector"}
        and set(source.get("podSelector", {})) == {"matchLabels"}
        and bool(source_selector)
        and labels_include(source_selector, OPEN_WEBUI_LABEL),
        "Kokoro NetworkPolicy must allow only Open WebUI",
    )
    require(
        labels_include(openwebui_labels, source_selector),
        "Kokoro NetworkPolicy source does not match Open WebUI pod-template labels",
    )
    require(
        ports == [{"port": 3000, "protocol": "TCP"}],
        "Kokoro NetworkPolicy must allow only Open WebUI on TCP port 3000",
    )
    kokoro_namespace = kokoro.get("metadata", {}).get("namespace")
    additional_ingress_policies = []
    for candidate in docs:
        if candidate.get("kind") != "NetworkPolicy" or candidate is policy:
            continue
        candidate_metadata = candidate.get("metadata", {})
        if candidate_metadata.get("namespace") != kokoro_namespace:
            continue
        candidate_spec = candidate.get("spec", {})
        if selector_matches_labels(candidate_spec.get("podSelector", {}), template_labels) and candidate_spec.get(
            "ingress", []
        ):
            additional_ingress_policies.append(candidate_metadata.get("name", "<unnamed>"))
    require(
        not additional_ingress_policies,
        "Kokoro is selected by an additional ingress NetworkPolicy: " + ", ".join(additional_ingress_policies),
    )


def check_compose(path: pathlib.Path) -> None:
    compose = yaml.safe_load(path.read_text())
    services = compose.get("services", {}) if isinstance(compose, dict) else {}
    require("kokoro-web" in services, "Compose kokoro-web service missing")
    require("open-webui" in services, "Compose open-webui service missing")
    kokoro = services["kokoro-web"]
    openwebui = services["open-webui"]

    require(kokoro.get("image") == KOKORO_IMAGE, "Compose Kokoro image is not pinned to the reviewed digest")
    require(kokoro.get("user") == "${HERMES_UID:-1000}:${HERMES_GID:-1000}", "Compose Kokoro service is not rootless")
    require(kokoro.get("cap_drop") == ["ALL"], "Compose Kokoro service must drop all capabilities")
    require(
        kokoro.get("security_opt") == ["no-new-privileges:true"],
        "Compose Kokoro service must enable no-new-privileges",
    )
    require(
        kokoro.get("environment")
        == {
            "KW_SECRET_API_KEY": "${KOKORO_API_KEY:?set in .env - run ./init.sh}",
            "KW_PUBLIC_NO_TRACK": "true",
        },
        "Compose Kokoro environment must match the reviewed contract",
    )
    require(
        kokoro.get("volumes") == ["kokoro-cache:/kokoro/cache"],
        "Compose Kokoro cache volume must match the reviewed contract",
    )
    require(kokoro.get("networks") == ["stack"], "Compose Kokoro service must use only the stack network")

    openwebui_env = openwebui.get("environment", {})
    require(openwebui_env.get("AUDIO_TTS_ENGINE") == "openai", "Compose Open WebUI TTS engine is not OpenAI")
    require(openwebui_env.get("ENABLE_PERSISTENT_CONFIG") == "false", "Compose Open WebUI persistent configuration must be disabled")
    require(openwebui_env.get("AUDIO_TTS_OPENAI_API_BASE_URL") == "http://kokoro-web:3000/api/v1", "Compose Kokoro base URL is wrong")
    require(
        openwebui_env.get("AUDIO_TTS_OPENAI_API_KEY")
        == "${KOKORO_API_KEY:?set in .env - run ./init.sh}",
        "Compose Open WebUI does not share Kokoro API key",
    )
    require(openwebui_env.get("AUDIO_TTS_MODEL") == "${KOKORO_MODEL:-model_fp16}", "Compose Kokoro model default is wrong")
    require(openwebui_env.get("AUDIO_TTS_VOICE") == "${KOKORO_VOICE:-af_heart}", "Compose Kokoro voice default is wrong")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kubernetes_manifest", type=pathlib.Path)
    parser.add_argument("compose_manifest", type=pathlib.Path)
    args = parser.parse_args()
    try:
        check_kubernetes(args.kubernetes_manifest)
        check_compose(args.compose_manifest)
    except AssertionError as exc:
        print(f"kokoro TTS contract failed: {exc}", file=sys.stderr)
        return 1
    print("kokoro TTS contract OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
