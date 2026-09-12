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

### Checking the drivers

`rigcheck.py` runs the acquisition drivers against fake instruments that
impersonate pyvisa, so `Vna` and `Positioner` execute unmodified:

```bash
python rigcheck.py                 # every scenario, PASS/FAIL
python rigcheck.py split_reply     # one scenario, verbose
```

This is a different kind of fake from `--sim`. `SimBackend` synthesizes a
finished pattern, which means it never runs the driver code at all — every fix
that hardware contact forced into `pattern_measure.py` had nothing checking it.
`mock_instruments.py` fakes one layer lower and can misbehave on cue, so the
retry-after-resync, the split-reply guard, the motion-start grace period and
the trigger-state restore are all exercised. The faults are the rig's own:

| Scenario | Reproduces |
|---|---|
| `split_reply` | a corrupted byte splitting `255.0 DEGREES` into `2` + `55.0 DEGREES` |
| `split_unguarded` | the same fault with the guard removed — proves the guard matters |
| `error_retry` | a provably valid `SK` rejected with `ERROR 1` mid-scan |
| `error_persistent` | a link that is genuinely broken, where retrying cannot help |
| `never_moves` | a seek that did not take — wrong prefix, wrong mnemonic, dead axis |
| `stuck` / `late_start` | a jammed axis, and a card slow to report motion |
| `short_sweep` / `drop_sweep` | a truncated trace, and a sweep that times out |
| `sweep_time` | firmware 26.3.1 answering `SENS:SWE:TIME?` with −110, then silence |
| `trigger_restore` | the VNA being handed back out of bus-trigger hold |
| `service_stops` | the dashboard's scan worker failing mid-move |
| `uncalibrated` / `correction_mute` | a VNA with correction off, and one that will not say |

The `cal_*` scenarios are a different kind of thing and are labelled as such in
the file: no calibration has ever been run through this path, so they pin the
*design* — exclusivity in both directions, a cancel that only claims what it can
do, a cleared collection buffer on every failing exit, and a run that says when
its sweep no longer matches the calibration. Those survive the ACM mnemonics
turning out to be wrong, which they may well.

`split_unguarded` is the one worth understanding. It neuters the position guard
and asserts the run *is* corrupted — without it, `split_reply` would pass even
if the guard were deleted, and a test that passes either way is not a test.

### Before the first run on new hardware

`rigcheck.py` proves the state machine but cannot prove the protocol
assumptions — the fakes encode the same assumptions the drivers do.
`bringup.py` is the other half. It talks to the real instruments one at a time
in increasing order of consequence:

```bash
python bringup.py --pos ASRL16::INSTR --slot 1 --device A
python bringup.py --mock                      # exercise the script itself
```

Stages 0–4 and 7 are read-only; **5 and 6 turn the tower and need
`--allow-motion`**. Stage 7 probes for the ACM2202 AutoCal module and never runs
a calibration: it asks whether the software can see a module, and tests whether
the AutoCal headers exist by sending them with their parameters missing and
reading the error queue, which cannot execute anything.
This rig's command set is already verified and recorded in `PositionerCmds`, so
most stages confirm rather than discover — the point is to notice drift on a
firmware change or a second card. `--probe` tries other serial framings and
slot/device prefixes when the card is silent, which is how 115200 8N1 was found
in the first place.

**Stage 4 is the one to reach for when nothing is obviously broken.** It hammers
the position query and reports an error rate, deliberately raw rather than
through `Positioner._q` — the driver's resync-and-retry is exactly what would
hide a degrading link. That turns the open FTDI question into a number you can
compare between cables.

`uicheck.py` drives the built dashboard against that simulator in headless
Chromium and asserts on what the page shows rather than on what the service sent:

