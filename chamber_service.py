#!/usr/bin/env python3
"""
WebSocket service backing the USAFA chamber dashboard.

Speaks the same shape of protocol as the Phaser service: JSON request/response
keyed by an `id`, plus unsolicited push frames for anything the UI needs to
learn about as it happens.

    client -> {"cmd": "start_scan", "id": "req_7", ...args}
    server -> {"id": "req_7", "ok": true, "data": {...}}
    server -> {"type": "scan_point", "index": 3, ...}          (unsolicited)

Two backends implement one interface:
  * HardwareBackend — drives the real A2202-Fx and EMControl 7006-001 through
    the instrument wrappers in pattern_measure.py, so every hardware fix made
    there applies here with no duplication.
  * SimBackend      — synthesizes a plausible pattern and preserves the payload
    contract exactly, so the frontend is fully developable away from the chamber.

A third, SdrBackend, puts a B205 measuring one CW tone in the VNA's place, and
any backend can take a ManualPositioner in the tower's: each scan step prompts
the operator for the angle and waits for them to say it is there.

Run:
    python chamber_service.py --sim        # no hardware needed
    python chamber_service.py              # real rig, falls back to sim if absent
    python chamber_service.py --sdr sim --sdr-tx sim --manual   # SDR + hand-turned, simulated
    python chamber_service.py --sdr serial=RX --sdr-tx serial=TX --manual
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import numpy as np

import pattern_measure as pm
import sdr_pattern as sp

DEFAULT_PORT = 8766          # 8765 belongs to the Phaser service; coexist with it
RUNS_DIR = Path("runs")
# One file, overwritten by each calibration. Runs copy it rather than pointing
# at it: a run that references a file the next calibration overwrites is a run
# that lies about itself. Derived from RUNS_DIR at call time, not bound at
# import, so a caller that redirects RUNS_DIR redirects this too.
CAL_NAME = Path("calibration/cal.json")


# --------------------------------------------------------------------------
# Scan parameters shared by both backends
# --------------------------------------------------------------------------

@dataclass
class ScanRequest:
    start_deg: float = 0.0
    stop_deg: float = 355.0
    step_deg: float = 5.0
    start_hz: float = 2.0e9
    stop_hz: float = 3.0e9
    points: int = 101
    if_bw_hz: float = 1.0e3
    power_dbm: float = 0.0
    parameter: str = "S21"
    name: str = ""

    @classmethod
    def from_args(cls, a: dict) -> "ScanRequest":
        f = {k: a[k] for k in cls.__dataclass_fields__ if k in a}
        return cls(**f)

    def angles(self) -> np.ndarray:
        return pm.ScanConfig(start_deg=self.start_deg, stop_deg=self.stop_deg,
                             step_deg=self.step_deg).angles()


@dataclass
class CalRequest:
    """One calibration, and the sweep it is taken at.

    Carries the sweep because a calibration is only meaningful paired with it.
    The worker configures the VNA from these values before calibrating, so the
    record and the instrument cannot disagree about what was calibrated.

    `reference_plane` is free text and is never inspected. Nothing in the data
    distinguishes a cal taken at the instrument front panel from one taken at
    the cable ends inside the chamber, and the difference is every dB of cable
    loss in the pattern - so the operator states it and it gets written down.
    """
    start_hz: float = 2.0e9
    stop_hz: float = 3.0e9
    points: int = 101
    if_bw_hz: float = 1.0e3
    power_dbm: float = 0.0
    parameter: str = "S21"
    # One port or two. A reflection measurement on a single antenna wants the
    # 1-port: nothing is connected to the other side to calibrate against.
    ports: tuple[int, ...] = (1, 2)
    orient: bool = False
    reference_plane: str = ""

    @classmethod
    def from_args(cls, a: dict) -> "CalRequest":
        f = {k: a[k] for k in cls.__dataclass_fields__ if k in a}
        if "ports" in f:
            ports = tuple(dict.fromkeys(int(p) for p in f["ports"]))[:2]
            if not ports:
                raise BackendError("a calibration needs at least one port")
            if any(p not in (1, 2) for p in ports):
                raise BackendError(f"ports are 1 and 2, not {list(ports)}")
            f["ports"] = ports
        return cls(**f)

    def sweep(self) -> dict:
        return {"start_hz": self.start_hz, "stop_hz": self.stop_hz,
                "points": self.points, "if_bw_hz": self.if_bw_hz,
                "power_dbm": self.power_dbm, "parameter": self.parameter}

    def as_scan(self) -> ScanRequest:
        return ScanRequest(**self.sweep())


@dataclass
class SweepRequest:
    """One VNA capture at a fixed position, with no positioner involvement.

    The tower is not addressed at all - not held, not read, not moved - so this
    is the one measurement that works on a bench with only the VNA attached.

    All four S-parameters by default. A Smith chart wants reflection and a
    transmission measurement wants S21, and taking them in one capture is the
    difference between a set that describes one instant and four that describe
    four. `parameter` is absent on purpose: the request says which parameters,
    plural, and `as_scan()` supplies a single one only because configure()
    needs something to program before set_parameter() takes over.
    """
    start_hz: float = 2.0e9
    stop_hz: float = 3.0e9
    points: int = 101
    if_bw_hz: float = 1.0e3
    power_dbm: float = 0.0
    parameters: tuple[str, ...] = ("S11", "S21", "S12", "S22")

    @classmethod
    def from_args(cls, a: dict) -> "SweepRequest":
        f = {k: a[k] for k in cls.__dataclass_fields__ if k in a}
        if "parameters" in f:
            names = [str(p).upper() for p in f["parameters"]]
            bad = [p for p in names if p not in ("S11", "S21", "S12", "S22")]
            if bad:
                raise BackendError(f"not S-parameters: {', '.join(bad)}")
            if not names:
                raise BackendError("a sweep with no parameters measures nothing")
            f["parameters"] = tuple(dict.fromkeys(names))   # de-duped, ordered
        return cls(**f)

    def sweep(self) -> dict:
        return {"start_hz": self.start_hz, "stop_hz": self.stop_hz,
                "points": self.points, "if_bw_hz": self.if_bw_hz,
                "power_dbm": self.power_dbm}

    def as_scan(self) -> ScanRequest:
        return ScanRequest(parameter=self.parameters[0], **self.sweep())


# Fields whose disagreement between a calibration and a run matters. Power is
# in the list because the A2202-Fx corrects per source level; parameter is not,
# because a 2-port cal corrects every parameter it collected.
CAL_SWEEP_KEYS = ("start_hz", "stop_hz", "points", "if_bw_hz", "power_dbm")


def cal_mismatch(record: dict | None, req) -> list[str]:
    """Which sweep settings a run does not share with the calibration.

    Empty when they agree, or when there is no calibration to disagree with -
    "uncalibrated" is already reported by correction_state and does not need
    saying twice in different words.
    """
    if not record:
        return []
    sweep = record.get("sweep") or {}
    out = []
    for k in CAL_SWEEP_KEYS:
        have, want = sweep.get(k), getattr(req, k, None)
        if have is None or want is None:
            continue
        if abs(float(have) - float(want)) > 1e-6:
            out.append(f"{k} {have:g} -> {want:g}")
    return out


def cal_unverified(record: dict | None, correction: str | None) -> str | None:
    """Why a correction reading ON still cannot be vouched for, or None.

    The dashboard can see *that* the instrument is corrected - CORR:STAT? says
    so, and the pill goes green on it - but not what sweep the correction was
    solved at, unless it ran the calibration itself. A calibration performed in
    the VNA's own software leaves no record here, so cal_mismatch has nothing
    to compare against and returns empty: "no calibration to disagree with".

    That silence is the wrong answer in the one case where it matters most.
    Every run configures the sweep before measuring, and setting the sweep is
    exactly what invalidates a calibration - so an unrecorded cal is the state
    where the instrument is most likely to be quietly interpolating, and the
    state the operator is least likely to suspect, because the pill says cal.
    """
    if pm.correction_is_on(correction) is not True:
        return None          # off or unknown - correction_state already says so
    if record:
        return None          # cal_mismatch owns the recorded case
    return ("error correction is ON, but no calibration was taken through this "
            "dashboard - the sweep it was solved at is unknown, and setting "
            "this run's sweep may already have invalidated it")


def unreferenced_warning(caps: "Capabilities") -> str | None:
    """Why an uncalibrated reading from this receiver means less than a VNA's.

    A VNA hands back a ratio: S21 is divided by its own source, so drift in the
    source cancels and the number survives being uncalibrated. A source into an
    amplifier into an antenna into a separate receiver has drift at every stage
    and nothing cancelling it, so the same trace is only as stable as the whole
    chain. A monitor channel on a coupler restores the ratio; without one, the
    honest thing is to say the shape is trustworthy and the level is not.
    """
    if caps.ratio:
        return None
    if caps.monitor:
        return None          # the monitor channel is the ratio
    return ("this receiver is not self-referencing and has no monitor channel "
            "- source drift does not cancel, so relative pattern shape is "
            "meaningful but level is not")


def phase_warning(caps: "Capabilities") -> str | None:
    """Why the phase column of a pattern may not mean what it looks like.

    Every run writes `phase_deg` because the VNA path always had something
    real to put there. A chain whose captures are not phase-comparable still
    fills the column - each value is what was measured - but the relationship
    between one angle and the next is not a property of the antenna. Magnitude
    is unaffected, which is why this is a warning and not a refusal: a power
    pattern off such a chain is perfectly good, and only the phase is not.
    """
    if caps.coherent:
        return None
    return ("this receiver is not phase-coherent between captures - pattern "
            "magnitude is unaffected, but the phase column does not relate "
            "one angle to the next and should not be transformed")


def span_warning(caps: "Capabilities", req) -> str | None:
    """Why a sweep wider than one capture is not one measurement.

    A VNA steps its own synthesizer across whatever span it is given. An SDR
    digitizes a window, so a wider request is several captures at different
    centre frequencies - which is fine for magnitude and not fine for phase,
    because retuning leaves the LO at an arbitrary phase and the segments no
    longer share one reference.
    """
    if caps.span_hz is None:
        return None
    span = abs(float(req.stop_hz) - float(req.start_hz))
    if span <= caps.span_hz:
        return None
    segments = math.ceil(span / caps.span_hz)
    return (f"this sweep spans {span / 1e6:.0f} MHz and the receiver captures "
            f"{caps.span_hz / 1e6:.0f} MHz at a time - it is taken in "
            f"{segments} retuned segments, and phase does not carry across "
            f"the joins")


def receiver_warnings(caps: "Capabilities", req) -> list[str]:
    """Everything worth saying about this receiver before it measures.

    Gathered in one place because the scan path and the sweep path have to say
    the same things, and two lists drift. Each is a limit of the chain rather
    than a fault in it, so all of them are warnings and none refuses the run:
    what they buy the operator is knowing which parts of the answer to trust.
    """
    return [w for w in (unreferenced_warning(caps),
                        phase_warning(caps),
                        span_warning(caps, req)) if w]


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

class BackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class Capabilities:
    """What the installed receiver can actually do.

    Published in get_state so neither the service nor the dashboard has to
    guess from which backend is loaded. `has_positioner` already worked this
    way and is the model: the tower's absence degrades the UI rather than
    erroring it, and a receiver that cannot calibrate deserves the same
    treatment instead of an AttributeError three layers down.

    The fields are the questions something actually branches on:

    `parameters`  what set_parameter accepts. A VNA takes four; a one-channel
                  SDR measures the path it is wired to and nothing else.
    `reflection`  whether any of that is reflection - the Smith chart and the
                  impedance readout have no meaning without it.
    `calibration` whether a SOLT/AutoCal exists to be run at all.
    `ratio`       whether a reading is referenced against its own source. This
                  is the quiet thing a VNA provides: S21 divides by the source,
                  so source drift cancels and an uncalibrated number still
                  means something. A bare SDR has drift in every stage and
                  nothing cancelling it.
    `monitor`     whether that reference comes from a second receive channel
                  watching a coupler on the source. An X310 with a populated
                  second slot can; a B205mini cannot, which is the whole
                  argument for the bigger box.
    `coherent`    whether phase is comparable *between* captures, and so
                  whether the phase column of a pattern means anything from
                  one angle to the next. Magnitude survives a chain that this
                  is false for; a near-field transform does not.
    `span_hz`     how much spectrum one capture covers, or None for an
                  instrument that sweeps its own range. A VNA steps its
                  synthesizer and hands back the whole span; an SDR digitizes
                  a window, and a wider request has to be taken in retuned
                  segments.
    `fixed_hz`    the one frequency a tone receiver measures at, or None for
                  one that takes the sweep it is asked for. A CW source on a
                  separate radio is set where it is set; the sweep fields have
                  nothing to say to it, and the dashboard locks them.

    These describe the *installed configuration*, not what the box is capable
    of. An X310 with two daughterboards and no coupler on the source has no
    monitor channel and no ratio, and is a B205mini as far as anything here is
    concerned. Capability that is not wired up is not capability.
    """

    parameters: tuple[str, ...] = ("S11", "S21", "S12", "S22")
    reflection: bool = True
    calibration: bool = True
    ratio: bool = True
    monitor: bool = False
    coherent: bool = True
    span_hz: float | None = None
    fixed_hz: float | None = None

    def as_dict(self) -> dict:
        return {"parameters": list(self.parameters),
                "reflection": self.reflection,
                "calibration": self.calibration,
                "ratio": self.ratio,
                "monitor": self.monitor,
                "coherent": self.coherent,
                "span_hz": self.span_hz,
                "fixed_hz": self.fixed_hz}


# A VNA: four parameters, reflection, a cal module, and self-referencing.
VNA_CAPS = Capabilities()

# One receive channel wired to one path. No reflection without a coupler, no
# SOLT to run, and nothing cancelling source drift. The floor every other
# receiver is measured against - if the app works on this, it works on all of
# them.
SCALAR_CAPS = Capabilities(parameters=("S21",), reflection=False,
                           calibration=False, ratio=False, monitor=False,
                           coherent=False, span_hz=56e6)

# An X310 with both slots populated, a coupler on the source, and the second
# receive channel watching it. The design note's "the second channel is the
# calibration", wired: the monitor restores the ratio a VNA gives for free, so
# source drift cancels and level means something again. Everything in the
# chassis shares a clock, so phase is comparable between captures.
#
# The span is the UBX-160's, and it is the field most likely to be wrong for
# somebody else's rig: it is the daughterboard's width, not the X310's, and
# the host network has to carry it - over 1 GbE the usable rate collapses to a
# fraction of this and the number here becomes a lie. Check the link before
# believing it.
#
# Wiring, not inventory. The same chassis without the coupler is SCALAR_CAPS
# with a wider span - two channels that are not ratioed are one channel and a
# spare.
X310_MONITOR_CAPS = Capabilities(parameters=("S21",), reflection=False,
                                 calibration=False, ratio=True, monitor=True,
                                 coherent=True, span_hz=160e6)


class RedoPrevious(Exception):
    """The operator asked for the previous angle to be measured again."""


class ManualPositioner:
    """A positioner that is a person.

    seek() pushes a prompt naming the angle and then waits for the operator to
    say the AUT is there. It keeps pm.Positioner's contract - seek, position,
    stop, an abort hook - so the scan loop drives a hand-turned tripod exactly
    as it drives the tower, and gains the one thing a person needs that a
    motor does not: "that one was wrong, do it again", which surfaces as
    RedoPrevious.

    The reported angle is the commanded one. Nothing measures where a tripod
    actually points, and a field that echoed the operator's guess back as a
    reading would be precision the data does not have.
    """

    manual = True

    def __init__(self):
        self._angle = 0.0
        self._cv = threading.Condition()
        self._pending: float | None = None
        self._answer: str | None = None
        self._push: Callable[[dict], None] = lambda payload: None

    def attach(self, push, _log) -> None:
        self._push = push

    def identity(self) -> str:
        return "manual - the AUT is turned by hand"

    def position(self) -> float:
        return self._angle

    def speed(self) -> float:
        return 0.0

    def latched_error(self) -> str:
        return "0"

    def set_speed(self, _pct: float) -> None:
        pass                     # a person sets their own pace

    def zero_here(self) -> None:
        self._angle = 0.0

    @property
    def pending(self) -> float | None:
        """The angle the operator is being asked for, or None."""
        return self._pending

    def respond(self, action: str) -> None:
        if action not in ("measure", "redo"):
            raise BackendError(f"unknown operator action: {action!r}")
        with self._cv:
            if self._pending is None:
                raise BackendError("nothing is waiting for the operator")
            self._answer = action
            self._cv.notify_all()

    def stop(self) -> None:
        with self._cv:
            if self._pending is not None:
                self._answer = "stop"
                self._cv.notify_all()

    def close(self) -> None:
        self.stop()

    def seek(self, deg: float, should_abort=None) -> float:
        deg = float(deg)
        with self._cv:
            self._pending, self._answer = deg, None
        self._push({"type": "manual_prompt", "deg": deg})
        try:
            with self._cv:
                while self._answer is None:
                    if should_abort is not None and should_abort():
                        raise pm.Aborted(f"stopped while waiting for the "
                                         f"operator at {deg:.1f} deg")
                    self._cv.wait(0.1)
                answer = self._answer
        finally:
            # Cleared on every exit, so a browser that reconnects mid-run is
            # never shown a prompt for an angle nobody is waiting on.
            with self._cv:
                self._pending, self._answer = None, None
            self._push({"type": "manual_cleared"})
        if answer == "stop":
            raise pm.Aborted(f"stopped while waiting for the operator at "
                             f"{deg:.1f} deg")
        if answer == "redo":
            raise RedoPrevious()
        self._angle = deg
        return deg


def _open_tower(rm, pos_resource: str | None, slot: int | None,
                device: str | None) -> "pm.Positioner":
    pcfg = pm.PositionerConfig()
    if pos_resource:
        pcfg.resource = pos_resource
    cmds = pm.PositionerCmds()
    if slot is not None:
        cmds.slot = slot
    if device:
        cmds.device = device
    return pm.Positioner(rm, pcfg, cmds)


class PositionerRole:
    """The positioner half of a backend, whichever receiver sits beside it.

    `self.pos` is the EMControl tower, a ManualPositioner, or None, and every
    method here answers for all three. Shared so the VNA and SDR backends
    cannot drift apart on the thing with a cable through it.
    """

    pos: Any = None

    @property
    def has_positioner(self) -> bool:
        return self.pos is not None

    def _require_positioner(self):
        if self.pos is None:
            raise BackendError(
                "no positioner attached - the service was started with "
                "--no-positioner, so nothing can turn the tower")
        return self.pos

    def pos_idn(self) -> str | None:
        return self.pos.identity() if self.pos else None

    def position(self) -> float | None:
        return self.pos.position() if self.pos else None

    def speed(self) -> float | None:
        return self.pos.speed() if self.pos else None

    def latched_error(self) -> str | None:
        return self.pos.latched_error() if self.pos else None

    def set_speed(self, pct: float) -> None:
        self._require_positioner().set_speed(pct)

    def zero_here(self) -> None:
        self._require_positioner().zero_here()

    def stop(self) -> None:
        # Not _require_positioner: stop is called on failure paths that do not
        # know whether there is an axis, and raising there would replace the
        # error being reported with one about the tower not existing.
        if self.pos:
            self.pos.stop()

    def seek(self, deg: float, should_abort=None) -> float:
        return self._require_positioner().seek(deg, should_abort=should_abort)

    def attach(self, push, log) -> None:
        hook = getattr(self.pos, "attach", None)
        if hook:
            hook(push, log)


class SdrBackend(PositionerRole):
    """A B205 measuring the power of one CW tone, beside any positioner.

    The receiver is sdr_pattern's - the same gated capture and the same
    Welch-and-Parseval tone estimate the CLI uses - so a reading here and a
    reading there are one measurement. What this adds is a transmitter on the
    same host, keyed only while something is being measured: configure() turns
    it on and release() turns it off, and the service calls release() on every
    way out of a scan or sweep. A tone left running after the run is over is
    the SDR version of a tower left turning.

    The level is dBFS, handed back as a linear amplitude so it lands in the
    same pattern.csv columns a VNA's S21 does. Nothing references it to the
    source, so the capabilities say ratio=False and the existing warning says
    what that costs.
    """

    def __init__(self, rx_cfg: "sp.RadioConfig", radio, positioner=None,
                 tx_radio=None, cap: "sp.CaptureConfig | None" = None,
                 mode: str = "hw"):
        self.rx_cfg = rx_cfg
        self.radio = radio
        self.tx = tx_radio
        self.pos = positioner
        self.cap = cap or sp.CaptureConfig()
        self.mode = mode
        self._rx_ready = False
        self._tx_thread: threading.Thread | None = None
        self._tx_stop = threading.Event()
        self._tx_error: BaseException | None = None
        self._log: Callable[[str, str, str], None] = lambda *_: None

    @property
    def tone_hz(self) -> float:
        return self.rx_cfg.freq_hz + self.rx_cfg.offset_hz

    def capabilities(self) -> Capabilities:
        return Capabilities(parameters=("S21",), reflection=False,
                            calibration=False, ratio=False, monitor=False,
                            coherent=False, span_hz=None, fixed_hz=self.tone_hz)

    def attach(self, push, log) -> None:
        self._log = log
        super().attach(push, log)

    # -- identity / state ---------------------------------------------------
    def vna_idn(self) -> str:
        return f"{self.radio.describe()} - tone {self.tone_hz / 1e6:.4f} MHz"

    def correction_state(self) -> str:
        return "n/a (scalar receiver)"

    def acm_module(self) -> str | None:
        return None

    def acm_characterization(self) -> dict | None:
        return None

    def calibrate(self, req, on_step=None, should_stop=None) -> str:
        raise BackendError("an SDR tone receiver has no calibration to run")

    def receiver_meta(self) -> dict:
        tx = self.tx.cfg if self.tx is not None else None
        return {"kind": "sdr_tone", "radio": self.radio.describe(),
                "tone_hz": self.tone_hz,
                "rx": self.rx_cfg.__dict__, "capture": self.cap.__dict__,
                "tx": tx.__dict__ if tx is not None else None,
                "units": "mag_db is dBFS; level is not referenced to the source"}

    # -- measurement --------------------------------------------------------
    def set_parameter(self, parameter: str) -> None:
        if parameter != "S21":
            raise BackendError(f"an SDR tone receiver measures one path; "
                               f"{parameter} is not something it can take")

    def configure(self, _req) -> None:
        # The sweep fields are not consulted. The tone is where the transmitter
        # puts it, and that is fixed when the service starts.
        if not self._rx_ready:
            self.radio.setup_rx()
            self._rx_ready = True
        if self.tx is None:
            self._log("info", "rx", f"no transmitter on this host - the tone "
                                    f"has to come from elsewhere, at "
                                    f"{self.tone_hz / 1e6:.4f} MHz")
        else:
            self._tx_on()

    def frequencies(self) -> np.ndarray:
        return np.array([self.tone_hz])

    def measure(self) -> np.ndarray:
        if self._tx_error is not None:
            err, self._tx_error = self._tx_error, None
            raise BackendError(f"the transmitter stopped: "
                               f"{type(err).__name__}: {err}")
        # The simulated radio synthesizes its pattern from the angle it is
        # told; a real one hears whatever the antenna is pointing at.
        if self.pos is not None and hasattr(self.radio, "angle"):
            self.radio.angle = self.pos.position()
        r = sp.read_tone(self.radio, self.rx_cfg, self.cap)
        where = (f"at {self.pos.position():.1f} deg: "
                 if self.pos is not None else "")
        for w in sp.warnings_for(r, self.cap, self.rx_cfg.offset_hz):
            self._log("warn", "rx", where + w)
        return np.array([10 ** (r.power_dbfs / 20) + 0j])

    # -- transmitter --------------------------------------------------------
    def _tx_on(self) -> None:
        if self._tx_thread is not None and self._tx_thread.is_alive():
            return
        self._tx_stop.clear()
        self._tx_error = None
        self._tx_thread = threading.Thread(target=self._tx_run, daemon=True,
                                           name="chamber-sdr-tx")
        self._tx_thread.start()
        time.sleep(0.5)          # let the tone settle before the first capture
        self._log("info", "tx", f"transmitter ON at {self.tone_hz / 1e6:.4f} "
                                f"MHz, gain {self.tx.cfg.tx_gain:.1f} dB")

    def _tx_run(self) -> None:
        try:
            self.tx.transmit_forever(stop=self._tx_stop)
        except BaseException as e:      # surfaced by the next measure()
            self._tx_error = e

    def _tx_off(self) -> None:
        t = self._tx_thread
        if t is None:
            return
        self._tx_stop.set()
        t.join(timeout=5.0)
        self._tx_thread = None
        self._log("info", "tx", "transmitter OFF")

    def release(self) -> None:
        self._tx_off()

    def stop(self) -> None:
        try:
            super().stop()
        finally:
            self._tx_off()

    def close(self) -> None:
        try:
            self._tx_off()
        finally:
            if self.pos:
                self.pos.close()


class SimBackend:
    """Synthetic rig. Same contract as HardwareBackend, no instruments required.

    Motion is simulated at a believable slew rate so the UI's live position
    readout and stop button have something real to exercise, and the pattern is
    a broadside-ish lobe with sidelobes so plots look like antenna data rather
    than a flat line.
    """

    mode = "sim"

    def __init__(self, slew_deg_s: float = 60.0,
                 caps: Capabilities = VNA_CAPS):
        self._angle = 0.0
        self._speed = 100.0
        self._slew = slew_deg_s
        self._caps = caps

    def capabilities(self) -> Capabilities:
        return self._caps

    # -- identity / state ---------------------------------------------------
    def vna_idn(self) -> str:
        return "CMT, A2202-Fx, SIMULATED, 26.3.1/1"

    def pos_idn(self) -> str:
        return "ETS-Lindgren, EMControl 7006-001, SIMULATED"

    def position(self) -> float:
        return self._angle

    def speed(self) -> float:
        return self._speed

    def latched_error(self) -> str:
        return "0"

    def correction_state(self) -> str:
        # Not "1". There is no calibration behind a synthesized pattern, and
        # saying otherwise would write a lie into meta.json.
        return "n/a"

    def acm_module(self) -> str | None:
        return "SIMULATED AutoCal module (no hardware)"

    def acm_characterization(self) -> dict | None:
        # The shape a real ACM2202.1 reports, so the label the cal log writes
        # is exercised in sim rather than only on the rig.
        return {"start": "100 kHz", "stop": "22 GHz", "points": 1601}

    def calibrate(self, req: "CalRequest", on_step=None,
                  should_stop=None) -> str:
        """Walk the procedure without calibrating anything.

        Returns "n/a", not "1". A simulated calibration corrects nothing, and
        the whole reason correction_state exists is so a run cannot claim to be
        calibrated when it is not - which a sim that answered "1" here would
        immediately break. What this is for is the UI: the modal, the steps,
        the cancel button and the record all get exercised away from the rig.
        """
        for step in ("orienting the module to the ports",
                     f"2-port AutoCal on ports {req.ports[0]} and {req.ports[1]}",
                     "applying"):
            if should_stop is not None and should_stop():
                raise pm.Aborted("calibration stopped after the previous step")
            if on_step:
                on_step(step)
            time.sleep(0.6)
        return "n/a"

    # -- control ------------------------------------------------------------
    def set_speed(self, pct: float) -> None:
        self._speed = float(pct)

    def zero_here(self) -> None:
        self._angle = 0.0

    def stop(self) -> None:
        pass

    def seek(self, deg: float, should_abort: Callable[[], bool] | None = None) -> float:
        deg = float(deg)
        rate = self._slew * max(self._speed, 1.0) / 100.0
        while abs(pm._wrap180(self._angle - deg)) > 0.05:
            if should_abort is not None and should_abort():
                raise pm.Aborted(f"move to {deg:.1f} deg aborted by request")
            step = rate * 0.05
            err = pm._wrap180(deg - self._angle)
            self._angle += math.copysign(min(step, abs(err)), err)
            self._angle = round(self._angle % 360.0, 3)
            time.sleep(0.05)
        self._angle = deg % 360.0
        return self._angle

    # -- measurement --------------------------------------------------------
    has_positioner = True       # the simulated rig always has a simulated tower

    def set_parameter(self, parameter: str) -> None:
        # The synthetic pattern does not vary by S-parameter. Recorded so the
        # payload names what it claims to have measured rather than silently
        # relabelling one trace four times.
        self._parameter = parameter

    def configure(self, req: ScanRequest) -> None:
        self._freqs = np.linspace(req.start_hz, req.stop_hz, req.points)

    def frequencies(self) -> np.ndarray:
        return self._freqs

    # Pattern floor in dB. Real chamber nulls bottom out on the noise floor
    # rather than reaching the ideal array factor's exact zeros, and a plot
    # whose radial axis is dragged to -120 dB by one null is unreadable.
    NULL_FLOOR_DB = -45.0

    def measure(self) -> np.ndarray:
        """A main lobe at 0 deg with decaying sidelobes, floored and dithered.

        Noise is added in dB and kept well below the null floor: adding it as a
        linear amplitude larger than the floor drives nulls into the clip limit
        and produces -120 dB spikes that no real measurement shows.
        """
        th = math.radians(self._angle)
        n = self._freqs.size
        # Array-factor envelope: narrow main lobe, decaying sidelobes.
        x = math.pi * 4.0 * math.sin(th)
        af = 1.0 if abs(x) < 1e-9 else abs(math.sin(x) / x)
        env_db = 20.0 * math.log10(max(af, 1e-12))
        env_db = max(env_db, self.NULL_FLOOR_DB)
        # Mild frequency tilt so the rectangular plot is not perfectly flat.
        span = max(self._freqs[-1] - self._freqs[0], 1.0)
        tilt_db = -0.7 * (self._freqs - self._freqs[0]) / span
        rng = np.random.default_rng(int(self._angle * 10) & 0xFFFF)
        mag_db = env_db + tilt_db + rng.normal(0.0, 0.05, n)
        mag = 10.0 ** (mag_db / 20.0)
        phase = np.radians((self._angle * 3.0 + np.linspace(0, 180, n)) % 360.0)
        time.sleep(0.06)                                  # stand in for a sweep

        if getattr(self, "_parameter", "S21") in ("S11", "S22"):
            # Reflection is not a pattern. The transmission model above peaks
            # at |S21| ~ 1, and handing that back for S11 puts the trace
            # outside the unit circle - a physically impossible passive load,
            # and a Smith chart that cannot be developed against. Model a
            # mismatched load behind a length of line instead: fixed magnitude,
            # phase winding with frequency, which draws the arc a real one does.
            gamma = 0.32 + 0.05 * (self._freqs - self._freqs[0]) / span
            delay_s = 1.2e-9
            wind = -2.0 * np.pi * 2.0 * delay_s * (self._freqs - self._freqs[0])
            return gamma * np.exp(1j * (wind + math.radians(self._angle)))

        return mag * np.exp(1j * phase)

    def close(self) -> None:
        pass


class HardwareBackend(PositionerRole):
    """The real rig, driven through pattern_measure's instrument wrappers."""

    mode = "hw"

    def __init__(self, vna_resource: str | None = None,
                 pos_resource: str | None = None,
                 slot: int | None = None, device: str | None = None,
                 with_positioner: bool = True,
                 with_vna: bool = True,
                 manual: bool = False):
        import pyvisa
        self._rm = pyvisa.ResourceManager()
        vcfg = pm.VnaConfig()
        if vna_resource:
            vcfg.resource = vna_resource

        self._vcfg = vcfg
        # Receiver and positioner are both roles, and both optional. A bench
        # with only the VNA on it is a real configuration - a VNA-only capture
        # never addresses the tower - and so is a bench with no VNA at all,
        # once an SDR is the receiver. Each is asked for explicitly and never
        # inferred: falling back to "no tower" because the EMCenter happened
        # not to answer is how a pattern run turns into 72 sweeps of the same
        # angle, and the same reasoning applies to the receiver.
        self.vna = pm.Vna(self._rm, vcfg) if with_vna else None
        # A person in place of the tower is asked for the same way: --manual
        # never opens the EMCenter, so a rig with the tower unplugged cannot
        # be mistaken for one with it.
        if manual:
            self.pos = ManualPositioner()
        elif with_positioner:
            self.pos = _open_tower(self._rm, pos_resource, slot, device)
        else:
            self.pos = None

    def capabilities(self) -> Capabilities:
        # Only the VNA receiver exists so far. A receiver that is absent can do
        # nothing, which is the honest answer and the one the UI can act on.
        return VNA_CAPS if self.vna else Capabilities(
            parameters=(), reflection=False, calibration=False,
            ratio=False, monitor=False)

    def _require_vna(self):
        if self.vna is None:
            raise BackendError(
                "no receiver attached - the service was started with --no-vna, "
                "so there is nothing to measure with")
        return self.vna

    def vna_idn(self) -> str | None:
        return self.vna.idn() if self.vna else None

    def correction_state(self) -> str:
        # No receiver is not "uncalibrated"; it is "there is nothing to
        # calibrate". correction_is_on reads an unrecognised string as unknown,
        # which is the right answer here.
        return self.vna.correction_state() if self.vna else "n/a (no receiver)"

    def acm_module(self) -> str | None:
        return self.vna.acm_module() if self.vna else None

    def acm_characterization(self) -> dict | None:
        return self.vna.acm_characterization() if self.vna else None

    def calibrate(self, req: "CalRequest", on_step=None,
                  should_stop=None) -> str:
        """Configure the sweep, then run a 2-port AutoCal at it.

        Configure first, deliberately: setting the sweep is what invalidates a
        calibration, so doing it afterwards would throw away what was just
        collected. The cancel hook is checked before each command and nowhere
        else - a running AutoCal cannot be interrupted, and a button that
        claimed otherwise would be worse than no button.
        """
        self._require_vna()
        self.configure(req.as_scan())
        if should_stop is not None and should_stop():
            raise pm.Aborted("calibration stopped before it began")
        return self.vna.acm_calibrate(ports=tuple(req.ports),
                                      orient=req.orient, on_step=on_step)

    def set_parameter(self, parameter: str) -> None:
        self._require_vna().set_parameter(parameter)

    def configure(self, req: ScanRequest) -> None:
        k = self._vcfg
        k.start_hz, k.stop_hz = req.start_hz, req.stop_hz
        k.points, k.if_bw_hz = req.points, req.if_bw_hz
        k.power_dbm, k.parameter = req.power_dbm, req.parameter
        vna = self._require_vna()
        vna.cfg = k
        vna.configure()

    def frequencies(self) -> np.ndarray:
        return self._require_vna().frequencies()

    def measure(self) -> np.ndarray:
        return self._require_vna().measure()

    def close(self) -> None:
        try:
            if self.vna:
                self.vna.close()
        finally:
            if self.pos:
                self.pos.close()


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------

