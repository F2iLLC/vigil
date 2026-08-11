"""Throwaway scratch module for issue #48 dismissal verification.

This file exists only to give a scratch PR a reviewable diff. It is not
imported by anything and the branch carrying it is deleted after the test.
"""


def average_severity(scores):
    """Mean of ``scores``; 0.0 for an empty sequence.

    Guarded so an empty sequence returns a defined value rather than raising
    ZeroDivisionError.
    """
    values = list(scores)
    if not values:
        return 0.0
    return sum(values) / len(values)


def pick_worst(findings):
    """Return the highest-severity finding, or None when there are none.

    Does not mutate the caller's list: ``max`` is used instead of sorting
    in place.
    """
    if not findings:
        return None
    return max(findings, key=lambda f: f["severity"])
