"""VNA and positioner wrappers.

The only module that knows about VISA. Everything above it talks to these two
classes, which is what lets the engine run unchanged against mocks.
"""

from __future__ import annotations

import sys
import time

import numpy as np
import pyvisa

from .config import PositionerCmds, PositionerConfig, VnaConfig, wrap180


def parse_floats(raw: str) -> np.ndarray:
    """Parse a comma-separated SCPI response, tolerating trailing separators."""
    txt = raw.strip().rstrip(",")
    if not txt:
        return np.empty(0, dtype=float)
    return np.array([float(t) for t in txt.split(",")], dtype=float)


class PositionError(RuntimeError):
    """The positioner did not reach the commanded angle within tolerance."""


class Vna:
    """Copper Mountain A2202-Fx over the S2VNA socket server.

    configure() deliberately does NOT issue SYST:PRES. A preset would clear the
    calibration along with the stale state. Instead every setting the
    measurement depends on is written explicitly, and correction state is
    queried so an uncalibrated run is loud rather than silent.
    """

    def __init__(self, rm: pyvisa.ResourceManager, cfg: VnaConfig):
        self.cfg = cfg
        self.io = rm.open_resource(cfg.resource)
        self.io.timeout = cfg.timeout_ms
        self.io.read_termination = "\n"
        self.io.write_termination = "\n"
        self.ch = cfg.channel
        self.npoints = cfg.points
        self._traces: list[str] = []

    @property
    def traces(self) -> list[str]:
        return list(self._traces)

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
        self.check_errors("after configure")

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
        f = parse_floats(raw)
        if len(f) != self.npoints:
            raise RuntimeError(
                f"VNA returned {len(f)} stimulus points, expected {self.npoints}")
        return f

    def _read_trace(self, index: int) -> np.ndarray:
        self.io.write(f"CALC{self.ch}:PAR{index:d}:SEL")
        raw = self.io.query(f"CALC{self.ch}:DATA:SDAT?")
        flat = parse_floats(raw)                # re, im, re, im, ...
        if len(flat) != 2 * self.npoints:
            raise ValueError(
                f"trace {index}: got {len(flat)} values, expected {2 * self.npoints}")
        return flat[0::2] + 1j * flat[1::2]

    def measure(self) -> dict[str, np.ndarray]:
        """Trigger one sweep, block until done, return {param: complex array}.

        A short or truncated response is retried once before giving up; an
        unchecked length blows up on array assignment mid-scan instead.
        """
        self.io.write("TRIG:SING")
        self.io.query("*OPC?")                  # blocks until the sweep completes
        out: dict[str, np.ndarray] = {}
        for i, par in enumerate(self._traces, start=1):
            try:
                out[par] = self._read_trace(i)
            except ValueError as exc:
                print(f"[vna] {exc}; re-reading", file=sys.stderr)
                out[par] = self._read_trace(i)
        return out

    def check_errors(self, where: str) -> str | None:
        try:
            err = self.io.query("SYST:ERR?").strip()
        except pyvisa.VisaIOError:
            return None
        if err and not err.startswith(("0", "+0")):
            print(f"[vna] error {where}: {err}", file=sys.stderr)
            return err
        return None

    def close(self) -> None:
        try:
            self.io.close()
        except Exception:
            pass


class Positioner:
    """ETS-Lindgren EMControl 7006-001 card in an EMCenter chassis."""

    def __init__(self, rm: pyvisa.ResourceManager,
                 cfg: PositionerConfig, cmds: PositionerCmds):
        self.cfg, self.cmds = cfg, cmds
        self.io = rm.open_resource(cfg.resource)
        self.io.timeout = cfg.timeout_ms
        self.io.write_termination = cfg.write_termination
        self.io.read_termination = cfg.read_termination
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

    def seek(self, deg: float) -> float:
        """Command an absolute move and block until the axis is really there.

        Backlash takeup: when enabled and the move reverses direction, the
        target is approached from below so every angle is reached with the gear
        train loaded the same way.
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
        return actual

    def _attempt(self, deg: float, tol: float) -> float | None:
        """Issue the move and poll. Returns the settled position, or None.

        Completion requires motion-complete AND an in-tolerance readback on
        `stable_polls` consecutive reads. Trusting *OPC? alone lets a controller
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
            if not moving and abs(wrap180(here - deg)) <= tol:
                good += 1
                if good >= self.cfg.stable_polls:
                    break
            else:
                good = 0
            time.sleep(self.cfg.poll_s)

        time.sleep(self.cfg.settle_s)           # mechanical ring-down
        actual = self.position()
        return actual if abs(wrap180(actual - deg)) <= tol else None

    def close(self) -> None:
        try:
            self.io.close()
        except Exception:
            pass
