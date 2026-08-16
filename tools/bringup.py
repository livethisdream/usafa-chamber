#!/usr/bin/env python3
"""Staged hardware bring-up. Nothing moves unless you say so.

The dry-run suites prove the state machine; they cannot prove the protocol
assumptions, because the mocks encode the same assumptions the code does. This
script is the other half: it talks to the real instruments one at a time, in
increasing order of consequence, and reports exactly what came back.

    python3 tools/bringup.py --vna TCPIP0::127.0.0.1::5025::SOCKET \
                             --pos TCPIP0::192.168.1.50::5025::SOCKET \
                             --slot 5 --device A

Stages 1-3 are read-only - they query, they never command motion. Stages 4 and
5 turn the table and require --allow-motion.

    --probe          when the positioner does not answer, try the other
                     plausible prefix and termination combinations and report
                     which one worked
    --probe-ports    TCP-connect to a few plausible ports on the positioner
                     host to find which one is listening
    --allow-motion   permit stages 4 and 5
    --report FILE    also write the transcript, for pasting into an issue

Run stage 4 with EMControl's simulation mode ON the first time. It exercises
the query semantics with nothing physically turning.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Ports worth trying on an EMCenter when the configured one is silent. This is
# a short courtesy list for your own instrument, not a scanner.
CANDIDATE_PORTS = [5025, 4000, 23, 1234, 10001]

TERMINATIONS = [("\r", "\n"), ("\n", "\n"), ("\r\n", "\r\n"), ("\n", "\r\n")]


class Report:
    def __init__(self):
        self.lines: list[str] = []
        self.failed = 0
        self.skipped = 0

    def say(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)

    def stage(self, n: int, title: str) -> None:
        self.say("")
        self.say(f"── stage {n}: {title} " + "─" * max(0, 46 - len(title)))

    def ok(self, label: str, detail: str = "") -> None:
        self.say(f"  PASS  {label}" + (f"  {detail}" if detail else ""))

    def bad(self, label: str, detail: str = "") -> None:
        self.failed += 1
        self.say(f"  FAIL  {label}" + (f"  {detail}" if detail else ""))

    def skip(self, label: str, why: str) -> None:
        self.skipped += 1
        self.say(f"  SKIP  {label}  ({why})")

    def note(self, text: str) -> None:
        self.say(f"        {text}")


def _host_of(resource: str) -> str | None:
    parts = resource.split("::")
    return parts[1] if len(parts) > 1 else None


# ---------------------------------------------------------------- stages

def stage_env(rep: Report, mock: bool) -> "object|None":
    rep.stage(0, "VISA environment")
    try:
        import pyvisa
    except ImportError:
        rep.bad("pyvisa import", "pip install pyvisa")
        return None

    if mock:
        from acquisition import mock_instruments
        mock_instruments.install()
        rep.ok("mock instruments installed", "no hardware will be contacted")

    try:
        rm = pyvisa.ResourceManager()
    except Exception as exc:
        rep.bad("open ResourceManager", str(exc)[:90])
        rep.note("no VISA backend found - try: pip install pyvisa-py")
        return None
    rep.ok("ResourceManager", type(rm).__name__)

    if not mock:
        try:
            found = rm.list_resources()
            rep.ok("list_resources", f"{len(found)} found")
            for r in found[:12]:
                rep.note(r)
        except Exception as exc:
            rep.note(f"list_resources unavailable: {str(exc)[:70]}")
            rep.note("normal for socket-only backends; not a problem")
    return rm


def stage_vna(rep: Report, rm, resource: str, sweep: bool):
    rep.stage(1, "VNA link (read-only)")
    from acquisition.config import VnaConfig
    from acquisition.instruments import Vna

    try:
        vna = Vna(rm, VnaConfig(resource=resource))
    except Exception as exc:
        rep.bad("open resource", str(exc)[:90])
        rep.note("is the S2VNA application running with its socket server "
                 "enabled? it is off by default")
        return None

    try:
        idn = vna.idn()
        rep.ok("*IDN?", idn)
    except Exception as exc:
        rep.bad("*IDN?", str(exc)[:90])
        rep.note("socket opened but the instrument did not answer - wrong port, "
                 "or the socket server is not enabled")
        vna.close()
        return None

    for label, q in (("correction state", vna.correction_state),
                     ("sweep time", vna.sweep_time_s)):
        try:
            rep.ok(label, str(q()))
        except Exception as exc:
            rep.note(f"{label} unavailable: {str(exc)[:60]}")

    if sweep:
        try:
            vna.configure()
            vna.warm_up()
            f = vna.frequencies()
            rep.ok("configure + one sweep",
                   f"{len(f)} points, {f[0]/1e9:.4f}-{f[-1]/1e9:.4f} GHz")
            t = vna.measure()
            par = list(t)[0]
            import numpy as np
            mag = 20 * np.log10(np.maximum(np.abs(t[par]), 1e-15))
            rep.ok("read trace", f"{par} peak {mag.max():.2f} dB, "
                                 f"min {mag.min():.2f} dB")
        except Exception as exc:
            rep.bad("sweep", str(exc)[:90])
            rep.note("the SCPI verbs in instruments.py may not match this "
                     "firmware; check against the CMT programming manual")
    else:
        rep.skip("configure + sweep", "pass --sweep to exercise it")
    return vna


def _try_positioner(rm, resource: str, prefix: str, wterm: str, rterm: str):
    """One prefix/termination combination. Returns the reply or raises."""
    io = rm.open_resource(resource)
    io.timeout = 4000
    io.write_termination = wterm
    io.read_termination = rterm
    try:
        return io.query(prefix + "CP?").strip(), io
    except Exception:
        try:
            io.close()
        except Exception:
            pass
        raise


def stage_pos_link(rep: Report, rm, resource: str, slot: int, device: str,
                   probe: bool, probe_ports: bool):
    rep.stage(2, "positioner link (read-only)")
    prefix = f"{slot}{device}:"
    rep.note(f"trying prefix {prefix!r}, write CR / read LF")

    try:
        reply, io = _try_positioner(rm, resource, prefix, "\r", "\n")
        rep.ok("CP? position query", f"{reply!r}")
        try:
            rep.ok("parses as degrees", f"{float(reply):.2f}")
        except ValueError:
            rep.bad("parses as degrees",
                    f"got {reply!r} - position() will raise on this")
        return io, prefix, ("\r", "\n")
    except Exception as exc:
        rep.bad("CP? position query", str(exc)[:90])

    if probe_ports:
        host = _host_of(resource)
        if host:
            rep.note(f"probing ports on {host}")
            for port in CANDIDATE_PORTS:
                try:
                    socket.create_connection((host, port), 1.5).close()
                    rep.note(f"  port {port}: accepts connections")
                except OSError:
                    rep.note(f"  port {port}: no")

    if not probe:
        rep.note("re-run with --probe to try other prefixes and terminations")
        return None, prefix, None

    rep.note("probing prefix and termination combinations")
    candidates = [prefix, f"{slot}{device.lower()}:", f"{slot}:", ""]
    for pfx in candidates:
        for wterm, rterm in TERMINATIONS:
            try:
                reply, io = _try_positioner(rm, resource, pfx, wterm, rterm)
            except Exception:
                continue
            rep.ok("probe hit",
                   f"prefix={pfx!r} write={wterm!r} read={rterm!r} -> {reply!r}")
            rep.note("set these in PositionerCmds / PositionerConfig "
                     "(acquisition/config.py)")
            return io, pfx, (wterm, rterm)
    rep.bad("probe", "no combination answered")
    rep.note("check the port first (--probe-ports), then the EMCenter's "
             "interface settings, then manual 399342")
    return None, prefix, None


def stage_motion_state(rep: Report, io, prefix: str):
    rep.stage(3, "motion-state semantics (read-only)")
    if io is None:
        rep.skip("*OPC? while stationary", "no positioner link")
        return
    try:
        reply = io.query(prefix + "*OPC?").strip()
    except Exception as exc:
        rep.bad("*OPC? while stationary", str(exc)[:90])
        rep.note("the settle logic depends on this query; without it the scan "
                 "cannot tell moving from arrived")
        return

    rep.ok("*OPC? while stationary", f"{reply!r}")
    if reply in ("1", "+1"):
        rep.note("matches the assumption: 1 = motion complete")
    else:
        rep.bad("unexpected reply",
                f"expected '1' while stationary, got {reply!r}")
        rep.note("Positioner.in_motion() treats anything but '1'/'+1' as "
                 "moving, so this would hang every seek. Adjust "
                 "PositionerCmds.opc or in_motion() to match.")


def stage_jog(rep: Report, io, prefix: str, degrees: float, allow: bool):
    rep.stage(4, f"jog {degrees:+.1f} deg  [MOVES THE TABLE]")
    if not allow:
        rep.skip("jog", "pass --allow-motion")
        return
    if io is None:
        rep.skip("jog", "no positioner link")
        return

    try:
        start = float(io.query(prefix + "CP?").strip())
    except Exception as exc:
        rep.bad("read start position", str(exc)[:90])
        return
    target = start + degrees
    rep.note(f"from {start:.2f} to {target:.2f}; polling OPC and CP")

    try:
        io.write(f"{prefix}SK {target:.1f}")
        saw_moving = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            opc = io.query(prefix + "*OPC?").strip()
            pos = io.query(prefix + "CP?").strip()
            rep.note(f"  t+{time.monotonic() % 100:5.1f}  OPC={opc!r:6} CP={pos!r}")
            if opc not in ("1", "+1"):
                saw_moving = True
            if opc in ("1", "+1") and abs(float(pos) - target) < 0.5:
                break
            time.sleep(0.25)
        else:
            rep.bad("jog", "did not report arrival within 30 s")
            io.write(prefix + "ST")
            return

        rep.ok("jog completed", f"final {pos!r}")
        if saw_moving:
            rep.ok("OPC reports motion", "0 seen while moving, as assumed")
        else:
            rep.bad("OPC never reported motion",
                    "it read complete the whole time")
            rep.note("this is the failure the dryrun 'early_opc' scenario "
                     "models. The scan still converges because it also "
                     "requires an in-tolerance readback, but seeks will be "
                     "slower and less certain.")

        io.write(f"{prefix}SK {start:.1f}")
        rep.note(f"returning to {start:.2f}")
    except Exception as exc:
        rep.bad("jog", str(exc)[:90])
        try:
            io.write(prefix + "ST")
        except Exception:
            pass


def stage_miniscan(rep: Report, args, allow: bool):
    rep.stage(5, "four-point scan  [MOVES THE TABLE]")
    if not allow:
        rep.skip("mini scan", "pass --allow-motion")
        return
    rep.note("RF cable slack? nothing precious connected? this turns the table")

    import pyvisa

    from acquisition.config import (PositionerCmds, PositionerConfig,
                                    ScanConfig, VnaConfig)
    from acquisition.engine import ScanEngine
    from acquisition.instruments import Positioner, Vna
    from acquisition.pattern_measure import make_printer

    rm = pyvisa.ResourceManager()
    vna = pos = None
    try:
        vna = Vna(rm, VnaConfig(resource=args.vna, points=21))
        pos = Positioner(rm, PositionerConfig(resource=args.pos),
                         PositionerCmds(slot=args.slot, device=args.device))
        scan = ScanConfig(start_deg=0.0, stop_deg=15.0, step_deg=5.0,
                          closure_check=False, write_plot=False,
                          outdir=Path(args.outdir))
        ScanEngine(vna, pos, scan, emit=make_printer()).run()
        rep.ok("mini scan", f"wrote {args.outdir}")
    except Exception as exc:
        rep.bad("mini scan", f"{type(exc).__name__}: {str(exc)[:80]}")
        rep.note(traceback.format_exc().splitlines()[-1])
    finally:
        if pos is not None:
            try:
                pos.stop()
            except Exception:
                pass
            pos.close()
        if vna is not None:
            vna.close()


# ------------------------------------------------------------------ main

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python3 tools/bringup.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--vna", default="TCPIP0::127.0.0.1::5025::SOCKET")
    p.add_argument("--pos", default="TCPIP0::192.168.1.50::5025::SOCKET")
    p.add_argument("--slot", type=int, default=5)
    p.add_argument("--device", default="A", choices=["A", "B"])
    p.add_argument("--sweep", action="store_true",
                   help="stage 1 also configures and takes one sweep")
    p.add_argument("--probe", action="store_true")
    p.add_argument("--probe-ports", action="store_true")
    p.add_argument("--allow-motion", action="store_true")
    p.add_argument("--jog", type=float, default=5.0,
                   help="degrees to jog in stage 4 (default 5)")
    p.add_argument("--outdir", default="./bringup_run")
    p.add_argument("--report", type=Path, default=None)
    p.add_argument("--mock", action="store_true",
                   help="run against fake instruments, to check this script")
    a = p.parse_args(argv)

    rep = Report()
    rep.say("chamber bring-up")
    rep.say(f"  VNA        {a.vna}")
    rep.say(f"  positioner {a.pos}   prefix {a.slot}{a.device}:")
    if not a.allow_motion:
        rep.say("  motion     LOCKED (stages 4-5 skipped; --allow-motion to run)")

    io = None
    try:
        rm = stage_env(rep, a.mock)
        if rm is None:
            rep.say("\nstopped: no usable VISA backend")
            return 1

        vna = stage_vna(rep, rm, a.vna, a.sweep)
        if vna is not None:
            vna.close()

        io, prefix, term = stage_pos_link(rep, rm, a.pos, a.slot, a.device,
                                          a.probe, a.probe_ports)
        stage_motion_state(rep, io, prefix)
        stage_jog(rep, io, prefix, a.jog, a.allow_motion)
    finally:
        # Never leave the axis under a standing move command.
        if io is not None:
            try:
                io.write(f"{a.slot}{a.device}:ST")
            except Exception:
                pass
            try:
                io.close()
            except Exception:
                pass

    stage_miniscan(rep, a, a.allow_motion)

    rep.say("")
    rep.say(f"{rep.failed} failed, {rep.skipped} skipped")
    if rep.failed:
        rep.say("")
        rep.say("Positioner mnemonics and terminations live in one place:")
        rep.say("  acquisition/config.py -> PositionerCmds, PositionerConfig")
        rep.say("Cross-check against ETS-Lindgren manual 399342 for your firmware.")

    if a.report:
        a.report.write_text("\n".join(rep.lines) + "\n")
        print(f"\nwrote {a.report}")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
