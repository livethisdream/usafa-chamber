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

Run:
    python chamber_service.py --sim        # no hardware needed
    python chamber_service.py              # real rig, falls back to sim if absent
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

import pattern_measure as pm

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
    ports: tuple[int, int] = (1, 2)
    orient: bool = False
    reference_plane: str = ""

    @classmethod
    def from_args(cls, a: dict) -> "CalRequest":
        f = {k: a[k] for k in cls.__dataclass_fields__ if k in a}
        if "ports" in f:
            f["ports"] = tuple(int(p) for p in f["ports"])[:2]
        return cls(**f)

    def sweep(self) -> dict:
        return {"start_hz": self.start_hz, "stop_hz": self.stop_hz,
                "points": self.points, "if_bw_hz": self.if_bw_hz,
                "power_dbm": self.power_dbm, "parameter": self.parameter}

    def as_scan(self) -> ScanRequest:
        return ScanRequest(**self.sweep())


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


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

class BackendError(RuntimeError):
    pass


class SimBackend:
    """Synthetic rig. Same contract as HardwareBackend, no instruments required.

    Motion is simulated at a believable slew rate so the UI's live position
    readout and stop button have something real to exercise, and the pattern is
    a broadside-ish lobe with sidelobes so plots look like antenna data rather
    than a flat line.
    """

    mode = "sim"

    def __init__(self, slew_deg_s: float = 60.0):
        self._angle = 0.0
        self._speed = 100.0
        self._slew = slew_deg_s

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
        return mag * np.exp(1j * phase)

    def close(self) -> None:
        pass