```bash
uv sync --extra ui                 # playwright; kept out of the default install
.venv-win/Scripts/playwright.exe install chromium      # ~150 MB browser
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
STOP must never be a scroll away. A **CAL / UNCAL** pill sits beside them whenever
the VNA will say: an uncalibrated run is valid data and is never blocked, but it
should not be possible to take one without noticing.

### Calibration

The VNA section carries the last calibration and a **Calibrate…** button that
opens a wizard rather than starting anything. Running an AutoCal with the ACM2202
means somebody walks into the chamber, unmates the horn and the AUT, mates the
module across the cable ends and comes back out — so the modal states the sweep it
will calibrate at, asks which reference plane is being calibrated (nothing in the
data distinguishes the cable ends from the front panel afterwards, and the
difference is every dB of cable loss), takes an explicit acknowledgement, and ends
by reminding the operator to put the antennas back.

Cancel means *stop after the current step*, and says so on the button. An AutoCal
runs the whole short/open/load/thru sequence inside one instrument command; there
is nowhere to poll and nothing to interrupt, and a button that claimed otherwise
would be worse than no button.

Each calibration writes `runs/calibration/cal.json`, and every run copies that
record into its own `meta.json` — copied rather than referenced, because the next
calibration overwrites the file and a finished run has to keep saying what it was
taken against. A run set up at a different span, point count, IF bandwidth or
power than the calibration is flagged in the panel and warned about in the log:
correction interpolated across a span it never measured is a plausible-looking
answer of unknown quality.

**None of the AutoCal SCPI is verified.** It is centralized in `AcmCmds` for the
same reason `PositionerCmds` exists, and `bringup.py`'s stage 7 is what settles
it. See `project/acm-calibration_DESIGN.md`.

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

## Which computer runs this

The rig has run on Windows so far, but nothing here requires it. S2VNA ships for
Windows and for Linux on both x86_64 and ARM, and it is S2VNA — not this project —
that talks to the VNA over USB. Everything here reaches the instrument through
S2VNA's **socket server on TCP 5025**, so the VNA side is identical on every
platform, and can just as easily live on a different machine:

```bash
python chamber_service.py --vna TCPIP0::<other-host>::5025::SOCKET
```

The only genuinely platform-specific value in the project is the positioner's
resource string — `ASRL16::INSTR` on Windows, `ASRL/dev/ttyUSB0::INSTR` on Linux.
pyvisa parses both into the same ASRL resource and the 115200 8N1 framing applies
either way. `bringup.py` stage 0 reports the host and, on Linux, names the serial
devices it can see.

Two things to confirm before settling on a host, neither of which is a code
question:

- **Does the build for that platform support the A2202?** S2VNA's model coverage
  has grown over time and this rig runs 26.3.1. A build older than that may not
  know the instrument, whatever the architecture.
- **Is AutoCal supported there?** The ACM2202 is a USB device driven by the VNA
  software. `bringup.py` stage 7 answers this wherever you run it.

Throughput is unlikely to decide it. A chamber run is dominated by mechanical
settling, not by sweeps — `thru_run/` is 72 angles of 101 points, and the axis
spends far longer moving than the VNA spends measuring.

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
- **Every failure path stops the axis before releasing the port.** Both entry points
  used to leave this to chance: the CLI stopped only on `KeyboardInterrupt`, and the
  service's worker logged the error and returned. An exception raised inside `seek()`'s
  wait loop arrives with the tower still turning, and closing the port first discards
  the only means of stopping it. `rigcheck.py`'s `drop_sweep` and `service_stops`
  scenarios hold both paths to it.

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
| `rigcheck.py` | Driver check: 28 fault scenarios against fake instruments |
| `bringup.py` | Staged hardware bring-up; read-only until `--allow-motion` |
| `mock_instruments.py` | pyvisa stand-ins that misbehave on cue |
| `runs/` | Dashboard scan output; each `meta.json` records `mode` as `hw` or `sim` |
| `thru_run/` | Thru-line reference measurement, 72 angles × 101 freqs |
| `project/usafa-chamber_PROJECT.md` | Detailed status, decisions, and open items |
| `project/instrument-plane_DESIGN.md` | Design note: driving SDRs, the AWG and the PA (not built) |
| `project/acm-calibration_DESIGN.md` | Design note: ACM2202 calibration from the dashboard |

Venvs live outside the tree because this project sits under OneDrive — `.venv-win`
is a junction and `.venv-linux` a symlink. See `/setup-dual-venv`.
