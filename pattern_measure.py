#!/usr/bin/env python3
"""
Step-and-measure antenna pattern acquisition.

Hardware:
  VNA        : Copper Mountain A2202-Fx via S2VNA socket server (SCPI over TCP:5025)
  Positioner : ETS-Lindgren EMControl 7006-001 card in an EMCenter chassis

Output:
  <outdir>/pattern.csv          angle, freq, re, im, mag_dB, phase_deg  (long format)
  <outdir>/cut_<angle>.s2p      optional per-angle Touchstone (S21 only, others zeroed)
  <outdir>/pattern.png          polar plot at the requested cut frequency

VERIFY BEFORE FIRST RUN
  * EMCenter command mnemonics and the slot/device prefix are centralized in
    PositionerCmds below. Cross-check them against ETS-Lindgren manual 399342
    (the EMCenter manual, not the 7006-001 card manual) for your firmware.
  * Confirm the turntable is in the intended continuous / non-continuous mode.
    Continuous ignores software limits and will happily wind up your RF cable
    if there is no rotary joint.
  * Run once with the EMControl simulation mode ON (Config screen) to shake out
    the state machine with nothing physically moving:
        pattern_measure.py --dry-run --step 5
    That steps every angle and writes dryrun.csv without opening the VNA.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyvisa


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

@dataclass
class VnaConfig:
    resource: str = "TCPIP0::127.0.0.1::5025::SOCKET"
    start_hz: float = 2.0e9
    stop_hz: float = 3.0e9
    points: int = 101
    if_bw_hz: float = 1.0e3        # lower IFBW = more dynamic range, slower sweep
    power_dbm: float = 0.0
    parameter: str = "S21"
    channel: int = 1
    timeout_ms: int = 120_000


@dataclass
class PositionerCmds:
    """EMCenter command mnemonics. Commands are prefixed with slot+device and
    terminated with CR, e.g. '1A:*OPC?'.

    Verified 2026-08-20 by read-only probe against EMCenter firmware 4.6.0 with
    an EMControl 7006-001 (2.10.3) in slot 1, device A:
      1A:*IDN?  -> 'ETS-Lindgren, EMControl 7006-001, 2.10.3'
      1A:CP?    -> '90.0 DEGREES'      (value carries units - must be parsed)
      1A:*OPC?  -> '1'                 (1 = complete, as assumed)
      1A:SPEED? -> '100.0'             (percent of max, NOT a 1-4 preset index)
      1A:ACC?   -> '2.0'
      1A:ERR?   -> '0'
    'SP?', 'UL?', 'LL?' and 'MODE?' all return ERROR 1 on this firmware - there
    is no over-the-wire limit or continuous-mode query, so the cable-wrap
    configuration has to be confirmed on the EMControl front panel."""
    slot: int = 1                  # EMCenter slot holding the 7006-001 card
    device: str = "A"              # 'A' = Device 1, 'B' = Device 2 on that card
    seek: str = "SK {pos:.1f}"     # seek to absolute position, degrees
    query_pos: str = "CP?"         # current position query
    set_pos: str = "CP {pos:.1f}"  # redefine current position (zeroing)
    speed: str = "SPEED {n:.1f}"   # speed as percent of maximum
    query_speed: str = "SPEED?"
    query_err: str = "ERR?"        # 0 = no error latched
    stop: str = "ST"
    opc: str = "*OPC?"             # 0 = in motion, 1 = motion complete

    @property
    def prefix(self) -> str:
        return f"{self.slot}{self.device}:"


@dataclass
class PositionerConfig:
    # USB/serial (this rig): the EMCenter's FTDI virtual COM port. Framing is
    # 115200 8N1 - the 9600,7,Odd,1 in ETS-Lindgren's docs describes the legacy
    # Holaday-compatible rear port, NOT the USB port. Verified 2026-08-20:
    # 9600 (both 7O1 and 8N1) times out, 115200 8N1 answers.
    # Ethernet alternative: 'TCPIP0::<host>::inst0::INSTR'.
    resource: str = "ASRL16::INSTR"
    baud_rate: int = 115_200
    data_bits: int = 8
    parity: str = "none"           # 'none' | 'odd' | 'even'
    timeout_ms: int = 10_000
    write_termination: str = "\r"
    read_termination: str = "\n"
    drain_timeout_ms: int = 400    # bounded read after a write, to catch 'ERROR n'
    speed_percent: float | None = None   # None = leave the card's speed alone
    settle_s: float = 0.5          # mechanical ring-down after motion completes
    move_timeout_s: float = 120.0
    motion_start_timeout_s: float = 3.0   # grace period for the card to report motion
    position_tol_deg: float = 0.5


@dataclass
class ScanConfig:
    start_deg: float = 0.0
    stop_deg: float = 355.0
    step_deg: float = 5.0
    return_home: bool = True
    cut_freq_hz: float | None = None   # frequency for the polar plot; None = center
    write_s2p: bool = False
    outdir: Path = field(default_factory=lambda: Path("./pattern_run"))

    def angles(self) -> np.ndarray:
        if self.step_deg <= 0:
            raise ValueError("step_deg must be positive")
        span = self.stop_deg - self.start_deg
        if span < 0:
            raise ValueError("stop_deg must be >= start_deg")
        # floor, not round: never command past the requested stop angle even
        # when step_deg does not divide the span evenly.
        n = int(math.floor(span / self.step_deg + 1e-9)) + 1
        a = self.start_deg + self.step_deg * np.arange(n)
        # A full 360 deg sweep would otherwise measure the start angle twice.
        if len(a) > 1 and abs((a[-1] - a[0]) % 360.0) < 1e-9:
            a = a[:-1]
        return a


# --------------------------------------------------------------------------
# Instrument wrappers
# --------------------------------------------------------------------------

class Aborted(Exception):
    """Raised when a move is cancelled through seek()'s should_abort hook."""


