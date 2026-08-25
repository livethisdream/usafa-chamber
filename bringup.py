#!/usr/bin/env python3
"""Staged hardware bring-up. Nothing moves unless you say so.

`rigcheck.py` proves the state machine, but it cannot prove the protocol
assumptions — the fakes encode the same assumptions the drivers do. This is the
other half: it talks to the real instruments one at a time, in increasing order
of consequence, and reports exactly what came back.

    python bringup.py --pos ASRL16::INSTR --slot 1 --device A
    python bringup.py --mock                       # exercise the script itself

Stages 0-4 and 7 are read-only. They query and never command motion. Stages 5
and 6 turn the tower and require --allow-motion.

    --probe          when the positioner is silent, try the other plausible
                     framing combinations and report which one answered
    --link-reads N   how many position reads stage 4 uses (default 200)
    --allow-motion   permit stages 5 and 6
    --report FILE    also write the transcript, for pasting into the note
    --mock           run against mock_instruments instead of hardware

This rig's command set was verified on 2026-08-20 and is recorded in
`PositionerCmds`, so most stages here confirm rather than discover — the point
is to notice *drift*, on a firmware change or a second card. Where a stage
knows the right answer it says so, and a disagreement is a FAIL rather than a
finding.

Stage 4 is the exception, and the reason to reach for this script when nothing
is obviously broken: it measures the FTDI link's error rate. One 72-point run
logged 3 ERROR 1 retries and 2 split position reads, roughly 7% of exchanges;
a later rotation test logged none. Handled in software, physical cause still
open. This turns that into a number you can compare between cables.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Framing worth trying when the configured setting is silent. The USB port
# wants 115200 8N1; the 9600 7O1 in ETS-Lindgren's docs describes the legacy
# Holaday-compatible rear port and times out here.
FRAMINGS = [(115200, 8, "none"), (9600, 7, "odd"), (9600, 8, "none"),
            (57600, 8, "none"), (19200, 8, "none")]

# Queries this firmware rejects. Confirming they still fail is worth a line:
# if one starts answering, the front-panel-only workaround can be dropped.
KNOWN_UNSUPPORTED = ["SP?", "UL?", "LL?", "MODE?"]

POS_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?\s*DEG", re.I)


class Report:
    def __init__(self):
        self.lines: list[str] = []
        self.failed = 0
        self.skipped = 0

    def say(self, text: str = "") -> None:
        print(text, flush=True)
        self.lines.append(text)

    def stage(self, n: int, title: str) -> None:
        self.say("")
        self.say(f"-- stage {n}: {title} " + "-" * max(0, 48 - len(title)))

    def ok(self, label: str, detail: str = "") -> None:
        self.say(f"  PASS  {label}" + (f"  {detail}" if detail else ""))

    def bad(self, label: str, detail: str = "") -> None:
        self.failed += 1
        self.say(f"  FAIL  {label}" + (f"  {detail}" if detail else ""))

    def note(self, label: str, detail: str = "") -> None:
        self.say(f"  ..    {label}" + (f"  {detail}" if detail else ""))

    def skip(self, label: str, why: str) -> None:
        self.skipped += 1
        self.say(f"  SKIP  {label}  ({why})")

    def write(self, path: Path) -> None:
        path.write_text("\n".join(self.lines) + "\n")
        print(f"\ntranscript -> {path}")


# ------------------------------------------------------------------ stage 0

def stage_env(rep: Report, mock: bool):
    rep.stage(0, "environment")
    if mock:
        import mock_instruments
        mock_instruments.install()
        rep.ok("mock instruments installed", "no hardware will be touched")

    import pyvisa
    rm = pyvisa.ResourceManager()
    rep.ok("pyvisa ResourceManager", type(rm).__name__)
    try:
        found = list(rm.list_resources())
        rep.note("visible resources", ", ".join(found) if found else "(none)")
    except Exception as e:
        # pyvisa-py cannot always enumerate; not a failure on its own.
        rep.note("resource enumeration unavailable", f"{type(e).__name__}: {e}")
    return rm


# ------------------------------------------------------------------ stage 1

def stage_vna(rep: Report, rm, resource: str):
    rep.stage(1, "VNA, read-only")
    import pyvisa
    try:
        io = rm.open_resource(resource)
        io.timeout = 5000
        io.read_termination = io.write_termination = "\n"
    except Exception as e:
        rep.bad("open", f"{type(e).__name__}: {e}")
        rep.note("", "S2VNA must be running with its socket server enabled")
        return None

    try:
        idn = io.query("*IDN?").strip()
        rep.ok("*IDN?", idn)
    except Exception as e:
        rep.bad("*IDN?", f"{type(e).__name__}: {e}")
        return None

    for label, q in (("SYST:ERR?", "SYST:ERR?"), ("CORR:STAT?", "SENS1:CORR:STAT?")):
        try:
            rep.ok(label, io.query(q).strip())
        except Exception as e:
            rep.note(label, f"{type(e).__name__}: {e}")

    # The one that bites: firmware 26.3.1 answers -110 and then goes quiet, so
    # an unguarded query blocks for the full instrument timeout.
    saved, io.timeout = io.timeout, 3000
    try:
        t = io.query("SENS1:SWE:TIME?").strip()
        rep.note("SENS:SWE:TIME?", f"answered {t!r} - this firmware supports it")
    except pyvisa.VisaIOError:
        rep.ok("SENS:SWE:TIME? unsupported",
               "expected on 26.3.1; sweep_time_s() degrades to n/a")
    finally:
        io.timeout = saved
        try:
            io.query("SYST:ERR?")            # clear the latched -110
        except Exception:
            pass
    return io


# ------------------------------------------------------------------ stage 2

def _try_positioner(rm, resource: str, prefix: str, framing):
    """Open with one framing and ask for identity. Returns (io, reply) or None."""
    import pyvisa.constants as pc
    baud, bits, parity = framing
    try:
        io = rm.open_resource(resource)
        io.timeout = 2000
        io.write_termination = "\r"
        io.read_termination = "\n"
        if str(resource).upper().startswith("ASRL"):
            io.baud_rate = baud
            io.data_bits = bits
            io.parity = {"none": pc.Parity.none, "odd": pc.Parity.odd,
                         "even": pc.Parity.even}[parity]
        reply = io.query(prefix + "*IDN?").strip()
        return (io, reply) if reply else None
    except Exception:
        return None


def stage_pos_link(rep: Report, rm, resource: str, prefix: str, probe: bool):
    rep.stage(2, "positioner link and identity")
    expected = FRAMINGS[0]
    got = _try_positioner(rm, resource, prefix, expected)
    if got:
        io, idn = got
        rep.ok(f"{prefix}*IDN? at {expected[0]} {expected[1]}"
               f"{expected[2][0].upper()}1", idn)
        return io

    rep.bad(f"{prefix}*IDN? at {expected[0]} 8N1", "no reply")
    if not probe:
        rep.note("", "re-run with --probe to try other framings and prefixes")
        return None

    rep.note("probing", "other framings and slot/device prefixes")
    for framing in FRAMINGS[1:]:
        got = _try_positioner(rm, resource, prefix, framing)
        if got:
            io, idn = got
            rep.ok(f"answered at {framing[0]} {framing[1]}{framing[2][0].upper()}1",
                   f"{idn} - update PositionerConfig")
            return io
    for slot in range(1, 8):
        for dev in ("A", "B"):
            p = f"{slot}{dev}:"
            if p == prefix:
                continue
            got = _try_positioner(rm, resource, p, expected)
            if got:
                io, idn = got
                rep.ok(f"answered on prefix {p!r}", f"{idn} - update --slot/--device")
                return io
    rep.bad("probe", "nothing answered on any framing or prefix")
    return None


# ------------------------------------------------------------------ stage 3

def stage_pos_state(rep: Report, io, prefix: str):
    rep.stage(3, "positioner state, read-only")
    if io is None:
        rep.skip("all", "no positioner link")
        return None

    pos = None
    try:
        reply = io.query(prefix + "CP?").strip()
        # The units suffix is load-bearing. A corrupted byte can split a reply
        # and the leading fragment still parses as a plausible angle, so the
        # driver requires the full form; confirm the card actually sends it.
        if POS_RE.match(reply):
            pos = float(re.search(r"[-+]?\d+(?:\.\d+)?", reply).group())
            rep.ok("CP? format", f"{reply!r} -> {pos:.1f} deg")
        else:
            rep.bad("CP? format", f"{reply!r} lacks the DEGREES suffix the "
                                  f"split-reply guard depends on")
    except Exception as e:
        rep.bad("CP?", f"{type(e).__name__}: {e}")

    for label, q in (("*OPC?", "*OPC?"), ("SPEED?", "SPEED?"), ("ERR?", "ERR?")):
        try:
            reply = io.query(prefix + q).strip()
            if q == "*OPC?" and reply not in ("1", "+1"):
                rep.bad("*OPC? at rest", f"{reply!r}; expected 1 = motion complete")
            else:
                rep.ok(label, reply)
        except Exception as e:
            rep.bad(label, f"{type(e).__name__}: {e}")

    for q in KNOWN_UNSUPPORTED:
        try:
            reply = io.query(prefix + q).strip()
        except Exception as e:
            reply = f"{type(e).__name__}"
        if reply.upper().startswith("ERROR"):
            rep.ok(f"{q} rejected", "as recorded; front panel remains the only source")
        else:
            rep.note(f"{q} ANSWERED", f"{reply!r} - firmware changed, "
                                      f"the front-panel workaround may be droppable")
    return pos


# ------------------------------------------------------------------ stage 4

def stage_link_quality(rep: Report, io, prefix: str, reads: int):
    """Hammer the position query and count what comes back malformed.

    Deliberately raw, not through Positioner._q: the driver's resync-and-retry
    is exactly what would hide a degrading link. This is the one stage that
    measures rather than confirms, and the number is comparable between cables.
    """
    rep.stage(4, f"link quality, {reads} raw position reads")
    if io is None:
        rep.skip("all", "no positioner link")
        return

    good = malformed = errored = timedout = 0
    latencies: list[float] = []
    samples: list[str] = []

    for _ in range(reads):
        t0 = time.monotonic()
        try:
            reply = io.query(prefix + "CP?").strip()
        except Exception:
            timedout += 1
            continue
        latencies.append((time.monotonic() - t0) * 1000.0)
        if reply.upper().startswith("ERROR"):
            errored += 1
            if len(samples) < 5:
                samples.append(reply)
        elif not POS_RE.match(reply):
            malformed += 1
            if len(samples) < 5:
                samples.append(reply)
            # Drain whatever fragment is left, or every later read is offset.
            saved, io.timeout = io.timeout, 50
            try:
                while True:
                    io.read()
            except Exception:
                pass
            finally:
                io.timeout = saved
        else:
            good += 1

    bad = malformed + errored + timedout
    rate = 100.0 * bad / reads if reads else 0.0
    rep.note("clean replies", f"{good}/{reads}")
    if bad:
        rep.note("malformed / ERROR / timeout",
                 f"{malformed} / {errored} / {timedout}")
        if samples:
            rep.note("samples", "  ".join(repr(s) for s in samples))
    if latencies:
        rep.note("latency ms",
                 f"median {statistics.median(latencies):.1f}, "
                 f"max {max(latencies):.1f}")

    if rate == 0.0:
        rep.ok("error rate", "0.00% - link clean over this sample")
    elif rate < 1.0:
        rep.ok("error rate", f"{rate:.2f}% - low, but not zero; note the cable used")
    else:
        rep.bad("error rate", f"{rate:.2f}% - the link is degrading. Try a "
                              f"different cable, drop any hub, and check routing "
                              f"relative to the VNA and chamber feed")


# ------------------------------------------------------------------ stage 5

def stage_jog(rep: Report, rm, args, start_deg, allow: bool):
    rep.stage(5, f"jog {args.jog:+.1f} deg  [MOTION]")
    if not allow:
        rep.skip("jog", "needs --allow-motion")
        return
    if start_deg is None:
        rep.skip("jog", "stage 3 could not read a starting position")
        return

    import pattern_measure as pm
    pcfg = pm.PositionerConfig(resource=args.pos)
    cmds = pm.PositionerCmds(slot=args.slot, device=args.device)
    pos = pm.Positioner(rm, pcfg, cmds)
    target = start_deg + args.jog
    try:
        rep.note("commanding", f"{start_deg:.1f} -> {target:.1f} deg")
        actual = pos.seek(target)
        err = abs(pm._wrap180(actual - target))
        if err <= pcfg.position_tol_deg:
            rep.ok("arrived", f"read {actual:.2f} deg, error {err:.2f}")
        else:
            rep.bad("arrival", f"read {actual:.2f} deg, error {err:.2f} deg")
        back = pos.seek(start_deg)
        rep.ok("returned", f"read {back:.2f} deg")
    except Exception as e:
        rep.bad("jog", f"{type(e).__name__}: {e}")
        try:
            pos.stop()
        except Exception:
            pass
    finally:
        pos.close()


# ------------------------------------------------------------------ stage 6

def stage_miniscan(rep: Report, args, allow: bool):
    rep.stage(6, "four-point scan  [MOTION]")
    if not allow:
        rep.skip("mini scan", "needs --allow-motion")
        return
    import tempfile
    import pattern_measure as pm
    with tempfile.TemporaryDirectory(prefix="bringup_") as td:
        argv = ["--vna", args.vna, "--pos", args.pos,
                "--slot", str(args.slot), "--device", args.device,
                "--points", "21", "--step", "90", "--to-deg", "270",
                "--outdir", str(Path(td) / "run")]
        try:
            rc = pm.main(argv)
            if rc == 0:
                rep.ok("scan", "four cuts measured and written")
            else:
                rep.bad("scan", f"exit {rc}")
        except Exception as e:
            rep.bad("scan", f"{type(e).__name__}: {e}")
            rep.note("", traceback.format_exc(limit=3).strip())


# -------------------------------------------------------------------- main

# ------------------------------------------------------------------ stage 7

def stage_acm(rep: Report, io, ch: int = 1):
    """Read-only AutoCal probe. Never runs a calibration.

    This is the stage that decides whether dashboard-driven calibration exists
    at all. The ACM2202 is a USB device on the PC running S2VNA, not on the
    VNA, and that the software drives it from its own GUI does not establish
    that it exposes it to a SCPI client. Everything asked here is a query.

    Every mnemonic below is unverified - see AcmCmds. A -113 'Undefined header'
    is a clean negative answer. A -110 followed by silence is the
    SENS:SWE:TIME? failure mode and means the same short-timeout treatment,
    not that the command is missing.
    """
    rep.stage(7, "AutoCal module (ACM2202), read-only")
    if io is None:
        rep.skip("all", "no VNA connection")
        return

    import pyvisa
    import pattern_measure as pm
    acm = pm.AcmCmds()

    before = _corr(io, ch)
    rep.note("correction before", before)

    def probe(label: str, q: str, timeout_ms: int = 5000):
        saved, io.timeout = io.timeout, timeout_ms
        try:
            reply = io.query(q).strip()
            return reply
        except pyvisa.VisaIOError as e:
            rep.note(label, f"no reply ({type(e).__name__}) - either an "
                            f"undefined header or the -110-then-silence case")
            return None
        finally:
            io.timeout = saved
            try:
                io.query("SYST:ERR?")        # clear whatever got latched
            except Exception:
                pass

    data = probe("module data", acm.module_data)
    if data:
        rep.ok("SYST:COMM:ECAL:DATA?", data.split(",")[0][:60]
               + ("..." if len(data) > 60 else ""))
        rep.note("", "the VNA software can see an AutoCal module")
    else:
        rep.note("SYST:COMM:ECAL:DATA?",
                 "no module data - either nothing is plugged in, or this "
                 "build does not expose AutoCal to SCPI")

    # Does the header exist? Asked by sending it with its parameters missing
    # and reading the error queue - a header that requires parameters cannot do
    # anything without them, so nothing runs. -113 says the command does not
    # exist; -109 (or any parameter complaint) says it does. Querying the write
    # form instead would prove nothing: a write-only command has no reply
    # either way.
    # Only the commands that take parameters. ECAL:ORI:EXEC and ECAL:CCH take
    # none, so sending them bare is not a probe - it is the command. Their
    # existence is inferred from SOLT2 rather than tested, because a read-only
    # stage that orients a module nobody has mated is not read-only.
    for label, template in (("ECAL:SOLT2", acm.solt2),
                            ("ECAL:SOLT1", acm.solt1)):
        head = template.format(ch=ch, p1=acm.port1, p2=acm.port2,
                               state="ON").split()[0]
        try:
            io.write(head)
            err = io.query("SYST:ERR?").strip()
        except pyvisa.VisaIOError as e:
            rep.note(label, f"no answer from the error queue "
                            f"({type(e).__name__}) - treat as the "
                            f"-110-then-silence case")
            continue
        code = err.split(",")[0].strip()
        if code.startswith("-113"):
            rep.note(label, f"undefined header ({err}) - not available here")
        elif code in ("0", "+0"):
            # It accepted a header with no parameters. Either the parameters
            # are optional or something ran; worth a person looking.
            rep.note(label, "accepted with no parameters - check the front "
                            "panel before trusting this")
        else:
            rep.ok(label + " header", f"parsed ({err})")

    rep.note("ECAL:ORI:EXEC / ECAL:CCH",
             "not probed - they take no parameters, so sending one is running "
             "it; infer from SOLT2 and try them with the module mated")

    after = _corr(io, ch)
    if after == before:
        rep.ok("correction undisturbed", f"{before} before and after")
    else:
        rep.bad("correction changed", f"{before} -> {after} - the probe was "
                                      f"supposed to be read-only")


def _corr(io, ch: int) -> str:
    try:
        return io.query(f"SENS{ch}:CORR:STAT?").strip()
    except Exception as e:
        return f"({type(e).__name__})"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--vna", default="TCPIP0::127.0.0.1::5025::SOCKET")
    p.add_argument("--pos", default="ASRL16::INSTR")
    p.add_argument("--slot", type=int, default=1)
    p.add_argument("--device", default="A", choices=["A", "B"])
    p.add_argument("--jog", type=float, default=5.0,
                   help="degrees to jog in stage 5 (default 5)")
    p.add_argument("--link-reads", type=int, default=200)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--allow-motion", action="store_true")
    p.add_argument("--skip-vna", action="store_true")
    p.add_argument("--report", type=Path, default=None)
    p.add_argument("--mock", action="store_true",
                   help="run against mock_instruments, to exercise this script")
    a = p.parse_args(argv)

    prefix = f"{a.slot}{a.device}:"
    rep = Report()
    rep.say("bring-up: stages 0-4 and 7 are read-only; 5 and 6 turn the tower.")
    if a.allow_motion:
        rep.say("MOTION ENABLED. Confirm continuous/non-continuous mode on the "
                "EMControl front panel before continuing.")

    rm = stage_env(rep, a.mock)

    if a.skip_vna:
        rep.stage(1, "VNA, read-only")
        rep.skip("all", "--skip-vna")
        vna_io = None
    else:
        vna_io = stage_vna(rep, rm, a.vna)

    io = stage_pos_link(rep, rm, a.pos, prefix, a.probe)
    start = stage_pos_state(rep, io, prefix)
    stage_link_quality(rep, io, prefix, a.link_reads)
    stage_jog(rep, rm, a, start, a.allow_motion)
    stage_miniscan(rep, a, a.allow_motion)
    stage_acm(rep, vna_io)

    rep.say("")
    rep.say(f"{rep.failed} failed, {rep.skipped} skipped")
    if a.report:
        rep.write(a.report)
    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
