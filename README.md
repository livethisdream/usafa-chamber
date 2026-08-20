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
| `runs/` | Dashboard scan output; each `meta.json` records `mode` as `hw` or `sim` |
| `thru_run/` | Thru-line reference measurement, 72 angles × 101 freqs |
| `project/usafa-chamber_PROJECT.md` | Detailed status, decisions, and open items |

Venvs live outside the tree because this project sits under OneDrive — `.venv-win`
is a junction and `.venv-linux` a symlink. See `/setup-dual-venv`.
