"""The scan itself.

The engine owns the step-and-measure loop and nothing else: it does not print,
does not parse arguments, and does not know whether a human or a websocket is
watching. It reports through `emit`, an event callback, and can be stopped
through `abort`, anything with `.is_set()`.

That seam is what lets the CLI and the service share one implementation: the
CLI turns events into stdout lines, the service turns the same events into JSON
frames.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import numpy as np

from .config import ScanConfig
from .instruments import Positioner, Vna
from .writers import PatternWriter, db, polar_plot, write_meta, write_s2p

Emit = Callable[[dict], None]


@dataclass
class ScanResult:
    angles: np.ndarray
    actual: np.ndarray
    freqs: np.ndarray
    data: np.ndarray
    closure: dict | None = None
    aborted: bool = False
    files: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


class _NeverAbort:
    @staticmethod
    def is_set() -> bool:
        return False


class ScanEngine:
    def __init__(self, vna: Vna, pos: Positioner, scan: ScanConfig,
                 emit: Emit | None = None,
                 abort: threading.Event | None = None):
        self.vna = vna
        self.pos = pos
        self.scan = scan
        self._emit = emit or (lambda ev: None)
        self.abort = abort or _NeverAbort()

    # -- event helpers -----------------------------------------------------

    def emit(self, **ev) -> None:
        self._emit(ev)

    def log(self, message: str, level: str = "info", source: str = "scan") -> None:
        self.emit(type="log", level=level, source=source, message=message)

    def phase(self, phase: str, detail: str = "") -> None:
        self.emit(type="phase", phase=phase, detail=detail)

    # -- run ---------------------------------------------------------------

    def prepare(self) -> dict:
        """Configure the VNA and report what we are about to do."""
        self.phase("configuring")
        idn = self.vna.idn()
        self.log(f"VNA: {idn}", source="vna")
        self.vna.configure()
        self.vna.warm_up()

        corr = self.vna.correction_state()
        if corr in ("0", "OFF", "off"):
            self.log("error correction is OFF - this run is uncalibrated",
                     level="warn", source="vna")
        else:
            self.log(f"error correction: {corr}", source="vna")

        sweep_s = self.vna.sweep_time_s()
        freqs = self.vna.frequencies()
        angles = self.scan.angles()

        short = self.scan.endpoint_shortfall()
        if abs(short) > 1e-6:
            self.log(f"{self.scan.step_deg:g} deg steps do not divide the span; "
                     f"last angle is {angles[-1]:.2f}, not {self.scan.stop_deg:.2f}",
                     level="warn")

        if self.pos.cfg.speed_preset is not None:
            self.pos.set_speed(self.pos.cfg.speed_preset)
        here = self.pos.position()
        self.log(f"positioner at {here:.2f} deg", source="pos")

        info = {
            "vna_idn": idn,
            "correction_state": corr,
            "sweep_time_s": sweep_s,
            "freq_hz": [float(f) for f in freqs],
            "angles_deg": [float(a) for a in angles],
            "parameters": self.vna.traces,
            "position_deg": here,
            "cut_index": self._cut_index(freqs),
            "full_circle": self.scan.is_full_circle(),
        }
        self.emit(type="ready", **info)
        return info

    def _cut_index(self, freqs: np.ndarray) -> int:
        target = self.scan.cut_freq_hz or float(freqs[len(freqs) // 2])
        return int(np.argmin(np.abs(freqs - target)))

    def run(self) -> ScanResult:
        scan, vna, pos = self.scan, self.vna, self.pos
        scan.outdir.mkdir(parents=True, exist_ok=True)
        info = self.prepare()

        freqs = np.array(info["freq_hz"])
        angles = np.array(info["angles_deg"])
        k = info["cut_index"]
        main = vna.cfg.parameter

        data = np.zeros((len(angles), len(freqs)), dtype=complex)
        actual = np.zeros(len(angles))
        first = None
        aborted = False
        files: list[str] = []

        meta: dict = {
            "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "vna": dict(vars(vna.cfg)),
            "positioner": dict(vars(pos.cfg)),
            "scan": {kk: (str(v) if hasattr(v, "as_posix") else v)
                     for kk, v in vars(scan).items()},
            "vna_idn": info["vna_idn"],
            "correction_state": info["correction_state"],
            "sweep_time_s": info["sweep_time_s"],
        }

        self.phase("scanning", f"{len(angles)} angles over {scan.span_deg():.1f} deg")
        t0 = time.monotonic()
        csv_path = scan.outdir / "pattern.csv"
        with PatternWriter(csv_path) as writer:
            for i, ang in enumerate(angles):
                if self.abort.is_set():
                    aborted = True
                    self.log(f"aborted before angle {ang:.2f}", level="warn")
                    break

                actual[i] = pos.seek(float(ang))
                traces = vna.measure()
                if i == 0:
                    first = traces
                    vna.check_errors("after first sweep")
                data[i, :] = traces[main]
                writer.add(float(ang), actual[i], freqs, traces)

                if scan.write_s2p:
                    p = scan.outdir / f"cut_{ang:07.2f}.s2p"
                    write_s2p(p, freqs, traces, main, vna.cfg.aux_parameter)

                mag = db(traces[main])
                self.emit(type="point", index=i, n=len(angles),
                          angle_cmd=float(ang), angle_actual=float(actual[i]),
                          cut_db=float(mag[k]), peak_db=float(mag.max()),
                          mag_db=[round(float(v), 2) for v in mag],
                          elapsed_s=round(time.monotonic() - t0, 2))

            closure = None
            measured = int(np.count_nonzero(np.any(data != 0, axis=1)))
            if scan.closure_check and not aborted and measured > 1:
                self.phase("closure", "re-measuring the start angle")
                back = pos.seek(float(angles[0]))
                again = vna.measure()
                writer.add(float(angles[0]), back, freqs, again)
                d = db(again[main]) - db(first[main])
                closure = {
                    "angle_deg": float(angles[0]),
                    "readback_deg": float(back),
                    "max_abs_delta_db": float(np.max(np.abs(d))),
                    "mean_delta_db": float(np.mean(d)),
                }
                # The closure event carries the numbers; both front ends render
                # it, so an extra log line here would just duplicate itself.
                self.emit(type="closure", **closure)

        files.append(str(csv_path))

        # Trim to what was actually measured so an aborted run still plots.
        n = measured if aborted else len(angles)
        angles, actual, data = angles[:n], actual[:n], data[:n, :]

        if scan.write_plot and n > 1:
            # Plot the angles the positioner actually reached, not the ones
            # asked for.
            png = polar_plot(actual, freqs, data, scan)
            files.append(str(png))

        if scan.return_home and len(angles):
            self.phase("homing")
            pos.seek(float(angles[0]))

        meta["finished_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta["closure"] = closure
        meta["aborted"] = aborted
        meta["angles_measured"] = int(n)
        meta_path = scan.outdir / "run_meta.json"
        write_meta(meta_path, meta)
        files.append(str(meta_path))

        self.phase("done" if not aborted else "aborted")
        self.emit(type="done", aborted=aborted, files=files,
                  outdir=str(scan.outdir), angles_measured=int(n))
        return ScanResult(angles=angles, actual=actual, freqs=freqs, data=data,
                          closure=closure, aborted=aborted, files=files, meta=meta)
