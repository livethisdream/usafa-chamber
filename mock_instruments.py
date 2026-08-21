#!/usr/bin/env python3
"""Fake instruments that impersonate pyvisa, so the real drivers can be tested.

The point of faking at *this* layer is what it leaves under test. The
dashboard's `SimBackend` fakes one level higher — it synthesizes a finished
pattern — which means it never executes `Vna` or `Positioner` at all. Every
hardware fix in those two classes (the retry after a resync, the split-reply
guard, the motion-start grace period, the trigger-state restore) was verified
once by hand at the chamber and has had nothing checking it since.

These fakes replace `pyvisa.ResourceManager`, so `pattern_measure` opens them
believing they are instruments and drives them through its own wrappers. That
puts the driver code on the test bench, and — unlike the real rig — these
instruments can be told to misbehave on cue.

The faults are not invented. Each one reproduces something the rig actually
did, recorded in the project note:

    split_reply_at_deg
                     a corrupted FTDI byte splits '255.0 DEGREES' into
                     '2' + '55.0 DEGREES'; the fragment parses as a plausible
                     angle and silently mislabels the cut
    error_on_write   a provably valid 'SK' rejected with 'ERROR 1' mid-scan
    error_on_query   the same corruption landing on a query instead
    sweep_time_dies  SENS:SWE:TIME? answers -110 and then never replies

Usage:
    import mock_instruments
    mock_instruments.install(mock_instruments.Faults(split_reply_at_deg=180))
    import pattern_measure
    pattern_measure.main([...])
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import pyvisa

VI_ERROR_TMO = -1073807339


@dataclass
class Faults:
    # -- positioner -------------------------------------------------------
    # Targeted by angle, not by read index, and deliberately so: the fragment
    # only misleads when its leading digits differ from the true angle. Split
    # '0.0 DEGREES' and the head is '0', which is the right answer by accident
    # and proves nothing. Split '180.0 DEGREES' and the head is '1'.
    split_reply_at_deg: float | None = None
    error_on_write: int | None = None    # 'ERROR 1' ack on the Nth write
    error_on_query: int | None = None    # 'ERROR 1' answer to the Nth query
    persistent_error: bool = False       # every ack is an ERROR; retry cannot help
    silent_acks: bool = False            # writes are never acknowledged at all
    never_moves: bool = False            # reports complete, never leaves the spot
    stuck: bool = False                  # reports in-motion forever
    late_start_polls: int = 0            # report complete for N polls before moving
    lag_deg: float = 0.0                 # park this far short of every target
    # -- VNA --------------------------------------------------------------
    drop_sweep: int | None = None        # VI_ERROR_TMO on the Nth data read
    short_sweep: int | None = None       # truncate the Nth sweep, once
    sweep_time_dies: bool = True         # emulate the A2202-Fx's -110 on SWE:TIME?


@dataclass
class State:
    faults: Faults = field(default_factory=Faults)
    stops: list = field(default_factory=list)
    sweeps: int = 0
    writes: int = 0
    queries: int = 0
    pos_reads: int = 0
    resyncs: int = 0
    short_done: bool = False
    split_done: bool = False
    trigger_restored: bool = False


class FakeVna:
    """Copper Mountain A2202-Fx responder, speaking the SCPI pattern_measure
    actually sends. The pattern is the same array factor the dashboard's
    SimBackend uses, so a rigcheck run and a `--sim` dashboard run agree."""

    NULL_FLOOR_DB = -45.0

    def __init__(self, state: State, pos: "FakePositioner"):
        self.s, self.pos = state, pos
        self.timeout = 0
        self.read_termination = self.write_termination = "\n"
        self.points, self.f0, self.f1 = 101, 2e9, 3e9
        self.param = "S21"
        self.ch = 1
        # What close() must put back. The real instrument is left in TRIG:SOUR
        # BUS by a run, and a scenario asserts that these come back.
        self.trig_source = "INT"
        self.init_cont = "1"

    # -- helpers ----------------------------------------------------------
    def _freqs(self) -> np.ndarray:
        return np.linspace(self.f0, self.f1, self.points)

    def _sweep(self) -> np.ndarray:
        th = math.radians(self.pos.angle)
        x = math.pi * 4.0 * math.sin(th)
        af = 1.0 if abs(x) < 1e-9 else abs(math.sin(x) / x)
        env_db = max(20.0 * math.log10(max(af, 1e-12)), self.NULL_FLOOR_DB)
        f = self._freqs()
        tilt_db = -0.7 * (f - f[0]) / max(f[-1] - f[0], 1.0)
        rng = np.random.default_rng(int(self.pos.angle * 10) & 0xFFFF)
        mag = 10.0 ** ((env_db + tilt_db + rng.normal(0.0, 0.05, f.size)) / 20.0)
        phase = np.radians((self.pos.angle * 3.0 + np.linspace(0, 180, f.size)) % 360.0)
        return mag * np.exp(1j * phase)

    # -- pyvisa surface ---------------------------------------------------
    def write(self, cmd: str) -> None:
        c = cmd.strip()
        if ":SWE:POIN" in c:
            self.points = int(float(c.split()[-1]))
        elif ":FREQ:STAR" in c:
            self.f0 = float(c.split()[-1])
        elif ":FREQ:STOP" in c:
            self.f1 = float(c.split()[-1])
        elif ":PAR1:DEF" in c:
            self.param = c.split()[-1]
        elif c.startswith("TRIG:SOUR "):
            value = c.split(None, 1)[1]
            # Restoring the saved source is what close() does; note it so a
            # scenario can prove the instrument was handed back usable.
            if value != "BUS":
                self.s.trigger_restored = True
            self.trig_source = value
        elif c.startswith("INIT") and ":CONT " in c:
            self.init_cont = c.split()[-1]
        elif c.endswith("TRIG:SING"):
            self.s.sweeps += 1

    def query(self, cmd: str) -> str:
        c = cmd.strip()
        if c.startswith("*IDN?"):
            return "CMT, A2202-Fx, 26018474, 26.3.1/1\n"
        if c.startswith("*OPC?"):
            return "1\n"
        if "SWE:TIME?" in c:
            # Firmware 26.3.1 answers -110 and then goes quiet, so the query
            # times out. pattern_measure catches this and reports "n/a".
            if self.s.faults.sweep_time_dies:
                raise pyvisa.VisaIOError(VI_ERROR_TMO)
            return "0.0213\n"
        if "SYST:ERR?" in c:
            return '0,"No error"\n'
        if "CORR:STAT?" in c:
            return "1\n"
        if c.startswith("TRIG:SOUR?"):
            return self.trig_source + "\n"
        if c.startswith("INIT") and ":CONT?" in c:
            return self.init_cont + "\n"
        if "FREQ:DATA?" in c:
            return ",".join(f"{x:.3f}" for x in self._freqs()) + "\n"
        if "DATA:SDAT?" in c:
            n = self.s.sweeps
            if self.s.faults.drop_sweep == n:
                raise pyvisa.VisaIOError(VI_ERROR_TMO)
            s = self._sweep()
            if self.s.faults.short_sweep == n and not self.s.short_done:
                self.s.short_done = True
                s = s[:-2]                          # truncated response
            out = []
            for v in s:
                out += [f"{v.real:.9e}", f"{v.imag:.9e}"]
            return ",".join(out) + "\n"
        raise RuntimeError(f"unhandled VNA query: {cmd!r}")

    def read(self) -> str:
        raise pyvisa.VisaIOError(VI_ERROR_TMO)

    def close(self) -> None:
        pass


class FakePositioner:
    """EMControl 7006-001 responder over a modelled reply stream.

    The stream is the point. The card answers a command with 'OK' or 'ERROR n'
    and a query with a value, and `Positioner` depends on reading exactly one
    reply per exchange — leaving one unread desynchronizes every later query,
    which is precisely how the split-reply bug corrupted a run. So replies live
    in a real queue here: read() pops one and raises a timeout when empty, and
    _flush_input() genuinely has to drain the orphaned fragment. A fake that
    just answered questions would let a broken resync pass.
    """

    SLEW_PER_POLL = 12.0
    IDN = "ETS-Lindgren, EMControl 7006-001, 2.10.3"

    def __init__(self, state: State):
        self.s = state
        self.timeout = 0
        self.read_termination, self.write_termination = "\n", "\r"
        self.baud_rate, self.data_bits, self.parity = 115200, 8, None
        self.angle = 0.0
        self.target = 0.0
        self.speed_pct = 100.0
        self._pending: deque[str] = deque()
        self._late = 0

    # -- motion model -----------------------------------------------------
    def _goal(self) -> float:
        f = self.s.faults
        if f.never_moves:
            return self.angle
        if f.stuck:
            return self.target + 1e6          # never arrives
        return self.target - f.lag_deg

    def _advance(self) -> None:
        goal = self._goal()
        d = goal - self.angle
        if abs(d) <= self.SLEW_PER_POLL:
            self.angle = goal
        else:
            self.angle += math.copysign(self.SLEW_PER_POLL, d)

    def _moving(self) -> bool:
        f = self.s.faults
        if f.stuck:
            return True
        if f.never_moves:
            return False
        if self._late < f.late_start_polls:
            self._late += 1
            return False                      # card has not reported motion yet
        return abs(self.angle - self._goal()) > 1e-9

    # -- reply stream -----------------------------------------------------
    def _ack(self) -> None:
        f = self.s.faults
        if f.silent_acks:
            return                            # nothing to read; a valid case
        if f.persistent_error:
            self._pending.append("ERROR 1")
            return
        if f.error_on_write is not None and self.s.writes == f.error_on_write:
            self._pending.append("ERROR 1")
            return
        self._pending.append("OK")

    def _answer(self, body: str) -> None:
        f = self.s.faults
        self.s.queries += 1
        if f.error_on_query is not None and self.s.queries == f.error_on_query:
            self._pending.append("ERROR 1")
            return

        if body.startswith("*IDN?"):
            self._pending.append(self.IDN)
        elif body.startswith("*OPC?"):
            moving = self._moving()
            if moving:
                self._advance()
            self._pending.append("0" if moving else "1")
        elif body.startswith("CP?"):
            self.s.pos_reads += 1
            reply = f"{self.angle:.1f} DEGREES"
            if (f.split_reply_at_deg is not None and not self.s.split_done
                    and abs(self.angle - f.split_reply_at_deg) < 0.05):
                self.s.split_done = True
                # A corrupted byte splits one reply into two reads. The leading
                # fragment parses as a plausible angle; the remainder is left
                # in the stream to poison the next query unless it is drained.
                head, tail = reply[:1], reply[1:]
                self._pending.append(head)
                self._pending.append(tail)
            else:
                self._pending.append(reply)
        elif body.startswith("SPEED?"):
            self._pending.append(f"{self.speed_pct:.1f}")
        elif body.startswith("ERR?"):
            self._pending.append("0")
        else:
            self._pending.append("ERROR 1")

    # -- pyvisa surface ---------------------------------------------------
    def write(self, cmd: str) -> None:
        body = cmd.split(":", 1)[1] if ":" in cmd else cmd
        body = body.strip()
        if body.endswith("?"):
            self._answer(body)
            return

        self.s.writes += 1
        if body.startswith("SK "):
            self.target = float(body.split()[1])
            self._late = 0
        elif body.startswith("CP "):
            self.angle = self.target = float(body.split()[1])
        elif body.startswith("SPEED "):
            self.speed_pct = float(body.split()[1])
        elif body.startswith("ST"):
            self.s.stops.append(round(self.angle, 2))
            self.target = self.angle
        self._ack()

    def read(self) -> str:
        if not self._pending:
            self.s.resyncs += 1               # a drain that found nothing left
            raise pyvisa.VisaIOError(VI_ERROR_TMO)
        return self._pending.popleft()

    def query(self, cmd: str) -> str:
        self.write(cmd)
        return self.read()

    def close(self) -> None:
        pass


def install(faults: Faults | None = None) -> State:
    """Monkeypatch pyvisa so pattern_measure opens fakes instead of hardware."""
    state = State(faults=faults or Faults())
    pos = FakePositioner(state)
    vna = FakeVna(state, pos)

    class FakeRM:
        def open_resource(self, resource, **kw):
            r = str(resource).upper()
            # The positioner is the serial/VXI-11 resource; the VNA is the
            # raw socket. Matches this rig's actual resource strings.
            is_pos = r.startswith("ASRL") or "INST0" in r or r.endswith("::INSTR")
            return pos if is_pos else vna

        def close(self):
            pass

    pyvisa.ResourceManager = lambda *a, **k: FakeRM()
    state.vna, state.positioner = vna, pos
    return state
