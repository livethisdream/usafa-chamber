#!/usr/bin/env python3
"""
Step-and-measure antenna pattern acquisition - command line front end.

    python3 -m acquisition.pattern_measure --help

Hardware:
  VNA        : Copper Mountain A2202-Fx via S2VNA socket server (SCPI over TCP:5025)
  Positioner : ETS-Lindgren EMControl 7006-001 card in an EMCenter chassis

This module is a thin shell: it parses arguments, opens the instruments, and
renders the engine's events as stdout lines. The measurement lives in
engine.py, which the service drives the same way.

VERIFY BEFORE FIRST RUN
  * EMCenter command mnemonics and the slot/device prefix are centralized in
    PositionerCmds (config.py). Cross-check them against ETS-Lindgren manual
    399342 for your firmware.
  * Confirm the turntable is in the intended continuous / non-continuous mode.
    Continuous ignores software limits and will happily wind up your RF cable
    if there is no rotary joint.
  * The S2VNA socket server is off by default; enable it in the application and
    make sure the application is running before connecting.
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

import pyvisa

from .config import PositionerCmds, PositionerConfig, ScanConfig, VnaConfig
from .engine import ScanEngine
from .instruments import Positioner, Vna


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m acquisition.pattern_measure",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--vna", default=VnaConfig.resource)
    p.add_argument("--pos", default=PositionerConfig.resource)
    p.add_argument("--slot", type=int, default=PositionerCmds.slot)
    p.add_argument("--device", default=PositionerCmds.device, choices=["A", "B"])
    p.add_argument("--start-ghz", type=float, default=VnaConfig.start_hz / 1e9)
    p.add_argument("--stop-ghz", type=float, default=VnaConfig.stop_hz / 1e9)
    p.add_argument("--points", type=int, default=VnaConfig.points)
    p.add_argument("--ifbw", type=float, default=VnaConfig.if_bw_hz)
    p.add_argument("--power", type=float, default=VnaConfig.power_dbm)
    p.add_argument("--param", default=VnaConfig.parameter)
    p.add_argument("--aux-param", default=None,
                   help="second parameter to record, e.g. S11")
    p.add_argument("--averaging", type=int, default=0,
                   help="sweeps averaged per point (0 = averaging off)")
    p.add_argument("--step", type=float, default=ScanConfig.step_deg)
    p.add_argument("--from-deg", type=float, default=ScanConfig.start_deg)
    p.add_argument("--to-deg", type=float, default=ScanConfig.stop_deg)
    p.add_argument("--cut-ghz", type=float, default=None)
    p.add_argument("--db-floor", type=float, default=None,
                   help="polar plot radial floor in dB (default: fit to data)")
    p.add_argument("--backlash", type=float, default=PositionerConfig.backlash_deg,
                   help="degrees of lash takeup when a move reverses direction")
    p.add_argument("--tol", type=float, default=PositionerConfig.position_tol_deg,
                   help="position tolerance in degrees")
    p.add_argument("--move-timeout", type=float,
                   default=PositionerConfig.move_timeout_s,
                   help="seconds to wait for a single move before aborting")
    p.add_argument("--s2p", action="store_true", help="also write per-angle Touchstone")
    p.add_argument("--no-closure", action="store_true",
                   help="skip the end-of-run repeat of the start angle")
    p.add_argument("--no-return-home", action="store_true")
    p.add_argument("--zero-here", action="store_true",
                   help="define the current mechanical position as 0 deg, then scan")
    p.add_argument("--outdir", type=Path, default=Path("./pattern_run"))
    return p


def configs_from_args(a: argparse.Namespace):
    vcfg = VnaConfig(resource=a.vna, start_hz=a.start_ghz * 1e9,
                     stop_hz=a.stop_ghz * 1e9, points=a.points,
                     if_bw_hz=a.ifbw, power_dbm=a.power, parameter=a.param,
                     aux_parameter=a.aux_param, averaging=a.averaging)
    pcfg = PositionerConfig(resource=a.pos, position_tol_deg=a.tol,
                            backlash_deg=a.backlash, move_timeout_s=a.move_timeout)
    cmds = PositionerCmds(slot=a.slot, device=a.device)
    scan = ScanConfig(start_deg=a.from_deg, stop_deg=a.to_deg, step_deg=a.step,
                      return_home=not a.no_return_home,
                      closure_check=not a.no_closure,
                      cut_freq_hz=(a.cut_ghz * 1e9 if a.cut_ghz else None),
                      db_floor=a.db_floor, write_s2p=a.s2p, outdir=a.outdir)
    return vcfg, pcfg, cmds, scan


def make_printer():
    """Render engine events as stdout lines."""
    def printer(ev: dict) -> None:
        t = ev.get("type")
        if t == "log":
            stream = sys.stderr if ev["level"] in ("warn", "error") else sys.stdout
            tag = "" if ev["level"] == "info" else f"{ev['level']}: "
            print(f"[{ev['source']}] {tag}{ev['message']}", file=stream)
        elif t == "ready":
            print(f"      {len(ev['freq_hz'])} pts, "
                  f"{ev['freq_hz'][0]/1e9:.3f}-{ev['freq_hz'][-1]/1e9:.3f} GHz, "
                  f"sweep {ev['sweep_time_s']*1e3:.1f} ms")
        elif t == "phase" and ev["phase"] == "scanning":
            print(f"\nscanning {ev['detail']}\n")
        elif t == "point":
            print(f"  {ev['angle_cmd']:7.2f} deg (read {ev['angle_actual']:7.2f})  "
                  f"peak {ev['peak_db']:7.2f} dB", flush=True)
        elif t == "closure":
            print(f"closure: {ev['max_abs_delta_db']:.3f} dB max drift, "
                  f"{ev['mean_delta_db']:+.3f} dB mean")
        elif t == "phase" and ev["phase"] == "homing":
            print("returning to start position...")
        elif t == "done":
            print(f"\n{'aborted' if ev['aborted'] else 'complete'} - "
                  f"{ev['angles_measured']} angles")
            for f in ev["files"]:
                print(f"wrote {f}")
    return printer


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    vcfg, pcfg, cmds, scan = configs_from_args(a)

    if len(scan.angles()) == 0:
        print("no angles to measure", file=sys.stderr)
        return 2

    rm = pyvisa.ResourceManager()
    vna = pos = None
    abort = threading.Event()
    try:
        vna = Vna(rm, vcfg)
        pos = Positioner(rm, pcfg, cmds)
        if a.zero_here:
            pos.zero_here()
        ScanEngine(vna, pos, scan, emit=make_printer(), abort=abort).run()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        # Any exit path - clean, error, or Ctrl-C - halts the axis before the
        # socket closes. Otherwise a comms error leaves the table turning with
        # nobody listening.
        if pos is not None:
            try:
                pos.stop()
            except Exception as exc:
                print(f"[pos] STOP failed on exit: {exc}", file=sys.stderr)
            pos.close()
        if vna is not None:
            vna.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
