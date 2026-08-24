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

import json
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
    argo_url: str | None = None,
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
        argo_url: overrides config.ARGO_URL -- set when the script defines
            an `argo_target` arg letting the user pick which Argo instance
            to submit to (see app.py's target-resolution logic). Falls back
            to the single global instance for every other script.

    Returns:
        dict with "workflow_name", "namespace", and "argo_url" (the instance
        actually used, so callers can persist it for later approve/reject/
        status calls against the same server) on success, "error" on failure
    """
    argo_url = argo_url or config.ARGO_URL
    namespace = f"{team}-workflows"
    template_name = TEMPLATE_MAP.get(language)
    if not template_name:
        logger.error("Unknown language '%s' — cannot map to ClusterWorkflowTemplate", language)
        return {"error": f"Unsupported language: {language}"}

    # Build args string: "key1=value1,key2=value2" -- the ClusterWorkflowTemplate's
    # run-python step does `export $(echo "...args..." | tr ',' ' ')`, which
    # only works for comma-separated KEY=VALUE pairs. A "--key value" CLI-flag
    # style string (what this used to build) isn't a valid `export` argument
    # at all -- `export --region us-east` fails with "Illegal option --",
    # aborting the whole script under `set -e` before it ever runs. This
    # also means a value containing a comma or a literal space will still
    # break the split -- not handled here, same constraint the run-approval
    # review UI's edited args are already subject to.
    args_parts = [f"{k}={v}" for k, v in user_args.items()]
    args_str = ",".join(args_parts)

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

    url = f"{argo_url}/api/v1/workflows/{namespace}/submit"
    logger.info(
        "Submitting workflow: team=%s script=%s language=%s namespace=%s template=%s argo_url=%s",
        team, script_name, language, namespace, template_name, argo_url,
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
        logger.error("Cannot reach Argo at %s: %s", argo_url, e)
        return {"error": f"Cannot reach Argo Workflows at {argo_url}"}
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
    logger.info("Workflow submitted successfully: %s in %s (%s)", workflow_name, namespace, argo_url)
    return {
        "workflow_name": workflow_name,
        "namespace": namespace,
        "argo_url": argo_url,
        "argo_ui_url": f"{argo_url}/workflows/{namespace}/{workflow_name}",
    }


def _argo_put(url: str, payload: dict) -> dict:
    """Shared PUT helper for the set/resume calls below -- same error
    handling shape as submit_workflow, just factored out since resume_workflow
    now needs it twice."""
    try:
        resp = requests.put(url, headers=_headers(), json=payload, timeout=15, verify=False)
    except requests.exceptions.ConnectionError as e:
        logger.error("Cannot reach Argo at %s: %s", url, e)
        return {"error": f"Cannot reach Argo Workflows at {url}"}
    except requests.exceptions.Timeout:
        logger.error("Argo request timed out: %s", url)
        return {"error": "Argo Workflows request timed out"}

    if resp.status_code not in (200, 201):
        msg = resp.json().get("message", resp.text[:200]) if resp.text else "Unknown error"
        logger.error("Argo request to %s failed: %d %s", url, resp.status_code, msg)
        return {"error": f"Argo error: {msg}"}
    return {"ok": True}


def resume_workflow(namespace: str, workflow_name: str, approve: str, args: dict,
                     argo_url: str | None = None) -> dict:
    """
    Resume a workflow suspended at its `approval` node, supplying both
    declared outputs (`approve` and `args`) -- this is what Eden's
    run-approval review UI calls when an admin approves/rejects a pending
    run, in place of a person resuming it by hand in Argo's own UI.

    Setting a suspended node's declared "supplied" outputs and actually
    un-suspending it are two separate Argo operations, matching what the
    `argo node set` + `argo resume` CLI commands do under the hood -- a
    single call to /resume with the values attached gets silently accepted
    (200 OK) but never actually records them, leaving the node's outputs
    empty and any `when` clause referencing them stuck comparing against
    unsubstituted template syntax.

    `args` uses the same "key=value,key2=value2" join `submit_workflow`
    sends, so an approved-unedited run reaches the script with
    byte-identical arguments to what would have been submitted directly.

    argo_url must be whatever instance the workflow was actually submitted
    to (see submit_workflow's returned "argo_url") -- for scripts with an
    argo_target arg, that may not be config.ARGO_URL at all, and there's no
    way to resume a workflow against the wrong server.
    """
    argo_url = argo_url or config.ARGO_URL
    args_parts = [f"{k}={v}" for k, v in args.items()]
    args_str = ",".join(args_parts)

    # outputParameters isn't a plain "key=value,key=value" string -- Argo's
    # server does its own json.Unmarshal on this field's content into
    # map[string]string, so it has to be a JSON object of name -> value,
    # not the {name, value} array shape (confirmed by trial: the array form
    # got "cannot unmarshal array into Go value of type map[string]string").
    set_payload = {
        "namespace": namespace,
        "name": workflow_name,
        "nodeFieldSelector": "templateName=approval",
        "outputParameters": json.dumps({
            "approve": approve,
            "args": args_str,
        }),
    }
    set_url = f"{argo_url}/api/v1/workflows/{namespace}/{workflow_name}/set"
    logger.info("Setting approval outputs on %s/%s: approve=%s (argo_url=%s)",
                namespace, workflow_name, approve, argo_url)
    logger.debug("Argo set payload: %s", set_payload)

    set_result = _argo_put(set_url, set_payload)
    if "error" in set_result:
        return set_result

    resume_payload = {
        "namespace": namespace,
        "name": workflow_name,
        "nodeFieldSelector": "templateName=approval",
    }
    resume_url = f"{argo_url}/api/v1/workflows/{namespace}/{workflow_name}/resume"
    logger.debug("Argo resume payload: %s", resume_payload)

    resume_result = _argo_put(resume_url, resume_payload)
    if "error" in resume_result:
        return resume_result

    logger.info("Workflow %s/%s resumed (approve=%s)", namespace, workflow_name, approve)
    return {"workflow_name": workflow_name, "namespace": namespace}


def get_workflow_status(namespace: str, workflow_name: str, argo_url: str | None = None) -> dict | None:
    """
    Fetch a workflow's current state, specifically whether its `approval`
    node is still suspended (genuinely pending) or was already resolved --
    including resumed directly through Argo's own UI, bypassing Eden
    entirely. Callers use this to reconcile Eden's own "pending" records
    against reality before showing/acting on them, since Eden isn't the
    only thing that can resume a suspended node.

    argo_url must match whatever instance the workflow actually lives on
    (see submit_workflow's returned "argo_url") -- same requirement as
    resume_workflow above.

    Returns None on any fetch failure (network issue, workflow deleted by
    the podGC/ttlStrategy cleanup, etc.) -- callers should treat that as
    "can't confirm, leave it alone" rather than assuming a particular state.
    """
    argo_url = argo_url or config.ARGO_URL
    url = f"{argo_url}/api/v1/workflows/{namespace}/{workflow_name}"
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