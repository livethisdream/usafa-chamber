#!/usr/bin/env python3
"""Scenario runner for the acquisition drivers, against fake instruments.

Each scenario pins one behaviour that hardware contact forced into
`pattern_measure.py`, so a regression shows up here instead of on the chamber
floor. Everything runs against `mock_instruments`, which impersonates pyvisa —
the real `Vna` and `Positioner` classes execute unmodified.

    python rigcheck.py                 # every scenario, PASS/FAIL
    python rigcheck.py split_reply     # one scenario, verbose

Named rigcheck, not dryrun, on purpose: `--dry-run` already means something
else here (no VNA, real motion), and `run_positioner_only` already writes
dryrun.csv. This never moves anything, because there is nothing to move.

Scenarios run as subprocesses so each gets a clean import and its own
monkeypatched pyvisa.
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMMON = ["--vna", "TCPIP0::127.0.0.1::5025::SOCKET",
          "--pos", "ASRL16::INSTR",
          "--points", "21"]


def _rows(outdir: Path, name: str = "pattern.csv") -> list[dict]:
    return list(csv.DictReader((outdir / name).open()))


def _angles(outdir: Path, name: str = "pattern.csv") -> list[float]:
    seen, order = set(), []
    for r in _rows(outdir, name):
        a = float(r["angle_cmd_deg"])
        if a not in seen:
            seen.add(a)
            order.append(a)
    return order


def _actuals(outdir: Path, name: str = "pattern.csv") -> list[float]:
    seen, order = set(), []
    for r in _rows(outdir, name):
        a = float(r["angle_cmd_deg"])
        if a not in seen:
            seen.add(a)
            order.append(float(r["angle_actual_deg"]))
    return order


# ---------------------------------------------------------------- scenarios
# Each returns the faults to install and the argv to run; its check() asserts
# on what landed on disk and what the process did.

def s_nominal():
    return {}, COMMON + ["--step", "90", "--to-deg", "355"]


def check_nominal(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']}: {r['exc']}"
    a = _angles(outdir)
    assert a == [0.0, 90.0, 180.0, 270.0], a
    assert (outdir / "pattern.png").exists(), "no plot written"
    return f"{len(a)} angles, 0..270, plot written"


def s_endpoint():
    # step 7 over 0..90 must stop at 84 rather than overshooting to 91.
    return {}, COMMON + ["--step", "7", "--to-deg", "90"]


def check_endpoint(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']}: {r['exc']}"
    a = _angles(outdir)
    assert max(a) == 84.0, f"expected last angle 84, got {max(a)}"
    return "step 7 over 0..90 stops at 84, no overshoot"


def s_full_circle():
    # 0..360 in 90 deg steps must not measure 0 and 360 both.
    return {}, COMMON + ["--step", "90", "--to-deg", "360"]


def check_full_circle(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']}: {r['exc']}"
    a = _angles(outdir)
    assert 360.0 not in a, "measured 360 deg, a duplicate of 0"
    assert a == [0.0, 90.0, 180.0, 270.0], a
    return "360 deg trimmed to a single 0 deg point"


def s_split_reply():
    # The bug that silently mislabelled two cuts: a corrupted byte splits
    # '255.0 DEGREES' into '2' + '55.0 DEGREES'.
    return {"split_reply_at_deg": 180.0}, COMMON + ["--step", "90", "--to-deg", "355"]


def check_split_reply(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']}: {r['exc']}"
    actual = _actuals(outdir)
    # Every recorded angle must be a real position, not a leading fragment.
    for cmd, act in zip(_angles(outdir), actual):
        assert abs(act - cmd) < 1.0, (
            f"cut commanded at {cmd} recorded as {act} - "
            f"a split reply was accepted as a position")
    assert "malformed position reply" in r["err"], (
        "the split reply was never detected; the guard did not fire")
    return f"fragment rejected and re-read, angles {actual} intact"


def s_split_reply_unguarded():
    # Control for the scenario above: with the guard's regex neutered, the
    # same fault must corrupt the run. A test that passes either way proves
    # nothing, so this asserts the fault is real.
    return {"split_reply_at_deg": 180.0, "_neuter_pos_guard": True}, \
        COMMON + ["--step", "90", "--to-deg", "355"]


def check_split_reply_unguarded(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']}: {r['exc']}"
    bad = [(c, a) for c, a in zip(_angles(outdir), _actuals(outdir))
           if abs(a - c) >= 1.0]
    assert bad, ("neutering the guard did not corrupt the run - "
                 "the split_reply scenario is not actually exercising it")
    return f"guard removed -> {len(bad)} cut(s) mislabelled, e.g. {bad[0]}"


def s_error_retry():
    # A provably valid command rejected with ERROR 1, as seen mid-scan.
    return {"error_on_write": 3}, COMMON + ["--step", "90", "--to-deg", "355"]


def check_error_retry(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']}: {r['exc']}"
    assert "resyncing and retrying" in r["err"], "no retry was logged"
    a = _angles(outdir)
    assert a == [0.0, 90.0, 180.0, 270.0], a
    return "one ERROR absorbed by the retry, scan completed"


def s_error_persistent():
    # A link that is genuinely broken, not a single corrupted byte: the retry
    # cannot help and the run must fail loudly rather than measure garbage.
    return {"persistent_error": True}, COMMON + ["--step", "90"]


def check_error_persistent(r, outdir):
    assert r["rc"] != 0, "a permanently rejecting card was treated as healthy"
    assert "positioner rejected" in (r["err"] + r["exc"]), r["err"][-300:]
    return "persistent ERROR surfaced instead of being retried forever"


def s_query_error():
    return {"error_on_query": 4}, COMMON + ["--step", "90", "--to-deg", "355"]


def check_query_error(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']}: {r['exc']}"
    assert "resyncing and retrying" in r["err"], "no query retry logged"
    return "ERROR on a query resynced and re-read"


def s_silent_acks():
    # Silence after a write is a documented success case, not a fault.
    return {"silent_acks": True}, COMMON + ["--step", "90", "--to-deg", "355"]


def check_silent_acks(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']}: {r['exc']}"
    assert len(_angles(outdir)) == 4
    return "unacknowledged writes accepted, scan completed"


def s_never_moves():
    # The seek did not take - wrong slot prefix, wrong mnemonic, dead axis.
    # Measuring here would log a cut at an angle the tower never reached.
    return {"never_moves": True}, COMMON + ["--step", "90"]


def check_never_moves(r, outdir):
    assert r["rc"] != 0, "measured anyway despite the tower never moving"
    assert "never moved" in (r["err"] + r["exc"]), r["err"][-300:]
    return "refused to measure a cut the tower never reached"


def s_stuck():
    return {"stuck": True}, COMMON + ["--step", "90"]


def check_stuck(r, outdir):
    assert r["rc"] != 0, "a jammed axis was treated as a completed move"
    blob = r["err"] + r["exc"]
    assert "did not reach" in blob or "TimeoutError" in blob, blob[-300:]
    return "jammed axis timed out and was commanded to stop"


def s_late_start():
    # The card takes a moment to report motion. Polling too early sees "not
    # moving" and sweeps mid-rotation; the grace period exists for this.
    return {"late_start_polls": 4}, COMMON + ["--step", "90", "--to-deg", "355"]


def check_late_start(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']}: {r['exc']}"
    for cmd, act in zip(_angles(outdir), _actuals(outdir)):
        assert abs(act - cmd) < 1.0, f"swept mid-rotation: asked {cmd}, read {act}"
    return "slow motion-start absorbed, every cut taken parked"


def s_short_sweep():
    # A truncated trace means the channel state does not match the configured
    # sweep. Silently padding it would corrupt the pattern.
    return {"short_sweep": 2}, COMMON + ["--step", "90"]


def check_short_sweep(r, outdir):
    assert r["rc"] != 0, "a truncated sweep was accepted"
    assert "expected" in (r["err"] + r["exc"]), r["err"][-300:]
    return "truncated sweep rejected with a point count"


def s_drop_sweep():
    return {"drop_sweep": 2}, COMMON + ["--step", "90"]


def check_drop_sweep(r, outdir):
    assert r["rc"] != 0, "a dropped sweep was swallowed"
    assert "VI_ERROR_TMO" in (r["err"] + r["exc"]) or "Timeout" in r["exc"], \
        r["err"][-300:]
    assert r["stops"], (
        "the axis was never commanded to stop - a failure path closed the "
        "port with the tower potentially still turning")
    return f"timeout surfaced, STOP issued at {r['stops']}"


def s_sweep_time_unsupported():
    # Firmware 26.3.1 answers SENS:SWE:TIME? with -110 and then goes quiet.
    return {}, COMMON + ["--step", "90", "--to-deg", "355"]


def check_sweep_time_unsupported(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']}: {r['exc']}"
    assert "sweep time n/a" in r["out"], (
        "the unsupported sweep-time query was not handled as optional")
    return "unsupported SWE:TIME? degraded to 'n/a' without blocking"


def s_service_stops():
    # The same failure, through the other entry point. The service worker
    # catches Exception and reports it; unattended, nothing else would halt a
    # tower that was still turning when the error landed.
    return {"drop_sweep": 2, "_target": "service"}, ["--step", "90"]


def check_service_stops(r, outdir):
    assert r["rc"] == 0, f"harness error: {r['exc']}"
    assert r["stops"], (
        "the service worker reported the failure and left the axis alone")
    return f"worker stopped the axis at {r['stops']} before reporting"


def s_scan_negative_angles():
    # A cut across boresight: -90 to -80, on a card whose limits allow it.
    # Every angle must reach the card exactly as asked. -90 and 270 are the
    # same position reached by turning opposite ways, and the tower has a
    # cable through it, so a helpful-looking `% 360` picks the direction for
    # the operator and can wind the cable up.
    return {"_target": "service"}, ["--step", "5", "--start", "-90",
                                    "--stop", "-80"]


def check_scan_negative_angles(r, outdir):
    assert r["rc"] == 0, f"the scan across boresight failed: {r['exc']}"
    assert r["seeks"], "no seek was issued at all"
    rewritten = [s for s in r["seeks"] if s > 180.0]
    assert not rewritten, (
        f"angles were normalised into 0-360 before being sent: {rewritten}. "
        f"That reaches the same positions by turning the other way.")
    assert any(s < 0 for s in r["seeks"]), (
        f"a scan starting at -90 sent no negative target at all: {r['seeks']}")
    return f"sent {r['seeks']} unmodified"


def s_scan_outside_limits():
    # The rig as it shipped: CCW limit 0, so the card refuses every negative
    # target with ERROR 3. Nothing in software can read the limits back - UL?
    # and LL? are rejected - so the only honest thing is to fail loudly rather
    # than quietly rewrite the angle into one the card will accept.
    return {"_target": "service", "ccw_limit_deg": 0.0}, \
        ["--step", "5", "--start", "-90", "--stop", "-80"]


def check_scan_outside_limits(r, outdir):
    blob = r["err"] + r["exc"]
    assert "ERROR 3" in blob or "rejected" in blob, (
        f"a target outside the travel limits was not surfaced: {blob[-300:]}")
    rewritten = [s for s in r["seeks"] if s > 180.0]
    assert not rewritten, (
        f"the angle was rewritten to dodge the limit: {rewritten}. The tower "
        f"would have turned the long way round instead of refusing.")
    return "out-of-range target refused rather than silently redirected"


def check_trigger_restored(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']}: {r['exc']}"
    assert r["trigger_restored"], (
        "the VNA was left in TRIG:SOUR BUS - the front panel stays frozen")
    return "trigger state handed back on close"


def s_trigger_restored():
    return {}, COMMON + ["--step", "180", "--to-deg", "180"]


def s_uncalibrated():
    # A VNA with no calibration applied. The run is still a run - this asserts
    # the operator is told, not that the scan is refused.
    return ({"correction_off": True},
            COMMON + ["--step", "90", "--to-deg", "355"])


def check_uncalibrated(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']}: {r['exc']}"
    assert "THIS RUN IS UNCALIBRATED" in r["err"], (
        "correction was off and nothing said so")
    assert _angles(outdir) == [0.0, 90.0, 180.0, 270.0], (
        "the warning aborted a scan that should have run anyway")
    return "correction OFF reported, scan completed regardless"


def s_correction_mute():
    # The instrument refuses to answer CORR:STAT?. Unknown is not "off", and
    # neither is a reason to lose the run.
    return ({"correction_mute": True},
            COMMON + ["--step", "180", "--to-deg", "180"])


def check_correction_mute(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']}: {r['exc']}"
    assert "correction unknown" in r["err"], (
        "an unanswered CORR:STAT? was not reported as unknown")
    assert "UNCALIBRATED" not in r["err"], (
        "silence was reported as an uncalibrated instrument")
    return "unanswered query degraded to unknown, run kept"


def s_meta_records_correction():
    # The service path: whatever the state was, meta.json has to carry it, or
    # a finished run cannot say whether it was calibrated.
    return ({"correction_off": True, "_target": "service"}, ["--step", "90"])


def check_meta_records_correction(r, outdir):
    assert r["rc"] == 0, f"harness error: {r['exc']}"
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta.get("correction_state") == "0", (
        f"meta.json recorded correction_state={meta.get('correction_state')!r}, "
        f"not the '0' the instrument reported")
    return "meta.json records the uncalibrated state of the run"


# -- calibration ------------------------------------------------------------
# Nothing in this group reproduces observed behaviour. No calibration has ever
# been run through this path, and the mnemonics it exercises are unverified -
# see AcmCmds. What these hold is the *design*: exclusivity, the honest cancel,
# and the record. Those survive the mnemonics turning out to be wrong.

def s_cal_nominal():
    return {"_target": "cal"}, []


def check_cal_nominal(r, outdir):
    assert r["rc"] == 0, f"harness error: {r['exc']}"
    done = r["cal"].get("done") or {}
    assert done.get("ok"), f"cal did not succeed: {done}"
    rec = r["cal"].get("record") or {}
    assert rec.get("sweep"), "no sweep recorded with the calibration"
    assert rec.get("module"), "the module was not identified"
    assert r["cal"].get("saved"), "cal.json was not written"
    assert pm_on(rec.get("correction_state")), (
        f"cal finished with correction {rec.get('correction_state')!r}")
    return f"cal applied and recorded at {rec['sweep']['points']} pts"


def s_cal_one_port():
    # Reflection only: an antenna on port 1 and nothing on port 2. A 2-port
    # would have no second side to calibrate against, so the cal has to be able
    # to be one port - and has to issue SOLT1, not SOLT2 with a port dropped.
    return {"_target": "cal"}, ["--ports", "1"]


def check_cal_one_port(r, outdir):
    assert r["rc"] == 0, f"harness error: {r['exc']}"
    done = r["cal"].get("done") or {}
    assert done.get("ok") is True, f"a 1-port cal failed: {done}"
    issued = " ".join(r["cals"])
    assert "SOLT1" in issued, f"no SOLT1 was issued: {r['cals']}"
    assert "SOLT2" not in issued, (
        f"a 1-port calibration issued SOLT2: {r['cals']}")
    rec = done.get("record") or {}
    assert rec.get("method") == "ecal_solt1", (
        f"recorded as {rec.get('method')!r} - the record has to say which cal "
        f"it was, or a 1-port gets trusted for transmission later")
    assert rec.get("ports") == [1], rec.get("ports")
    return f"issued {r['cals'][-1]!r} and recorded it as 1-port"


def s_cal_no_module():
    # The likeliest real outcome of Wednesday: the software cannot see the
    # module over SCPI. It has to fail as a clear message, not a traceback.
    return {"acm_present": False, "_target": "cal"}, []


def check_cal_no_module(r, outdir):
    assert r["rc"] == 0, f"harness error: {r['exc']}"
    done = r["cal"].get("done") or {}
    assert done.get("ok") is False and not done.get("cancelled"), done
    assert "AutoCal module" in done.get("error", ""), done.get("error")
    assert not r["cal"].get("saved"), (
        "a calibration that never happened was written to cal.json")
    return "absent module refused with a readable message, nothing recorded"


def s_cal_unsupported():
    # The other likely outcome: the headers do not exist on this build.
    return {"acm_headers_known": False, "_target": "cal"}, []


def check_cal_unsupported(r, outdir):
    assert r["rc"] == 0, f"harness error: {r['exc']}"
    done = r["cal"].get("done") or {}
    assert done.get("ok") is False, done
    assert not r["cal"].get("saved"), "unsupported cal still wrote a record"
    return "unsupported headers surfaced as a failed cal, nothing recorded"


def s_cal_fails_midway():
    # The state that matters: the cal command errors part-way. Whatever was
    # collected must not be left sitting in the instrument.
    return {"acm_fails": True, "_target": "cal"}, []


def check_cal_fails_midway(r, outdir):
    assert r["rc"] == 0, f"harness error: {r['exc']}"
    done = r["cal"].get("done") or {}
    assert done.get("ok") is False, done
    assert r["cleared"] >= 1, (
        "the cal failed and the collection buffer was never cleared - the "
        "instrument is left half-collected")
    assert not r["cal"].get("saved"), "a failed cal wrote a record"
    return f"failure cleared the collection buffer ({r['cleared']}x)"


def s_cal_no_apply():
    # The quiet one: the command returns, and correction is still off. Nothing
    # was applied, so nothing should be claimed.
    return {"acm_no_apply": True, "_target": "cal"}, []


def check_cal_no_apply(r, outdir):
    assert r["rc"] == 0, f"harness error: {r['exc']}"
    done = r["cal"].get("done") or {}
    assert done.get("ok") is False, (
        "a cal that applied nothing was reported as a success")
    assert r["cleared"] >= 1, "nothing was cleared after a cal that did not apply"
    return "cal that applied nothing reported as a failure"


def s_cal_error_surfaced():
    # The 2026-09-17 fault, reproduced: the cal is accepted, applies nothing,
    # and the only account of why is one line in the error queue. Reading that
    # queue empties it, so if the driver does not carry the text into the
    # exception it is gone for good and the dashboard can only say "correction
    # is off" - which is what sent somebody probing SCPI by hand for an hour.
    return {"acm_error": '-230,"Auto-Orientation Error for Analyzer Port(s): 1"',
            "_target": "cal"}, []


def check_cal_error_surfaced(r, outdir):
    assert r["rc"] == 0, f"harness error: {r['exc']}"
    done = r["cal"].get("done") or {}
    assert done.get("ok") is False, "a cal that applied nothing claimed success"
    msg = done.get("error") or ""
    assert "Auto-Orientation" in msg, (
        f"the instrument said why and the operator never saw it: {msg!r}")
    assert "Port(s): 1" in msg, (
        f"the faulty port was named and then dropped: {msg!r}")
    assert r["cleared"] >= 1, "nothing was cleared after a cal that did not apply"
    assert not r["cal"].get("saved"), "a failed cal wrote a record"
    return "instrument's reason reached the operator, naming the port"


def s_sweep_nominal():
    # A VNA-only capture: all four S-parameters at one position, no positioner
    # involvement at all.
    return {"_target": "sweep"}, []


def check_sweep_nominal(r, outdir):
    assert r["rc"] == 0, f"harness error: {r['exc']}"
    sw = r["sweep"]
    # Log frames are interleaved throughout; the shape that matters is the
    # measurement frames in order.
    seq = [t for t in sw["types"] if t != "log"]
    assert seq[0] == "sweep_started", seq
    assert seq[-1] == "sweep_done", seq
    got = [t["parameter"] for t in sw["traces"]]
    assert got == ["S11", "S21", "S12", "S22"], got
    for t in sw["traces"]:
        assert t["n_re"] == t["n_im"] == 11, t
        assert t["complex"], (
            f"{t['parameter']} arrived with an all-zero imaginary part - the "
            f"phase was dropped, and a Smith chart cannot be drawn from "
            f"magnitude")
    assert sw["done"]["cancelled"] is False, sw["done"]
    return f"captured {', '.join(got)} as complex pairs"


def s_sweep_vs_scan():
    return {"_target": "sweep"}, ["--while-scanning"]


def check_sweep_vs_scan(r, outdir):
    assert r["rc"] == 0, f"harness error: {r['exc']}"
    msg = r["sweep"]["refused"].get("sweep_vs_scan")
    assert msg, "a sweep started while a scan was running - both would program the VNA"
    return f"refused while scanning: {msg}"


def s_sweep_no_positioner():
    # The bench rig: a VNA and nothing else. The capture must work and
    # everything that turns the tower must refuse readably.
    return {"_target": "sweep"}, ["--no-positioner"]


def check_sweep_no_positioner(r, outdir):
    assert r["rc"] == 0, f"harness error: {r['exc']}"
    sw = r["sweep"]
    assert len(sw["traces"]) == 4, (
        f"a VNA-only rig could not complete a capture: {sw['types']}")
    assert sw["state"]["has_positioner"] is False, sw["state"]
    assert sw["pos_opened"] is False, (
        "the service opened the positioner anyway - --no-positioner reached "
        "argparse and went no further")
    assert sw["state"]["connected"] is True, (
        "a rig with no tower reported itself disconnected - the VNA was "
        f"answering: {sw['state']}")
    for name in ("seek", "jog", "zero"):
        msg = sw["refused"].get(name, "")
        assert msg, f"{name} was allowed with no positioner attached"
        assert "AttributeError" not in msg, (
            f"{name} failed on a None rather than refusing: {msg}")
        assert "no positioner" in msg, f"{name} refused unreadably: {msg}"
    return "captured without a tower; seek/jog/zero refused readably"


def s_cal_cancelled():
    # Cancel before the first command goes out. This is the only place cancel
    # can act - a running AutoCal has nowhere to poll - and the point is that
    # it leaves nothing behind.
    return {"_target": "cal"}, ["--cancel-first"]


def check_cal_cancelled(r, outdir):
    assert r["rc"] == 0, f"harness error: {r['exc']}"
    done = r["cal"].get("done") or {}
    assert done.get("cancelled") is True, done
    assert not r["cals"], (
        f"cancelled before the first step and still issued {r['cals']}")
    assert not r["cal"].get("saved"), "a cancelled cal wrote a record"
    return "cancel before the first command issued nothing and recorded nothing"


def s_cal_refused_while_scanning():
    return {"_target": "cal"}, ["--while-scanning"]


def check_cal_refused_while_scanning(r, outdir):
    assert r["rc"] == 0, f"harness error: {r['exc']}"
    assert r["cal"].get("refused") is True, (
        "a calibration started while a scan was running - two workers on "
        "single-threaded instruments, with a person at the connectors")
    assert r["cal"].get("scan_refused") is True, (
        "a scan started while a calibration was running - the tower turns with "
        "somebody's hands on the connectors")
    return "refused both ways: " + r["cal"].get("scan_reason", "")


def s_cal_sweep_mismatch():
    # Calibrate at 2-3 GHz, then scan at 5-6 GHz. The run has to say so.
    return {"_target": "cal"}, ["--then-scan"]


def check_cal_sweep_mismatch(r, outdir):
    assert r["rc"] == 0, f"harness error: {r['exc']}"
    drift = r["cal"].get("scan_mismatch")
    assert drift, ("a run at 5-6 GHz against a 2-3 GHz calibration reported no "
                   "mismatch")
    assert any("does not match the calibration" in w
               for w in r["cal"].get("warnings", [])), (
        "the mismatch was never said out loud")
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta.get("calibration"), "meta.json carries no calibration record"
    assert meta.get("cal_mismatch"), "meta.json does not record the mismatch"
    return f"{len(drift)} setting(s) flagged and written into meta.json"


def pm_on(state):
    import pattern_measure
    return pattern_measure.correction_is_on(state)


SCENARIOS = [
    ("nominal", s_nominal, check_nominal),
    ("endpoint", s_endpoint, check_endpoint),
    ("full_circle", s_full_circle, check_full_circle),
    ("split_reply", s_split_reply, check_split_reply),
    ("split_unguarded", s_split_reply_unguarded, check_split_reply_unguarded),
    ("error_retry", s_error_retry, check_error_retry),
    ("error_persistent", s_error_persistent, check_error_persistent),
    ("query_error", s_query_error, check_query_error),
    ("silent_acks", s_silent_acks, check_silent_acks),
    ("never_moves", s_never_moves, check_never_moves),
    ("stuck", s_stuck, check_stuck),
    ("late_start", s_late_start, check_late_start),
    ("short_sweep", s_short_sweep, check_short_sweep),
    ("drop_sweep", s_drop_sweep, check_drop_sweep),
    ("sweep_time", s_sweep_time_unsupported, check_sweep_time_unsupported),
    ("trigger_restore", s_trigger_restored, check_trigger_restored),
    ("service_stops", s_service_stops, check_service_stops),
    ("scan_negative_angles", s_scan_negative_angles, check_scan_negative_angles),
    ("scan_outside_limits", s_scan_outside_limits, check_scan_outside_limits),
    ("uncalibrated", s_uncalibrated, check_uncalibrated),
    ("correction_mute", s_correction_mute, check_correction_mute),
    ("meta_correction", s_meta_records_correction, check_meta_records_correction),
    ("cal_nominal", s_cal_nominal, check_cal_nominal),
    ("cal_one_port", s_cal_one_port, check_cal_one_port),
    ("cal_no_module", s_cal_no_module, check_cal_no_module),
    ("cal_unsupported", s_cal_unsupported, check_cal_unsupported),
    ("cal_fails", s_cal_fails_midway, check_cal_fails_midway),
    ("cal_no_apply", s_cal_no_apply, check_cal_no_apply),
    ("cal_error_surfaced", s_cal_error_surfaced, check_cal_error_surfaced),
    ("cal_cancelled", s_cal_cancelled, check_cal_cancelled),
    ("sweep_nominal", s_sweep_nominal, check_sweep_nominal),
    ("sweep_vs_scan", s_sweep_vs_scan, check_sweep_vs_scan),
    ("sweep_no_positioner", s_sweep_no_positioner, check_sweep_no_positioner),
    ("cal_vs_scan", s_cal_refused_while_scanning, check_cal_refused_while_scanning),
    ("cal_mismatch", s_cal_sweep_mismatch, check_cal_sweep_mismatch),
]


# ------------------------------------------------------------------- child

def _child(faults_json: str, argv: list[str]) -> int:
    """Run one scenario in this process, with the fakes installed."""
    import mock_instruments

    faults = json.loads(faults_json)
    neuter = faults.pop("_neuter_pos_guard", False)
    target = faults.pop("_target", "cli")
    state = mock_instruments.install(mock_instruments.Faults(**faults))

    import pattern_measure

    if neuter:
        # Control case: accept any numeric reply, the way the driver did before
        # the split-reply guard was added.
        import re
        pattern_measure.Positioner._POS_RE = re.compile(r"^\s*[-+]?\d")
        # Also collapse seek() to a single attempt. The retry was added later,
        # for a different fault, and it happens to rescue this one too: a bogus
        # position reads as "not where I asked", the move is re-commanded, and
        # the tower is already there. Leaving it on would make this control
        # pass whatever the guard did, which is the exact failure mode the
        # control exists to rule out. Isolate the guard by holding the second
        # defence still. Patching the config default does not work - a
        # dataclass bakes its defaults into __init__ at class creation.
        pattern_measure.Positioner.seek = (
            lambda self, deg, should_abort=None:
                self._seek_once(deg, should_abort)[0])

    exc = ""
    try:
        if target == "cal":
            rc = _run_cal(argv)
        elif target == "sweep":
            rc = _run_sweep(argv)
        elif target == "service":
            rc = _run_service(argv)
        else:
            rc = pattern_measure.main(argv)
    except SystemExit as e:                     # argparse and friends
        rc = int(e.code or 0)
    except BaseException as e:
        rc = 1
        exc = f"{type(e).__name__}: {e}"

    print("__RIGCHECK__" + json.dumps({
        "rc": rc,
        "exc": exc,
        "stops": state.stops,
        "seeks": state.seeks,
        "sweeps": state.sweeps,
        "trigger_restored": state.trigger_restored,
        "cals": state.cals,
        "cleared": state.collection_cleared,
        "cal": _CAL_RESULT.copy(),
        "sweep": _SWEEP_RESULT.copy(),
    }), flush=True)
    return 0


_SWEEP_RESULT: dict = {}


def _run_sweep(argv: list[str]) -> int:
    """Drive the VNA-only sweep worker on the fake instruments.

    Frames are captured rather than broadcast, the same way _run_cal does it,
    because what this proves is in the payload: that all four parameters were
    measured, that complex data survived to the client, and that the positioner
    was never touched.
    """
    import asyncio
    import warnings

    import chamber_service as cs

    warnings.filterwarnings("ignore", message=r"coroutine .* was never awaited")

    no_pos = "--no-positioner" in argv
    loop = asyncio.new_event_loop()
    frames: list = []
    try:
        # Through build_backend rather than constructing directly, so the CLI
        # wiring is covered too. It was not, once: --no-positioner reached
        # argparse and stopped there, build_backend opened the tower anyway,
        # and a scenario that built the backend by hand passed throughout.
        import argparse
        ns = argparse.Namespace(
            sim=False, vna="TCPIP0::127.0.0.1::5025::SOCKET",
            pos="ASRL16::INSTR", slot=1, device="A",
            no_fallback=True, no_positioner=no_pos)
        backend = cs.build_backend(ns)
        assert backend.mode == "hw", "fell back to the simulator"
        svc = cs.ChamberService(backend, loop)
        svc.push = frames.append

        refused = {}
        if "--while-scanning" in argv:
            import threading
            svc._worker = threading.Thread(target=lambda: time.sleep(2))
            svc._worker.start()
            try:
                svc.cmd_sweep({})
            except Exception as e:
                refused["sweep_vs_scan"] = str(e)
            svc._worker.join()

        if no_pos:
            # The tower is the thing that is absent; prove the refusals are
            # readable rather than an AttributeError from a None.
            for name, fn in (("seek", lambda: backend.seek(10.0)),
                             ("jog", lambda: svc.cmd_jog({"deg": 10.0})),
                             ("zero", lambda: svc.cmd_zero_here({}))):
                try:
                    fn()
                    refused[name] = ""
                except Exception as e:
                    refused[name] = f"{type(e).__name__}: {e}"

        svc._run_sweep(cs.SweepRequest(start_hz=1e9, stop_hz=2e9, points=11))

        _SWEEP_RESULT.update({
            "types": [f.get("type") for f in frames],
            "traces": [{"parameter": f["parameter"],
                        "n_re": len(f["re"]), "n_im": len(f["im"]),
                        "complex": any(v != 0.0 for v in f["im"])}
                       for f in frames if f.get("type") == "sweep_trace"],
            # Summarised, not echoed: the frame carries the frequency grid as
            # a numpy array, which the real push serialises with _json_default
            # and this harness's plain json.dumps does not.
            "started": {
                "n_freqs": len(st["freqs"]),
                "parameters": list(st["parameters"]),
                "correction": st["correction"],
            } if (st := next((f for f in frames
                              if f.get("type") == "sweep_started"), None)) else None,
            "done": next((f for f in frames
                          if f.get("type") == "sweep_done"), None),
            "refused": refused,
            "state": {k: (float(v) if hasattr(v, "dtype") else v)
                      for k, v in svc.get_state().items()},
            "pos_opened": backend.pos is not None,
        })
    finally:
        loop.close()
    return 0


_CAL_RESULT: dict = {}


def _run_cal(argv: list[str]) -> int:
    """Drive ChamberService's calibration worker on the fake instruments.

    Runs the worker on this thread so the scenario inspects a finished state,
    the same arrangement _run_service uses. Push frames are collected rather
    than dropped: what the UI is told is half of what these scenarios assert.
    """
    import asyncio
    import warnings

    import chamber_service as cs

    warnings.filterwarnings("ignore", message=r"coroutine .* was never awaited")

    outdir = Path(argv[argv.index("--outdir") + 1])
    cs.RUNS_DIR = outdir.parent

    frames: list[dict] = []
    loop = asyncio.new_event_loop()
    try:
        backend = cs.HardwareBackend("TCPIP0::127.0.0.1::5025::SOCKET",
                                     "ASRL16::INSTR", 1, "A")
        svc = cs.ChamberService(backend, loop)
        svc.push = frames.append                 # capture instead of broadcast

        if "--cancel-first" in argv:
            svc._cal_cancel.set()
        if "--while-scanning" in argv:
            # Stand in for a live scan worker without starting one: the guard
            # reads the thread's liveness, so give it a thread that is alive.
            import threading
            stop = threading.Event()
            svc._worker = threading.Thread(target=stop.wait, daemon=True)
            svc._worker.start()
            try:
                svc.cmd_start_cal({})
                _CAL_RESULT["refused"] = False
            except cs.BackendError as e:
                _CAL_RESULT["refused"] = True
                _CAL_RESULT["reason"] = str(e)
            finally:
                stop.set()

            # The other direction, and the one with a person in it: a tower
            # that starts turning while somebody has their hands on the
            # connectors. Same guard, asserted separately because a one-way
            # lock would pass the test above and still be wrong.
            stop2 = threading.Event()
            svc._worker = None
            svc._cal_worker = threading.Thread(target=stop2.wait, daemon=True)
            svc._cal_worker.start()
            try:
                svc.cmd_start_scan({"step_deg": 90.0})
                _CAL_RESULT["scan_refused"] = False
            except cs.BackendError as e:
                _CAL_RESULT["scan_refused"] = True
                _CAL_RESULT["scan_reason"] = str(e)
            finally:
                stop2.set()
        else:
            ports = ((int(argv[argv.index("--ports") + 1]),)
                     if "--ports" in argv else (1, 2))
            svc._run_cal(cs.CalRequest(reference_plane="rigcheck", ports=ports))

        done = [f for f in frames if f.get("type") == "cal_done"]
        _CAL_RESULT.update({
            "frames": [f.get("type") for f in frames],
            "done": done[-1] if done else None,
            "record": svc.calibration,
            "saved": (cs.RUNS_DIR / cs.CAL_NAME).is_file(),
        })

        if "--then-scan" in argv:
            # A run taken at a different sweep than the calibration has to say
            # so; this is the assertion the whole cal record exists for.
            frames.clear()
            svc._run_scan(cs.ScanRequest(start_deg=0.0, stop_deg=90.0,
                                         step_deg=90.0, points=21,
                                         start_hz=5.0e9, stop_hz=6.0e9,
                                         name=outdir.name))
            started = [f for f in frames if f.get("type") == "scan_started"]
            _CAL_RESULT["scan_mismatch"] = (started[0].get("cal_mismatch")
                                            if started else None)
            _CAL_RESULT["warnings"] = [f["message"] for f in frames
                                       if f.get("type") == "log"
                                       and f.get("level") == "warn"]
    finally:
        loop.close()
    return 0


def _run_service(argv: list[str]) -> int:
    """Drive ChamberService's scan worker directly, on the fake instruments.

    The worker is called on this thread rather than spawned: the scenario wants
    the failure path to have finished before it inspects what happened. The
    loop is created but never run, so push() queues frames nobody reads - which
    is all this scenario needs, since it asserts on the instruments, not the
    socket.
    """
    import asyncio
    import warnings

    import chamber_service as cs

    # push() hands coroutines to a loop that never runs, so they are never
    # awaited. Expected here, and only noise in the output.
    warnings.filterwarnings("ignore", message=r"coroutine .* was never awaited")

    outdir = Path(argv[argv.index("--outdir") + 1])
    cs.RUNS_DIR = outdir.parent
    step = float(argv[argv.index("--step") + 1])

    def opt(flag, default):
        return float(argv[argv.index(flag) + 1]) if flag in argv else default

    start, stop = opt("--start", 0.0), opt("--stop", 270.0)

    loop = asyncio.new_event_loop()
    try:
        backend = cs.HardwareBackend("TCPIP0::127.0.0.1::5025::SOCKET",
                                     "ASRL16::INSTR", 1, "A")
        svc = cs.ChamberService(backend, loop)
        svc._run_scan(cs.ScanRequest(start_deg=start, stop_deg=stop,
                                     step_deg=step, points=21,
                                     name=outdir.name))
    finally:
        loop.close()
    return 0


# -------------------------------------------------------------------- main

def run_one(name: str, setup, check, verbose: bool) -> tuple[bool, str]:
    faults, argv = setup()
    with tempfile.TemporaryDirectory(prefix=f"rigcheck_{name}_") as td:
        outdir = Path(td) / "run"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "rigcheck.py"), "--child",
             json.dumps(faults), "--"] + argv + ["--outdir", str(outdir)],
            cwd=ROOT, capture_output=True, text=True)

        payload, out = {}, []
        for line in proc.stdout.splitlines():
            if line.startswith("__RIGCHECK__"):
                payload = json.loads(line[len("__RIGCHECK__"):])
            else:
                out.append(line)
        r = {"rc": payload.get("rc", proc.returncode),
             "exc": payload.get("exc", ""),
             "cals": payload.get("cals", []),
             "cleared": payload.get("cleared", 0),
             "cal": payload.get("cal", {}),
             "sweep": payload.get("sweep", {}),
             "stops": payload.get("stops", []),
             "seeks": payload.get("seeks", []),
             "sweeps": payload.get("sweeps", 0),
             "trigger_restored": payload.get("trigger_restored", False),
             "out": "\n".join(out),
             "err": proc.stderr}

        if verbose:
            print(r["out"])
            if r["err"]:
                print("--- stderr ---\n" + r["err"], file=sys.stderr)
        try:
            return True, check(r, outdir)
        except AssertionError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--child":
        sep = argv.index("--")
        return _child(argv[1], argv[sep + 1:])

    wanted = set(argv)
    picked = [s for s in SCENARIOS if not wanted or s[0] in wanted]
    if wanted and not picked:
        print(f"no such scenario. known: {', '.join(s[0] for s in SCENARIOS)}",
              file=sys.stderr)
        return 2

    failures = 0
    for name, setup, check in picked:
        ok, detail = run_one(name, setup, check, verbose=bool(wanted))
        print(f"{'PASS' if ok else 'FAIL'}  {name:<18}  {detail}")
        if not ok:
            failures += 1

    print(f"\n{len(picked) - failures}/{len(picked)} scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
