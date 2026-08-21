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
    assert "resyncing and retrying once" in r["err"], "no retry was logged"
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
    assert "resyncing and retrying once" in r["err"], "no query retry logged"
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


def check_trigger_restored(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']}: {r['exc']}"
    assert r["trigger_restored"], (
        "the VNA was left in TRIG:SOUR BUS - the front panel stays frozen")
    return "trigger state handed back on close"


def s_trigger_restored():
    return {}, COMMON + ["--step", "180", "--to-deg", "180"]


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

    exc = ""
    try:
        rc = _run_service(argv) if target == "service" else pattern_measure.main(argv)
    except SystemExit as e:                     # argparse and friends
        rc = int(e.code or 0)
    except BaseException as e:
        rc = 1
        exc = f"{type(e).__name__}: {e}"

    print("__RIGCHECK__" + json.dumps({
        "rc": rc,
        "exc": exc,
        "stops": state.stops,
        "sweeps": state.sweeps,
        "trigger_restored": state.trigger_restored,
    }), flush=True)
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

    loop = asyncio.new_event_loop()
    try:
        backend = cs.HardwareBackend("TCPIP0::127.0.0.1::5025::SOCKET",
                                     "ASRL16::INSTR", 1, "A")
        svc = cs.ChamberService(backend, loop)
        svc._run_scan(cs.ScanRequest(start_deg=0.0, stop_deg=270.0,
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
             "stops": payload.get("stops", []),
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
