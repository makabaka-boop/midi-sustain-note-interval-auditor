"""Sustain-pedal aware piano event-stream auditor.

Semantics
---------
* Events are processed in input order; events sharing the same time are
  therefore executed in input order too.
* NOTE_ON opens a new note. Repeated NOTE_ONs of the same pitch simply stack
  up as multiple notes waiting to be paired.
* NOTE_OFF releases (lifts the key of) the *earliest still-held* NOTE_ON of
  the same channel and pitch. If the sustain pedal is down on that channel
  the key is released but the note keeps sounding; otherwise its sound ends
  immediately.
* PEDAL_UP ends every released-but-sustained note of that channel; notes
  whose key is still held keep sounding.
* Notes still sounding at ``endTime`` are truncated there and marked with
  endReason ``END_TIME``.
* An orphan NOTE_OFF (no held NOTE_ON to pair with) or a repeated pedal
  action (PEDAL_DOWN while down / PEDAL_UP while up) rejects the whole input.
"""

from __future__ import annotations

import math
from collections import deque

EVENT_TYPES = ("NOTE_ON", "NOTE_OFF", "PEDAL_DOWN", "PEDAL_UP")

MIN_EVENTS = 1
MAX_EVENTS = 5000
MIN_CHANNEL = 0
MAX_CHANNEL = 15
MIN_PITCH = 0
MAX_PITCH = 127
NUM_CHANNELS = 16

REASON_NOTE_OFF = "NOTE_OFF"
REASON_PEDAL_UP = "PEDAL_UP"
REASON_END_TIME = "END_TIME"


class Reject(Exception):
    """Raised when the whole input must be rejected."""

    def __init__(self, code, message, event_index=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.event_index = event_index


def _is_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_int_in(value, lo, hi):
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _validate_event(raw, index, prev_time):
    if not isinstance(raw, dict):
        raise Reject("BAD_EVENT", f"event #{index} is not a JSON object", index)
    etype = raw.get("type")
    if etype not in EVENT_TYPES:
        raise Reject("BAD_EVENT", f"event #{index} has unknown type {etype!r}", index)
    time = raw.get("time")
    if not _is_number(time) or time < 0:
        raise Reject("BAD_EVENT", f"event #{index} has invalid time {time!r}", index)
    if prev_time is not None and time < prev_time:
        raise Reject(
            "TIME_ORDER",
            f"event #{index} time {time} is earlier than previous time {prev_time}",
            index,
        )
    channel = raw.get("channel")
    if not _is_int_in(channel, MIN_CHANNEL, MAX_CHANNEL):
        raise Reject(
            "BAD_EVENT", f"event #{index} has invalid channel {channel!r}", index
        )
    pitch = None
    if etype in ("NOTE_ON", "NOTE_OFF"):
        pitch = raw.get("pitch")
        if not _is_int_in(pitch, MIN_PITCH, MAX_PITCH):
            raise Reject(
                "BAD_EVENT", f"event #{index} has invalid pitch {pitch!r}", index
            )
    return {"type": etype, "time": time, "channel": channel, "pitch": pitch}


def _peak_polyphony(notes):
    """Max number of notes sounding simultaneously.

    Sounding intervals are treated as half-open [soundStart, soundEnd): a
    note ending at t and a note starting at t never overlap.
    """
    deltas = {}
    for note in notes:
        deltas[note["soundStart"]] = deltas.get(note["soundStart"], 0) + 1
        deltas[note["soundEnd"]] = deltas.get(note["soundEnd"], 0) - 1
    current = peak = 0
    for time in sorted(deltas):
        current += deltas[time]
        if current > peak:
            peak = current
    return peak


def run(payload):
    """Audit one JSON payload and return the result dict.

    Raises Reject if the input must be refused as a whole.
    """
    if not isinstance(payload, dict):
        raise Reject("BAD_PAYLOAD", "top level must be a JSON object")
    raw_events = payload.get("events")
    if not isinstance(raw_events, list) or not (
        MIN_EVENTS <= len(raw_events) <= MAX_EVENTS
    ):
        raise Reject(
            "BAD_PAYLOAD",
            f"events must be a list of {MIN_EVENTS}..{MAX_EVENTS} items",
        )

    events = []
    prev_time = None
    for index, raw in enumerate(raw_events):
        event = _validate_event(raw, index, prev_time)
        prev_time = event["time"]
        events.append(event)

    end_time = payload.get("endTime")
    if not _is_number(end_time) or end_time < 0:
        raise Reject("BAD_PAYLOAD", f"endTime must be a non-negative number, got {end_time!r}")
    if end_time < events[-1]["time"]:
        raise Reject(
            "BAD_PAYLOAD",
            f"endTime {end_time} is earlier than the last event at {events[-1]['time']}",
        )

    notes = []
    held = {}  # (channel, pitch) -> deque of notes whose key is still down
    sustained = [[] for _ in range(NUM_CHANNELS)]  # released but still sounding
    pedal_down = [False] * NUM_CHANNELS

    for index, event in enumerate(events):
        time = event["time"]
        channel = event["channel"]
        etype = event["type"]

        if etype == "NOTE_ON":
            note = {
                "id": len(notes),
                "channel": channel,
                "pitch": event["pitch"],
                "keyStart": time,
                "keyEnd": None,
                "soundStart": time,
                "soundEnd": None,
                "endReason": None,
            }
            notes.append(note)
            held.setdefault((channel, event["pitch"]), deque()).append(note)

        elif etype == "NOTE_OFF":
            queue = held.get((channel, event["pitch"]))
            if not queue:
                raise Reject(
                    "ORPHAN_NOTE_OFF",
                    f"event #{index}: NOTE_OFF channel {channel} pitch "
                    f"{event['pitch']} has no held NOTE_ON to release",
                    index,
                )
            note = queue.popleft()  # earliest still-held NOTE_ON
            note["keyEnd"] = time
            if pedal_down[channel]:
                sustained[channel].append(note)  # key up, sound carries on
            else:
                note["soundEnd"] = time
                note["endReason"] = REASON_NOTE_OFF

        elif etype == "PEDAL_DOWN":
            if pedal_down[channel]:
                raise Reject(
                    "REPEATED_PEDAL",
                    f"event #{index}: PEDAL_DOWN while pedal already down "
                    f"on channel {channel}",
                    index,
                )
            pedal_down[channel] = True

        else:  # PEDAL_UP
            if not pedal_down[channel]:
                raise Reject(
                    "REPEATED_PEDAL",
                    f"event #{index}: PEDAL_UP while pedal already up "
                    f"on channel {channel}",
                    index,
                )
            pedal_down[channel] = False
            for note in sustained[channel]:
                note["soundEnd"] = time
                note["endReason"] = REASON_PEDAL_UP
            sustained[channel].clear()

    for note in notes:
        if note["keyEnd"] is None:  # key still held when the input ends
            note["keyEnd"] = end_time
        if note["soundEnd"] is None:
            note["soundEnd"] = end_time
            note["endReason"] = REASON_END_TIME

    return {
        "endTime": end_time,
        "notes": notes,
        "peakPolyphony": _peak_polyphony(notes),
    }
