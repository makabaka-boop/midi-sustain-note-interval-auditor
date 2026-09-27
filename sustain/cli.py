"""Command-line interface: reads one JSON document from stdin, audits it.

Exit code 0 with the result JSON on stdout; exit code 1 with an error JSON
on stderr when the input is rejected (invalid JSON, bad payload, orphan
NOTE_OFF, repeated pedal action, ...).
"""

import json
import sys

from .core import Reject, run


def _fail(code, message, event_index=None):
    error = {"code": code, "message": message}
    if event_index is not None:
        error["eventIndex"] = event_index
    json.dump({"error": error}, sys.stderr, ensure_ascii=False)
    sys.stderr.write("\n")
    return 1


def main(argv=None):
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _fail("INVALID_JSON", f"input is not valid JSON: {exc}")
    try:
        result = run(payload)
    except Reject as rej:
        return _fail(rej.code, rej.message, rej.event_index)
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0
