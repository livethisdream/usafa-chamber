#!/usr/bin/env python3
"""Scenario runner for the acquisition stack, against mock instruments.

Each scenario pins one of the failure modes found in review, so a regression
shows up here instead of on the chamber floor.

    python3 -m acquisition.dryrun            # all scenarios, PASS/FAIL
    python3 -m acquisition.dryrun nominal    # one scenario, verbose

Scenarios run as subprocesses so each gets a clean import.
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMON = ["--vna", "TCPIP0::127.0.0.1::5025::SOCKET",
          "--pos", "TCPIP0::192.168.1.50::5025::SOCKET",
          "--points", "21"]


def _read_angles(outdir: Path) -> list[float]:
    rows = list(csv.DictReader((outdir / "pattern.csv").open()))
    seen, order = set(), []
    for r in rows:
        a = float(r["angle_cmd_deg"])
        if a not in seen:
            seen.add(a)
            order.append(a)
    return order


# ---------------------------------------------------------------- scenarios

def s_nominal(_):
    return dict(faults={}, argv=COMMON + ["--step", "30", "--to-deg", "355"])


def check_nominal(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']} / {r['exc']}"
    angles = _read_angles(outdir)
    assert max(angles) <= 355.0, f"overshot stop angle: {max(angles)}"
    assert 360.0 not in angles, "measured 360 deg, a duplicate of 0"
    meta = json.loads((outdir / "run_meta.json").read_text())
    assert meta["closure"] is not None, "closure check missing"
    assert (outdir / "pattern.png").exists(), "no plot"
    return (f"{len(angles)} angles, last {max(angles):g}, closure "
            f"{meta['closure']['max_abs_delta_db']:.3f} dB")


def s_partial(_):
    return dict(faults={}, argv=COMMON + ["--step", "15", "--from-deg=-90",
                                          "--to-deg", "90"])


def check_partial(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']} / {r['exc']}"
    angles = _read_angles(outdir)
    assert min(angles) == -90.0 and max(angles) == 90.0, angles
    assert (outdir / "pattern.png").exists()
    return f"{len(angles)} angles, -90..90, trace left open"


def s_indivisible(_):
    return dict(faults={}, argv=COMMON + ["--step", "7", "--to-deg", "90"])


def check_indivisible(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']} / {r['exc']}"
    angles = _read_angles(outdir)
    assert max(angles) == 84.0, f"expected last angle 84, got {max(angles)}"
    return "step 7 over 0..90 stops at 84, does not overshoot to 91"


def s_early_opc(_):
    return dict(faults={"early_opc": True},
                argv=COMMON + ["--step", "45", "--move-timeout", "5"])


def check_early_opc(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']} / {r['exc']}"
    rows = list(csv.DictReader((outdir / "pattern.csv").open()))
    worst = max(abs(float(x["angle_cmd_deg"]) - float(x["angle_actual_deg"]))
                for x in rows)
    assert worst <= 0.5, f"recorded a sweep {worst} deg off target"
    return f"controller lied about motion-complete; worst error {worst:.2f} deg"


def s_lagging(_):
    return dict(faults={"early_opc": True, "lag_deg": 3.0},
                argv=COMMON + ["--step", "45", "--move-timeout", "2"])


def check_lagging(r, outdir):
    assert r["rc"] != 0 or r["exc"], "a 3 deg misregistration was accepted silently"
    assert r["stops"], "axis was not halted"
    return f"failed loudly ({r['exc'] or 'rc=' + str(r['rc'])}), STOP issued"


def s_short_sweep(_):
    return dict(faults={"short_sweep": 3},
                argv=COMMON + ["--step", "45", "--move-timeout", "5"])


def check_short_sweep(r, outdir):
    assert r["rc"] == 0, f"truncated sweep was fatal: {r['exc']}"
    return "truncated sweep re-read, scan completed"


def s_drop_sweep(_):
    return dict(faults={"drop_sweep": 3},
                argv=COMMON + ["--step", "45", "--move-timeout", "5"])


def check_drop_sweep(r, outdir):
    assert r["exc"], "expected the comms drop to surface"
    assert r["stops"], "comms dropped and the axis was left turning"
    return f"{r['exc']} surfaced, STOP issued at {r['stops']}"


def s_poll_drops(_):
    return dict(faults={"drop_polls": 3},
                argv=COMMON + ["--step", "45", "--move-timeout", "5"])


def check_poll_drops(r, outdir):
    assert r["rc"] == 0, f"transient poll failures aborted the run: {r['exc']}"
    return "3 dropped polls absorbed, scan completed"


def s_backlash(_):
    return dict(faults={}, argv=COMMON + ["--step", "45", "--backlash", "2",
                                          "--move-timeout", "5"])


def check_backlash(r, outdir):
    assert r["rc"] == 0, f"exit {r['rc']} / {r['exc']}"
    return "lash takeup on reversals, scan completed"


SCENARIOS = {
    "nominal": (s_nominal, check_nominal, "full circle, no overshoot"),
    "partial": (s_partial, check_partial, "partial cut is not closed"),
    "indivisible": (s_indivisible, check_indivisible, "step does not divide span"),
    "early_opc": (s_early_opc, check_early_opc, "controller reports done too soon"),
    "lagging": (s_lagging, check_lagging, "axis settles off target"),
    "short_sweep": (s_short_sweep, check_short_sweep, "truncated VNA response"),
    "drop_sweep": (s_drop_sweep, check_drop_sweep, "comms drop mid-scan"),
    "poll_drops": (s_poll_drops, check_poll_drops, "transient poll failures"),
    "backlash": (s_backlash, check_backlash, "backlash takeup enabled"),
}


# ------------------------------------------------------------------ runners

def run_one(name: str, outdir: Path) -> dict:
    """In-process: patch pyvisa, run the CLI, report what happened."""
    from . import mock_instruments

    spec = SCENARIOS[name][0](outdir)
    state = mock_instruments.install(mock_instruments.Faults(**spec["faults"]))
    from . import pattern_measure

    argv = spec["argv"] + ["--outdir", str(outdir)]
    rc, exc = 0, None
    try:
        rc = pattern_measure.main(argv)
    except BaseException as e:                      # noqa: BLE001 - reporting
        rc, exc = 1, f"{type(e).__name__}: {str(e)[:80]}"
    return {"rc": rc, "exc": exc, "stops": state.stops, "sweeps": state.sweeps}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in SCENARIOS:
        name = sys.argv[1]
        outdir = Path(sys.argv[2]) if len(sys.argv) > 2 else \
            Path(tempfile.mkdtemp()) / name
        r = run_one(name, outdir)
        print("\n__RESULT__" + json.dumps(r))
        return 0

    width = max(len(n) for n in SCENARIOS)
    failures = 0
    tmp = Path(tempfile.mkdtemp(prefix="pattern_dryrun_"))
    for name, (_, check, blurb) in SCENARIOS.items():
        outdir = tmp / name
        proc = subprocess.run(
            [sys.executable, "-m", "acquisition.dryrun", name, str(outdir)],
            capture_output=True, text=True, timeout=300, cwd=ROOT)
        tag = [ln for ln in proc.stdout.splitlines() if ln.startswith("__RESULT__")]
        if not tag:
            print(f"FAIL  {name:<{width}}  runner crashed\n{proc.stdout[-500:]}"
                  f"\n{proc.stderr[-800:]}")
            failures += 1
            continue
        r = json.loads(tag[0][len("__RESULT__"):])
        try:
            detail = check(r, outdir)
            print(f"PASS  {name:<{width}}  {detail}")
        except AssertionError as e:
            print(f"FAIL  {name:<{width}}  {blurb}: {e}")
            failures += 1

    print(f"\n{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios passed")
    print(f"artifacts in {tmp}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
