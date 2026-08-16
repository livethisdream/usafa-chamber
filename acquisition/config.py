"""Configuration dataclasses and the angle grid.

Kept free of any instrument or I/O dependency so the CLI, the service, and the
tests can all agree on what a run *is* without pulling in pyvisa.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


def wrap180(x: float) -> float:
    return (x + 180.0) % 360.0 - 180.0


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
    write_plot: bool = True
    outdir: Path = field(default_factory=lambda: Path("./pattern_run"))

    def angles(self) -> np.ndarray:
        """Angle grid that never overshoots stop_deg.

        The naive form, int(round(span/step)) + 1, walks past the requested stop
        whenever the span is not an integer multiple of the step: 0->355 in 30
        deg steps produced a 360 deg point, both a duplicate of 0 and an extra
        wrap of the cable. Floor instead, and let the caller report the
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
        if n > 1 and abs(wrap180(a[-1] - a[0])) < self.step_deg / 2:
            a = a[:-1]
        return a

    def span_deg(self) -> float:
        a = self.angles()
        return float(a[-1] - a[0]) if len(a) > 1 else 0.0

    def is_full_circle(self) -> bool:
        # bool() is load-bearing: numpy comparisons yield np.bool_, which is not
        # JSON serializable and would break the service's event stream.
        a = self.angles()
        return bool(len(a) > 2 and (a[-1] - a[0]) + self.step_deg >= 360.0 - 1e-6)

    def endpoint_shortfall(self) -> float:
        """How far short of stop_deg the last angle lands. 0 when it divides."""
        a = self.angles()
        if self.is_full_circle():
            return 0.0
        return float(self.stop_deg - a[-1])
