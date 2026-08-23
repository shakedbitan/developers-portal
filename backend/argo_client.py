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


def resume_workflow(namespace: str, workflow_name: str, approve: str, args: dict) -> dict:
    """
    Resume a workflow suspended at its `approval` node, supplying both
    declared outputs (`approve` and `args`) in one call -- this is what
    Eden's run-approval review UI calls when an admin approves/rejects a
    pending run, in place of a person resuming it by hand in Argo's own UI.

    `args` uses the same "--key value" join `submit_workflow` already sends,
    so an approved-unedited run reaches the script with byte-identical
    arguments to what would have been submitted directly.
    """
    args_parts = [f"--{k} {v}" for k, v in args.items()]
    args_str = " ".join(args_parts)

    payload = {
        "namespace": namespace,
        "name": workflow_name,
        "nodeFieldSelector": "templateName=approval",
        "parameter": [f"approve={approve}", f"args={args_str}"],
    }

    url = f"{config.ARGO_URL}/api/v1/workflows/{namespace}/{workflow_name}/resume"
    logger.info("Resuming workflow %s/%s with approve=%s", namespace, workflow_name, approve)
    logger.debug("Argo resume payload: %s", payload)

    try:
        resp = requests.put(
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
        logger.error("Argo resume request timed out: %s", url)
        return {"error": "Argo Workflows request timed out"}

    if resp.status_code not in (200, 201):
        msg = resp.json().get("message", resp.text[:200]) if resp.text else "Unknown error"
        logger.error("Argo resume failed: %d %s", resp.status_code, msg)
        return {"error": f"Argo error: {msg}"}

    logger.info("Workflow %s/%s resumed (approve=%s)", namespace, workflow_name, approve)
    return {"workflow_name": workflow_name, "namespace": namespace}


def get_workflow_status(namespace: str, workflow_name: str) -> dict | None:
    """
    Fetch a workflow's current state, specifically whether its `approval`
    node is still suspended (genuinely pending) or was already resolved --
    including resumed directly through Argo's own UI, bypassing Eden
    entirely. Callers use this to reconcile Eden's own "pending" records
    against reality before showing/acting on them, since Eden isn't the
    only thing that can resume a suspended node.

    Returns None on any fetch failure (network issue, workflow deleted by
    the podGC/ttlStrategy cleanup, etc.) -- callers should treat that as
    "can't confirm, leave it alone" rather than assuming a particular state.
    """
    url = f"{config.ARGO_URL}/api/v1/workflows/{namespace}/{workflow_name}"
    try:
        resp = requests.get(url, headers=_headers(), timeout=10, verify=False)
    except requests.exceptions.RequestException as e:
        logger.warning("Failed to fetch workflow %s/%s: %s", namespace, workflow_name, e)
        return None

    if resp.status_code != 200:
        logger.warning("Workflow %s/%s status fetch → %d", namespace, workflow_name, resp.status_code)
        return None

    data = resp.json()
    nodes = ((data.get("status") or {}).get("nodes")) or {}
    approval_node = next(
        (n for n in nodes.values() if n.get("templateName") == "approval"), None
    )
    approval_outputs = {}
    if approval_node:
        for p in (approval_node.get("outputs") or {}).get("parameters", []):
            approval_outputs[p.get("name")] = p.get("value")

    return {
        "workflow_phase": (data.get("status") or {}).get("phase"),
        "approval_phase": approval_node.get("phase") if approval_node else None,
        "approval_outputs": approval_outputs,
    }