class ChamberService:
    """Command dispatch plus a single scan worker.

    The instruments are single-threaded and stateful, so exactly one worker
    thread ever touches the backend. A stop request does not write to the
    hardware from the socket thread; it sets an event that the worker polls,
    including inside a move that may run for tens of seconds. That keeps the
    serial request/response exchange strictly single-threaded while still
    giving the UI a stop button that responds within about 100 ms.
    """

    def __init__(self, backend, loop: asyncio.AbstractEventLoop):
        self.backend = backend
        self.loop = loop
        self.clients: set = set()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._cal_cancel = threading.Event()
        self._cal_worker: threading.Thread | None = None
        self._sweep_cancel = threading.Event()
        self._sweep_worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last_run: dict | None = None
        self.calibration: dict | None = _load_calibration()
        # The running scan's frames, so a browser that connects mid-scan can
        # draw what it missed: scan_started, then the latest point per index.
        self._scan_started: str | None = None
        self._scan_points: dict[int, str] = {}
        # A backend whose parts talk to the operator - a hand-turned
        # positioner's prompts, a transmitter saying it is keyed - gets the
        # same push and log the service uses.
        # Late-bound, so anything that replaces push afterwards (the rigcheck
        # harness captures frames that way) is what they reach.
        hook = getattr(backend, "attach", None)
        if hook:
            hook(lambda payload: self.push(payload),
                 lambda level, source, message: self.log(level, source, message))

    # -- push ---------------------------------------------------------------
    def push(self, payload: dict) -> None:
        """Broadcast a frame. Safe to call from the worker thread."""
        msg = json.dumps(payload, default=_json_default)
        kind = payload.get("type")
        if kind == "scan_started":
            self._scan_started, self._scan_points = msg, {}
        elif kind == "scan_point":
            self._scan_points[payload["index"]] = msg   # a redo replaces it
        elif kind == "scan_done":
            self._scan_started, self._scan_points = None, {}
        asyncio.run_coroutine_threadsafe(self._broadcast(msg), self.loop)

    def scan_replay(self) -> list[str]:
        """Frames a client connecting mid-scan needs, in order."""
        started, points = self._scan_started, dict(self._scan_points)
        if started is None or not self.scanning:
            return []
        return [started] + [points[i] for i in sorted(points)]

    async def _broadcast(self, msg: str) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def log(self, level: str, source: str, message: str) -> None:
        self.push({"type": "log", "level": level, "source": source,
                   "message": message})

    # -- state --------------------------------------------------------------
    @property
    def scanning(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    @property
    def calibrating(self) -> bool:
        return self._cal_worker is not None and self._cal_worker.is_alive()

    @property
    def sweeping(self) -> bool:
        return self._sweep_worker is not None and self._sweep_worker.is_alive()

    def capabilities(self) -> Capabilities:
        """What the installed receiver can do, defaulting to a VNA's answer.

        The default matters for backends written before this existed: assuming
        full capability keeps them working exactly as they did, and a receiver
        that is actually limited says so rather than being guessed at.
        """
        get = getattr(self.backend, "capabilities", None)
        return get() if get else VNA_CAPS

    def _manual(self) -> "ManualPositioner | None":
        pos = getattr(self.backend, "pos", None)
        return pos if getattr(pos, "manual", False) else None

    def _release(self) -> None:
        """Let the backend drop anything it holds only while measuring.

        For the SDR backend that is the transmitter. Called on every way out
        of a scan or a sweep, success included, so RF is never left on after
        the run that needed it.
        """
        rel = getattr(self.backend, "release", None)
        if rel is None:
            return
        try:
            rel()
        except Exception as e:
            self.log("error", "rx", f"release failed: {type(e).__name__}: {e}")

    def get_state(self) -> dict:
        has_pos = getattr(self.backend, "has_positioner", True)
        caps = self.capabilities()
        manual = self._manual()
        st = {"mode": self.backend.mode, "scanning": self.scanning,
              "calibrating": self.calibrating, "sweeping": self.sweeping,
              "calibration": self.calibration, "has_positioner": has_pos,
              "manual": manual is not None,
              # A browser that connects while the operator is being asked for
              # an angle has to be able to show the question.
              "manual_pending": manual.pending if manual else None,
              "capabilities": caps.as_dict()}
        try:
            st.update({
                "vna_idn": self.backend.vna_idn(),
                "correction": self.backend.correction_state(),
                "connected": True,
            })
            # Read in its own block. These four used to share the VNA's try,
            # so a rig with no tower reported the whole service as
            # disconnected - including the VNA that was answering perfectly.
            if has_pos:
                st.update({
                    "pos_idn": self.backend.pos_idn(),
                    "angle": round(self.backend.position(), 2),
                    "speed": round(self.backend.speed(), 1),
                    "latched_error": self.backend.latched_error(),
                })
        except Exception as e:
            st.update({"connected": False, "error": f"{type(e).__name__}: {e}"})
        return st

    # -- commands -----------------------------------------------------------
    def cmd_get_state(self, _a: dict) -> dict:
        return self.get_state()

    def cmd_stop(self, _a: dict) -> dict:
        """Cancel any scan and halt the tower.

        Sets the cancel event first so the worker stops issuing new moves. Only
        when no worker is running do we touch the hardware from this thread -
        otherwise the worker's own abort path issues the stop, keeping serial
        access on one thread.
        """
        self._cancel.set()
        if not self.scanning:
            with self._lock:
                self.backend.stop()
        self.log("warn", "control", "Stop requested")
        return {"stopped": True, "was_scanning": self.scanning}

    def cmd_cancel_scan(self, _a: dict) -> dict:
        if not self.scanning:
            return {"cancelled": False, "reason": "no scan running"}
        self._cancel.set()
        self.log("warn", "scan", "Cancel requested")
        return {"cancelled": True}

    def _require_idle(self) -> None:
        if self.scanning:
            raise BackendError("a scan is running; cancel it first")
        if self.calibrating:
            # Both directions. The instruments are single-threaded and stateful,
            # and a calibration has a person with their hands on the connectors.
            raise BackendError("a calibration is running; cancel it first")
        if self.sweeping:
            # Every direction, not just the newest one. A guard that only knows
            # about the workers that existed when it was written is how two
            # things end up programming the same VNA at once.
            raise BackendError("a sweep is running; cancel it first")

    def _refuse_manual(self, what: str) -> None:
        if self._manual():
            raise BackendError(f"the AUT is turned by hand - there is no axis "
                               f"to {what}")

    def cmd_jog(self, a: dict) -> dict:
        self._require_idle()
        self._refuse_manual("jog")
        with self._lock:
            if "delta" in a:
                # A delta can walk off the end of the travel - +10 from 175 -
                # so fold it back into the -180..180 the card is set to.
                target = pm._wrap180(self.backend.position() + float(a["delta"]))
            else:
                # Absolute targets go exactly as asked. This used to be
                # `target % 360.0`, which turned -90 into 270: the same place,
                # reached by turning the opposite way. The tower has a cable
                # through it, so the direction of travel is the whole point,
                # and 270 is not a synonym for -90.
                target = float(a["deg"])
            self._cancel.clear()
            actual = self.backend.seek(target,
                                       should_abort=self._cancel.is_set)
        self.push({"type": "position", "deg": round(actual, 2)})
        return {"angle": round(actual, 2)}

    def cmd_zero_here(self, _a: dict) -> dict:
        self._require_idle()
        self._refuse_manual("zero")
        with self._lock:
            self.backend.zero_here()
            ang = self.backend.position()
        self.push({"type": "position", "deg": round(ang, 2)})
        self.log("info", "control", "Current position defined as 0 deg")
        return {"angle": round(ang, 2)}

    def cmd_set_speed(self, a: dict) -> dict:
        self._require_idle()
        with self._lock:
            self.backend.set_speed(float(a["percent"]))
            sp = self.backend.speed()
        return {"speed": round(sp, 1)}

    def cmd_manual_step(self, a: dict) -> dict:
        """The operator's answer to a prompt: the AUT is at the angle, or the
        previous point needs measuring again.

        Deliberately does not take the lock. The scan worker holds it for the
        whole of seek-then-measure, and seek is where it is waiting for this.
        """
        manual = self._manual()
        if manual is None:
            raise BackendError("this rig has a positioner; there is nobody to "
                               "answer for it")
        action = str(a.get("action", "measure"))
        manual.respond(action)
        return {"action": action}

    def cmd_list_runs(self, _a: dict) -> dict:
        RUNS_DIR.mkdir(exist_ok=True)
        runs = []
        for d in sorted(RUNS_DIR.iterdir(), reverse=True):
            meta = d / "meta.json"
            if meta.is_file():
                try:
                    runs.append(json.loads(meta.read_text()))
                except Exception:
                    pass
        return {"runs": runs}

    def cmd_load_run(self, a: dict) -> dict:
        d = RUNS_DIR / str(a["name"])
        if not (d / "meta.json").is_file():
            raise BackendError(f"no such run: {a['name']}")
        meta = json.loads((d / "meta.json").read_text())
        angles, freqs, mag = _read_run_csv(d / "pattern.csv")
        return {"meta": meta, "angles": angles, "freqs": freqs, "mag_db": mag}

    # -- calibration --------------------------------------------------------
    def cmd_acm_probe(self, _a: dict) -> dict:
        """Read-only: can the software see an AutoCal module?

        Never runs a calibration. A probe that quietly recalibrated the
        instrument it was asked to inspect is a probe nobody trusts twice.
        """
        self._require_idle()
        with self._lock:
            module = self.backend.acm_module()
        return {"module": module, "present": bool(module)}

    def cmd_start_cal(self, a: dict) -> dict:
        self._require_idle()
        if not self.capabilities().calibration:
            raise BackendError(
                "this receiver has no calibration to run - error correction "
                "belongs to the instrument, and nothing here provides it")
        req = CalRequest.from_args(a)
        self._cal_cancel.clear()
        self._cal_worker = threading.Thread(target=self._run_cal, args=(req,),
                                            daemon=True, name="chamber-cal")
        self._cal_worker.start()
        return {"started": True, "sweep": req.sweep()}

    def cmd_cancel_cal(self, _a: dict) -> dict:
        """Stop after the current step.

        Deliberately not called abort. An AutoCal runs the whole
        short/open/load/thru sequence inside one SCPI command with nowhere to
        poll, so this cannot interrupt one in flight - it can only decline to
        issue the next. The UI says the same thing in the same words.
        """
        if not self.calibrating:
            return {"cancelled": False, "reason": "no calibration running"}
        self._cal_cancel.set()
        self.log("warn", "cal", "Cancel requested - stops after the current step")
        return {"cancelled": True}

    # -- VNA-only sweep -----------------------------------------------------
    def cmd_sweep(self, a: dict) -> dict:
        self._require_idle()
        req = SweepRequest.from_args(a)
        caps = self.capabilities()
        unsupported = [p for p in req.parameters if p not in caps.parameters]
        if unsupported:
            raise BackendError(
                f"this receiver cannot measure {', '.join(unsupported)} - it "
                f"offers {', '.join(caps.parameters) or 'nothing'}")
        self._sweep_cancel.clear()
        self._sweep_worker = threading.Thread(
            target=self._run_sweep, args=(req,), daemon=True,
            name="chamber-sweep")
        self._sweep_worker.start()
        return {"started": True, "parameters": list(req.parameters)}

    def cmd_cancel_sweep(self, _a: dict) -> dict:
        """Stop after the current parameter.

        A single sweep is one blocking SCPI exchange with nowhere to poll, so
        this declines to start the next parameter rather than interrupting the
        one in flight. With four parameters that is a real thing to be able to
        do; with one it is barely distinguishable from waiting.
        """
        if not self.sweeping:
            return {"cancelled": False, "reason": "no sweep running"}
        self._sweep_cancel.set()
        self.log("warn", "sweep", "Cancel requested - stops after this parameter")
        return {"cancelled": True}

    def _run_sweep(self, req: SweepRequest) -> None:
        """Capture every requested S-parameter at one position.

        The positioner is never addressed - not read, not held, not moved - so
        this is the measurement that works with only a VNA attached. Complex
        data goes out as it is measured: a Smith chart needs re/im, and the
        scan path's habit of shipping magnitude alone is what made reflection
        impossible to look at.
        """
        try:
            with self._lock:
                self.backend.configure(req.as_scan())
                freqs = self.backend.frequencies()
                correction = self.backend.correction_state()

            mismatch = cal_mismatch(self.calibration, req)
            self.push({"type": "sweep_started",
                       "freqs": freqs,
                       "parameters": list(req.parameters),
                       "sweep": req.sweep(),
                       "correction": correction,
                       "cal_mismatch": mismatch,
                       "mode": self.backend.mode})
            self.log("info", "sweep",
                     f"{', '.join(req.parameters)} at {req.points} pts, "
                     f"{req.start_hz / 1e9:.3f}-{req.stop_hz / 1e9:.3f} GHz")
            for m in mismatch:
                self.log("warn", "vna",
                         f"this sweep does not match the calibration: {m}")
            corr_on = pm.correction_is_on(correction)
            if corr_on is False:
                self.log("warn", "vna", f"error correction is OFF ({correction}) "
                                        f"- this sweep is uncalibrated")
            unverified = cal_unverified(self.calibration, correction)
            if unverified:
                self.log("warn", "vna", unverified)
            for w in receiver_warnings(self.capabilities(), req):
                self.log("warn", "rx", w)

            for i, parameter in enumerate(req.parameters):
                if self._sweep_cancel.is_set():
                    self.log("warn", "sweep", f"cancelled after {i} parameter(s)")
                    self.push({"type": "sweep_done", "cancelled": True,
                               "n_done": i})
                    return
                with self._lock:
                    self.backend.set_parameter(parameter)
                    s = self.backend.measure()
                self.push({"type": "sweep_trace",
                           "index": i,
                           "parameter": parameter,
                           "re": [float(v.real) for v in s],
                           "im": [float(v.imag) for v in s]})

            self.push({"type": "sweep_done", "cancelled": False,
                       "n_done": len(req.parameters),
                       "correction": correction})
            self.log("info", "sweep", "capture complete")

        except Exception as e:
            self.log("error", "sweep", f"{type(e).__name__}: {e}")
            self.push({"type": "sweep_done", "cancelled": True,
                       "error": str(e)})
        finally:
            self._release()

    def _run_cal(self, req: CalRequest) -> None:
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        self.push({"type": "cal_started", "sweep": req.sweep(),
                   "ports": list(req.ports),
                   "reference_plane": req.reference_plane,
                   "mode": self.backend.mode})
        where = (f"port {req.ports[0]}" if len(req.ports) == 1
                 else f"ports {req.ports[0]} and {req.ports[1]}")
        self.log("info", "cal", f"{len(req.ports)}-port AutoCal on {where}, "
                                f"{req.points} pts, "
                                f"{req.start_hz/1e9:.3f}-{req.stop_hz/1e9:.3f} GHz")

        def step(msg: str) -> None:
            self.log("info", "cal", msg)
            self.push({"type": "cal_step", "message": msg})

        try:
            with self._lock:
                module = self.backend.acm_module()
                if not module:
                    raise BackendError(
                        "no AutoCal module visible to the VNA software - check "
                        "the ACM's USB connection, and see whether this build "
                        "exposes AutoCal to SCPI at all")
                self.push({"type": "cal_step", "message": f"module: {module}"})
                # Said in full, and said as a module property. These three
                # numbers look exactly like a sweep setup, and read as one they
                # suggest the VNA calibrates its whole range every time. It does
                # not: the cal runs at this run's sweep, printed just below.
                chars = self.backend.acm_characterization()
                if chars:
                    self.log("info", "cal",
                             f"module characterized {chars['start']}-"
                             f"{chars['stop']} at {chars['points']} pts "
                             f"- the module's own range, not this "
                             f"calibration's sweep")
                state = self.backend.calibrate(req, on_step=step,
                                               should_stop=self._cal_cancel.is_set)

            record = {"taken_at": started,
                      "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "method": f"ecal_solt{len(req.ports)}",
                      "ports": list(req.ports),
                      "reference_plane": req.reference_plane,
                      "module": module,
                      "module_characterization": chars,
                      "mode": self.backend.mode,
                      "sweep": req.sweep(),
                      "correction_state": state}
            self.calibration = record
            _save_calibration(record)
            on = pm.correction_is_on(state)
            if on:
                self.log("info", "cal", "calibration applied; correction is on")
            else:
                # Reached in sim, where "n/a" is the truthful answer and the
                # record exists to exercise the UI rather than to be trusted.
                self.log("warn", "cal", f"calibration finished but correction "
                                        f"reads {state!r}")
            self.push({"type": "cal_done", "ok": True, "cancelled": False,
                       "record": record})

        except pm.Aborted as e:
            self.log("warn", "cal", str(e))
            self.push({"type": "cal_done", "ok": False, "cancelled": True,
                       "error": str(e)})
        except Exception as e:
            self.log("error", "cal", f"{type(e).__name__}: {e}")
            self.push({"type": "cal_done", "ok": False, "cancelled": False,
                       "error": str(e)})

    def cmd_start_scan(self, a: dict) -> dict:
        self._require_idle()
        req = ScanRequest.from_args(a)
        angles = req.angles()
        if angles.size == 0:
            raise BackendError("scan range produces no angles")
        self._cancel.clear()
        self._worker = threading.Thread(target=self._run_scan, args=(req,),
                                        daemon=True, name="chamber-scan")
        self._worker.start()
        return {"started": True, "n_angles": int(angles.size)}

    # -- worker -------------------------------------------------------------
    def _run_scan(self, req: ScanRequest) -> None:
        name = req.name or time.strftime("run_%Y%m%d_%H%M%S")
        outdir = RUNS_DIR / name
        outdir.mkdir(parents=True, exist_ok=True)
        angles = req.angles()

        try:
            with self._lock:
                self.backend.configure(req)
                freqs = self.backend.frequencies()
                correction = self.backend.correction_state()

            # Read after configure, because configuring the sweep is what can
            # invalidate a calibration. Said now rather than at the end: a
            # 72-point run is minutes long, and an uncalibrated one is minutes
            # wasted if nobody noticed until the file was written.
            corr_on = pm.correction_is_on(correction)
            if corr_on is False:
                self.log("warn", "vna", f"error correction is OFF ({correction}) "
                                        f"- this run will be uncalibrated")
            elif corr_on is None and self.backend.mode != "sim":
                self.log("warn", "vna", f"error correction state unknown "
                                        f"({correction})")

            # A calibration is only meaningful at the sweep it was taken at. A
            # VNA interpolating a cal across a span it never measured produces a
            # plausible-looking answer of unknown quality, which is the same bug
            # class as the split position reply - so say it out loud.
            drift = cal_mismatch(self.calibration, req)
            if drift:
                self.log("warn", "vna",
                         "this run does not match the calibration: "
                         + "; ".join(drift))
            unverified = cal_unverified(self.calibration, correction)
            if unverified:
                self.log("warn", "vna", unverified)
            for w in receiver_warnings(self.capabilities(), req):
                self.log("warn", "rx", w)

            self.push({"type": "scan_started", "name": name,
                       "angles": angles.tolist(), "freqs": freqs.tolist(),
                       "params": req.__dict__, "correction": correction,
                       "cal_mismatch": drift})
            self.log("info", "scan", f"{name}: {angles.size} angles, "
                                     f"{freqs.size} freqs")

            header = ["angle_cmd_deg", "angle_actual_deg", "freq_hz",
                      "re", "im", "mag_db", "phase_deg"]
            csv_path = outdir / "pattern.csv"
            # Kept per index so a redo replaces its angle's rows rather than
            # appending a second set: one angle, one trace, in every file.
            written: dict[int, list[list[str]]] = {}
            with csv_path.open("w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(header)

                i = 0
                while i < angles.size:
                    ang = float(angles[i])
                    if self._cancel.is_set():
                        self.log("warn", "scan", f"cancelled at index {i}")
                        self.push({"type": "scan_done", "name": name,
                                   "cancelled": True, "n_done": i})
                        return

                    try:
                        with self._lock:
                            actual = self.backend.seek(
                                ang, should_abort=self._cancel.is_set)
                            s = self.backend.measure()
                    except RedoPrevious:
                        # Only a hand-turned positioner raises this. Step back
                        # one; the next pass prompts for that angle again.
                        if i > 0:
                            i -= 1
                            self.log("info", "scan", f"re-measuring "
                                                     f"{float(angles[i]):.1f} deg")
                        continue

                    mag = 20.0 * np.log10(np.maximum(np.abs(s), 1e-15))
                    phase = np.degrees(np.angle(s))
                    rows = [[f"{ang:.2f}", f"{actual:.2f}", f"{f:.0f}",
                             f"{sv.real:.9e}", f"{sv.imag:.9e}",
                             f"{m:.4f}", f"{ph:.4f}"]
                            for f, sv, m, ph in zip(freqs, s, mag, phase)]
                    redone = i in written
                    written[i] = rows
                    if redone:
                        fh.seek(0)
                        fh.truncate()
                        w.writerow(header)
                        for k in sorted(written):
                            w.writerows(written[k])
                    else:
                        w.writerows(rows)
                    fh.flush()

                    self.push({"type": "scan_point", "index": i,
                               "angle_cmd": float(ang),
                               "angle_actual": round(float(actual), 2),
                               "mag_db": [round(float(v), 4) for v in mag],
                               "peak_db": round(float(mag.max()), 3)})
                    self.push({"type": "position", "deg": round(float(actual), 2)})
                    i += 1

            receiver = getattr(self.backend, "receiver_meta", None)
            meta = {"name": name, "params": req.__dict__,
                    "n_angles": int(angles.size), "n_freqs": int(freqs.size),
                    "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "mode": self.backend.mode,
                    # Without this a finished run cannot say whether it was
                    # calibrated, which makes two runs incomparable and neither
                    # of them trustworthy on its own.
                    "correction_state": correction,
                    # Copied, not referenced. The next calibration overwrites
                    # cal.json, and a finished run has to keep saying what it
                    # was taken against.
                    "calibration": self.calibration,
                    "cal_mismatch": drift,
                    # What took the reading when it was not the VNA, and how
                    # the AUT was turned. Neither is recoverable from the CSV.
                    "receiver": receiver() if receiver else None,
                    "positioner": ("manual" if self._manual()
                                   else "tower" if getattr(self.backend,
                                                           "has_positioner", True)
                                   else None)}
            (outdir / "meta.json").write_text(json.dumps(meta, indent=2))
            self.last_run = meta
            self.push({"type": "scan_done", "name": name, "cancelled": False,
                       "n_done": int(angles.size), "meta": meta})
            self.log("info", "scan", f"{name} complete -> {csv_path}")

        except pm.Aborted as e:
            self.log("warn", "scan", str(e))
            self.push({"type": "scan_done", "name": name, "cancelled": True})
        except Exception as e:
            # Stop the axis before reporting the failure. seek() can raise with
            # the tower still turning - a dropped poll, a rejected command -
            # and the worker thread exiting is not a reason to leave it that
            # way: nothing else will halt it until a human notices and presses
            # STOP. Same invariant the CLI holds in its finally block.
            try:
                with self._lock:
                    self.backend.stop()
            except Exception:
                pass
            self.log("error", "scan", f"{type(e).__name__}: {e}")
            self.push({"type": "scan_done", "name": name, "cancelled": True,
                       "error": str(e)})
        finally:
            self._release()

    # -- dispatch -----------------------------------------------------------
    HANDLERS = {
        "get_state": "cmd_get_state",
        "start_scan": "cmd_start_scan",
        "cancel_scan": "cmd_cancel_scan",
        "stop": "cmd_stop",
        "jog": "cmd_jog",
        "zero_here": "cmd_zero_here",
        "set_speed": "cmd_set_speed",
        "list_runs": "cmd_list_runs",
        "load_run": "cmd_load_run",
        "acm_probe": "cmd_acm_probe",
        "start_cal": "cmd_start_cal",
        "cancel_cal": "cmd_cancel_cal",
        "sweep": "cmd_sweep",
        "cancel_sweep": "cmd_cancel_sweep",
        "manual_step": "cmd_manual_step",
    }

    async def handle(self, msg: str) -> str | None:
        try:
            req = json.loads(msg)
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": "malformed JSON"})
        cmd, rid = req.get("cmd"), req.get("id")
        handler = self.HANDLERS.get(cmd)
        if handler is None:
            return json.dumps({"id": rid, "ok": False,
                               "error": f"unknown command: {cmd!r}"})
        try:
            # Handlers touch blocking hardware, so keep them off the event loop.
            data = await asyncio.to_thread(getattr(self, handler), req)
            return json.dumps({"id": rid, "ok": True, "data": data},
                              default=_json_default)
        except Exception as e:
            return json.dumps({"id": rid, "ok": False,
                               "error": f"{type(e).__name__}: {e}"})


def _json_default(o: Any):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, Path):
        return str(o)
    return str(o)


def _load_calibration() -> dict | None:
    """The last calibration, or None. A missing or unreadable file is not an
    error - it means the rig has not been calibrated through this service, which
    is the normal state on a fresh checkout."""
    try:
        return json.loads((RUNS_DIR / CAL_NAME).read_text())
    except (OSError, ValueError):
        return None


def _save_calibration(record: dict) -> None:
    path = RUNS_DIR / CAL_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2))


