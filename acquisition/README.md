# Acquisition

Step-and-measure pattern acquisition for the chamber: a Copper Mountain
A2202-Fx VNA and an ETS-Lindgren EMControl 7006-001 positioner card, driven
directly instead of through EMQuest.

| File | Purpose |
| --- | --- |
| `pattern_measure.py` | The acquisition script |
| `mock_instruments.py` | Fake VNA and positioner, with fault injection |
| `dryrun.py` | Scenario suite run against the mocks |

## Before touching hardware

```bash
pip install -r requirements.txt
python3 dryrun.py
```

Nine scenarios exercise the state machine with nothing physically moving,
including the failure modes that are awkward to stage on real hardware: a
controller that reports motion-complete before it moves, an axis that settles
off target, a truncated sweep, and a comms drop mid-scan. Every scenario that
ends abnormally also asserts that the axis was commanded to stop.

Run one scenario verbosely with `python3 dryrun.py nominal`.

## A real run

```bash
python3 pattern_measure.py \
    --vna  TCPIP0::127.0.0.1::5025::SOCKET \
    --pos  TCPIP0::192.168.1.50::5025::SOCKET \
    --slot 5 --device A \
    --start-ghz 2.0 --stop-ghz 3.0 --points 201 --ifbw 300 \
    --from-deg=-180 --to-deg 175 --step 5 \
    --outdir runs/2026-08-16_aut1
```

Outputs land in `--outdir`: `pattern.csv` (long format, one row per
angle/parameter/frequency), `run_meta.json` (instrument IDs, sweep settings,
correction state, closure result), `pattern.png`, and optional per-angle
`.s2p` with `--s2p`.

### Worth knowing

- **Scan −180→175, not 0→355.** Home is then never more than half a turn away
  and the cable never passes ±180°. Check whether there is a rotary joint
  before doing anything else.
- **The angle grid never overshoots `--to-deg`.** If the step does not divide
  the span the run stops short and says so, rather than adding a point past
  the end.
- **Correction state is queried and reported.** An uncalibrated run prints a
  warning; it is not blocked, because a normalized relative pattern is
  sometimes exactly what you want.
- **The closure check** (on by default, `--no-closure` to skip) re-measures the
  start angle at the end and records the drift in `run_meta.json`. It costs one
  sweep and catches cable flex and thermal drift, which is the failure mode
  that quietly ruins a long scan.
- **`--backlash N`** takes up gear lash by approaching a target from below
  whenever a move reverses direction. Off by default; a monotonic scan already
  approaches every angle the same way.
- **`--aux-param S11`** records reflection alongside S21, which tells you if the
  AUT connection changed part way through.

## Still unverified against hardware

The EMCenter mnemonics are centralized in `PositionerCmds`. Cross-check them
against ETS-Lindgren manual 399342 for your firmware before the first live
run — particularly the slot/device prefix format and whether `*OPC?` returns
0/1 for motion state on your card.

The S2VNA socket server is off by default and the application must be running
for the instrument to answer on port 5025 at all.
