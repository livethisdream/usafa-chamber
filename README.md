# USAFA Chamber

Antenna pattern measurement for the USAFA anechoic chamber.

- **VNA** — Copper Mountain A2202-Fx, SCPI over the socket server S2VNA exposes on TCP 5025
- **Positioner** — ETS-Lindgren EMControl 7006-001, **slot 1 device A**, in an EMCenter chassis,
  over its FTDI virtual COM port at **115200 8N1**

## First-time setup

```powershell
.\setup.ps1
```

```bash
./setup.sh          # WSL / Linux
```

Creates the out-of-tree venv and its junction/symlink, installs Python and frontend
dependencies, and — on Windows — reports whether the rig is actually ready. It never
downloads or installs drivers for you; where something is missing it names the fault
and where to get the fix.

`.\setup.ps1 -CheckOnly` diagnoses without changing anything, which is safe to run on
the acquisition PC mid-session. Both scripts are idempotent.

The readiness checks distinguish three states that look identical in Device Manager
and are easy to misdiagnose: hardware never connected, hardware installed but
currently unplugged, and hardware connected with a driver that failed to install.
Only the last one calls for reinstalling anything.

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

`uicheck.py` drives the built dashboard against that simulator in headless
Chromium and asserts on what the page shows rather than on what the service sent:

```bash
pip install playwright && playwright install chromium
npm --prefix frontend install && npm --prefix frontend run build
python uicheck.py                  # screenshots land in ui-shots/
```

## The dashboard

The chrome follows [`livethisdream/phaser`](https://github.com/livethisdream/phaser)
— same tokens, same components — so the two applications read as one family:

- **An accordion of settings** on the left, over four sections: **VNA**,
  **Turntable** (the angle grid, plus jog and define-zero), **Simulation**, and
  **Output** (run name, and the stored runs, any of which can be reloaded into the
  plots). Collapsing the sidebar leaves an icon rail behind, and clicking a rail
  icon expands straight back into that section.
- **Tabs over the plots**: Pattern Measurement, VNA, Turntable, Logs. Inactive
  panes are hidden with `visibility`, not `display`, so a chart keeps its size and
  comes back drawn rather than the wrong shape.
- **Theme at the foot of the sidebar**, cycling system → light → dark. System is
  the default and stays live: flipping the OS theme with the page open moves the
  page with it, until someone picks a side.

Start scan and **STOP** sit in the tab header rather than in the accordion, because
STOP must never be a scroll away.

The Turntable tab draws the axis: commanded against reported, and which points on
the grid are measured. It is canvas rather than Plotly (`frontend/src/dial.js`) —
an instrument face needs no axes, legend, or hover.

### Comparing against a simulation

The Simulation section imports a pattern and overlays it on the measured cut in
amber, with an RMS deviation in the footer. It reads this project's own
`pattern.csv` and, failing that, any CSV carrying an angle column and a dB column,
which covers most solver exports. Both traces are normalized to their own peak, so
a model in dBi and a measurement in raw S21 dB are still comparable; **Rotate**
takes out a known mount offset.

The comparison is clamped 30 dB below peak before differencing, which matters more
than it sounds. Nulls are where a model and a measurement disagree most and where
the disagreement means least — a null one degree off its predicted angle
differences to tens of dB against a neighbouring lobe. Un-clamped, the RMS reports
null alignment rather than pattern agreement.

Parsing happens in the browser (`frontend/src/reference.js`); nothing is uploaded,
and the service never learns a comparison is running. `uicheck.py` re-imports a
finished run's own `pattern.csv` and asserts the deviation comes back at 0.00 dB,
which is a round trip through the writer, the parser, and the comparator at once.

## Software and drivers

Neither instrument works out of the box on a fresh machine. Both of these were
needed to get this rig talking.

**EMCenter USB drivers (ETS-Lindgren)** — [`EMCenter_USB_Drivers_2.12.36.4_Signed.zip`](https://support.ets-lindgren.com/public/other/downloads/get-download?software=other&filename=EMCenter_USB_Drivers_2.12.36.4_Signed.zip&securetype=public&folder=other)

Without these the chassis still enumerates — Device Manager shows *ETS-Lindgren
EMCenter USB Device* under **Universal Serial Bus devices** and it looks healthy —
but its virtual COM port child fails with **problem code 28**
(`CM_PROB_FAILED_INSTALL`) and no COM port is ever assigned, so nothing can open it.
Stock FTDI drivers do not claim ETS-Lindgren's custom PID (`VID_0403&PID_8570`).
Installing this package produces the COM port (COM16 on this machine).

**S2VNA (Copper Mountain)** — [demo software download](https://coppermountaintech.com/demo-the-software/#elementor-action%3Aaction%3Dpopup%3Aopen%26settings%3DeyJpZCI6IjIxNzcyIiwidG9nZ2xlIjpmYWxzZX0%3D)

Installs to `C:\VNA\S2VNA\` rather than Program Files, which is worth knowing if you
go looking for it. The A2202-Fx enumerates as `USB\VID_36BF&PID_1413` under device
class `USBDevice`, so in Device Manager it appears under **Universal Serial Bus
devices** — not Ports, and not under any VNA-sounding heading.

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
| `uicheck.py` | Browser check: drives the built dashboard against `--sim` |
| `runs/` | Dashboard scan output; each `meta.json` records `mode` as `hw` or `sim` |
| `thru_run/` | Thru-line reference measurement, 72 angles × 101 freqs |
| `project/usafa-chamber_PROJECT.md` | Detailed status, decisions, and open items |
| `project/instrument-plane_DESIGN.md` | Design note: driving SDRs, the AWG and the PA (not built) |

Venvs live outside the tree because this project sits under OneDrive — `.venv-win`
is a junction and `.venv-linux` a symlink. See `/setup-dual-venv`.