def _read_run_csv(path: Path):
    rows = list(csv.DictReader(path.open()))
    ang = sorted({float(r["angle_cmd_deg"]) for r in rows})
    frq = sorted({float(r["freq_hz"]) for r in rows})
    grid = np.zeros((len(ang), len(frq)))
    ai = {a: i for i, a in enumerate(ang)}
    fi = {f: i for i, f in enumerate(frq)}
    for r in rows:
        grid[ai[float(r["angle_cmd_deg"])], fi[float(r["freq_hz"])]] = \
            float(r["mag_db"])
    return ang, frq, grid.tolist()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

async def serve(service: ChamberService, port: int) -> None:
    import websockets

    async def handler(ws):
        service.clients.add(ws)
        try:
            await ws.send(json.dumps({"type": "state",
                                      "data": service.get_state()},
                                     default=_json_default))
            for frame in service.scan_replay():
                await ws.send(frame)
            async for msg in ws:
                reply = await service.handle(msg)
                if reply is not None:
                    await ws.send(reply)
        except Exception:
            pass
        finally:
            service.clients.discard(ws)

    async with websockets.serve(handler, "0.0.0.0", port, max_size=8 << 20):
        print(f"chamber service listening on ws://0.0.0.0:{port}  "
              f"[{service.backend.mode}]", flush=True)
        await asyncio.Future()