class Vna:
    def __init__(self, rm: pyvisa.ResourceManager, cfg: VnaConfig):
        self.cfg = cfg
        self.io = rm.open_resource(cfg.resource)
        self.io.timeout = cfg.timeout_ms
        self.io.read_termination = "\n"
        self.io.write_termination = "\n"
        self.ch = cfg.channel
        self._restore: dict[str, str] = {}

    def idn(self) -> str:
        return self.io.query("*IDN?").strip()

    def _save_trigger_state(self) -> None:
        """Remember the trigger config so close() can put it back.

        A run leaves the instrument in TRIG:SOUR BUS, which stops the S2VNA
        display updating - the VNA looks hung to the next person who walks up
        to it. Cheap to save, and restoring costs nothing.
        """
        for key, q in (("trig", "TRIG:SOUR?"), ("cont", f"INIT{self.ch}:CONT?")):
            try:
                self._restore[key] = self.io.query(q).strip()
            except pyvisa.VisaIOError:
                pass

    def configure(self) -> None:
        self._save_trigger_state()
        c, k = self.ch, self.cfg
        self.io.write(f"SENS{c}:FREQ:STAR {k.start_hz:.0f}")
        self.io.write(f"SENS{c}:FREQ:STOP {k.stop_hz:.0f}")
        self.io.write(f"SENS{c}:SWE:POIN {k.points:d}")
        self.io.write(f"SENS{c}:BWID {k.if_bw_hz:.0f}")
        self.io.write(f"SOUR{c}:POW {k.power_dbm:.2f}")

        # one trace, the S-parameter we care about, selected for data reads
        self.io.write(f"CALC{c}:PAR:COUN 1")
        self.io.write(f"CALC{c}:PAR1:DEF {k.parameter}")
        self.io.write(f"CALC{c}:PAR1:SEL")
        self.io.write(f"CALC{c}:FORM MLOG")

        # bus-triggered single sweeps: nothing runs until we ask for it
        self.io.write("TRIG:SOUR BUS")
        self.io.write(f"INIT{c}:CONT ON")
        self.io.write("FORM:DATA ASC")
        self._check_errors("after configure")

    def sweep_time_s(self) -> float | None:
        """Sweep time in seconds, or None when the firmware has no such query.

        Not universally supported: the A2202-Fx (firmware 26.3.1) answers
        SENS:SWE:TIME? with -110 'Command header error' and then never replies,
        so an unguarded call blocks for the full instrument timeout - 120 s by
        default - before raising. Queried with a short timeout and treated as
        an optional nicety; it only feeds a progress estimate.
        """
        saved, self.io.timeout = self.io.timeout, 3_000
        try:
            return float(self.io.query(f"SENS{self.ch}:SWE:TIME?"))
        except (pyvisa.VisaIOError, ValueError):
            return None
        finally:
            self.io.timeout = saved
            # Clear the latched -110 so it is not misreported later.
            try:
                self.io.query("SYST:ERR?")
            except pyvisa.VisaIOError:
                pass

    def correction_state(self) -> str:
        """Error-correction state: '1' when a calibration is applied, '0' when not.

        Returns '?' rather than raising. This is metadata about a run, not a
        precondition for it - an instrument that will not answer should leave
        the record honestly unknown rather than abort a scan that would
        otherwise be fine. The caller decides what an unknown is worth.
        """
        try:
            return self.io.query(f"SENS{self.ch}:CORR:STAT?").strip()
        except (pyvisa.VisaIOError, ValueError):
            return "?"

    def frequencies(self) -> np.ndarray:
        raw = self.io.query(f"SENS{self.ch}:FREQ:DATA?")
        return np.fromstring(raw, sep=",")

    def measure(self) -> np.ndarray:
        """Trigger one sweep, block until done, return complex S-parameter array."""
        self.io.write("TRIG:SING")
        self.io.query("*OPC?")                      # blocks until the sweep completes
        raw = self.io.query(f"CALC{self.ch}:DATA:SDAT?")
        flat = np.fromstring(raw, sep=",")          # re, im, re, im, ...
        if flat.size != 2 * self.cfg.points:
            raise RuntimeError(
                f"VNA returned {flat.size // 2} points, expected {self.cfg.points}. "
                f"Trace/channel state does not match the configured sweep.")
        return flat[0::2] + 1j * flat[1::2]

    def _check_errors(self, where: str) -> None:
        try:
            err = self.io.query("SYST:ERR?").strip()
        except pyvisa.VisaIOError:
            return
        if err and not err.startswith(("0", "+0")):
            print(f"[vna] error {where}: {err}", file=sys.stderr)

    def close(self) -> None:
        # Hand the instrument back the way we found it, so the front panel is
        # live again rather than sitting frozen in bus-trigger hold.
        try:
            if self._restore.get("trig"):
                self.io.write(f"TRIG:SOUR {self._restore['trig']}")
            if self._restore.get("cont"):
                self.io.write(f"INIT{self.ch}:CONT {self._restore['cont']}")
            self.io.query("*OPC?")
        except Exception:
            pass
        try:
            self.io.close()
        except Exception:
            pass


