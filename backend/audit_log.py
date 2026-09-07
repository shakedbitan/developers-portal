"""
audit_log.py
------------
Structured, single-line JSON audit events for the submission lifecycle
(web apps, script MRs, script runs) -- created so an existing Logstash
pipeline that's already shipping Eden's logs to Elasticsearch can pick
these out and drive a "live pending submissions" Kibana dashboard.

Deliberately a *separate* logger channel (name "eden.audit", not the
per-module loggers everywhere else) so these lines are trivially
filterable/routable in Logstash without needing to grok Eden's normal
human-readable operational logs at all -- each line here is nothing but
a JSON object.

Design choice: every event carries the *current total pending count* for
its kind (submission_created / submission_resolved alike), computed at
that instant from the same DB queries the admin "pending" UIs already use.
That's what makes the Kibana side simple -- a panel just needs to show the
*last value* of pending_count filtered by kind, no "count creates minus
count resolves" aggregation/transform to build and keep correct. Because
it's counted fresh at each transition, it's self-correcting: a bug in the
count on one event doesn't compound into the next one the way an
incrementing counter would.
"""

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("eden.audit")


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        # record.msg is already a JSON-serializable dict (see _emit below) --
        # this formatter's only job is to turn it into the final line, not
        # to build the payload itself.
        return json.dumps(record.msg, default=str)


def init():
    """Call once at startup. Separate from module import time so it's
    obvious in app.py's startup sequence that this is happening, same as
    every other init_* call there."""
    logger.propagate = False  # don't also emit through the root handler as a second, non-JSON line
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _emit(event: str, **fields):
    # ISO 8601 rather than a raw epoch float -- directly usable as Kibana's
    # @timestamp without needing a Logstash date filter just to parse it.
    ts = datetime.now(timezone.utc).isoformat()
    payload = {"event": event, "ts": ts, **fields}
    logger.info(payload)


def submission_created(kind: str, item_id, submitted_by: str, pending_count: int):
    """
    kind: 'webapp' | 'script_mr' | 'script_run'
    pending_count: the *admin review queue* count for this kind right after
        this submission landed in it (i.e. what get_pending_submissions /
        get_pending_script_submissions / get_pending_run_approvals returns) --
        this is the number the Kibana panel should show live.
    """
    _emit("submission_created", kind=kind, id=item_id,
          submitted_by=submitted_by, pending_count=pending_count)


def submission_reviewed(kind: str, item_id, status: str, actor: str, pending_count: int):
    """
    An admin decision on something in the review queue -- approving/
    rejecting a web app, a script MR, or a run's approval gate. This is
    what actually drains pending_count back toward 0 on the dashboard; it
    does NOT mean a script run has *finished* (see run_finished below for
    that -- approving a run just opens the gate, Argo still has to run it).
    status: 'approved' | 'rejected'
    """
    _emit("submission_reviewed", kind=kind, id=item_id, status=status,
          actor=actor, pending_count=pending_count)


def run_finished(run_id, team: str, script_name: str, outcome: str):
    """
    A script run's underlying Argo workflow reached a terminal phase --
    detected by run_tracker's poller, not a human action. Separate from
    submission_reviewed's pending_count bookkeeping entirely (this isn't
    about the review queue); mainly here so "did my/anyone's run actually
    succeed" is visible in Kibana too, not just inside Eden's own UI.
    outcome: 'succeeded' | 'failed' | 'error'
    """
    _emit("run_finished", id=run_id, team=team, script_name=script_name, outcome=outcome)
