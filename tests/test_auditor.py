"""Tests for the sustain auditor, comparing full per-note object models."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sustain import Reject, run

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- helpers

def on(t, ch=0, p=60):
    return {"time": t, "type": "NOTE_ON", "channel": ch, "pitch": p}


def off(t, ch=0, p=60):
    return {"time": t, "type": "NOTE_OFF", "channel": ch, "pitch": p}


def pdown(t, ch=0):
    return {"time": t, "type": "PEDAL_DOWN", "channel": ch}


def pup(t, ch=0):
    return {"time": t, "type": "PEDAL_UP", "channel": ch}


def note(nid, ch, p, key_start, key_end, sound_start, sound_end, reason):
    return {
        "id": nid,
        "channel": ch,
        "pitch": p,
        "keyStart": key_start,
        "keyEnd": key_end,
        "soundStart": sound_start,
        "soundEnd": sound_end,
        "endReason": reason,
    }


def audit(events, end_time):
    return run({"events": events, "endTime": end_time})


# ------------------------------------------------------- sounding semantics

def test_single_note_without_pedal():
    result = audit([on(10), off(40)], 50)
    assert result == {
        "endTime": 50,
        "notes": [note(0, 0, 60, 10, 40, 10, 40, "NOTE_OFF")],
        "peakPolyphony": 1,
    }


def test_pedal_extends_sound_until_pedal_up():
    result = audit([pdown(5), on(10), off(40), pup(80)], 100)
    assert result["notes"] == [note(0, 0, 60, 10, 40, 10, 80, "PEDAL_UP")]
    assert result["peakPolyphony"] == 1


def test_held_note_survives_pedal_up():
    # A is released under the pedal and dies at PEDAL_UP; B is still held
    # and keeps sounding until endTime.
    events = [on(10, p=60), pdown(20), on(30, p=64), off(40, p=60), pup(50)]
    result = audit(events, 90)
    assert result["notes"] == [
        note(0, 0, 60, 10, 40, 10, 50, "PEDAL_UP"),
        note(1, 0, 64, 30, 90, 30, 90, "END_TIME"),
    ]
    assert result["peakPolyphony"] == 2


def test_end_time_truncation_marks_reason():
    # Note 0 is never released (key still down at endTime); note 1 is
    # released under the pedal and the pedal never comes up.
    events = [on(10, p=60), pdown(20), on(30, p=64), off(40, p=64)]
    result = audit(events, 100)
    assert result["notes"] == [
        note(0, 0, 60, 10, 100, 10, 100, "END_TIME"),
        note(1, 0, 64, 30, 40, 30, 100, "END_TIME"),
    ]
    assert result["peakPolyphony"] == 2


def test_multiple_pedal_cycles():
    events = [
        pdown(0), on(2, p=60), off(4, p=60), pup(6),
        on(8, p=62), off(10, p=62),          # no pedal: ends at key-up
        pdown(12), on(14, p=64), off(16, p=64), pup(18),
    ]
    result = audit(events, 20)
    assert result["notes"] == [
        note(0, 0, 60, 2, 4, 2, 6, "PEDAL_UP"),
        note(1, 0, 62, 8, 10, 8, 10, "NOTE_OFF"),
        note(2, 0, 64, 14, 16, 14, 18, "PEDAL_UP"),
    ]
    assert result["peakPolyphony"] == 1


# ------------------------------------------------------- same-pitch overlap

def test_same_pitch_overlap_pairs_earliest_held_first():
    # Two NOTE_ONs of the same pitch stack up; NOTE_OFFs pair FIFO.
    events = [on(0, p=60), on(10, p=60), off(20, p=60), off(30, p=60)]
    result = audit(events, 40)
    assert result["notes"] == [
        note(0, 0, 60, 0, 20, 0, 20, "NOTE_OFF"),
        note(1, 0, 60, 10, 30, 10, 30, "NOTE_OFF"),
    ]
    # Same pitch sounding twice at once still counts as two voices.
    assert result["peakPolyphony"] == 2


def test_same_pitch_restrike_while_sustained():
    # The first note is sustained by the pedal when the second strike of
    # the same pitch arrives; both are ended by the same PEDAL_UP.
    events = [pdown(0), on(10, p=60), off(20, p=60), on(30, p=60), off(40, p=60), pup(50)]
    result = audit(events, 60)
    assert result["notes"] == [
        note(0, 0, 60, 10, 20, 10, 50, "PEDAL_UP"),
        note(1, 0, 60, 30, 40, 30, 50, "PEDAL_UP"),
    ]
    assert result["peakPolyphony"] == 2


def test_sustained_note_is_not_repaired_by_note_off():
    # After off(10) the only note is released (sustained), not held, so the
    # second NOTE_OFF has nothing to pair with -> whole input rejected.
    with pytest.raises(Reject) as exc:
        audit([pdown(0), on(5, p=60), off(10, p=60), off(15, p=60)], 20)
    assert exc.value.code == "ORPHAN_NOTE_OFF"
    assert exc.value.event_index == 3


# ------------------------------------------------------------ cross-channel

def test_cross_channel_notes_and_pedal_are_independent():
    events = [
        pdown(0, ch=0),
        on(10, ch=0, p=60),
        on(12, ch=1, p=60),      # same pitch, other channel
        off(20, ch=0, p=60),     # sustained by ch-0 pedal
        off(25, ch=1, p=60),     # ch-1 has no pedal: ends immediately
        pup(30, ch=0),
    ]
    result = audit(events, 80)
    assert result["notes"] == [
        note(0, 0, 60, 10, 20, 10, 30, "PEDAL_UP"),
        note(1, 1, 60, 12, 25, 12, 25, "NOTE_OFF"),
    ]
    assert result["peakPolyphony"] == 2


def test_pedal_up_ends_only_its_own_channel():
    events = [
        pdown(0, ch=0), pdown(0, ch=1),
        on(5, ch=0, p=60), on(6, ch=1, p=60),
        off(10, ch=0, p=60), off(11, ch=1, p=60),
        pup(20, ch=0),           # ends ch-0 note only
    ]
    result = audit(events, 50)
    assert result["notes"] == [
        note(0, 0, 60, 5, 10, 5, 20, "PEDAL_UP"),
        note(1, 1, 60, 6, 11, 6, 50, "END_TIME"),
    ]
    assert result["peakPolyphony"] == 2


def test_pedal_state_is_per_channel():
    # PEDAL_DOWN on two different channels is not a repetition.
    result = audit([pdown(0, ch=0), pdown(0, ch=1), pup(1, ch=0), pup(1, ch=1)], 5)
    assert result["notes"] == []
    assert result["peakPolyphony"] == 0


# ------------------------------------------- same-time events / pedal lift

def test_same_timestamp_note_off_before_pedal_up():
    # NOTE_OFF lands while the pedal is still down -> sustained, then the
    # same-instant PEDAL_UP ends it: reason is PEDAL_UP.
    events = [pdown(0), on(5, p=60), off(10, p=60), pup(10)]
    result = audit(events, 30)
    assert result["notes"] == [note(0, 0, 60, 5, 10, 5, 10, "PEDAL_UP")]


def test_same_timestamp_pedal_up_before_note_off():
    # Pedal is already up when the NOTE_OFF executes -> ends immediately.
    events = [pdown(0), on(5, p=60), pup(10), off(10, p=60)]
    result = audit(events, 30)
    assert result["notes"] == [note(0, 0, 60, 5, 10, 5, 10, "NOTE_OFF")]


def test_same_timestamp_pedal_up_and_new_note_on_either_order():
    # A NOTE_ON arriving at the same instant as PEDAL_UP is never ended by
    # that PEDAL_UP, regardless of input order.
    tail_orders = [
        [pup(10), on(10, p=60)],
        [on(10, p=60), pup(10)],
    ]
    results = [audit([pdown(0), on(5, p=60), off(8, p=60)] + tail, 40)
               for tail in tail_orders]
    expected_notes = [
        note(0, 0, 60, 5, 8, 5, 10, "PEDAL_UP"),
        note(1, 0, 60, 10, 40, 10, 40, "END_TIME"),
    ]
    for result in results:
        assert result["notes"] == expected_notes
        assert result["peakPolyphony"] == 1


def test_same_timestamp_note_off_pairs_with_earlier_note_on_only():
    # At t=10 a new strike and a release of the same pitch arrive together;
    # in input order the release pairs with the older held note.
    events = [on(0, p=60), on(10, p=60), off(10, p=60), off(20, p=60)]
    result = audit(events, 30)
    assert result["notes"] == [
        note(0, 0, 60, 0, 10, 0, 10, "NOTE_OFF"),
        note(1, 0, 60, 10, 20, 10, 20, "NOTE_OFF"),
    ]
    assert result["peakPolyphony"] == 1


# ------------------------------------------------------------ peak polyphony

def test_peak_not_counted_when_one_note_ends_as_another_starts():
    events = [on(0, p=60), off(10, p=60), on(10, p=62), off(20, p=62)]
    result = audit(events, 30)
    assert result["peakPolyphony"] == 1


def test_peak_is_global_across_channels():
    events = [
        on(0, ch=0, p=60), on(5, ch=1, p=62), on(8, ch=2, p=64),
        off(20, ch=0, p=60), off(25, ch=1, p=62), off(30, ch=2, p=64),
    ]
    result = audit(events, 40)
    assert result["peakPolyphony"] == 3


def test_zero_length_note_adds_nothing_to_peak():
    events = [on(0, p=60), on(5, p=64), off(5, p=64), off(10, p=60)]
    result = audit(events, 20)
    assert result["notes"] == [
        note(0, 0, 60, 0, 10, 0, 10, "NOTE_OFF"),
        note(1, 0, 64, 5, 5, 5, 5, "NOTE_OFF"),
    ]
    assert result["peakPolyphony"] == 1


def test_end_time_equal_to_last_event_is_allowed():
    result = audit([on(0, p=60), off(10, p=60)], 10)
    assert result["notes"][0]["soundEnd"] == 10
    assert result["peakPolyphony"] == 1


def test_note_held_at_zero_length_window():
    result = audit([on(0, p=60)], 0)
    assert result["notes"] == [note(0, 0, 60, 0, 0, 0, 0, "END_TIME")]
    assert result["peakPolyphony"] == 0


# ----------------------------------------------------------------- rejection

@pytest.mark.parametrize(
    "events,code",
    [
        ([off(0)], "ORPHAN_NOTE_OFF"),                       # nothing held
        ([on(0), off(1), off(2)], "ORPHAN_NOTE_OFF"),        # already paired
        ([on(0), on(1), off(2), off(3), off(4)], "ORPHAN_NOTE_OFF"),
        ([off(0, ch=1), ], "ORPHAN_NOTE_OFF"),               # wrong channel
        ([on(0, p=60), off(1, p=61)], "ORPHAN_NOTE_OFF"),    # wrong pitch
        ([pdown(0), pdown(1)], "REPEATED_PEDAL"),
        ([pup(0)], "REPEATED_PEDAL"),
        ([pdown(0), pup(1), pup(2)], "REPEATED_PEDAL"),
        ([on(10), on(5)], "TIME_ORDER"),
        ([{"time": 0, "type": "NOTE_ON", "channel": 16, "pitch": 60}], "BAD_EVENT"),
        ([{"time": 0, "type": "NOTE_ON", "channel": -1, "pitch": 60}], "BAD_EVENT"),
        ([{"time": 0, "type": "NOTE_ON", "channel": 0, "pitch": 128}], "BAD_EVENT"),
        ([{"time": 0, "type": "NOTE_ON", "channel": 0}], "BAD_EVENT"),
        ([{"time": -1, "type": "NOTE_ON", "channel": 0, "pitch": 60}], "BAD_EVENT"),
        ([{"time": 0, "type": "BREW_COFFEE", "channel": 0}], "BAD_EVENT"),
        ([{"time": 0, "type": "PEDAL_DOWN"}], "BAD_EVENT"),
        (["not-an-object"], "BAD_EVENT"),
    ],
)
def test_rejected_inputs(events, code):
    with pytest.raises(Reject) as exc:
        audit(events, 100)
    assert exc.value.code == code


@pytest.mark.parametrize(
    "payload,code",
    [
        ({}, "BAD_PAYLOAD"),                                # missing events
        ({"events": [], "endTime": 0}, "BAD_PAYLOAD"),      # too few events
        ({"events": [on(0)]}, "BAD_PAYLOAD"),               # missing endTime
        ({"events": [on(10)], "endTime": 9}, "BAD_PAYLOAD"),  # endTime too early
        ({"events": [on(0)], "endTime": -1}, "BAD_PAYLOAD"),
        ([on(0)], "BAD_PAYLOAD"),                           # not an object
        ("nope", "BAD_PAYLOAD"),
    ],
)
def test_rejected_payloads(payload, code):
    with pytest.raises(Reject) as exc:
        run(payload)
    assert exc.value.code == code


def test_5000_events_accepted():
    events = []
    for i in range(2500):
        events.append(on(i * 2, ch=i % 16, p=i % 128))
        events.append(off(i * 2 + 1, ch=i % 16, p=i % 128))
    result = audit(events, 5000)
    assert len(result["notes"]) == 2500
    assert result["peakPolyphony"] == 1


def test_5001_events_rejected():
    events = [on(0, ch=i % 16, p=i % 128) for i in range(5001)]
    with pytest.raises(Reject) as exc:
        audit(events, 10)
    assert exc.value.code == "BAD_PAYLOAD"


# ----------------------------------------------------------------------- CLI

def run_cli(stdin_text):
    return subprocess.run(
        [sys.executable, "-m", "sustain"],
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_cli_happy_path():
    payload = {"events": [pdown(5), on(10), off(40), pup(80)], "endTime": 100}
    proc = run_cli(json.dumps(payload))
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["notes"] == [note(0, 0, 60, 10, 40, 10, 80, "PEDAL_UP")]
    assert out["peakPolyphony"] == 1


def test_cli_rejects_non_json():
    proc = run_cli("this is not json")
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"]["code"] == "INVALID_JSON"


def test_cli_rejects_orphan_note_off():
    proc = run_cli(json.dumps({"events": [off(0)], "endTime": 10}))
    assert proc.returncode == 1
    err = json.loads(proc.stderr)["error"]
    assert err["code"] == "ORPHAN_NOTE_OFF"
    assert err["eventIndex"] == 0


def test_cli_rejects_repeated_pedal():
    proc = run_cli(json.dumps({"events": [pdown(0), pdown(1)], "endTime": 5}))
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"]["code"] == "REPEATED_PEDAL"
