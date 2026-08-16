# Acquisition

The measurement. Complete on its own — the service and UI are optional layers
on top.

| Module | Role |
| --- | --- |
| `config.py` | What a run *is*. Dataclasses only, no I/O, no VISA. |
| `instruments.py` | `Vna` and `Positioner`. The only module that imports VISA. |
| `engine.py` | The step-and-measure loop, reporting through `emit`. |
| `writers.py` | CSV, Touchstone, run metadata, polar plot. |
| `pattern_measure.py` | Command line front end. |
| `mock_instruments.py` | Fake instruments with fault injection. |
| `dryrun.py` | Scenario suite against the mocks. |

## Before touching hardware

```bash
pip install -r ../requirements.txt
python3 -m acquisition.dryrun
```

Nine scenarios exercise the state machine with nothing physically moving,
including failure modes that are awkward to stage on real hardware: a controller
that reports motion-complete before it moves, an axis that settles off target, a
truncated sweep, a comms drop mid-scan. Every scenario that ends abnormally also
asserts the axis was commanded to stop.

Run one verbosely with `python3 -m acquisition.dryrun nominal`.

## A real run

```bash
python3 -m acquisition.pattern_measure \
    --vna  TCPIP0::127.0.0.1::5025::SOCKET \
    --pos  TCPIP0::192.168.1.50::5025::SOCKET \
    --slot 5 --device A \
    --start-ghz 2.0 --stop-ghz 3.0 --points 201 --ifbw 300 \
    --from-deg=-180 --to-deg 175 --step 5 \
    --outdir runs/2026-08-16_aut1
```

## Worth knowing

- **Scan −180→175, not 0→355.** Home is then never more than half a turn away
  and the cable never passes ±180°. Check whether there is a rotary joint
  before doing anything else.
- **The angle grid never overshoots `--to-deg`.** If the step does not divide
  the span the run stops short and says so, rather than adding a point past the
  end. A full circle drops the duplicate endpoint.
- **A position mismatch aborts, it does not warn.** An axis that will not settle
  within `--tol` retries once, then fails. A pattern recorded at the wrong angle
  is worse than no pattern. If your table routinely lands 1–2° off, loosen
  `--tol` rather than removing the check.
- **No `SYST:PRES`.** It is the obvious fix for inherited instrument state, but
  on most VNAs it clears the calibration too. Sweep type, averaging, and
  smoothing are written explicitly instead, and correction state is queried and
  reported. An uncalibrated run warns and proceeds — a normalized relative
  pattern is sometimes exactly what you want.
- **The closure check** (default on, `--no-closure` to skip) re-measures the
  start angle at the end and records the drift in `run_meta.json`. One extra
  sweep; catches cable flex and thermal drift, the failure mode that quietly
  ruins a long scan.
- **`--backlash N`** takes up gear lash by approaching a target from below when
  a move reverses direction. Off by default; a monotonic scan already approaches
  every angle the same way.
- **`--aux-param S11`** records reflection alongside S21, which tells you if the
  AUT connection changed part way through.

## Still unverified against hardware

The EMCenter mnemonics live in `PositionerCmds` (`config.py`). Cross-check them
against ETS-Lindgren manual 399342 for your firmware before the first live run —
particularly the slot/device prefix format and whether `*OPC?` returns 0/1 for
motion state on your card.

The S2VNA socket server is off by default and the application must be running
for the instrument to answer on port 5025 at all.
