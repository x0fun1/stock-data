"""One definition of "the News layer was assessed".

Before this module the answer depended on which caller asked: the orchestrator
mapped ``partial`` to ``not_assessed`` in the stage receipt, the report rendered
"未评估" for anything that was not ``complete``/``valid``, and the delivery
validator accepted that sentence.  The result was a delivered line that
contradicted the frozen evidence: a run whose News stage had analysed the
articles, applied the gates and produced warnings was described as if nothing had
been read.

The three states are distinct and none of them may be collapsed into another:

``complete``      assessed, no warnings;
``partial``       assessed, with one or more disclosed warnings;
``not_assessed``  no News evidence was produced at all.

Only the last one may be described as "未评估".  A warning count is part of the
partial state on purpose: a reader has to be able to tell a clean run from one
that dropped twenty records.
"""

from __future__ import annotations

from typing import Any

NEWS_STATUS_COMPLETE = "complete"
NEWS_STATUS_PARTIAL = "partial"
NEWS_STATUS_NOT_ASSESSED = "not_assessed"

#: Legacy aliases still written by older artifacts.  ``valid`` meant the same as
#: ``complete`` before the three-state split.
ASSESSED_NEWS_STATUSES = frozenset({"complete", "valid", "partial"})
UNASSESSED_NEWS_STATUSES = frozenset({
    "not_assessed", "unavailable", "failed", "invalid", "empty", "blocked", "skipped", "not_applicable",
})


def news_assessed(status: Any) -> bool:
    """Return True exactly when the News layer produced assessed evidence.

    Unknown values fail closed: an unrecognised status is not assessed, so a
    corrupted artifact can never be described as evaluated.
    """

    if not isinstance(status, str):
        return False
    return status.strip().lower() in ASSESSED_NEWS_STATUSES


def news_status_label(status: Any) -> str:
    """Map an artifact status onto the three disclosed states."""

    value = str(status or "").strip().lower()
    if not news_assessed(value):
        return NEWS_STATUS_NOT_ASSESSED
    return NEWS_STATUS_COMPLETE if value in {"complete", "valid"} else NEWS_STATUS_PARTIAL