def build_backend(a) -> Any:
    if getattr(a, "sim_sdr_monitor", False):
        # The other end of the SDR range: two channels, one of them watching a
        # coupler on the source, everything sharing a clock. Worth simulating
        # separately from --sim-sdr because the two differ in what they can be
        # trusted for, not in what they can do, and that difference is exactly
        # what the warnings exist to say.
        print("backend: SIMULATED, monitored coherent receiver "
              "(forced with --sim-sdr-monitor)")
        return SimBackend(caps=X310_MONITOR_CAPS)
    if getattr(a, "sim_sdr", False):
        # A one-channel receiver's capabilities on the simulated rig. Not a
        # USRP - it is the same synthetic pattern - but it is the same *shape*
        # of instrument: one path, no reflection, no calibration, no ratio. The
        # point is to develop and test everything above the driver against the
        # limited case before any radio is on the bench, the way the fake
        # instruments let the VNA path be tested away from the rig.
        print("backend: SIMULATED, scalar receiver (forced with --sim-sdr)")
        return SimBackend(caps=SCALAR_CAPS)
    if a.sim:
        print("backend: SIMULATED (forced with --sim)")
        return SimBackend()
    manual = getattr(a, "manual", False)
    if getattr(a, "sdr", None):
        return build_sdr_backend(a)
    try:
        b = HardwareBackend(a.vna, a.pos, a.slot, a.device,
                            with_positioner=not a.no_positioner,
                            with_vna=not getattr(a, "no_vna", False),
                            manual=manual)
        pos = b.pos_idn() if b.has_positioner else "none (--no-positioner)"
        vna = b.vna_idn() or "none (--no-vna)"
        print(f"backend: hardware\n  VNA {vna}\n  POS {pos}")
        return b
    except pm.InstrumentUnavailable as e:
        # These carry an instruction, not just a status code. Print the
        # instruction; a stack trace here buries the one line worth reading.
        print(f"backend: {e}", file=sys.stderr)
        if a.no_fallback:
            raise SystemExit(2)
        print("backend: falling back to SIMULATED - pass --no-fallback to refuse",
              file=sys.stderr)
        return SimBackend()
    except Exception as e:
        if a.no_fallback:
            raise
        print(f"backend: hardware unavailable ({type(e).__name__}: {e})")
        print("backend: falling back to SIMULATED - pass --no-fallback to refuse")
        return SimBackend()


