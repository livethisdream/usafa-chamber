# USAFA Chamber

Custom step-and-measure controller for the anechoic chamber — a Copper Mountain
A2202-Fx VNA and an ETS-Lindgren EMControl 7006-001 positioner card, driven
directly instead of through EMQuest.

Three layers, each usable without the ones above it:

```
acquisition/   the measurement.  CLI-complete on its own, no service needed.
service/       WebSocket control plane wrapping the engine.  No UI needed.
frontend/      browser UI.  Talks only to the service, over one transport facade.
```

The seam that makes this work is `ScanEngine.emit` — the engine reports through
a callback and knows nothing about who is listening. The CLI turns those events
into stdout lines; the service turns the same events into JSON frames. Neither
front end can drift from the other, because there is only one implementation of
the scan.

## Quick start, no hardware

Everything below runs against mock instruments.

```bash
pip install -r requirements.txt

# 1. the measurement, on its own
python3 -m acquisition.dryrun                 # 9 scenarios, PASS/FAIL

# 2. the service protocol
python3 -m service.smoke                      # 3 scenarios over a real socket

# 3. the whole stack with a UI
cd frontend && npm install && npm run build && cd ..
python3 -m service.server --mock
# open http://localhost:8080
```

## A real run

Command line:

```bash
python3 -m acquisition.pattern_measure \
    --vna  TCPIP0::127.0.0.1::5025::SOCKET \
    --pos  TCPIP0::192.168.1.50::5025::SOCKET \
    --slot 5 --device A \
    --start-ghz 2.0 --stop-ghz 3.0 --points 201 --ifbw 300 \
    --from-deg=-180 --to-deg 175 --step 5 \
    --outdir runs/2026-08-16_aut1
```

Or start the service without `--mock` and drive it from the browser. Both write
the same outputs to the run directory: `pattern.csv` (long format, one row per
angle/parameter/frequency), `run_meta.json` (instrument IDs, sweep settings,
correction state, closure result), `pattern.png`, and optional per-angle `.s2p`.

See `acquisition/README.md` for the measurement details and the flags that
matter, and `service/README.md` for the wire protocol.

## Before the first live run

The dry-run suites prove the state machine. They cannot prove the *protocol*
assumptions, because the mocks encode the same assumptions the code does. So
the first contact with hardware goes through the bring-up script, which talks to
one instrument at a time in increasing order of consequence:

```bash
python3 tools/bringup.py --vna TCPIP0::127.0.0.1::5025::SOCKET \
                         --pos TCPIP0::192.168.1.50::5025::SOCKET \
                         --slot 5 --device A --sweep
```

Stages 0–3 are read-only — they query and never command motion. Stages 4 (a 5°
jog) and 5 (a four-point scan) require `--allow-motion`. Run stage 4 with
EMControl's simulation mode on the first time; it exercises the query semantics
with nothing physically turning.

If the positioner does not answer, `--probe` tries the other plausible prefix
and termination combinations and reports which one worked; `--probe-ports`
TCP-connects to a few plausible ports to find which is listening. `--report
FILE` saves the transcript.

What is most likely to need adjusting, all of it in `acquisition/config.py`:

- **The positioner's TCP port.** The default resource string carries `5025`,
  which is the *VNA's* convention used as a placeholder. Confirm the EMCenter's
  actual port before trusting it.
- The `5A:` slot/device prefix format, and the `SK`/`CP?`/`ST`/`SP` mnemonics.
- Whether `*OPC?` returns 0 while moving on your card — the settle logic rests
  on it, and stage 3 checks it directly.
- Termination; CR out and LF in is assumed.

Cross-check against ETS-Lindgren manual 399342 for your firmware. Everything
positioner-side routes through `PositionerCmds` and `PositionerConfig`, so a
mismatch is a one-file edit.

Also: you need a VISA backend (`pip install pyvisa-py`, or vendor VISA), and the
S2VNA socket server is off by default — the application must be running with it
enabled for the instrument to answer at all.

## Design language

The UI follows [`livethisdream/phaser`](https://github.com/livethisdream/phaser)
— same token names, same values, same `html[data-theme="light"]` override, so
the two applications read as one family. The one deliberate divergence: plots
are drawn on canvas (`frontend/src/plot.js`) rather than with Plotly, so the UI
builds and runs on a lab machine with no package registry reachable. Both plot
classes expose `resize()` / `setData()` / `draw()`, so swapping in a charting
library later is a single-file change.
