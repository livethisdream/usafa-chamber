#!/usr/bin/env python3
"""
Step-and-measure antenna pattern acquisition.

Hardware:
  VNA        : Copper Mountain A2202-Fx via S2VNA socket server (SCPI over TCP:5025)
  Positioner : ETS-Lindgren EMControl 7006-001 card in an EMCenter chassis

Output:
  <outdir>/pattern.csv          angle, freq, re, im, mag_dB, phase_deg  (long format)
  <outdir>/run_meta.json        instrument IDs, sweep settings, cal state, closure check
  <outdir>/cut_<angle>.s2p      optional per-angle Touchstone
  <outdir>/pattern.png          polar plot at the requested cut frequency

VERIFY BEFORE FIRST RUN
  * EMCenter command mnemonics and the slot/device prefix are centralized in
    PositionerCmds below. Cross-check them against ETS-Lindgren manual 399342
    (the EMCenter manual, not the 7006-001 card manual) for your firmware.
  * Confirm the turntable is in the intended continuous / non-continuous mode.
    Continuous ignores software limits and will happily wind up your RF cable
    if there is no rotary joint.
  * The S2VNA socket server is off by default; enable it in the application and
    make sure the application is running before connecting.
  * Run once against mock_instruments.py (see dryrun.py) to shake out the state
    machine with nothing physically moving.

DESIGN NOTE
  configure() deliberately does NOT issue SYST:PRES. A preset would clear the
  calibration along with the stale state. Instead every setting the measurement
  depends on is written explicitly, and correction state is queried and
  reported so an uncalibrated run is loud rather than silent.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    aux_parameter: str | None = None   # e.g. "S11", to catch a connection change
    averaging: int = 0             # 0 = off, else sweeps averaged per point
    channel: int = 1
    timeout_ms: int = 120_000


@dataclass
class PositionerCmds:
    """EMCenter command mnemonics. Commands are prefixed with slot+device and
    terminated with CR, e.g. '5B:*OPC?'. Confirm against manual 399342."""
    slot: int = 5                  # EMCenter slot holding the 7006-001 card
    device: str = "A"              # 'A' = Device 1, 'B' = Device 2 on that card
    seek: str = "SK {pos:.1f}"     # seek to absolute position, degrees
    query_pos: str = "CP?"         # current position query
    set_pos: str = "CP {pos:.1f}"  # redefine current position (zeroing)
    speed: str = "SP {n:d}"        # speed preset index
    stop: str = "ST"
    opc: str = "*OPC?"             # 0 = in motion, 1 = motion complete

    @property
    def prefix(self) -> str:
        return f"{self.slot}{self.device}:"


@dataclass
class PositionerConfig:
    # Ethernet:  'TCPIP0::192.168.1.50::5025::SOCKET'  (raw socket is preferred:
    #            termination is explicit, unlike VXI-11 '::inst0::INSTR')
    # USB/serial: 'ASRL3::INSTR' on the Holaday-compatible port
    resource: str = "TCPIP0::192.168.1.50::5025::SOCKET"
    timeout_ms: int = 10_000
    write_termination: str = "\r"
    read_termination: str = "\n"
    # serial line settings, applied only for ASRL resources
    baud_rate: int = 9600
    data_bits: int = 7
    parity: str = "odd"
    stop_bits: float = 1.0
    speed_preset: int | None = 4
    settle_s: float = 0.5          # mechanical ring-down after motion completes
    move_timeout_s: float = 120.0
    position_tol_deg: float = 0.5
    poll_s: float = 0.1
    stable_polls: int = 2          # consecutive good reads before motion is "done"
    query_retries: int = 3         # transient VISA failures tolerated per query
    backlash_deg: float = 0.0      # >0: take up lash by approaching from below


@dataclass
class ScanConfig:
    start_deg: float = 0.0
    stop_deg: float = 355.0
    step_deg: float = 5.0
    return_home: bool = True
    closure_check: bool = True     # re-measure the start angle to catch drift
    cut_freq_hz: float | None = None   # frequency for the polar plot; None = center
    db_floor: float | None = None      # polar plot floor; None = fit to data
    write_s2p: bool = False
    outdir: Path = field(default_factory=lambda: Path("./pattern_run"))

    def angles(self) -> np.ndarray:
        """Angle grid that never overshoots stop_deg.

        The old form, int(round(span/step)) + 1, walked past the requested stop
        whenever the span was not an integer multiple of the step: 0->355 in 30
        deg steps produced a 360 deg point, both a duplicate of 0 and an extra
        wrap of the cable. Floor instead, and let the caller warn about the
        shortfall.
        """
        if self.step_deg <= 0:
            raise ValueError("step_deg must be positive")
        span = self.stop_deg - self.start_deg
        if span < 0:
            raise ValueError("stop_deg must be >= start_deg")
        n = int(math.floor(span / self.step_deg + 1e-9)) + 1
        a = self.start_deg + self.step_deg * np.arange(n)
        # A full circle puts the last point on top of the first; drop it.
        if n > 1 and abs(_wrap180(a[-1] - a[0])) < self.step_deg / 2:
            a = a[:-1]
        return a

    def span_deg(self) -> float:
        a = self.angles()
        return float(a[-1] - a[0]) if len(a) > 1 else 0.0

    def is_full_circle(self) -> bool:
        a = self.angles()
        return len(a) > 2 and (a[-1] - a[0]) + self.step_deg >= 360.0 - 1e-6


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _wrap180(x: float) -> float:
    return (x + 180.0) % 360.0 - 180.0


def _parse_floats(raw: str) -> np.ndarray:
    """Parse a comma-separated SCPI response, tolerating trailing separators."""
    txt = raw.strip().rstrip(",")
    if not txt:
        return np.empty(0, dtype=float)
    return np.array([float(t) for t in txt.split(",")], dtype=float)


class PositionError(RuntimeError):
    """The positioner did not reach the commanded angle within tolerance."""


# --------------------------------------------------------------------------
# Instrument wrappers
# --------------------------------------------------------------------------

class Vna:
    def __init__(self, rm: pyvisa.ResourceManager, cfg: VnaConfig):
        self.cfg = cfg
        self.io = rm.open_resource(cfg.resource)
        self.io.timeout = cfg.timeout_ms
        self.io.read_termination = "\n"
        self.io.write_termination = "\n"
        self.ch = cfg.channel
        self.npoints = cfg.points
        self._traces: list[str] = []

    def idn(self) -> str:
        return self.io.query("*IDN?").strip()

    def configure(self) -> None:
        c, k = self.ch, self.cfg
        self.io.write(f"SENS{c}:SWE:TYPE LIN")
        self.io.write(f"SENS{c}:FREQ:STAR {k.start_hz:.0f}")
        self.io.write(f"SENS{c}:FREQ:STOP {k.stop_hz:.0f}")
        self.io.write(f"SENS{c}:SWE:POIN {k.points:d}")
        self.io.write(f"SENS{c}:BWID {k.if_bw_hz:.0f}")
        self.io.write(f"SOUR{c}:POW {k.power_dbm:.2f}")

        # Averaging and smoothing are set explicitly rather than inherited, so a
        # previous user's state cannot quietly change what we record.
        if k.averaging and k.averaging > 1:
            self.io.write(f"SENS{c}:AVER:COUN {k.averaging:d}")
            self.io.write(f"SENS{c}:AVER:STAT ON")
            self.io.write(f"SENS{c}:AVER:CLE")
        else:
            self.io.write(f"SENS{c}:AVER:STAT OFF")

        self._traces = [k.parameter] + ([k.aux_parameter] if k.aux_parameter else [])
        self.io.write(f"CALC{c}:PAR:COUN {len(self._traces):d}")
        for i, par in enumerate(self._traces, start=1):
            self.io.write(f"CALC{c}:PAR{i}:DEF {par}")
            self.io.write(f"CALC{c}:PAR{i}:SEL")
            self.io.write(f"CALC{c}:SMO:STAT OFF")
        self.io.write(f"CALC{c}:PAR1:SEL")

        # bus-triggered single sweeps: nothing runs until we ask for it
        self.io.write("TRIG:SOUR BUS")
        self.io.write(f"INIT{c}:CONT ON")
        self.io.write("FORM:DATA ASC")
        self._check_errors("after configure")

    def correction_state(self) -> str:
        """Report whether error correction is applied. Never fatal on its own."""
        try:
            return self.io.query(f"SENS{self.ch}:CORR:STAT?").strip()
        except Exception:
            return "unknown"

    def sweep_time_s(self) -> float:
        try:
            return float(self.io.query(f"SENS{self.ch}:SWE:TIME?"))
        except Exception:
            return float("nan")

    def warm_up(self) -> None:
        """Run one throwaway sweep.

        With TRIG:SOUR BUS and nothing yet triggered, a stimulus or data query
        can return the previous setup's array. Sweeping once first makes the
        frequency vector we read trustworthy, and settles the source.
        """
        self.io.write("TRIG:SING")
        self.io.query("*OPC?")

    def frequencies(self) -> np.ndarray:
        raw = self.io.query(f"SENS{self.ch}:FREQ:DATA?")
        f = _parse_floats(raw)
        if len(f) != self.npoints:
            raise RuntimeError(
                f"VNA returned {len(f)} stimulus points, expected {self.npoints}")
        return f

    def _read_trace(self, index: int) -> np.ndarray:
        self.io.write(f"CALC{self.ch}:PAR{index:d}:SEL")
        raw = self.io.query(f"CALC{self.ch}:DATA:SDAT?")
        flat = _parse_floats(raw)              # re, im, re, im, ...
        if len(flat) != 2 * self.npoints:
            raise ValueError(
                f"trace {index}: got {len(flat)} values, expected {2 * self.npoints}")
        return flat[0::2] + 1j * flat[1::2]

    def measure(self) -> dict[str, np.ndarray]:
        """Trigger one sweep, block until done, return {param: complex array}.

        A short or truncated response is retried once before giving up; the old
        code let a mismatched length blow up on array assignment mid-scan.
        """
        self.io.write("TRIG:SING")
        self.io.query("*OPC?")                 # blocks until the sweep completes
        out: dict[str, np.ndarray] = {}
        for i, par in enumerate(self._traces, start=1):
            try:
                out[par] = self._read_trace(i)
            except ValueError as exc:
                print(f"[vna] {exc}; re-reading", file=sys.stderr)
                out[par] = self._read_trace(i)
        return out

    def _check_errors(self, where: str) -> None:
        try:
            err = self.io.query("SYST:ERR?").strip()
        except pyvisa.VisaIOError:
            return
        if err and not err.startswith(("0", "+0")):
            print(f"[vna] error {where}: {err}", file=sys.stderr)

    def close(self) -> None:
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
        self._last_dir = 0
        if cfg.resource.upper().startswith("ASRL"):
            self._configure_serial()

    def _configure_serial(self) -> None:
        """pyvisa defaults serial to 9600,8,N,1. The EMCenter's Holaday-
        compatible port is 7,Odd,1, so the defaults silently produce garbage."""
        from pyvisa import constants
        parity = {"none": constants.Parity.none, "odd": constants.Parity.odd,
                  "even": constants.Parity.even}[self.cfg.parity.lower()]
        stop = {1.0: constants.StopBits.one,
                1.5: constants.StopBits.one_and_a_half,
                2.0: constants.StopBits.two}[float(self.cfg.stop_bits)]
        self.io.baud_rate = self.cfg.baud_rate
        self.io.data_bits = self.cfg.data_bits
        self.io.parity = parity
        self.io.stop_bits = stop

    def _w(self, body: str) -> None:
        self.io.write(self.cmds.prefix + body)

    def _q(self, body: str) -> str:
        """Query with a few retries.

        A dropped poll on a serial or socket link is common and transient; it
        should not end a scan that is otherwise going fine. Persistent failures
        still raise, so a genuinely dead link is not papered over.
        """
        last: Exception | None = None
        for attempt in range(self.cfg.query_retries + 1):
            try:
                return self.io.query(self.cmds.prefix + body).strip()
            except pyvisa.VisaIOError as exc:
                last = exc
                if attempt < self.cfg.query_retries:
                    time.sleep(self.cfg.poll_s)
        raise last  # type: ignore[misc]

    def position(self) -> float:
        return float(self._q(self.cmds.query_pos))

    def in_motion(self) -> bool:
        # *OPC? on this card returns 0 while moving, 1 when motion is complete
        return self._q(self.cmds.opc) not in ("1", "+1")

    def set_speed(self, n: int) -> None:
        self._w(self.cmds.speed.format(n=n))

    def stop(self) -> None:
        self._w(self.cmds.stop)

    def zero_here(self) -> None:
        self._w(self.cmds.set_pos.format(pos=0.0))
        self._last_dir = 0

    def seek(self, deg: float) -> float:
        """Command an absolute move and block until the axis is really there.

        Backlash takeup: when enabled and the move reverses direction, the
        target is approached from below so every angle is reached with the
        gear train loaded the same way.
        """
        if self.cfg.backlash_deg > 0:
            try:
                here = self.position()
            except Exception:
                here = None
            if here is not None and here > deg:
                self._move_and_wait(deg - self.cfg.backlash_deg,
                                    tol=self.cfg.backlash_deg)
        return self._move_and_wait(deg)

    def _move_and_wait(self, deg: float, tol: float | None = None) -> float:
        tol = self.cfg.position_tol_deg if tol is None else tol
        actual = self._attempt(deg, tol)
        if actual is None:
            # One retry: a controller that reported complete early sometimes
            # just needs the move re-issued.
            print(f"[pos] {deg:.1f} deg not reached, retrying", file=sys.stderr)
            actual = self._attempt(deg, tol)
        if actual is None:
            self.stop()
            raise PositionError(
                f"positioner did not settle within {tol:.2f} deg of {deg:.1f}")
        self._last_dir = 1 if deg >= 0 else -1
        return actual

    def _attempt(self, deg: float, tol: float) -> float | None:
        """Issue the move and poll. Returns the settled position, or None.

        Completion requires motion-complete AND an in-tolerance readback on
        `stable_polls` consecutive reads. Trusting *OPC? alone let a controller
        that asserts complete before it starts moving hand back a sweep taken
        mid-rotation.
        """
        self._w(self.cmds.seek.format(pos=deg))
        deadline = time.monotonic() + self.cfg.move_timeout_s
        good = 0
        while True:
            if time.monotonic() > deadline:
                self.stop()
                raise TimeoutError(
                    f"positioner did not reach {deg:.1f} deg in "
                    f"{self.cfg.move_timeout_s:.0f} s")
            try:
                moving = self.in_motion()
                here = self.position()
            except pyvisa.VisaIOError:
                # A dropped poll is not a failed move; keep polling until the
                # deadline rather than aborting the run.
                time.sleep(self.cfg.poll_s)
                continue
            if not moving and abs(_wrap180(here - deg)) <= tol:
                good += 1
                if good >= self.cfg.stable_polls:
                    break
            else:
                good = 0
            time.sleep(self.cfg.poll_s)

        time.sleep(self.cfg.settle_s)           # mechanical ring-down
        actual = self.position()
        return actual if abs(_wrap180(actual - deg)) <= tol else None

    def close(self) -> None:
        try:
            self.io.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Acquisition
# --------------------------------------------------------------------------

CSV_HEADER = ["angle_cmd_deg", "angle_actual_deg", "param", "freq_hz",
              "re", "im", "mag_db", "phase_deg"]


def _db(x: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(np.abs(x), 1e-15))


def _write_rows(w, ang: float, actual: float, freqs: np.ndarray,
                traces: dict[str, np.ndarray]) -> None:
    for par, s in traces.items():
        for f, sv, m, p in zip(freqs, s, _db(s), np.degrees(np.angle(s))):
            w.writerow([f"{ang:.2f}", f"{actual:.2f}", par, f"{f:.0f}",
                        f"{sv.real:.9e}", f"{sv.imag:.9e}",
                        f"{m:.4f}", f"{p:.4f}"])


def run_scan(vna: Vna, pos: Positioner, scan: ScanConfig):
    scan.outdir.mkdir(parents=True, exist_ok=True)
    freqs = vna.frequencies()
    angles = scan.angles()
    main_par = vna.cfg.parameter
    data = np.zeros((len(angles), len(freqs)), dtype=complex)
    actual = np.zeros(len(angles))
    first_traces = None

    csv_path = scan.outdir / "pattern.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)

        for i, ang in enumerate(angles):
            actual[i] = pos.seek(float(ang))
            traces = vna.measure()
            if i == 0:
                first_traces = traces
                vna._check_errors("after first sweep")
            data[i, :] = traces[main_par]

            _write_rows(w, float(ang), actual[i], freqs, traces)
            fh.flush()

            if scan.write_s2p:
                _write_s2p(scan.outdir / f"cut_{ang:07.2f}.s2p", freqs, traces,
                           main_par, vna.cfg.aux_parameter)

            print(f"  {ang:7.2f} deg (read {actual[i]:7.2f})  "
                  f"peak {_db(traces[main_par]).max():7.2f} dB", flush=True)

        closure = None
        if scan.closure_check and len(angles) > 1:
            print("closure check: re-measuring start angle...")
            back = pos.seek(float(angles[0]))
            again = vna.measure()
            _write_rows(w, float(angles[0]), back, freqs, again)
            fh.flush()
            d = _db(again[main_par]) - _db(first_traces[main_par])
            closure = {
                "angle_deg": float(angles[0]),
                "readback_deg": float(back),
                "max_abs_delta_db": float(np.max(np.abs(d))),
                "mean_delta_db": float(np.mean(d)),
            }
            print(f"  drift over the run: {closure['max_abs_delta_db']:.3f} dB "
                  f"max, {closure['mean_delta_db']:+.3f} dB mean")

    print(f"\nwrote {csv_path}")
    return angles, actual, freqs, data, closure


def _write_s2p(path: Path, freqs: np.ndarray, traces: dict[str, np.ndarray],
               main_par: str, aux_par: str | None) -> None:
    """Minimal Touchstone, real/imag, columns S11 S21 S12 S22.

    Only the measured parameters are populated; the rest are zero-filled, which
    reads as -inf dB in most viewers. Capture an aux parameter (S11) if you want
    the reflection column to mean something.
    """
    zero = np.zeros(len(freqs), dtype=complex)
    s11 = traces.get(aux_par, zero) if aux_par == "S11" else zero
    s21 = traces.get(main_par, zero)
    with path.open("w") as fh:
        fh.write("! Antenna pattern cut\n")
        fh.write(f"! measured: {main_par}"
                 f"{' + ' + aux_par if aux_par else ''}; other terms zero-filled\n")
        fh.write("# HZ S RI R 50\n")
        fh.write("!freq ReS11 ImS11 ReS21 ImS21 ReS12 ImS12 ReS22 ImS22\n")
        for f, a, b in zip(freqs, s11, s21):
            fh.write(f"{f:.0f} {a.real:.9e} {a.imag:.9e} "
                     f"{b.real:.9e} {b.imag:.9e} 0 0 0 0\n")


# --------------------------------------------------------------------------
# Plot
# --------------------------------------------------------------------------

def polar_plot(angles, freqs, data, scan: ScanConfig, normalize=True):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    f_target = scan.cut_freq_hz if scan.cut_freq_hz else float(freqs[len(freqs) // 2])
    k = int(np.argmin(np.abs(freqs - f_target)))
    cut = _db(data[:, k])
    if normalize:
        cut = cut - cut.max()

    th = np.radians(np.asarray(angles, dtype=float))
    r = np.asarray(cut, dtype=float)
    # Close the trace only on a full revolution. Closing a partial cut drew a
    # chord straight across the span that was never measured.
    if scan.is_full_circle():
        th = np.append(th, th[0] + 2 * np.pi)
        r = np.append(r, r[0])

    fig = plt.figure(figsize=(6, 6))
    ax = fig.add_subplot(111, projection="polar")
    ax.plot(th, r, lw=1.6)
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)

    # Fit the radial axis to the data instead of clipping at a fixed -40 dB.
    lo = scan.db_floor if scan.db_floor is not None else 10.0 * np.floor(r.min() / 10.0)
    hi = 0.0 if normalize else 10.0 * np.ceil(r.max() / 10.0)
    if hi <= lo:
        hi = lo + 10.0
    ax.set_ylim(lo, hi)
    if not scan.is_full_circle():
        ax.set_thetamin(float(np.degrees(th.min())))
        ax.set_thetamax(float(np.degrees(th.max())))
    ax.set_rlabel_position(135)
    ax.grid(True, alpha=0.4)
    ax.set_title(f"Pattern @ {freqs[k]/1e9:.4f} GHz"
                 f"{' (normalized)' if normalize else ''}", pad=18)

    out = scan.outdir / "pattern.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
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
    a = p.parse_args(argv)

    vcfg = VnaConfig(resource=a.vna, start_hz=a.start_ghz * 1e9,
                     stop_hz=a.stop_ghz * 1e9, points=a.points,
                     if_bw_hz=a.ifbw, power_dbm=a.power, parameter=a.param,
                     aux_parameter=a.aux_param, averaging=a.averaging)
    pcfg = PositionerConfig(resource=a.pos, position_tol_deg=a.tol,
                            backlash_deg=a.backlash,
                            move_timeout_s=a.move_timeout)
    cmds = PositionerCmds(slot=a.slot, device=a.device)
    scan = ScanConfig(start_deg=a.from_deg, stop_deg=a.to_deg, step_deg=a.step,
                      return_home=not a.no_return_home,
                      closure_check=not a.no_closure,
                      cut_freq_hz=(a.cut_ghz * 1e9 if a.cut_ghz else None),
                      db_floor=a.db_floor,
                      write_s2p=a.s2p, outdir=a.outdir)

    angles = scan.angles()
    if len(angles) == 0:
        print("no angles to measure", file=sys.stderr)
        return 2
    if abs(angles[-1] - scan.stop_deg) > 1e-6 and not scan.is_full_circle():
        print(f"note: {scan.step_deg:g} deg steps do not divide the span; "
              f"last angle is {angles[-1]:.2f}, not {scan.stop_deg:.2f}")

    rm = pyvisa.ResourceManager()
    vna = pos = None
    meta: dict = {
        "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vna": {k: v for k, v in vars(vcfg).items()},
        "positioner": {k: v for k, v in vars(pcfg).items()},
        "scan": {k: (str(v) if isinstance(v, Path) else v)
                 for k, v in vars(scan).items()},
    }
    try:
        vna = Vna(rm, vcfg)
        meta["vna_idn"] = vna.idn()
        print(f"VNA : {meta['vna_idn']}")
        vna.configure()
        vna.warm_up()
        corr = vna.correction_state()
        meta["correction_state"] = corr
        st = vna.sweep_time_s()
        meta["sweep_time_s"] = st
        print(f"      {vcfg.points} pts, {vcfg.start_hz/1e9:.3f}-{vcfg.stop_hz/1e9:.3f} GHz, "
              f"IFBW {vcfg.if_bw_hz:.0f} Hz, sweep {st*1e3:.1f} ms")
        if corr in ("0", "OFF", "off"):
            print("      WARNING: error correction is OFF - this run is uncalibrated",
                  file=sys.stderr)
        else:
            print(f"      correction: {corr}")

        pos = Positioner(rm, pcfg, cmds)
        if pcfg.speed_preset is not None:
            pos.set_speed(pcfg.speed_preset)
        if a.zero_here:
            pos.zero_here()
        print(f"POS : slot {cmds.slot} device {cmds.device}, "
              f"at {pos.position():.2f} deg")

        n = len(angles)
        per = (st if st == st else 0.1) + pcfg.settle_s + scan.step_deg * 0.2
        print(f"\nscanning {n} points over {scan.span_deg():.1f} deg, "
              f"rough estimate {n * per / 60:.1f} min\n")

        angles, actual, freqs, data, closure = run_scan(vna, pos, scan)
        meta["closure"] = closure
        # Plot the angles the positioner actually reached, not the ones asked for.
        polar_plot(actual, freqs, data, scan)

        if scan.return_home:
            print("returning to start position...")
            pos.seek(float(angles[0]))

        meta["finished_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta_path = scan.outdir / "run_meta.json"
        meta_path.write_text(json.dumps(meta, indent=2, default=str))
        print(f"wrote {meta_path}")

    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        # Any exit path - clean, error, or Ctrl-C - halts the axis before the
        # socket closes. Previously only KeyboardInterrupt did, so a comms
        # error left the table turning with nobody listening.
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
