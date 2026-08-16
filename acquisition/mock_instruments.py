"""Fake VNA and positioner so the acquisition state machine can be exercised
with nothing physically moving.

This is faster than the EMControl simulation mode for software-side bugs, and
unlike the real hardware it can be told to misbehave on cue: drop a poll, hand
back a truncated sweep, report motion complete before it starts moving, or jam.

Usage:
    import mock_instruments
    mock_instruments.install(faults=mock_instruments.Faults(early_opc=True))
    import pattern_measure
    pattern_measure.main([...])
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pyvisa


@dataclass
class Faults:
    early_opc: bool = False      # assert motion-complete before actually arriving
    lag_deg: float = 0.0         # settle this far short of every target
    stuck: bool = False          # never reach the target
    drop_sweep: int | None = None    # raise VI_ERROR_TMO on the Nth SDAT read
    short_sweep: int | None = None   # return a truncated Nth sweep (once)
    drop_polls: int = 0          # fail this many positioner polls, then recover


@dataclass
class State:
    faults: Faults = field(default_factory=Faults)
    stops: list = field(default_factory=list)
    sweeps: int = 0
    polls_dropped: int = 0
    short_done: bool = False


class FakeVna:
    """Copper Mountain-flavoured responder. Returns a cos-shaped pattern whose
    amplitude tracks the positioner angle, so the plot has recognisable lobes."""

    def __init__(self, state: State, pos: "FakePositioner"):
        self.s, self.pos = state, pos
        self.timeout = 0
        self.read_termination = self.write_termination = "\n"
        self.points, self.f0, self.f1 = 101, 2e9, 3e9
        self.traces = ["S21"]
        self.corr = "1"

    def write(self, cmd: str) -> None:
        if ":SWE:POIN" in cmd:
            self.points = int(float(cmd.split()[-1]))
        elif ":FREQ:STAR" in cmd:
            self.f0 = float(cmd.split()[-1])
        elif ":FREQ:STOP" in cmd:
            self.f1 = float(cmd.split()[-1])
        elif ":PAR:COUN" in cmd:
            self.traces = self.traces[:int(cmd.split()[-1])]
        elif ":DEF " in cmd:
            i = int(cmd.split(":PAR")[1].split(":")[0]) - 1
            while len(self.traces) <= i:
                self.traces.append("")
            self.traces[i] = cmd.split()[-1]
        elif cmd.strip().endswith("TRIG:SING"):
            self.s.sweeps += 1

    def _freqs(self) -> np.ndarray:
        return np.linspace(self.f0, self.f1, self.points)

    def _sdat(self) -> np.ndarray:
        f = self._freqs()
        ang = np.radians(self.pos.pos)
        amp = 0.3 * abs(np.cos(ang)) ** 1.5 + 0.004
        return amp * np.exp(1j * 2 * np.pi * f / 3e9)

    def query(self, cmd: str) -> str:
        if cmd.startswith("*IDN?"):
            return "Copper Mountain Technologies,A2202-Fx,00000,1.0\n"
        if cmd.startswith("*OPC?"):
            return "1\n"
        if "SYST:ERR?" in cmd:
            return '0,"No error"\n'
        if "CORR:STAT?" in cmd:
            return self.corr + "\n"
        if "SWE:TIME?" in cmd:
            return "0.0213\n"
        if "FREQ:DATA?" in cmd:
            return ",".join(f"{x:.3f}" for x in self._freqs()) + "\n"
        if "DATA:SDAT?" in cmd:
            n = self.s.sweeps
            if self.s.faults.drop_sweep == n:
                raise pyvisa.VisaIOError(-1073807339)      # VI_ERROR_TMO
            s = self._sdat()
            if self.s.faults.short_sweep == n and not self.s.short_done:
                self.s.short_done = True
                s = s[:-2]                                 # truncated response
            out = []
            for v in s:
                out += [f"{v.real:.9e}", f"{v.imag:.9e}"]
            return ",".join(out) + "\n"
        raise RuntimeError(f"unhandled VNA query: {cmd!r}")

    def close(self) -> None:
        pass


class FakePositioner:
    """EMCenter-flavoured responder with a simple slew model: the axis advances
    toward the target a fixed amount per poll rather than teleporting."""

    SLEW_PER_POLL = 12.0

    def __init__(self, state: State):
        self.s = state
        self.timeout = 0
        self.read_termination = self.write_termination = "\n"
        self.pos = 0.0
        self.target = 0.0
        self.baud_rate = 9600
        self.data_bits = 8
        self.parity = None
        self.stop_bits = None

    def _advance(self) -> None:
        f = self.s.faults
        goal = self.target
        if f.stuck:
            goal = self.target - 20.0
        elif f.lag_deg:
            goal = self.target - f.lag_deg
        d = goal - self.pos
        if abs(d) <= self.SLEW_PER_POLL:
            self.pos = goal
        else:
            self.pos += np.sign(d) * self.SLEW_PER_POLL

    def write(self, cmd: str) -> None:
        body = cmd.split(":", 1)[1] if ":" in cmd else cmd
        if body.startswith("SK "):
            self.target = float(body.split()[1])
        elif body.startswith("CP "):
            self.pos = self.target = float(body.split()[1])
        elif body.startswith("ST"):
            self.s.stops.append(round(self.pos, 2))

    def query(self, cmd: str) -> str:
        body = cmd.split(":", 1)[1] if ":" in cmd else cmd
        if self.s.polls_dropped < self.s.faults.drop_polls:
            self.s.polls_dropped += 1
            raise pyvisa.VisaIOError(-1073807339)
        if body.startswith("*OPC?"):
            arrived = abs(self.pos - self.target) < 1e-9
            self._advance()
            if self.s.faults.early_opc:
                return "1\n"
            return "1\n" if arrived else "0\n"
        if body.startswith("CP?"):
            return f"{self.pos:.1f}\n"
        raise RuntimeError(f"unhandled positioner query: {cmd!r}")

    def close(self) -> None:
        pass


def install(faults: Faults | None = None) -> State:
    """Monkeypatch pyvisa so pattern_measure opens fakes instead of hardware."""
    state = State(faults=faults or Faults())
    pos = FakePositioner(state)
    vna = FakeVna(state, pos)

    class FakeRM:
        def open_resource(self, resource, **kw):
            r = resource.upper()
            is_pos = "192.168" in r or r.startswith("ASRL")
            return pos if is_pos else vna

    pyvisa.ResourceManager = lambda *a, **k: FakeRM()
    state.vna, state.positioner = vna, pos
    return state
