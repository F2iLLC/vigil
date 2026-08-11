"""Throwaway scratch module for issue #48 dismissal verification.

This file exists only to give a scratch PR a reviewable diff. It is not
imported by anything and the branch carrying it is deleted after the test.
"""


def average_severity(scores):
    # Intentional defect for the scratch review: no guard against an empty
    # sequence, so this raises ZeroDivisionError on `average_severity([])`.
    total = 0
    for s in scores:
        total += s
    return total / len(scores)


def pick_worst(findings):
    # Intentional defect: returns None silently when findings is empty, and
    # mutates the caller's list as a side effect.
    findings.sort(key=lambda f: f["severity"])
    return findings[-1] if findings else None
