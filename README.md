# USAFA Chamber

Antenna pattern measurement for the USAFA anechoic chamber.

- **VNA** — Copper Mountain A2202-Fx, SCPI over the socket server S2VNA exposes on TCP 5025
- **Positioner** — ETS-Lindgren EMControl 7006-001, **slot 1 device A**, in an EMCenter chassis,
  over its FTDI virtual COM port at **115200 8N1**

## Working away from the chamber

Everything below runs with no instruments attached.

```bash
# 1. service, simulated backend
python chamber_service.py --sim

# 2. frontend, in another shell
npm run dev --prefix frontend      # http://localhost:5173
```

The simulator preserves the WebSocket payload contract exactly, so anything built
against it works unchanged on the real rig. It synthesizes an array-factor pattern
(main lobe, decaying sidelobes, nulls floored at −45 dB) and simulates slew at a
believable rate, so live position, progress and the stop button all have something
real to exercise.

## At the chamber

S2VNA must be **running** with its socket server enabled — *System → Misc Setup →
Network Setup → Socket Server*. It is off by default, and nothing listens on 5025
until you turn it on.

```bash
python chamber_service.py                 # real rig; falls back to sim if absent
python chamber_service.py --no-fallback    # refuse to fall back — fail loudly instead
```

Standalone acquisition without the dashboard:

```bash
python pattern_measure.py --from-deg 0 --to-deg 355 --step 5 \
    --start-ghz 2 --stop-ghz 3 --points 101 --param S21 --outdir my_run
```

`--dry-run` steps the positioner without opening the VNA. **It means no VNA, not no
motion** — it issues real seek commands and will turn the tower unless EMControl is
in simulation mode.

## Safety

- **Cable wind-up is the standing hazard.** This firmware exposes no way to read the
  software limits or the continuous/non-continuous mode (`UL?`, `LL?`, `MODE?` all
  return `ERROR 1`), so confirm both on the EMControl front panel before any run with
  a cable routed through the tower. Continuous mode ignores software limits.
- *Device Emulation* on the front panel is **not** simulation. It is legacy-protocol
  compatibility and does not inhibit the motor. It leaves this command set unchanged.
- The dashboard's STOP is always enabled and preempts a move in progress; the service
  cancels via a flag the scan worker polls rather than writing to the port from a
  second thread.

## Link reliability

The FTDI link corrupts bytes at a measurable rate — roughly 7% of exchanges in one
72-point run. Two distinct failure modes, both handled in software:

- **Outgoing corruption** → the card replies `ERROR 1`. Commands are retried once
  after a resync. Every write is an idempotent absolute instruction, so this is safe.
- **Incoming corruption** → a reply is split and the fragment still parses as a
  plausible angle (`'255.0 DEGREES'` → `'2'` + `'55.0 DEGREES'`). Position replies are
  validated against the full `<number> DEGREES` format, so a bare number is rejected.

Retries warn on stderr. A run logging many of them means the link is degrading —
suspect cable, hub, or RF pickup rather than the software.

## Layout

| Path | What |
|---|---|
| `pattern_measure.py` | Instrument wrappers and standalone step-and-measure acquisition |
| `chamber_service.py` | WebSocket service (port 8766), hardware + simulated backends |
| `frontend/` | Vite + Plotly dashboard |
| `runs/` | Dashboard scan output; each `meta.json` records `mode` as `hw` or `sim` |
| `thru_run/` | Thru-line reference measurement, 72 angles × 101 freqs |
| `project/usafa-chamber_PROJECT.md` | Detailed status, decisions, and open items |

Venvs live outside the tree because this project sits under OneDrive — `.venv-win`
is a junction and `.venv-linux` a symlink. See `/setup-dual-venv`.
