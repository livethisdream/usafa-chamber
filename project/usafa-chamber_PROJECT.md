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
- **2026-08-20** — Positioner serial framing is **115200 8N1** on the EMCenter USB
  port. Reason: probed it directly; 9600 in both 7O1 and 8N1 times out. The
  9600,7,Odd,1 in ETS-Lindgren's documentation describes the legacy
  Holaday-compatible rear port, not the USB virtual COM port.
- **2026-08-20** — Positioner writes drain their reply and queries raise on an
  `ERROR n` response. Reason: the chassis answers *every* command including bad
  ones. A write that left its error line unread desynchronized the stream, so
  each later query returned the previous command's error as if it were data.
- **2026-08-20** — Speed is left alone by default (`--speed` to override).
  Reason: the real mnemonic is `SPEED <percent>`, not `SP <preset>`; the old
  default would have pushed a preset index into a percentage field.
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

**Positioner: talking, verified, read-only tested.** **VNA: still no link.**

Rig identity, read off the hardware 2026-08-20:
- Chassis — `ETS Lindgren EMCenter version 4.6.0`
- Slot 1, device A — `ETS-Lindgren, EMControl 7006-001, 2.10.3`
- Slot 1B → `ERROR 305` (no second device); slot 2 answers a *different* error
  (`Error 23`) so some other card is present; slots 3–8 → `ERROR 21` (empty)
- Turntable parked at **90.0°**, speed **100.0%**, `ACC?` 2.0, `ERR?` 0

`Positioner` now round-trips against real hardware: `identity()`, `position()`,
`speed()`, `latched_error()`, `in_motion()` all correct, a bogus query raises
`RuntimeError`, and the stream stays in sync afterwards. **Nothing has been
commanded to move.**

Five acquisition bugs fixed earlier (motion-start race, angle overshoot,
duplicate 360°, silent row truncation, unchecked sweep errors) plus five driver
bugs the hardware exposed — see Decisions. Dual venvs and packaging in place;
`pyvisa-py` + `pyserial` supply the serial backend via the `sim` extra.

**Remaining blocker — S2VNA socket server is off.** The A2202-Fx itself is fine:
it enumerates as `USB\VID_36BF&PID_1413` under device class `USBDevice`, which is
why it shows up in Device Manager under *Universal Serial Bus devices* rather
than Ports or anything VNA-named. S2VNA is installed at `C:\VNA\S2VNA\S2VNA.exe`
(outside Program Files) and runs, but holds **no listening TCP port at all**. The
socket server is a GUI toggle — *System → Misc Setup → Network Setup → Socket
Server* — and is not exposed in any config file on disk.

Also unresolved: there is no over-the-wire way to read the turntable's software
limits or continuous/non-continuous mode on this firmware, so the cable-wrap
configuration has to be confirmed on the EMControl front panel before a scan.

# ToDo

- [ ] Enable the S2VNA socket server (System → Misc Setup → Network Setup) and
      confirm something listens on 5025.
- [ ] Read the VNA banner over the socket to verify the SCPI mnemonics in `Vna`
      the same way the positioner's were verified.
- [ ] Confirm turntable continuous / non-continuous mode and the cable path on
      the EMControl front panel — not readable over the wire.
- [ ] First commanded motion: small bounded move (90° → 95° → 90°) to validate
      `seek()` arrival detection against real mechanics.
- [ ] Full dry run in EMControl simulation mode.
- [ ] First real cut on a reference antenna to sanity-check pattern shape.
- [ ] Add sweep averaging; consider wiring `ACC?`/acceleration into the config.
- [x] ~~Install the ETS-Lindgren EMCenter USB/VCP driver~~ — done, now COM16.
- [x] ~~Point `PositionerConfig.resource` at the real port~~ — `ASRL16::INSTR`.
- [x] ~~Cross-check `PositionerCmds` mnemonics~~ — probed directly against the card.
- [x] ~~Resolve the VISA backend~~ — `pyvisa-py` via the `sim` extra.