class HardwareBackend:
    """The real rig, driven through pattern_measure's instrument wrappers."""

    mode = "hw"

    def __init__(self, vna_resource: str | None = None,
                 pos_resource: str | None = None,
                 slot: int | None = None, device: str | None = None):
        import pyvisa
        self._rm = pyvisa.ResourceManager()
        vcfg = pm.VnaConfig()
        if vna_resource:
            vcfg.resource = vna_resource
        pcfg = pm.PositionerConfig()
        if pos_resource:
            pcfg.resource = pos_resource
        cmds = pm.PositionerCmds()
        if slot is not None:
            cmds.slot = slot
        if device:
            cmds.device = device

        self._vcfg = vcfg
        self.vna = pm.Vna(self._rm, vcfg)
        self.pos = pm.Positioner(self._rm, pcfg, cmds)

    def vna_idn(self) -> str:
        return self.vna.idn()

    def pos_idn(self) -> str:
        return self.pos.identity()

    def position(self) -> float:
        return self.pos.position()

    def speed(self) -> float:
        return self.pos.speed()

    def latched_error(self) -> str:
        return self.pos.latched_error()

    def correction_state(self) -> str:
        return self.vna.correction_state()

    def acm_module(self) -> str | None:
        return self.vna.acm_module()

    def calibrate(self, req: "CalRequest", on_step=None,
                  should_stop=None) -> str:
        """Configure the sweep, then run a 2-port AutoCal at it.

        Configure first, deliberately: setting the sweep is what invalidates a
        calibration, so doing it afterwards would throw away what was just
        collected. The cancel hook is checked before each command and nowhere
        else - a running AutoCal cannot be interrupted, and a button that
        claimed otherwise would be worse than no button.
        """
        self.configure(req.as_scan())
        if should_stop is not None and should_stop():
            raise pm.Aborted("calibration stopped before it began")
        return self.vna.acm_calibrate(ports=tuple(req.ports),
                                      orient=req.orient, on_step=on_step)

    def set_speed(self, pct: float) -> None:
        self.pos.set_speed(pct)

    def zero_here(self) -> None:
        self.pos.zero_here()

    def stop(self) -> None:
        self.pos.stop()

    def seek(self, deg: float, should_abort=None) -> float:
        return self.pos.seek(deg, should_abort=should_abort)

    def configure(self, req: ScanRequest) -> None:
        k = self._vcfg
        k.start_hz, k.stop_hz = req.start_hz, req.stop_hz
        k.points, k.if_bw_hz = req.points, req.if_bw_hz
        k.power_dbm, k.parameter = req.power_dbm, req.parameter
        self.vna.cfg = k
        self.vna.configure()

    def frequencies(self) -> np.ndarray:
        return self.vna.frequencies()

    def measure(self) -> np.ndarray:
        return self.vna.measure()

    def close(self) -> None:
        try:
            self.vna.close()
        finally:
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
        self._lock = threading.Lock()
        self.last_run: dict | None = None
        self.calibration: dict | None = _load_calibration()

    # -- push ---------------------------------------------------------------
    def push(self, payload: dict) -> None:
        """Broadcast a frame. Safe to call from the worker thread."""
        msg = json.dumps(payload, default=_json_default)
        asyncio.run_coroutine_threadsafe(self._broadcast(msg), self.loop)

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

    def get_state(self) -> dict:
        st = {"mode": self.backend.mode, "scanning": self.scanning,
              "calibrating": self.calibrating, "calibration": self.calibration}
        try:
            st.update({
                "vna_idn": self.backend.vna_idn(),
                "pos_idn": self.backend.pos_idn(),
                "angle": round(self.backend.position(), 2),
                "speed": round(self.backend.speed(), 1),
                "latched_error": self.backend.latched_error(),
                "correction": self.backend.correction_state(),
                "connected": True,
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

    def cmd_jog(self, a: dict) -> dict:
        self._require_idle()
        with self._lock:
            if "delta" in a:
                target = self.backend.position() + float(a["delta"])
            else:
                target = float(a["deg"])
            self._cancel.clear()
            actual = self.backend.seek(target % 360.0,
                                       should_abort=self._cancel.is_set)
        self.push({"type": "position", "deg": round(actual, 2)})
        return {"angle": round(actual, 2)}

    def cmd_zero_here(self, _a: dict) -> dict:
        self._require_idle()
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

    def _run_cal(self, req: CalRequest) -> None:
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        self.push({"type": "cal_started", "sweep": req.sweep(),
                   "ports": list(req.ports),
                   "reference_plane": req.reference_plane,
                   "mode": self.backend.mode})
        self.log("info", "cal", f"AutoCal on ports {req.ports[0]} and "
                                f"{req.ports[1]}, {req.points} pts, "
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
                state = self.backend.calibrate(req, on_step=step,
                                               should_stop=self._cal_cancel.is_set)

            record = {"taken_at": started,
                      "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "method": "ecal_solt2",
                      "ports": list(req.ports),
                      "reference_plane": req.reference_plane,
                      "module": module,
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

            self.push({"type": "scan_started", "name": name,
                       "angles": angles.tolist(), "freqs": freqs.tolist(),
                       "params": req.__dict__, "correction": correction,
                       "cal_mismatch": drift})
            self.log("info", "scan", f"{name}: {angles.size} angles, "
                                     f"{freqs.size} freqs")

            csv_path = outdir / "pattern.csv"
            with csv_path.open("w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["angle_cmd_deg", "angle_actual_deg", "freq_hz",
                            "re", "im", "mag_db", "phase_deg"])

                for i, ang in enumerate(angles):
                    if self._cancel.is_set():
                        self.log("warn", "scan", f"cancelled at index {i}")
                        self.push({"type": "scan_done", "name": name,
                                   "cancelled": True, "n_done": i})
                        return

                    with self._lock:
                        actual = self.backend.seek(
                            float(ang), should_abort=self._cancel.is_set)
                        s = self.backend.measure()

                    mag = 20.0 * np.log10(np.maximum(np.abs(s), 1e-15))
                    phase = np.degrees(np.angle(s))
                    for f, sv, m, ph in zip(freqs, s, mag, phase):
                        w.writerow([f"{ang:.2f}", f"{actual:.2f}", f"{f:.0f}",
                                    f"{sv.real:.9e}", f"{sv.imag:.9e}",
                                    f"{m:.4f}", f"{ph:.4f}"])
                    fh.flush()

                    self.push({"type": "scan_point", "index": i,
                               "angle_cmd": float(ang),
                               "angle_actual": round(float(actual), 2),
                               "mag_db": [round(float(v), 4) for v in mag],
                               "peak_db": round(float(mag.max()), 3)})
                    self.push({"type": "position", "deg": round(float(actual), 2)})

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
                    "cal_mismatch": drift}
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
    if a.sim:
        print("backend: SIMULATED (forced with --sim)")
        return SimBackend()
    try:
        b = HardwareBackend(a.vna, a.pos, a.slot, a.device)
        print(f"backend: hardware\n  VNA {b.vna_idn()}\n  POS {b.pos_idn()}")
        return b
    except Exception as e:
        if a.no_fallback:
            raise
        print(f"backend: hardware unavailable ({type(e).__name__}: {e})")
        print("backend: falling back to SIMULATED - pass --no-fallback to refuse")
        return SimBackend()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--sim", action="store_true",
                   help="use the simulated backend; no instruments required")
    p.add_argument("--no-fallback", action="store_true",
                   help="fail instead of falling back to the simulator")
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
