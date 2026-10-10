#!/usr/bin/env python3
"""
Scalar antenna pattern with two B205minis - no VNA, no chamber.

One B205 transmits a CW tone into a reference antenna; the other receives from
the AUT on a tripod. The AUT is turned by hand (prompted per angle) or by the
EMControl positioner (--pos). Magnitude only: the two radios do not share a
clock, so there is no phase, and power is relative (dBFS), not calibrated.

Subcommands
  tx       transmit a CW tone at --freq-mhz + --offset-khz until Ctrl-C
  monitor  print received tone power continuously (aim at boresight, measure
           the noise floor with the AUT replaced by a 50 ohm load, check
           linearity by stepping a pad)
  rx       step through the angle grid, measure tone power at each angle

Output (rx), same layout as pattern_measure.py:
  <outdir>/pattern.csv     angle_cmd_deg, angle_actual_deg, freq_hz, re, im,
                           mag_db, phase_deg   (re = amplitude, im = 0,
                           mag_db in dBFS, phase_deg = nan)
  <outdir>/sdr_detail.csv  per-angle tone power, noise, SNR, tone offset, clipping
  <outdir>/meta.json       radio settings and the boresight drift recheck
  <outdir>/pattern.png     polar plot, normalized to peak

Typical session (915 MHz, ISM band):
  TX laptop:  sdr_pattern.py tx --freq-mhz 915 --tx-gain 70
  RX laptop:  sdr_pattern.py monitor --freq-mhz 915        # aim, check SNR
              sdr_pattern.py rx --freq-mhz 915 --step 5 --outdir runs/aut1

The receiver tunes its LO to --freq-mhz and looks for the tone --offset-khz
above it, clear of the LO/DC spike in the center of the spectrum. Both ends
must use the same --freq-mhz and --offset-khz.

Try the whole flow without hardware:
  sdr_pattern.py rx --sim --no-prompt --step 10 --outdir sim_run

UHD's Python bindings come with the UHD install (they are not on PyPI), so
run this with the interpreter UHD installed into. Only numpy and matplotlib
are needed beyond that.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

# The B205mini's input damages around -15 dBm. A pad and a DC block on the RX
# port are cheap; a front end is not.
RX_SAFETY_NOTE = ("RX port: DC block + 10-20 dB pad fitted, cables shorted to "
                  "ground before mating?")


@dataclass
class RadioConfig:
    args: str = "type=b200"         # UHD device args; add serial=... with two on one host
    freq_hz: float = 915e6
    offset_hz: float = 100e3        # tone sits this far above the RX LO
    rate: float = 1e6
    tx_gain: float = 70.0           # B205mini: 0-89.75 dB, ~+10 dBm at max
    rx_gain: float = 40.0           # B205mini: 0-76 dB
    tx_ant: str = "TX/RX"
    rx_ant: str = "RX2"


@dataclass
class CaptureConfig:
    nfft: int = 8192                # 122 Hz bins at 1 MS/s
    averages: int = 32              # Welch segments per capture (~0.26 s)
    search_hz: float = 15e3         # two TCXOs at +/-2 ppm each: ~3.7 kHz at 915 MHz
    integ_hz: float = 1e3           # tone power summed over +/- this around the peak
    discard: int = 20_000           # samples dropped after stream start (settling)
    repeats: int = 3                # captures averaged per angle


# --------------------------------------------------------------------------
# Radios
# --------------------------------------------------------------------------

class UhdRadio:
    def __init__(self, cfg: RadioConfig):
        import uhd  # deferred so --sim and --help work without UHD
        self.uhd = uhd
        self.cfg = cfg
        self.usrp = uhd.usrp.MultiUSRP(cfg.args)

    def describe(self) -> str:
        try:
            return self.usrp.get_pp_string().strip().splitlines()[0]
        except Exception:
            return self.cfg.args

    # ---- receive ---------------------------------------------------------

    def setup_rx(self) -> None:
        u, c = self.usrp, self.cfg
        u.set_rx_rate(c.rate, 0)
        u.set_rx_freq(self.uhd.types.TuneRequest(c.freq_hz), 0)
        u.set_rx_antenna(c.rx_ant, 0)
        try:
            u.set_rx_agc(False, 0)  # fixed gain or the pattern is meaningless
        except Exception:
            pass
        u.set_rx_gain(c.rx_gain, 0)
        sa = self.uhd.usrp.StreamArgs("fc32", "sc16")
        sa.channels = [0]
        self.rx = u.get_rx_stream(sa)
        self._buf = np.zeros((1, self.rx.get_max_num_samps()), dtype=np.complex64)
        time.sleep(0.2)             # LO lock

    def capture(self, n: int) -> tuple[np.ndarray, int]:
        """Gated capture: a fresh num_done command, so every sample is taken
        after the call - nothing buffered from while the AUT was moving."""
        t = self.uhd.types
        cmd = t.StreamCMD(t.StreamMode.num_done)
        cmd.num_samps = n
        cmd.stream_now = True
        self.rx.issue_stream_cmd(cmd)
        md = t.RXMetadata()
        out = np.empty(n, dtype=np.complex64)
        got, overflows = 0, 0
        while got < n:
            k = self.rx.recv(self._buf, md, 2.0)
            if md.error_code == t.RXMetadataErrorCode.overflow:
                overflows += 1
                continue
            if md.error_code == t.RXMetadataErrorCode.timeout:
                raise RuntimeError("RX timeout - is the B205 still on USB 3?")
            if md.error_code != t.RXMetadataErrorCode.none:
                raise RuntimeError(f"RX error: {md.strerror()}")
            k = min(k, n - got)
            out[got:got + k] = self._buf[0, :k]
            got += k
        return out, overflows

    # ---- transmit --------------------------------------------------------

    def transmit_forever(self) -> None:
        u, c, t = self.usrp, self.cfg, self.uhd.types
        u.set_tx_rate(c.rate, 0)
        u.set_tx_freq(t.TuneRequest(c.freq_hz), 0)
        u.set_tx_antenna(c.tx_ant, 0)
        u.set_tx_gain(c.tx_gain, 0)
        sa = self.uhd.usrp.StreamArgs("fc32", "sc16")
        sa.channels = [0]
        st = u.get_tx_stream(sa)

        # A whole number of tone cycles per buffer keeps the phase continuous
        # across sends. Amplitude 0.5 leaves DAC headroom.
        n = 10_000
        cycles = round(c.offset_hz * n / c.rate)
        tone = (0.5 * np.exp(2j * np.pi * cycles * np.arange(n) / n)).astype(np.complex64)
        buf = tone.reshape(1, -1)

        md = t.TXMetadata()
        md.start_of_burst = True
        md.end_of_burst = False
        md.has_time_spec = False
        try:
            while True:
                st.send(buf, md)
                md.start_of_burst = False
        finally:
            # RF off on every exit path.
            md.end_of_burst = True
            st.send(np.zeros((1, 0), dtype=np.complex64), md)


class SimRadio:
    """Stand-in receiver: a dipole-like pattern plus noise, a few kHz of clock
    offset, and slow drift, so the full rx flow runs without hardware."""

    def __init__(self, cfg: RadioConfig, seed: int = 1):
        self.cfg = cfg
        self.angle = 0.0
        self.rng = np.random.default_rng(seed)
        self.t0 = time.monotonic()
        self.clock_off = 2.7e3

    def describe(self) -> str:
        return "SIMULATED B205mini"

    def setup_rx(self) -> None:
        pass

    def capture(self, n: int) -> tuple[np.ndarray, int]:
        g = abs(math.cos(math.radians(self.angle))) + 0.003   # nulls at 90/270
        drift = 1.0 + 0.01 * (time.monotonic() - self.t0)
        a = 0.3 * g * drift
        k = np.arange(n)
        f = self.cfg.offset_hz + self.clock_off
        x = a * np.exp(2j * np.pi * f * k / self.cfg.rate + 1j * self.rng.uniform(0, 6.3))
        noise = (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n)) * 1e-4
        return (x + noise).astype(np.complex64), 0

    def transmit_forever(self) -> None:
        print("SIM: pretending to transmit, Ctrl-C to stop")
        while True:
            time.sleep(1)


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

@dataclass
class ToneReading:
    power_dbfs: float
    noise_dbfs: float       # noise in the same integration band
    snr_db: float
    tone_offset_hz: float   # measured tone position relative to the RX LO
    peak_abs: float         # largest |sample|; near 1.0 means the ADC clipped
    overflows: int


def measure_tone(x: np.ndarray, rate: float, nominal_hz: float,
                 cap: CaptureConfig, overflows: int = 0) -> ToneReading:
    """Welch periodogram, then tone power by Parseval over the bins near the
    peak, minus the noise those bins carry. Amplitude 1.0 full scale = 0 dBFS."""
    nfft = cap.nfft
    nseg = len(x) // nfft
    if nseg < 1:
        raise ValueError("capture shorter than one FFT")
    w = np.blackman(nfft)
    segs = x[:nseg * nfft].reshape(nseg, nfft) * w
    psd = np.mean(np.abs(np.fft.fft(segs, axis=1)) ** 2, axis=0)
    psd = np.fft.fftshift(psd)
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / rate))
    scale = nfft * np.sum(w ** 2)

    search = np.abs(f - nominal_hz) <= cap.search_hz
    if not search.any():
        raise ValueError("search window outside the captured bandwidth")
    k_peak = int(np.flatnonzero(search)[np.argmax(psd[search])])
    f_peak = float(f[k_peak])

    band = np.abs(f - f_peak) <= cap.integ_hz
    # Noise reference: everything away from DC and the search window. The
    # median shrugs off the odd interferer (cellular is next door).
    ref = ~search & (np.abs(f) > 5e3)
    noise_bin = float(np.median(psd[ref])) / math.log(2) if nseg == 1 \
        else float(np.median(psd[ref]))
    noise_band = noise_bin * band.sum()
    tone = max(float(psd[band].sum()) - noise_band, 1e-30)

    p = 10 * math.log10(tone / scale)
    n = 10 * math.log10(max(noise_band, 1e-30) / scale)
    return ToneReading(power_dbfs=p, noise_dbfs=n, snr_db=p - n,
                       tone_offset_hz=f_peak, peak_abs=float(np.max(np.abs(x))),
                       overflows=overflows)


def read_tone(radio, cfg: RadioConfig, cap: CaptureConfig) -> ToneReading:
    """Average `repeats` gated captures in linear power."""
    n = cap.discard + cap.nfft * cap.averages
    rs = []
    for _ in range(cap.repeats):
        x, ov = radio.capture(n)
        rs.append(measure_tone(x[cap.discard:], cfg.rate, cfg.offset_hz, cap, ov))
    lin = lambda db: 10 ** (db / 10)
    p = 10 * math.log10(np.mean([lin(r.power_dbfs) for r in rs]))
    nz = 10 * math.log10(np.mean([lin(r.noise_dbfs) for r in rs]))
    return ToneReading(power_dbfs=p, noise_dbfs=nz, snr_db=p - nz,
                       tone_offset_hz=float(np.median([r.tone_offset_hz for r in rs])),
                       peak_abs=max(r.peak_abs for r in rs),
                       overflows=sum(r.overflows for r in rs))


def warnings_for(r: ToneReading, cap: CaptureConfig, nominal_hz: float) -> list[str]:
    out = []
    if r.peak_abs > 0.9:
        out.append("ADC near clipping - lower --rx-gain or add pad")
    if r.snr_db < 10:
        out.append(f"SNR {r.snr_db:.1f} dB - reading is near the floor")
    if abs(r.tone_offset_hz - nominal_hz) > 0.9 * cap.search_hz:
        out.append("tone at edge of search window - wrong frequency, or no tone?")
    if r.overflows:
        out.append(f"{r.overflows} overflow(s) - USB 3 port? lower --rate")
    return out


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_tx(radio, cfg: RadioConfig) -> int:
    print(f"TX  {radio.describe()}")
    print(f"    tone at {(cfg.freq_hz + cfg.offset_hz)/1e6:.4f} MHz, "
          f"gain {cfg.tx_gain:.1f} dB. Ctrl-C to stop (RF off on exit).")
    try:
        radio.transmit_forever()
    except KeyboardInterrupt:
        print("\nstopped, RF off")
    return 0


def cmd_monitor(radio, cfg: RadioConfig, cap: CaptureConfig, count: int | None) -> int:
    print(f"RX  {radio.describe()}")
    print(f"    {RX_SAFETY_NOTE}")
    print(f"    LO {cfg.freq_hz/1e6:.4f} MHz, tone expected at "
          f"+{cfg.offset_hz/1e3:.1f} kHz, gain {cfg.rx_gain:.1f} dB. Ctrl-C to stop.\n")
    radio.setup_rx()
    i = 0
    try:
        while count is None or i < count:
            r = read_tone(radio, cfg, cap)
            warn = "; ".join(warnings_for(r, cap, cfg.offset_hz))
            print(f"  {r.power_dbfs:8.2f} dBFS   noise {r.noise_dbfs:8.2f}   "
                  f"SNR {r.snr_db:6.1f} dB   tone {r.tone_offset_hz/1e3:+8.3f} kHz"
                  f"{'   ! ' + warn if warn else ''}", flush=True)
            i += 1
    except KeyboardInterrupt:
        print()
    return 0


def _prompt(text: str) -> str:
    try:
        return input(text).strip().lower()
    except EOFError:
        return "q"


def cmd_rx(radio, cfg: RadioConfig, cap: CaptureConfig, a) -> int:
    # Reuse the chamber tool's angle grid and polar plot so SDR runs and VNA
    # runs land in the same format.
    from pattern_measure import ScanConfig, polar_plot

    scan = ScanConfig(start_deg=a.from_deg, stop_deg=a.to_deg, step_deg=a.step,
                      outdir=a.outdir)
    angles = scan.angles()
    scan.outdir.mkdir(parents=True, exist_ok=True)

    print(f"RX  {radio.describe()}")
    print(f"    {RX_SAFETY_NOTE}")
    print(f"    LO {cfg.freq_hz/1e6:.4f} MHz, tone at +{cfg.offset_hz/1e3:.1f} kHz, "
          f"gain {cfg.rx_gain:.1f} dB, {len(angles)} angles")
    radio.setup_rx()

    pos = None
    if a.pos:
        import pyvisa
        from pattern_measure import Positioner, PositionerCmds, PositionerConfig
        rm = pyvisa.ResourceManager(a.visa) if a.visa else pyvisa.ResourceManager()
        pos = Positioner(rm, PositionerConfig(resource=a.pos),
                         PositionerCmds(slot=a.slot, device=a.device))
        print(f"POS {pos.identity()} at {pos.position():.2f} deg")
    elif not a.no_prompt:
        print("\nManual rotation: turn the AUT clockwise seen from above. At each "
              "prompt, Enter = measure, r = redo previous angle, q = stop.")

    def go_to(ang: float):
        """Bring the AUT to `ang`. Returns the angle reached, None if the
        operator quit, or "redo" to re-measure the previous angle."""
        if isinstance(radio, SimRadio):
            radio.angle = ang
        if pos is not None:
            return pos.seek(ang)
        if a.no_prompt:
            if a.dwell:
                time.sleep(a.dwell)
            return ang
        ans = _prompt(f"  -> {ang:7.2f} deg, Enter: ")
        return {"q": None, "r": "redo"}.get(ans, ang)

    rows: list[tuple[float, float, ToneReading]] = []
    recheck = None
    try:
        i = 0
        while i < len(angles):
            ang = float(angles[i])
            actual = go_to(ang)
            if actual is None:
                break
            if actual == "redo":
                if rows:
                    rows.pop()
                    i -= 1
                continue
            r = read_tone(radio, cfg, cap)
            warn = "; ".join(warnings_for(r, cap, cfg.offset_hz))
            print(f"  {ang:7.2f} deg  {r.power_dbfs:8.2f} dBFS  SNR {r.snr_db:5.1f} dB"
                  f"{'  ! ' + warn if warn else ''}", flush=True)
            rows.append((ang, actual, r))
            i += 1

        # Boresight recheck: the radios drift with temperature, and the only
        # way to know by how much over this run is to measure the start again.
        if rows and not a.no_recheck:
            ang0, _, r0 = rows[0]
            actual = go_to(ang0) if pos is not None or a.no_prompt else (
                ang0 if _prompt(f"\n  drift check: back to {ang0:.2f} deg, Enter "
                                f"(q skips): ") != "q" else None)
            if actual is not None:
                r1 = read_tone(radio, cfg, cap)
                recheck = {"angle_deg": ang0, "start_dbfs": r0.power_dbfs,
                           "end_dbfs": r1.power_dbfs,
                           "drift_db": r1.power_dbfs - r0.power_dbfs}
                print(f"  drift over run: {recheck['drift_db']:+.2f} dB"
                      f"{'  ! more than 0.5 dB' if abs(recheck['drift_db']) > 0.5 else ''}")
    except KeyboardInterrupt:
        print("\ninterrupted - saving what was measured", file=sys.stderr)
    finally:
        if pos is not None:
            try:
                pos.stop()
            except Exception:
                pass
            pos.close()

    if not rows:
        print("nothing measured")
        return 1

    f_tone = cfg.freq_hz + cfg.offset_hz
    csv_path = scan.outdir / "pattern.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["angle_cmd_deg", "angle_actual_deg", "freq_hz",
                    "re", "im", "mag_db", "phase_deg"])
        for ang, actual, r in rows:
            amp = 10 ** (r.power_dbfs / 20)
            w.writerow([f"{ang:.2f}", f"{actual:.2f}", f"{f_tone:.0f}",
                        f"{amp:.9e}", "0", f"{r.power_dbfs:.4f}", "nan"])

    with (scan.outdir / "sdr_detail.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["angle_cmd_deg", "angle_actual_deg", "power_dbfs", "noise_dbfs",
                    "snr_db", "tone_offset_hz", "peak_abs", "overflows"])
        for ang, actual, r in rows:
            w.writerow([f"{ang:.2f}", f"{actual:.2f}", f"{r.power_dbfs:.4f}",
                        f"{r.noise_dbfs:.4f}", f"{r.snr_db:.2f}",
                        f"{r.tone_offset_hz:.1f}", f"{r.peak_abs:.4f}", r.overflows])

    meta = {"mode": "sim" if isinstance(radio, SimRadio) else "sdr",
            "radio": radio.describe(), "rotation": "positioner" if pos else "manual",
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "radio_cfg": asdict(cfg), "capture_cfg": asdict(cap),
            "recheck": recheck, "note": a.note}
    (scan.outdir / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"\nwrote {csv_path}, sdr_detail.csv, meta.json")

    ang_arr = np.array([r[0] for r in rows])
    data = np.array([[10 ** (r[2].power_dbfs / 20)] for r in rows], dtype=complex)
    polar_plot(ang_arr, np.array([f_tone]), data, scan)
    return 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def radio_args(sp):
        d = RadioConfig()
        sp.add_argument("--args", default=d.args,
                        help="UHD device args, e.g. 'serial=3234ABC' (default: %(default)s)")
        sp.add_argument("--freq-mhz", type=float, default=d.freq_hz / 1e6)
        sp.add_argument("--offset-khz", type=float, default=d.offset_hz / 1e3,
                        help="tone offset above the LO; same on both ends")
        sp.add_argument("--rate", type=float, default=d.rate)
        sp.add_argument("--sim", action="store_true", help="no hardware")

    def capture_args(sp):
        d = CaptureConfig()
        sp.add_argument("--rx-gain", type=float, default=RadioConfig.rx_gain)
        sp.add_argument("--averages", type=int, default=d.averages)
        sp.add_argument("--repeats", type=int, default=d.repeats)
        sp.add_argument("--search-khz", type=float, default=d.search_hz / 1e3)
        sp.add_argument("--integ-khz", type=float, default=d.integ_hz / 1e3)

    t = sub.add_parser("tx", help="transmit a CW tone")
    radio_args(t)
    t.add_argument("--tx-gain", type=float, default=RadioConfig.tx_gain)

    m = sub.add_parser("monitor", help="print tone power continuously")
    radio_args(m)
    capture_args(m)
    m.add_argument("--count", type=int, default=None, help="stop after N readings")

    r = sub.add_parser("rx", help="measure a pattern cut")
    radio_args(r)
    capture_args(r)
    r.add_argument("--step", type=float, default=5.0)
    r.add_argument("--from-deg", type=float, default=0.0)
    r.add_argument("--to-deg", type=float, default=355.0)
    r.add_argument("--outdir", type=Path, default=Path("./sdr_run"))
    r.add_argument("--note", default="", help="free text saved in meta.json")
    r.add_argument("--no-prompt", action="store_true",
                   help="do not wait for Enter at each angle (sim, or with --dwell)")
    r.add_argument("--dwell", type=float, default=0.0,
                   help="with --no-prompt: seconds to wait at each angle")
    r.add_argument("--no-recheck", action="store_true",
                   help="skip the return-to-start drift check")
    r.add_argument("--pos", default="",
                   help="EMControl VISA resource; rotate with the positioner "
                        "instead of by hand")
    r.add_argument("--slot", type=int, default=1)
    r.add_argument("--device", default="A", choices=["A", "B"])
    r.add_argument("--visa", default="")

    a = p.parse_args(argv)
    cfg = RadioConfig(args=a.args, freq_hz=a.freq_mhz * 1e6,
                      offset_hz=a.offset_khz * 1e3, rate=a.rate)
    if a.cmd == "tx":
        cfg.tx_gain = a.tx_gain
    else:
        cfg.rx_gain = a.rx_gain
        if a.search_khz * 1e3 >= cfg.offset_hz - 5e3:
            p.error("--search-khz must stay clear of DC: keep it below "
                    "--offset-khz minus 5")
        cap = CaptureConfig(averages=a.averages, repeats=a.repeats,
                            search_hz=a.search_khz * 1e3, integ_hz=a.integ_khz * 1e3)

    radio = SimRadio(cfg) if a.sim else UhdRadio(cfg)
    if a.cmd == "tx":
        return cmd_tx(radio, cfg)
    if a.cmd == "monitor":
        return cmd_monitor(radio, cfg, cap, a.count)
    return cmd_rx(radio, cfg, cap, a)


if __name__ == "__main__":
    raise SystemExit(main())