class Positioner:
    def __init__(self, rm: pyvisa.ResourceManager,
                 cfg: PositionerConfig, cmds: PositionerCmds):
        self.cfg, self.cmds = cfg, cmds
        self.io = rm.open_resource(cfg.resource)
        self.io.timeout = cfg.timeout_ms
        self.io.write_termination = cfg.write_termination
        self.io.read_termination = cfg.read_termination
        # Serial framing must be applied explicitly. Leaving it unset silently
        # falls back to pyvisa's 9600 8N1 default, which this chassis ignores -
        # every query then times out and the rig looks disconnected.
        if cfg.resource.upper().startswith("ASRL"):
            import pyvisa.constants as _pc
            self.io.baud_rate = cfg.baud_rate
            self.io.data_bits = cfg.data_bits
            self.io.parity = {"none": _pc.Parity.none,
                              "odd": _pc.Parity.odd,
                              "even": _pc.Parity.even}[cfg.parity]

    def _flush_input(self) -> None:
        """Discard anything sitting unread, to resynchronize the reply stream."""
        saved, self.io.timeout = self.io.timeout, 50
        try:
            while True:
                try:
                    self.io.read()
                except pyvisa.VisaIOError:
                    break
        finally:
            self.io.timeout = saved

    def _w(self, body: str, _retry: bool = True) -> None:
        """Write and check the acknowledgement.

        The card answers a good command with 'OK' and a malformed one with
        'ERROR n'. Leaving either unread desynchronizes every later query,
        which then reads the previous command's answer instead of its own.

        An ERROR here is occasionally a corrupted byte on the FTDI link rather
        than a genuinely bad command - observed once mid-scan on a 'SK' that
        was provably valid. Aborting a multi-minute scan over one bad byte is
        the wrong trade, so a rejection is retried once after resynchronizing.
        Every command sent through here is an idempotent absolute instruction
        (seek to an angle, define the current angle, stop), so re-sending is
        safe by construction. The retry warns rather than staying silent, so a
        link that is genuinely degrading still shows up in the log.
        """
        self.io.write(self.cmds.prefix + body)
        saved, self.io.timeout = self.io.timeout, self.cfg.drain_timeout_ms
        try:
            reply = self.io.read().strip()
        except pyvisa.VisaIOError:
            reply = ""              # silence is an acceptable success case
        finally:
            self.io.timeout = saved
        if reply and reply.upper().startswith("ERROR"):
            if _retry:
                print(f"[pos] {body!r} -> {reply}; resyncing and retrying once",
                      file=sys.stderr)
                self._flush_input()
                return self._w(body, _retry=False)
            raise RuntimeError(f"positioner rejected {body!r}: {reply}")

    def _q(self, body: str, _retry: bool = True) -> str:
        reply = self.io.query(self.cmds.prefix + body).strip()
        if reply.upper().startswith("ERROR"):
            if _retry:
                print(f"[pos] {body!r} -> {reply}; resyncing and retrying once",
                      file=sys.stderr)
                self._flush_input()
                return self._q(body, _retry=False)
            raise RuntimeError(f"positioner rejected {body!r}: {reply}")
        return reply

    # A complete position reply always carries its units: '90.0 DEGREES'.
    _POS_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?\s*DEG", re.I)

    @staticmethod
    def _deg(reply: str) -> float:
        """Parse a numeric reply. The card answers with units, e.g. '90.0 DEGREES'."""
        m = re.search(r"[-+]?\d+(?:\.\d+)?", reply)
        if not m:
            raise RuntimeError(f"unparseable reply: {reply!r}")
        return float(m.group())

    def position(self, _retry: bool = True) -> float:
        """Current angle in degrees, validated against the full reply format.

        Byte-level corruption on the FTDI link can split a reply in two, and
        the leading fragment still parses as a perfectly plausible angle:
        observed mid-scan, '255.0 DEGREES' arrived as '2' + '55.0 DEGREES' and
        '295.0 DEGREES' as '29' + '5.0 DEGREES', so the run recorded 2.0 and
        29.0 degrees for those cuts. Nothing rejects a bare number, which makes
        this worse than an outright ERROR - with a real antenna on the tower it
        would silently mislabel the angle of a cut.

        Requiring the units suffix turns the fragment into a detectable fault:
        '2' fails, '2.0 DEGREES' passes. On failure, resynchronize (which also
        discards the orphaned remainder) and re-read once.
        """
        reply = self._q(self.cmds.query_pos)
        if not self._POS_RE.match(reply):
            if _retry:
                print(f"[pos] malformed position reply {reply!r}; "
                      f"resyncing and re-reading once", file=sys.stderr)
                self._flush_input()
                return self.position(_retry=False)
            raise RuntimeError(f"malformed position reply: {reply!r}")
        return self._deg(reply)

    def in_motion(self) -> bool:
        # *OPC? on this card returns 0 while moving, 1 when motion is complete
        return self._q(self.cmds.opc) not in ("1", "+1")

    def speed(self) -> float:
        return self._deg(self._q(self.cmds.query_speed))

    def latched_error(self) -> str:
        return self._q(self.cmds.query_err)

    def identity(self) -> str:
        return self._q("*IDN?")

    def set_speed(self, pct: float) -> None:
        self._w(self.cmds.speed.format(n=pct))

    def stop(self) -> None:
        self._w(self.cmds.stop)

    def zero_here(self) -> None:
        self._w(self.cmds.set_pos.format(pos=0.0))

    def seek(self, deg: float, should_abort=None) -> float:
        """Command an absolute move and block until the tower is verifiably parked
        at `deg`. Arrival is confirmed by position readback, never by elapsed time:
        a sweep taken while the tower is still turning is silently corrupt.

        `should_abort` is an optional zero-argument predicate polled while the
        tower is moving. When it returns true the move is stopped and Aborted is
        raised. It exists so a UI stop button can preempt a move that may run for
        tens of seconds: the caller owning the serial port acts on a flag, rather
        than a second thread writing a stop command into the middle of this
        thread's request/response exchange. Serial access stays single-threaded.
        """
        if abs(_wrap180(self.position() - deg)) <= self.cfg.position_tol_deg:
            return self.position()          # already parked; no move to wait on

        self._w(self.cmds.seek.format(pos=deg))

        # Wait for motion to actually BEGIN. Polling in_motion() after a fixed
        # sleep races the card: if it has not started yet we see "not moving"
        # and sweep mid-rotation.
        t0 = time.monotonic()
        started = False
        while time.monotonic() - t0 < self.cfg.motion_start_timeout_s:
            if self.in_motion():
                started = True
                break
            time.sleep(0.05)

        deadline = time.monotonic() + self.cfg.move_timeout_s
        while self.in_motion():
            if should_abort is not None and should_abort():
                self.stop()
                raise Aborted(f"move to {deg:.1f} deg aborted by request")
            if time.monotonic() > deadline:
                self.stop()
                raise TimeoutError(f"positioner did not reach {deg:.1f} deg in time")
            time.sleep(0.1)

        time.sleep(self.cfg.settle_s)                # mechanical ring-down
        actual = self.position()
        err = abs(_wrap180(actual - deg))
        if err > self.cfg.position_tol_deg:
            if not started:
                # Never saw motion and we are not where we asked to be: the seek
                # did not take. Refuse to measure rather than log a bad cut.
                raise RuntimeError(
                    f"positioner never moved: asked {deg:.1f} deg, still at "
                    f"{actual:.1f} deg. Check the slot/device prefix "
                    f"({self.cmds.prefix!r}) and the seek mnemonic.")
            print(f"[pos] warning: asked {deg:.1f}, read {actual:.1f} "
                  f"({err:.2f} deg error)", file=sys.stderr)
        return actual

    def close(self) -> None:
        try:
            self.io.close()
        except Exception:
            pass


