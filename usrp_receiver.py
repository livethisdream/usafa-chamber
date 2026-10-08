#!/usr/bin/env python3
"""USRP receiver: a measurement path that needs no VNA.

The service talks to a receiver through a handful of methods - configure,
frequencies, measure - and `chamber_service.Capabilities` says what the one
installed can be trusted for. This implements that contract on a USRP, so a
pattern cut can be taken with an SDR and a tower and nothing else.

How a sweep happens here is not how it happens on a VNA, and the difference is
the whole design:

A VNA steps its own synthesizer and hands back the span. A USRP digitizes a
window, so a span wider than that window is several captures with a retune
between them, and within each window every frequency is measured *at once* -
by transmitting a comb of tones and reading their amplitudes out of one FFT.
That is faster than stepping, and it is also the only way the measurement is
self-consistent: all the tones in a segment share a capture, so they share
whatever the source was doing at that instant.

Tones are placed on exact FFT bins. A tone that is not an exact bin leaks into
its neighbours and the leakage looks like a real signal at the wrong frequency,
which is the kind of error that reads as a lobe. `plan_segments` therefore
snaps the requested grid onto bins and reports what it actually measured
through `frequencies()`, rather than quietly answering a question it was not
asked.

Nothing here imports chamber_service: it describes itself with a plain dict and
the service builds its own Capabilities from that, so the two can be tested
apart. UHD is imported lazily for the same reason - the planning and the
arithmetic below run anywhere, and `usrpcheck.py` tests them with no radio.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# Baseband DC is where the LO leaks through, and the first few bins either side
# carry its skirt. No tone is placed there; the cost is a hole at each segment
# centre, which plan_segments covers from the neighbouring segment.
DC_GUARD_BINS = 4

# How much of the sample rate is usable before the anti-alias filters roll off.
# 80% is the conventional figure for a USRP and it is deliberately conservative:
# a tone in the roll-off is attenuated by the receiver rather than by the
# antenna, which is indistinguishable from a real null.
USABLE_FRACTION = 0.8


@dataclass
class Segment:
    """One capture: a centre frequency, and the tones measured within it."""

    center_hz: float
    bins: np.ndarray          # FFT bin index per tone, signed, never 0
    freqs_hz: np.ndarray      # what those bins actually correspond to
    index: np.ndarray         # where each tone belongs in the full sweep


def plan_segments(freqs_hz, rate_hz: float, n_fft: int) -> list[Segment]:
    """Split a requested frequency grid into captures, snapped onto FFT bins.

    The grid is covered left to right. Each segment is centred so that its
    tones sit inside the usable fraction of the band and clear of DC, and each
    requested frequency is snapped to the nearest bin - so the returned
    `freqs_hz` is what will be measured, which is not always what was asked
    for. A 101-point sweep over 2.5 GHz with a 160 MHz window needs sixteen or
    so of these, and phase does not carry across the joins.
    """
    freqs = np.asarray(freqs_hz, dtype=float)
    if freqs.size == 0:
        return []
    bin_hz = rate_hz / n_fft
    half = (rate_hz * USABLE_FRACTION) / 2.0
    guard = DC_GUARD_BINS * bin_hz

    # The widest run of tones one window can hold, less the room the DC shift
    # below needs. Centring on the middle of that run rather than on its first
    # tone is worth about double: a window reaches half a band either side of
    # its centre, so a centre sitting on the first tone wastes the half behind
    # it.
    reach = 2.0 * half - 2.0 * guard

    segments: list[Segment] = []
    taken = 0
    while taken < freqs.size:
        last = int(np.searchsorted(freqs, freqs[taken] + reach, side="right")) - 1
        last = max(last, taken)
        chunk = freqs[taken:last + 1]
        # Centre on the middle of the run, then step off it by the guard so
        # nothing lands on DC, where the LO leaks through.
        center = 0.5 * (chunk[0] + chunk[-1]) + guard
        count = chunk.size
        chunk_off = chunk - center
        bins = np.rint(chunk_off / bin_hz).astype(int)
        # Snapping can collide two requested points onto one bin, which would
        # measure one frequency and report two. Push the duplicate out by a bin
        # rather than dropping it: the grid stays the length the caller asked
        # for, and frequencies() tells the truth about where it landed.
        for i in range(1, bins.size):
            if bins[i] <= bins[i - 1]:
                bins[i] = bins[i - 1] + 1
        keep = bins != 0
        bins, idx = bins[keep], np.arange(taken, taken + count)[keep]
        segments.append(Segment(center_hz=center, bins=bins,
                                freqs_hz=center + bins * bin_hz, index=idx))
        taken += count
    return segments


def comb_waveform(bins, n_fft: int, seed: int = 0) -> np.ndarray:
    """A unit-amplitude tone on every requested bin, in one baseband buffer.

    Phases are randomised rather than aligned. Aligned phases put every tone in
    step at t=0, and the peak-to-average ratio of fifty such tones will clip
    the DAC and intermodulate - which shows up as extra tones at frequencies
    nothing transmitted. A fixed seed keeps it reproducible between runs.
    """
    bins = np.asarray(bins, dtype=int)
    spec = np.zeros(n_fft, dtype=complex)
    rng = np.random.default_rng(seed)
    spec[bins % n_fft] = np.exp(1j * rng.uniform(0, 2 * np.pi, bins.size))
    wave = np.fft.ifft(spec) * n_fft
    peak = np.max(np.abs(wave))
    return wave / peak if peak else wave


def extract(samples, bins, n_fft: int) -> np.ndarray:
    """Complex amplitude at each tone, averaged over whole buffers.

    Averaging in the frequency domain across repeats is coherent, so noise
    falls as the number of buffers while the tones do not - this is where the
    dynamic range comes from, and it is why a capture is many periods of the
    comb rather than one.
    """
    samples = np.asarray(samples)
    whole = samples.size // n_fft
    if whole == 0:
        raise ValueError(f"need at least {n_fft} samples, got {samples.size}")
    block = samples[:whole * n_fft].reshape(whole, n_fft)
    spec = np.fft.fft(block, axis=1).mean(axis=0) / n_fft
    return spec[np.asarray(bins, dtype=int) % n_fft]


def ratio(measured: np.ndarray, monitor: np.ndarray) -> np.ndarray:
    """Divide the measurement by the monitor, which is what a VNA does for free.

    The monitor channel watches a coupler on the source, so whatever the source
    did during the capture appears in both and divides out. Without this the
    level is only as stable as the whole transmit chain; with it, the answer is
    a ratio again. A monitor bin at zero means no reference was received, and
    is left as NaN rather than becoming an enormous number.
    """
    out = np.full(measured.shape, np.nan, dtype=complex)
    live = monitor != 0
    out[live] = measured[live] / monitor[live]
    return out


# --------------------------------------------------------------------------
# The radio
# --------------------------------------------------------------------------

@dataclass
class UsrpConfig:
    """Everything the receiver needs that is not a frequency.

    `rate_hz` is asked for, not assumed: the X310 will quietly give a different
    rate than requested when the master clock does not divide it, and over a
    1 GbE link it will accept a rate it cannot actually stream. What was really
    set is read back and used, so the segment plan matches the hardware rather
    than the hope.
    """

    args: str = "type=x300"
    rate_hz: float = 200e6
    n_fft: int = 4096
    repeats: int = 16            # buffers averaged per capture
    rx_gain_db: float = 20.0
    tx_gain_db: float = 0.0      # low by default; nothing is gained by surprise
    ref: str = "internal"
    monitor_chan: int | None = None   # second RX watching a coupler, if wired
    settle_s: float = 0.02


class UsrpReceiver:
    """A receiver built from a USRP, implementing the service's contract.

    Transmit is gated. `allow_tx` has to be set by the caller that actually
    intends to radiate, for the same reason `bringup.py` gates motion: the
    expensive mistakes in this room are a keyed amplifier and a turning tower,
    and neither should happen because a default was convenient.
    """

    def __init__(self, cfg: UsrpConfig, allow_tx: bool = False):
        import uhd                                    # lazy: see module docstring

        self.cfg = cfg
        self.allow_tx = allow_tx
        self._uhd = uhd
        self.usrp = uhd.usrp.MultiUSRP(cfg.args)

        if cfg.ref != "internal":
            self.usrp.set_clock_source(cfg.ref)
            self.usrp.set_time_source(cfg.ref)

        self.usrp.set_rx_rate(cfg.rate_hz)
        self.usrp.set_tx_rate(cfg.rate_hz)
        # What the hardware actually did, which is the number everything below
        # plans against.
        self.rate_hz = float(self.usrp.get_rx_rate())
        self.usrp.set_rx_gain(cfg.rx_gain_db, 0)
        if cfg.monitor_chan is not None:
            self.usrp.set_rx_gain(cfg.rx_gain_db, cfg.monitor_chan)

        self._segments: list[Segment] = []
        self._freqs = np.zeros(0)

    # -- identity ---------------------------------------------------------
    def describe(self) -> str:
        mb = self.usrp.get_mboard_name(0)
        rx = self.usrp.get_rx_subdev_spec(0)
        return f"{mb}, {rx}, {self.rate_hz / 1e6:.3f} MS/s"

    def ref_locked(self) -> bool | None:
        """True / False / None when the motherboard has no such sensor."""
        try:
            return bool(self.usrp.get_mboard_sensor("ref_locked", 0).to_bool())
        except Exception:
            return None

    def capabilities_dict(self) -> dict:
        """What this configuration can be trusted for, for Capabilities(**d).

        `coherent` is tied to the monitor channel rather than to the chassis.
        Everything in an X310 does share a clock, but a retune between segments
        leaves the LO at an arbitrary phase, so phase only carries across
        captures when each one is divided by a reference taken at the same
        instant. Without the monitor, the honest answer is no.
        """
        mon = self.cfg.monitor_chan is not None
        return {"parameters": ("S21",), "reflection": False,
                "calibration": False, "ratio": mon, "monitor": mon,
                "coherent": mon,
                "span_hz": self.rate_hz * USABLE_FRACTION}

    # -- the contract -----------------------------------------------------
    def configure(self, start_hz: float, stop_hz: float, points: int) -> None:
        want = np.linspace(float(start_hz), float(stop_hz), int(points))
        self._segments = plan_segments(want, self.rate_hz, self.cfg.n_fft)
        self._freqs = np.zeros(want.size)
        for seg in self._segments:
            self._freqs[seg.index] = seg.freqs_hz

    def frequencies(self) -> np.ndarray:
        """The grid actually measured, after snapping onto FFT bins."""
        return self._freqs

    def set_parameter(self, parameter: str) -> None:
        if parameter != "S21":
            raise ValueError(
                f"this receiver measures the path it is wired to, not "
                f"{parameter} - reflection needs a coupler and a calibration "
                f"neither of which exist here")

    def measure(self) -> np.ndarray:
        """One complex value per frequency, taken segment by segment."""
        if not self._segments:
            raise RuntimeError("configure() first")
        if not self.allow_tx:
            raise RuntimeError(
                "transmit is not enabled - construct with allow_tx=True once "
                "the RF chain is known to be safe to radiate into")
        out = np.zeros(self._freqs.size, dtype=complex)
        for seg in self._segments:
            out[seg.index] = self._measure_segment(seg)
        return out

    def _measure_segment(self, seg: Segment) -> np.ndarray:
        import threading

        n = self.cfg.n_fft
        tx = comb_waveform(seg.bins, n)
        chans = [0] if self.cfg.monitor_chan is None else [0, self.cfg.monitor_chan]
        need = n * self.cfg.repeats
        # Transmit for longer than the capture needs, so the comb is already
        # running when the first sample lands and still running at the last.
        duration = (need / self.rate_hz) * 3.0 + 0.1

        stop = threading.Event()

        def radiate():
            try:
                self.usrp.send_waveform(tx, duration, seg.center_hz,
                                        self.rate_hz, [0], self.cfg.tx_gain_db)
            except Exception:
                stop.set()

        t = threading.Thread(target=radiate, daemon=True, name="usrp-tx")
        t.start()
        try:
            import time
            time.sleep(self.cfg.settle_s)     # let the LO settle and TX start
            rx = self.usrp.recv_num_samps(need, seg.center_hz, self.rate_hz,
                                          chans, self.cfg.rx_gain_db)
        finally:
            t.join(timeout=duration + 1.0)

        rx = np.atleast_2d(rx)
        measured = extract(rx[0], seg.bins, n)
        if self.cfg.monitor_chan is None:
            # No reference: divide by the transmitted comb so the answer is at
            # least referenced to what was asked for, even though nothing
            # cancels what the chain did to it.
            return measured / extract(np.tile(tx, self.cfg.repeats), seg.bins, n)
        return ratio(measured, extract(rx[1], seg.bins, n))

    def close(self) -> None:
        self.usrp = None
