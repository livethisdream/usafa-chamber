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

The EMCenter mnemonics in `acquisition/config.py` (`PositionerCmds`) are the one
thing not yet verified against hardware. Cross-check them against ETS-Lindgren
manual 399342 for your firmware — particularly the slot/device prefix format and
whether `*OPC?` returns 0/1 for motion state on your card. Everything
positioner-side routes through that one dataclass.

Also: the S2VNA socket server is off by default, and the application must be
running for the instrument to answer on port 5025 at all.

## Design language

The UI follows [`livethisdream/phaser`](https://github.com/livethisdream/phaser)
— same token names, same values, same `html[data-theme="light"]` override, so
the two applications read as one family. The one deliberate divergence: plots
are drawn on canvas (`frontend/src/plot.js`) rather than with Plotly, so the UI
builds and runs on a lab machine with no package registry reachable. Both plot
classes expose `resize()` / `setData()` / `draw()`, so swapping in a charting
library later is a single-file change.