def correction_is_on(state: str | None) -> bool | None:
    """True / False / None-for-unknown, from a CORR:STAT? reply.

    Three states, not two. An instrument that did not answer is not the same
    as one that answered "off", and a run recorded as uncalibrated when the
    query merely timed out would be thrown away for no reason.
    """
    if state is None:
        return None
    s = str(state).strip().upper()
    if s in ("1", "+1", "ON", "TRUE"):
        return True
    if s in ("0", "+0", "OFF", "FALSE"):
        return False
    return None


def _wrap180(x: float) -> float:
    return (x + 180.0) % 360.0 - 180.0


# --------------------------------------------------------------------------
# Acquisition
# --------------------------------------------------------------------------

def run_scan(vna: Vna, pos: Positioner, scan: ScanConfig):
    scan.outdir.mkdir(parents=True, exist_ok=True)
    freqs = vna.frequencies()
    angles = scan.angles()
    data = np.zeros((len(angles), len(freqs)), dtype=complex)
    actual = np.zeros(len(angles))

    csv_path = scan.outdir / "pattern.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["angle_cmd_deg", "angle_actual_deg", "freq_hz",
                    "re", "im", "mag_db", "phase_deg"])

        for i, ang in enumerate(angles):
            actual[i] = pos.seek(float(ang))
            s = vna.measure()
            vna._check_errors(f"at {ang:.2f} deg")
            data[i, :] = s

            mag_db = 20.0 * np.log10(np.maximum(np.abs(s), 1e-15))
            phase = np.degrees(np.angle(s))
            for f, sv, m, p in zip(freqs, s, mag_db, phase):
                w.writerow([f"{ang:.2f}", f"{actual[i]:.2f}", f"{f:.0f}",
                            f"{sv.real:.9e}", f"{sv.imag:.9e}",
                            f"{m:.4f}", f"{p:.4f}"])
            fh.flush()

            if scan.write_s2p:
                _write_s2p(scan.outdir / f"cut_{ang:07.2f}.s2p", freqs, s)

            print(f"  {ang:7.2f} deg (read {actual[i]:7.2f})  "
                  f"peak {mag_db.max():7.2f} dB", flush=True)

    print(f"\nwrote {csv_path}")
    return angles, actual, freqs, data


