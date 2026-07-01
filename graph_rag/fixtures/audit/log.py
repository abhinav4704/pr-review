"""Audit module — intentionally NOT imported anywhere.

Used to exercise the REFERENCES fallback: a call on an unknown receiver whose
method name matches this in-repo function (but with no scope/import/receiver-type
evidence) should resolve to a weak REFERENCES edge, not a confident CALLS.
"""


def stamp(record):
    return record
