"""Output formats: CSV, Touchstone, run metadata, polar plot.

Separated from the engine so a run can stream to a UI without writing files,
or write files without plotting.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

from .config import ScanConfig

CSV_HEADER = ["angle_cmd_deg", "angle_actual_deg", "param", "freq_hz",
              "re", "im", "mag_db", "phase_deg"]


def db(x: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(np.abs(x), 1e-15))


class PatternWriter:
    """Streaming CSV writer. Flushes per angle so a killed run keeps its data."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None
        self._w = None

    def __enter__(self) -> "PatternWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", newline="")
        self._w = csv.writer(self._fh)
        self._w.writerow(CSV_HEADER)
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is not None:
            self._fh.close()

    def add(self, ang: float, actual: float, freqs: np.ndarray,
            traces: dict[str, np.ndarray]) -> None:
        for par, s in traces.items():
            for f, sv, m, p in zip(freqs, s, db(s), np.degrees(np.angle(s))):
                self._w.writerow([f"{ang:.2f}", f"{actual:.2f}", par, f"{f:.0f}",
                                  f"{sv.real:.9e}", f"{sv.imag:.9e}",
                                  f"{m:.4f}", f"{p:.4f}"])
        self._fh.flush()


def write_s2p(path: Path, freqs: np.ndarray, traces: dict[str, np.ndarray],
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


def write_meta(path: Path, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, default=str))


def polar_plot(angles, freqs, data, scan: ScanConfig, normalize: bool = True) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    f_target = scan.cut_freq_hz if scan.cut_freq_hz else float(freqs[len(freqs) // 2])
    k = int(np.argmin(np.abs(freqs - f_target)))
    cut = db(data[:, k])
    if normalize:
        cut = cut - cut.max()

    th = np.radians(np.asarray(angles, dtype=float))
    r = np.asarray(cut, dtype=float)
    # Close the trace only on a full revolution. Closing a partial cut draws a
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
    return out
