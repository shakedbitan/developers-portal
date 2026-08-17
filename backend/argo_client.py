"""
argo_client.py
--------------
Submits workflows to Argo Workflows API.

The ClusterWorkflowTemplate name is derived from the script language:
  python     → ClusterWorkflowTemplate-for-python
  bash       → ClusterWorkflowTemplate-for-bash
  powershell → ClusterWorkflowTemplate-for-powershell

The namespace is: <team>-workflows
  e.g. team "db" → namespace "db-workflows"
"""

import logging

import requests

import config

logger = logging.getLogger(__name__)

TEMPLATE_MAP = {
    "python":     "ClusterWorkflowTemplate-for-python",
    "bash":       "ClusterWorkflowTemplate-for-bash",
    "powershell": "ClusterWorkflowTemplate-for-powershell",
}


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.ARGO_TOKEN}",
        "Content-Type": "application/json",
    }


def submit_workflow(
    team: str,
    script_name: str,
    language: str,
    script_path: str,
    user_args: dict,
    dependencies: list,
    approval_required: bool,
    resources: dict,
) -> dict:
    """
    Submit a workflow to Argo.

    Args:
        team: team name (e.g. "db")
        script_name: script folder name (e.g. "rotate-secret")
        language: "python" | "bash" | "powershell"
        script_path: relative path in repo (e.g. "db/rotate-secret/script.py")
        user_args: dict of arg name → value from the UI form
        dependencies: list of dependency strings
        approval_required: bool
        resources: dict with optional "cpu" and "memory" keys

    Returns:
        dict with "workflow_name" and "namespace" on success, "error" on failure
    """
    namespace = f"{team}-workflows"
    template_name = TEMPLATE_MAP.get(language)
    if not template_name:
        logger.error("Unknown language '%s' — cannot map to ClusterWorkflowTemplate", language)
        return {"error": f"Unsupported language: {language}"}

    # Build args string: "--key1 value1 --key2 value2"
    args_parts = []
    for k, v in user_args.items():
        args_parts.append(f"--{k} {v}")
    args_str = " ".join(args_parts)

    # Dependencies as space-separated string
    deps_str = " ".join(dependencies) if dependencies else ""

    # Resources
    cpu    = resources.get("cpu",    "200m")
    memory = resources.get("memory", "256Mi")

    parameters = [
        f"script={script_path}",
        f"args={args_str}",
        f"approval_required={'true' if approval_required else 'false'}",
        f"teamname={team}",
        f"resources=cpu:{cpu}, memory:{memory}",
        f"dependencies={deps_str}",
    ]

    payload = {
        "resourceKind": "ClusterWorkflowTemplate",
        "resourceName": template_name,
        "submitOptions": {
            "parameters": parameters,
        },
    }

    url = f"{config.ARGO_URL}/api/v1/workflows/{namespace}/submit"
    logger.info(
        "Submitting workflow: team=%s script=%s language=%s namespace=%s template=%s",
        team, script_name, language, namespace, template_name,
    )
    logger.debug("Argo payload: %s", payload)

    try:
        resp = requests.post(
            url,
            headers=_headers(),
            json=payload,
            timeout=15,
            verify=False,
        )
    except requests.exceptions.ConnectionError as e:
        logger.error("Cannot reach Argo at %s: %s", config.ARGO_URL, e)
        return {"error": f"Cannot reach Argo Workflows at {config.ARGO_URL}"}
    except requests.exceptions.Timeout:
        logger.error("Argo request timed out: %s", url)
        return {"error": "Argo Workflows request timed out"}

    logger.debug("Argo submit → %d %s", resp.status_code, resp.text[:300])

    if resp.status_code not in (200, 201):
        msg = resp.json().get("message", resp.text[:200]) if resp.text else "Unknown error"
        logger.error("Argo submit failed: %d %s", resp.status_code, msg)
        return {"error": f"Argo error: {msg}"}

    data = resp.json()
    workflow_name = data.get("metadata", {}).get("name", "unknown")
    logger.info("Workflow submitted successfully: %s in %s", workflow_name, namespace)
    return {
        "workflow_name": workflow_name,
        "namespace": namespace,
        "argo_url": f"{config.ARGO_URL}/workflows/{namespace}/{workflow_name}",
    }