---
name: "#usafa-chamber"
dateCreated: 2026-08-20
dateModified: 2026-08-20
container: cdocker
---
# Overview

Antenna pattern measurement in the USAFA anechoic chamber. `pattern_measure.py` is a
step-and-measure acquisition script: it rotates the tower to each angle, triggers a
single VNA sweep, and logs the complex S-parameter across the whole frequency span.

Hardware pair:
- **VNA** — Copper Mountain A2202-Fx, driven by SCPI over a TCP socket (`:5025`)
  exposed by the S2VNA host application.
- **Positioner** — ETS-Lindgren EMControl 7006-001 card in an EMCenter chassis.

Outputs per run: `pattern.csv` (long format — angle, freq, re, im, mag_dB, phase_deg),
an optional per-angle Touchstone `cut_<angle>.s2p` (S21 populated, other parameters
zero-filled), and a normalized polar `pattern.png` at the requested cut frequency.

# Special Instructions

- **Cable wind-up is the standing hazard.** Confirm the turntable's continuous vs.
  non-continuous mode before any scan — continuous mode ignores software limits and
  will wrap the RF cable if there is no rotary joint in the path.
- **Shake the state machine out in simulation mode first.** EMControl has a simulation
  toggle on its Config screen; run a full scan against it before anything physically moves.
- **The EMCenter command mnemonics are unverified.** They are centralized in the
  `PositionerCmds` dataclass specifically so they can be cross-checked in one place
  against ETS-Lindgren manual **399342** (the EMCenter manual, not the 7006-001 card
  manual) for the installed firmware revision.
- Venvs live outside the tree (`.venv-win` / `.venv-linux` junctions) because this
  project sits under OneDrive, which otherwise syncs venv contents.

# Decisions

- **2026-08-20** — Positioner arrival is confirmed by position readback, never by
  elapsed time. Reason: the original `seek()` slept a fixed 200 ms and then polled
  `*OPC?`; if the card was slower than that to report motion, the poll saw "not
  moving" and the sweep ran mid-rotation. That failure is silent and produces cuts
  that look plausible, which is the worst kind of measurement bug.
- **2026-08-20** — `seek()` raises rather than warns when motion was never observed
  *and* the tower is off-target. Reason: a wrong slot/device prefix would otherwise
  log a full scan of identical cuts with only a stderr warning per angle.
- **2026-08-20** — `angles()` floors instead of rounding. Reason: rounding let a
  non-divisible step overshoot the requested stop angle (`0→355 step 7` commanded
  357°), which on a limit-configured turntable is a move outside the intended range.
- **2026-08-20** — Repo hosted under OneDrive at `USAFA/usafa-chamber` (sibling to
  `USAFA/ece444`), mounted into cdocker, remote `livethisdream/usafa-chamber`.

# Plan

**Phase 1 (current): get a link to both instruments.** Neither is reachable from this
laptop yet — see Status. Until the EMCenter enumerates a COM port and S2VNA is
running, the acquisition path cannot be exercised against hardware.

**Later:**
- Dry-run the full scan against EMControl simulation mode.
- Verify every mnemonic in `PositionerCmds` against manual 399342.
- First real cut on a known reference antenna to sanity-check pattern shape.
- Add sweep averaging (matters for depth in the nulls) and a `--speed` CLI flag.

# Status

Code is fixed and the offline half is verified; hardware is not yet reachable.

**Verified working (2026-08-20)**, via a stubbed-instrument harness under Python 3.12 / numpy 2.5.2:
- `angles()` no longer overshoots the stop angle and no longer double-measures 0°/360°.
- `measure()` raises on a point-count mismatch instead of letting `zip` silently truncate rows.
- `seek()` parks correctly even when the card is ~1 s late to report motion, and raises
  when the tower never moves at all.
- `_write_s2p` emits correct 9-column RI 2-port Touchstone (S11/S21/S12/S22 ordering).
- `polar_plot` renders; `np.fromstring(sep=",")` confirmed *not* deprecated on numpy 2.5.2.
- `pattern_measure.py --help` runs against real `pyvisa` 1.16.2 in `.venv-win`.

**Hardware blockers (2026-08-20):**
- **EMCenter positioner** — chassis is plugged in and enumerates
  (`USB\VID_0403&PID_8570`, `BusReportedDeviceDesc: EMCenter 7000-series`, parent
  driver healthy). Its virtual COM port child
  (`FTDIBUS\VID_0403+PID_8570+00AUG2EEA\0000`) fails with **problem code 28**
  (`CM_PROB_FAILED_INSTALL`), `ConfigFlags 64`, and no `PortName` in the registry.
  `ftdibus.sys` and `ftser2k.sys` are both present, so this is the ETS-Lindgren `.inf`
  binding the custom PID `8570` to the VCP driver not being installed — stock FTDI
  drivers only claim standard PIDs. **There is no COM port to open.**
- **VNA** — absent. No Copper Mountain software in Program Files, nothing listening on
  TCP 5025. The A2202-Fx needs S2VNA running on this host to expose the SCPI socket.
- **VISA backend** — no `visa64.dll` at the IVI Foundation path. Keysight and NI trees
  exist under Program Files but the IVI shared component is not where `pyvisa` looks.
  `pyvisa-py` is available as the `sim` extra as a fallback.
- The config default `192.168.1.50` for the positioner is not on any subnet this
  machine has (`10.192.93.x`, `172.25.144.x` WSL, link-local) — it is a placeholder,
  not the real address.

# ToDo

- [ ] Install the ETS-Lindgren EMCenter USB/VCP driver package to clear problem code 28
      and get a COM port assigned.
- [ ] Point `PositionerConfig.resource` at the resulting `ASRL<n>::INSTR` (9600,7,Odd,1
      on the Holaday-compatible port) instead of the placeholder TCPIP address.
- [ ] Install / launch S2VNA and confirm the socket server is listening on 5025.
- [ ] Resolve the VISA backend — either install IVI shared components or pin
      `pyvisa-py` via the `sim` extra.
- [ ] Cross-check `PositionerCmds` mnemonics against ETS-Lindgren manual 399342.
- [ ] Full dry run in EMControl simulation mode before commanding real motion.
- [ ] Confirm turntable continuous / non-continuous mode and the cable path.
- [ ] Add sweep averaging and a `--speed` CLI flag.