def run_positioner_only(pos: Positioner, scan: ScanConfig):
    """Step the full angle list without touching the VNA.

    This is the run to make against EMControl's simulation mode: it exercises
    seek(), motion-start detection, arrival tolerance and the settle path -
    everything that can silently corrupt a real scan - with no VNA in the loop
    and, in simulation, nothing physically turning.

    Writes dryrun.csv (commanded vs. read-back angle and the per-step wall
    time) so positioner accuracy and slew timing can be inspected afterwards.
    """
    scan.outdir.mkdir(parents=True, exist_ok=True)
    angles = scan.angles()
    csv_path = scan.outdir / "dryrun.csv"
    worst = 0.0
    t_start = time.monotonic()

    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["angle_cmd_deg", "angle_actual_deg", "error_deg", "step_s"])
        for ang in angles:
            t0 = time.monotonic()
            actual = pos.seek(float(ang))
            dt = time.monotonic() - t0
            err = _wrap180(actual - float(ang))
            worst = max(worst, abs(err))
            w.writerow([f"{ang:.2f}", f"{actual:.2f}", f"{err:+.3f}", f"{dt:.3f}"])
            fh.flush()
            print(f"  {ang:7.2f} deg -> read {actual:7.2f}  "
                  f"err {err:+6.3f}  {dt:5.2f} s", flush=True)

    total = time.monotonic() - t_start
    print(f"\nwrote {csv_path}")
    print(f"{len(angles)} points in {total/60:.1f} min, "
          f"worst position error {worst:.3f} deg")
    print(f"latched positioner error: {pos.latched_error()}")
    return angles


