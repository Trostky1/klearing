"""The shared contractor handoff contract for Tax and Feature C.

Feature C must link its accountant checklist to the existing tracker rather
than recreating contractor detection or implying that a 1099 decision has
been made.
"""

TRACKER_PATH = "/tools/contractors"
CHECKLIST_LABEL = "1099 / contractor candidates"
ELIGIBILITY_NOTE = "Eligibility not verified"


def checklist_status(candidate_count):
    """Return honest checklist copy for the current candidate count."""
    if candidate_count:
        return "Candidates identified; eligibility not verified."
    return "No candidates identified; eligibility not verified."
