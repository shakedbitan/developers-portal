"""
run_tracker.py
---------------
Background poller that keeps script_run_approvals.workflow_phase current
against Argo, and marks a run resolved -- moving it from "my requests" to
"history" -- the moment its workflow reaches a terminal phase.

This is what actually completes the "user gets notified their request
succeeded" story: an *approval* alone doesn't mean the run finished, the
workflow still has to actually run to completion (or fail) afterward,
which can take anywhere from seconds to a long time depending on the
script. Nothing else in Eden watches for that transition.

Deliberately polling rather than any push mechanism from Argo -- the
underlying event (a pod finishing) is itself minutes-granularity at best,
so a 20s poll loop costs nothing meaningful in latency while staying far
simpler than standing up a webhook/watch listener for it.
"""

import logging
import threading
import time

import argo_client
import audit_log
import db

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 20


def _poll_once():
    runs = db.get_runs_awaiting_phase_poll()
    for run in runs:
        status = argo_client.get_workflow_status(
            run["namespace"], run["workflow_name"], run.get("argo_url")
        )
        if status is None:
            # Unreachable / transient fetch failure this cycle -- leave the
            # last-known phase alone and try again next cycle rather than
            # overwriting it with nothing.
            continue

        phase = status.get("workflow_phase")
        if phase == run.get("workflow_phase"):
            continue  # unchanged -- skip the write

        db.update_run_phase(run["id"], phase)
        logger.info("Run %s (%s/%s) phase -> %s", run["id"], run["team"], run["script_name"], phase)

        if phase in db.TERMINAL_WORKFLOW_PHASES:
            audit_log.run_finished(run["id"], run["team"], run["script_name"], phase.lower())


def start():
    """Starts the poller as a daemon thread. Safe to call even when the DB
    or Argo aren't reachable -- each cycle just no-ops (get_runs_awaiting_
    phase_poll and get_workflow_status both already degrade to an empty
    list / None on failure), same shape as every other background loop in
    this app quietly sitting idle rather than crashing at startup."""
    def _loop():
        while True:
            try:
                _poll_once()
            except Exception as e:
                logger.error("run_tracker poll cycle failed: %s", e)
            time.sleep(POLL_INTERVAL_SECONDS)

    threading.Thread(target=_loop, daemon=True).start()
    logger.info("run_tracker started (poll interval %ds)", POLL_INTERVAL_SECONDS)