def _write_s2p(path: Path, freqs: np.ndarray, s21: np.ndarray) -> None:
    """Minimal Touchstone. Only S21 is populated; the rest are zero-filled."""
    with path.open("w") as fh:
        fh.write("! Antenna pattern cut, S21 only\n")
        fh.write("# HZ S RI R 50\n")
        for f, s in zip(freqs, s21):
            fh.write(f"{f:.0f} 0 0 {s.real:.9e} {s.imag:.9e} 0 0 0 0\n")


# --------------------------------------------------------------------------
# Plot
# --------------------------------------------------------------------------

def polar_plot(angles, freqs, data, scan: ScanConfig, normalize=True):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    f_target = scan.cut_freq_hz if scan.cut_freq_hz else float(freqs[len(freqs) // 2])
    k = int(np.argmin(np.abs(freqs - f_target)))
    cut = 20.0 * np.log10(np.maximum(np.abs(data[:, k]), 1e-15))
    if normalize:
        cut = cut - cut.max()

    th = np.radians(angles)
    th = np.append(th, th[0])          # close the trace
    r = np.append(cut, cut[0])

    fig = plt.figure(figsize=(6, 6))
    ax = fig.add_subplot(111, projection="polar")
    ax.plot(th, r, lw=1.6)
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    ax.set_ylim(-40, 0 if normalize else float(np.ceil(r.max())))
    ax.set_rlabel_position(135)
    ax.grid(True, alpha=0.4)
    ax.set_title(f"Pattern @ {freqs[k]/1e9:.4f} GHz"
                 f"{' (normalized)' if normalize else ''}", pad=18)

    out = scan.outdir / "pattern.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"wrote {out}")
    return out


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--vna", default=VnaConfig.resource)
    p.add_argument("--pos", default=PositionerConfig.resource)
    p.add_argument("--slot", type=int, default=PositionerCmds.slot)
    p.add_argument("--speed", type=float, default=None,
                   help="positioner speed, percent of max (default: leave unchanged)")
    p.add_argument("--device", default=PositionerCmds.device, choices=["A", "B"])
    p.add_argument("--start-ghz", type=float, default=VnaConfig.start_hz / 1e9)
    p.add_argument("--stop-ghz", type=float, default=VnaConfig.stop_hz / 1e9)
    p.add_argument("--points", type=int, default=VnaConfig.points)
    p.add_argument("--ifbw", type=float, default=VnaConfig.if_bw_hz)
    p.add_argument("--power", type=float, default=VnaConfig.power_dbm)
    p.add_argument("--param", default=VnaConfig.parameter)
    p.add_argument("--step", type=float, default=ScanConfig.step_deg)
    p.add_argument("--from-deg", type=float, default=ScanConfig.start_deg)
    p.add_argument("--to-deg", type=float, default=ScanConfig.stop_deg)
    p.add_argument("--cut-ghz", type=float, default=None)
    p.add_argument("--s2p", action="store_true", help="also write per-angle Touchstone")
    p.add_argument("--zero-here", action="store_true",
                   help="define the current mechanical position as 0 deg, then scan")
    p.add_argument("--dry-run", action="store_true",
                   help="step the positioner through every angle without opening "
                        "the VNA. NOTE: dry means no VNA, NOT no motion - this "
                        "issues real seek commands and WILL turn the tower unless "
                        "EMControl is in simulation mode.")
    p.add_argument("--visa", default="",
                   help="VISA backend, e.g. '@py' for pyvisa-py (default: let pyvisa choose)")
    p.add_argument("--outdir", type=Path, default=Path("./pattern_run"))
    a = p.parse_args(argv)

    vcfg = VnaConfig(resource=a.vna, start_hz=a.start_ghz * 1e9,
                     stop_hz=a.stop_ghz * 1e9, points=a.points,
                     if_bw_hz=a.ifbw, power_dbm=a.power, parameter=a.param)
    pcfg = PositionerConfig(resource=a.pos, speed_percent=a.speed)
    cmds = PositionerCmds(slot=a.slot, device=a.device)
    scan = ScanConfig(start_deg=a.from_deg, stop_deg=a.to_deg, step_deg=a.step,
                      cut_freq_hz=(a.cut_ghz * 1e9 if a.cut_ghz else None),
                      write_s2p=a.s2p, outdir=a.outdir)

    rm = pyvisa.ResourceManager(a.visa) if a.visa else pyvisa.ResourceManager()
    vna = pos = None
    try:
        sweep_s = 0.0
        if a.dry_run:
            print("DRY RUN: positioner only, VNA not opened.")
        else:
            vna = Vna(rm, vcfg)
            print(f"VNA : {vna.idn()}")
            vna.configure()
            sweep_s = vna.sweep_time_s()
            sweep_txt = f"sweep {sweep_s*1e3:.1f} ms" if sweep_s else "sweep time n/a"
            print(f"      {vcfg.points} pts, {vcfg.start_hz/1e9:.3f}-{vcfg.stop_hz/1e9:.3f} GHz, "
                  f"IFBW {vcfg.if_bw_hz:.0f} Hz, {sweep_txt}")
            sweep_s = sweep_s or 0.0

            # Worth knowing before the tower turns, not after the run is on disk.
            corr = vna.correction_state()
            on = correction_is_on(corr)
            if on is False:
                print(f"      error correction OFF ({corr!r}) - THIS RUN IS "
                      f"UNCALIBRATED", file=sys.stderr)
            elif on is None:
                print(f"      error correction unknown ({corr!r})", file=sys.stderr)
            else:
                print(f"      error correction on ({corr})")

        pos = Positioner(rm, pcfg, cmds)
        if pcfg.speed_percent is not None:
            pos.set_speed(pcfg.speed_percent)
        if a.zero_here:
            pos.zero_here()
        print(f"POS : {pos.identity()}")
        print(f"      slot {cmds.slot} device {cmds.device}, "
              f"at {pos.position():.2f} deg, speed {pos.speed():.1f}%, "
              f"err {pos.latched_error()}")

        n = len(scan.angles())
        est = n * (sweep_s + pcfg.settle_s + scan.step_deg * 0.2)
        print(f"\n{'stepping' if a.dry_run else 'scanning'} {n} points, "
              f"rough estimate {est/60:.1f} min\n")

        if a.dry_run:
            run_positioner_only(pos, scan)
        else:
            angles, actual, freqs, data = run_scan(vna, pos, scan)
            polar_plot(angles, freqs, data, scan)

        if scan.return_home:
            print("returning to start position...")
            pos.seek(scan.start_deg)

    except KeyboardInterrupt:
        print("\ninterrupted - stopping positioner", file=sys.stderr)
        if pos is not None:
            pos.stop()
        return 130
    finally:
        # Stop the axis before closing the port, on every exit path.
        #
        # Only KeyboardInterrupt used to do this, which left a gap: an
        # exception raised inside seek()'s wait loop - a dropped poll, a
        # rejected command, a VNA read that times out on the next line -
        # propagates with the tower still turning, and closing the port first
        # throws away the only means of stopping it. Cable wind-up is the
        # standing hazard in this chamber, so the invariant is worth stating
        # plainly: the axis is commanded to stop before the port goes away.
        #
        # Harmless on the success path, where the tower is already parked, and
        # harmless twice after a KeyboardInterrupt - the command is idempotent.
        if pos is not None:
            try:
                pos.stop()
            except Exception:
                pass
        if vna is not None:
            vna.close()
        if pos is not None:
            pos.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