def build_sdr_backend(a) -> "SdrBackend":
    """The SDR receiver, an optional transmitter on the same host, and
    whichever positioner was asked for.

    No fallback to the simulator. A radio that will not open is a cable or a
    USB port, and a dashboard that quietly drew a synthetic dipole instead is
    worse than one that refused to start. `--sdr sim` is how to ask for the
    simulator on purpose; it uses sdr_pattern's SimRadio, so everything from
    the tone estimate up is the code that runs on the real radio.
    """
    sim = a.sdr == "sim"
    rx = sp.RadioConfig(args="" if sim else a.sdr,
                        freq_hz=a.sdr_freq_mhz * 1e6,
                        offset_hz=a.sdr_offset_khz * 1e3,
                        rx_gain=a.sdr_rx_gain, tx_gain=a.sdr_tx_gain)
    make = sp.SimRadio if sim else sp.UhdRadio
    radio = make(rx)
    tx = None
    if a.sdr_tx:
        if (a.sdr_tx == "sim") != sim:
            raise SystemExit("--sdr and --sdr-tx must both be 'sim' or both "
                             "be real radios")
        # One radio with a TX and an RX port (an N200 at addr=...) is opened
        # once; UHD will not hand out a second session to the same device.
        same = not sim and a.sdr_tx == a.sdr
        tx = radio if same else make(replace(rx, args="" if sim else a.sdr_tx))

    if getattr(a, "manual", False):
        pos = ManualPositioner()
    elif a.no_positioner:
        pos = None
    else:
        import pyvisa
        pos = _open_tower(pyvisa.ResourceManager(), a.pos, a.slot, a.device)

    b = SdrBackend(rx, radio, positioner=pos, tx_radio=tx,
                   mode="sim" if sim else "hw")
    print(f"backend: SDR tone receiver{' (SIMULATED)' if sim else ''}\n"
          f"  RX  {radio.describe()}, tone {b.tone_hz / 1e6:.4f} MHz, "
          f"gain {rx.rx_gain:.1f} dB\n"
          f"  TX  {tx.describe() if tx else 'none on this host'}\n"
          f"  POS {b.pos_idn() or 'none (--no-positioner)'}")
    return b


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--sim", action="store_true",
                   help="use the simulated backend; no instruments required")
    p.add_argument("--no-fallback", action="store_true",
                   help="fail instead of falling back to the simulator")
    p.add_argument("--sim-sdr", action="store_true",
                   help="simulated rig with a one-channel receiver's "
                        "capabilities: no reflection, no calibration, no "
                        "ratio - what an SDR path has to work within")
    p.add_argument("--sim-sdr-monitor", action="store_true",
                   help="simulated rig with a monitored, phase-coherent "
                        "receiver's capabilities: still no reflection and no "
                        "calibration, but the ratio is restored")
    p.add_argument("--no-vna", action="store_true",
                   help="start without a receiver; the tower still works")
    p.add_argument("--no-positioner", action="store_true",
                   help="VNA only: do not open the positioner at all. Sweeps "
                        "work, scans and jogs are refused. Asked for "
                        "explicitly, never inferred from a tower that failed "
                        "to answer.")
    p.add_argument("--manual", action="store_true",
                   help="the AUT is turned by hand: each scan step prompts the "
                        "operator for the angle and waits for Measure. The "
                        "EMCenter is never opened.")
    sdr = sp.RadioConfig()
    p.add_argument("--sdr", default=None, metavar="ARGS",
                   help="use a B205 as the receiver instead of the VNA. ARGS "
                        "are UHD device args, e.g. 'serial=3234ABC', or 'sim'")
    p.add_argument("--sdr-tx", default=None, metavar="ARGS",
                   help="a second B205 on this host transmitting the tone, "
                        "keyed only while a scan runs. Omit when the tone "
                        "comes from another machine (sdr_pattern.py tx)")
    p.add_argument("--sdr-freq-mhz", type=float, default=sdr.freq_hz / 1e6,
                   help="LO frequency; the tone sits --sdr-offset-khz above it")
    p.add_argument("--sdr-offset-khz", type=float, default=sdr.offset_hz / 1e3)
    p.add_argument("--sdr-rx-gain", type=float, default=sdr.rx_gain)
    p.add_argument("--sdr-tx-gain", type=float, default=sdr.tx_gain)
    p.add_argument("--vna", default=None)
    p.add_argument("--pos", default=None)
    p.add_argument("--slot", type=int, default=None)
    p.add_argument("--device", default=None, choices=["A", "B"])
    a = p.parse_args(argv)

    RUNS_DIR.mkdir(exist_ok=True)
    backend = build_backend(a)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    service = ChamberService(backend, loop)
    try:
        loop.run_until_complete(serve(service, a.port))
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
