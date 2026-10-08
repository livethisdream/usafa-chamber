#!/usr/bin/env python3
"""Staged USRP bring-up. Nothing transmits unless you say so.

The counterpart to `bringup.py`, for the other receiver. Same shape and same
reasoning: talk to the real hardware one stage at a time, in increasing order
of consequence, and report exactly what came back rather than what was hoped
for.

    python usrpcheck.py                        # stages 0-4, receive only
    python usrpcheck.py --allow-tx             # adds stage 5, the loopback
    python usrpcheck.py --selftest             # no radio at all

Stages 0-4 and 6 never key the transmitter. Stage 5 does, and needs
`--allow-tx`, for the reason `bringup.py` gates motion: in this room the
expensive mistakes are a keyed amplifier and a turning tower, and neither
should happen because a default was convenient.

Two stages exist to settle questions `project/instrument-plane_DESIGN.md`
records as open rather than assumed:

Stage 3 asks the device for a ladder of sample rates and prints what it
actually set. An X310 will accept a request its master clock cannot divide and
give you a neighbouring rate instead, silently.

Stage 4 streams at the chosen rate and counts overflows. This is the one that
matters for `span_hz=160e6` in `X310_MONITOR_CAPS`: the UBX-160's analog width
is only available if the host link can carry it, and over 1 GbE it cannot. An
overflow here means the number in the profile is a lie for this rig, and the
stage prints the rate that did hold so the profile can be corrected.

Before the chamber: run with a cable from TX to RX through a fixed attenuator.
Stage 5 then measures a path whose answer is known - flat, and about as lossy
as the attenuator - which is the cheapest possible way to find out whether the
comb, the tuning and the arithmetic are right before any of it is pointed at
an antenna.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bringup import Report                                  # noqa: E402
import usrp_receiver as ur                                  # noqa: E402

# Rates worth asking an X310 for. 200 MS/s is the master clock undivided;
# below that are the integer decimations a UBX sweep would realistically use.
RATE_LADDER = [200e6, 100e6, 50e6, 25e6, 12.5e6, 6.25e6]


def stage_env(rep: Report) -> object | None:
    rep.stage(0, "environment")
    try:
        import uhd
    except ImportError as e:
        rep.bad("UHD Python bindings", f"{e}")
        rep.note("install", "apt install uhd-host python3-uhd   (or conda uhd)")
        rep.note("note", "UHD ships its bindings with the driver, not on PyPI")
        return None
    rep.ok("UHD Python bindings", getattr(uhd, "__version__", "version unknown"))
    rep.ok("numpy", np.__version__)
    return uhd


def stage_find(rep: Report, uhd, args: str) -> bool:
    rep.stage(1, "discovery")
    try:
        found = uhd.find(args)
    except Exception as e:
        rep.bad("uhd.find raised", f"{type(e).__name__}: {e}")
        return False
    if not found:
        rep.bad("no device answered", f"args {args!r}")
        rep.note("X310 over Ethernet", "give the host a static IP on the "
                                       "device's subnet (default 192.168.10.1/24)")
        rep.note("then", "ping 192.168.10.2, and pass --args addr=192.168.10.2")
        return False
    for d in found:
        rep.ok("found", str(d))
    return True


def stage_open(rep: Report, args: str, ref: str):
    rep.stage(2, "device")
    cfg = ur.UsrpConfig(args=args, ref=ref)
    try:
        rx = ur.UsrpReceiver(cfg, allow_tx=False)
    except Exception as e:
        rep.bad("could not open", f"{type(e).__name__}: {e}")
        return None
    rep.ok("opened", rx.describe())
    locked = rx.ref_locked()
    if locked is None:
        rep.note("reference lock", "no ref_locked sensor on this motherboard")
    elif locked:
        rep.ok("reference lock", f"locked to {ref}")
    else:
        rep.bad("reference lock", f"NOT locked to {ref}")
    return rx


def stage_rates(rep: Report, rx) -> float:
    rep.stage(3, "sample rates - asked for, versus actually set")
    best = 0.0
    for want in RATE_LADDER:
        try:
            rx.usrp.set_rx_rate(want)
            got = float(rx.usrp.get_rx_rate())
        except Exception as e:
            rep.bad(f"{want / 1e6:>7.2f} MS/s", f"{type(e).__name__}: {e}")
            continue
        exact = abs(got - want) <= 1.0
        (rep.ok if exact else rep.note)(
            f"{want / 1e6:>7.2f} MS/s",
            "as asked" if exact else f"-> {got / 1e6:.3f} MS/s (clock cannot divide it)")
        best = max(best, got)
    return best


def stage_stream(rep: Report, rx, rate: float, seconds: float) -> float:
    rep.stage(4, "streaming - what the host link actually carries")
    held = 0.0
    for want in [r for r in RATE_LADDER if r <= rate]:
        rx.usrp.set_rx_rate(want)
        got = float(rx.usrp.get_rx_rate())
        n = int(got * seconds)
        try:
            samples = rx.usrp.recv_num_samps(n, 2.4e9, got, [0], 20.0)
        except Exception as e:
            rep.bad(f"{got / 1e6:>7.2f} MS/s", f"{type(e).__name__}: {e}")
            continue
        got_n = np.asarray(samples).shape[-1]
        if got_n < n:
            rep.bad(f"{got / 1e6:>7.2f} MS/s",
                    f"short by {n - got_n} samples - overflow, link cannot keep up")
            continue
        rep.ok(f"{got / 1e6:>7.2f} MS/s", f"{got_n} samples, no overflow")
        held = max(held, got)
    if held:
        rep.note("usable span at the best rate",
                 f"{held * ur.USABLE_FRACTION / 1e6:.0f} MHz "
                 f"- put this in X310_MONITOR_CAPS.span_hz if it differs")
    else:
        rep.bad("nothing streamed", "no rate held; check the link and MTU")
    return held


def stage_loopback(rep: Report, args: str, ref: str, rate: float,
                   center: float, tones: int, tx_gain: float, rx_gain: float,
                   monitor: int | None) -> None:
    rep.stage(5, "loopback - TX to RX through an attenuator")
    cfg = ur.UsrpConfig(args=args, ref=ref, rate_hz=rate, tx_gain_db=tx_gain,
                        rx_gain_db=rx_gain, monitor_chan=monitor)
    try:
        rx = ur.UsrpReceiver(cfg, allow_tx=True)
    except Exception as e:
        rep.bad("could not open for transmit", f"{type(e).__name__}: {e}")
        return
    half = rx.rate_hz * ur.USABLE_FRACTION / 2
    want = np.linspace(center - half * 0.9, center + half * 0.9, tones)
    rx.configure(want[0], want[-1], tones)
    rep.note("plan", f"{tones} tones over "
                     f"{(want[-1] - want[0]) / 1e6:.1f} MHz, "
                     f"{len(rx._segments)} segment(s)")
    try:
        s = rx.measure()
    except Exception as e:
        rep.bad("capture failed", f"{type(e).__name__}: {e}")
        return
    db = 20 * np.log10(np.maximum(np.abs(s), 1e-15))
    if not np.isfinite(db).all():
        rep.bad("non-finite values", "a tone came back as zero or NaN")
        return
    rep.ok("captured", f"{s.size} tones, "
                       f"{db.mean():.2f} dB mean, {db.ptp():.2f} dB peak-to-peak")
    # A cable and a pad are flat. Ripple here is the measurement, not the path:
    # a few tenths is the noise floor, several dB means something is wrong
    # before any antenna is involved.
    if db.ptp() > 3.0:
        rep.bad("flatness", f"{db.ptp():.2f} dB across a cable - expected < 3 dB")
        rep.note("suspect", "overdriven RX (lower --rx-gain), or a tone in the "
                            "filter roll-off (lower --tones or --rate)")
    else:
        rep.ok("flatness", f"{db.ptp():.2f} dB across the band")
    for f, d in zip(rx.frequencies()[:8], db[:8]):
        rep.note(f"{f / 1e9:.6f} GHz", f"{d:+.2f} dB")
    rx.close()


def stage_plan(rep: Report, rate: float, start: float, stop: float,
               points: int, n_fft: int) -> None:
    rep.stage(6, "what a real sweep would cost")
    if rate <= 0:
        rep.skip("plan", "no working rate from stage 4")
        return
    want = np.linspace(start, stop, points)
    segs = ur.plan_segments(want, rate, n_fft)
    err = np.concatenate([s.freqs_hz - want[s.index] for s in segs])
    rep.ok("segments", f"{len(segs)} captures for {points} points over "
                       f"{(stop - start) / 1e6:.0f} MHz")
    rep.note("tones per capture",
             f"{min(s.bins.size for s in segs)}-{max(s.bins.size for s in segs)}")
    rep.note("grid snapped by", f"at most {np.abs(err).max() / 1e3:.1f} kHz "
                                f"(bin = {rate / n_fft / 1e3:.1f} kHz)")
    rep.note("phase", "does not carry across the joins between captures")


def selftest(rep: Report) -> int:
    """The arithmetic, with no radio. Runs anywhere, including the lab laptop."""
    rep.stage(0, "self test - planning and extraction, no hardware")
    rate, n = 200e6, 4096
    want = np.linspace(0.5e9, 3.0e9, 101)
    segs = ur.plan_segments(want, rate, n)
    covered = np.sort(np.concatenate([s.index for s in segs]))
    if np.array_equal(covered, np.arange(101)):
        rep.ok("every point planned exactly once", f"{len(segs)} segments")
    else:
        rep.bad("coverage", "points missed or measured twice")
    if all((np.abs(s.bins) > 0).all() for s in segs):
        rep.ok("no tone on DC", "LO leakage avoided")
    else:
        rep.bad("DC guard", "a tone landed on the LO")

    s = segs[0]
    truth = 10 ** (np.linspace(-0.5, -25, s.bins.size) / 20) * np.exp(
        1j * np.linspace(0, 6, s.bins.size))
    tx = ur.comb_waveform(s.bins, n, seed=7)
    spec = np.fft.fft(tx) / n
    spec[s.bins % n] *= truth
    rx = np.tile(np.fft.ifft(spec) * n, 8)
    got = ur.extract(rx, s.bins, n) / ur.extract(np.tile(tx, 8), s.bins, n)
    worst = np.abs(20 * np.log10(np.abs(got / truth))).max()
    if worst < 1e-9:
        rep.ok("known channel recovered", f"worst error {worst:.1e} dB")
    else:
        rep.bad("known channel", f"worst error {worst:.3f} dB")
    return 1 if rep.failed else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--args", default="type=x300",
                   help="UHD device args, e.g. addr=192.168.10.2")
    p.add_argument("--ref", default="internal", help="clock source")
    p.add_argument("--allow-tx", action="store_true",
                   help="permit stage 5, which keys the transmitter")
    p.add_argument("--selftest", action="store_true",
                   help="run the arithmetic only; no radio needed")
    p.add_argument("--center", type=float, default=2.45e9)
    p.add_argument("--tones", type=int, default=21)
    p.add_argument("--tx-gain", type=float, default=0.0)
    p.add_argument("--rx-gain", type=float, default=20.0)
    p.add_argument("--monitor-chan", type=int, default=None,
                   help="second RX channel watching a coupler on the source")
    p.add_argument("--seconds", type=float, default=0.05,
                   help="capture length for the streaming stage")
    p.add_argument("--sweep", default="0.5e9,3e9,101",
                   help="start,stop,points for the stage 6 plan")
    p.add_argument("--n-fft", type=int, default=4096)
    p.add_argument("--report", type=Path)
    a = p.parse_args(argv)

    rep = Report()
    rep.say("USRP bring-up")
    rep.say(f"args {a.args!r}   ref {a.ref!r}   "
            f"transmit {'ENABLED' if a.allow_tx else 'disabled'}")

    if a.selftest:
        rc = selftest(rep)
        if a.report:
            rep.write(a.report)
        return rc

    uhd = stage_env(rep)
    rate = 0.0
    if uhd and stage_find(rep, uhd, a.args):
        rx = stage_open(rep, a.args, a.ref)
        if rx is not None:
            best = stage_rates(rep, rx)
            rate = stage_stream(rep, rx, best, a.seconds)
            rx.close()
            if a.allow_tx and rate:
                stage_loopback(rep, a.args, a.ref, rate, a.center, a.tones,
                               a.tx_gain, a.rx_gain, a.monitor_chan)
            elif not a.allow_tx:
                rep.stage(5, "loopback")
                rep.skip("transmit", "pass --allow-tx once the RF chain is safe")
    start, stop, points = (float(x) for x in a.sweep.split(","))
    stage_plan(rep, rate or 200e6, start, stop, int(points), a.n_fft)

    rep.say("")
    rep.say(f"{rep.failed} failed, {rep.skipped} skipped")
    if a.report:
        rep.write(a.report)
    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
