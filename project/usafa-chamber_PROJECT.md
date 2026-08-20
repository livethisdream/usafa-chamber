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
- **2026-08-20** — `sweep_time_s()` is optional and fails fast. Reason: the
  A2202-Fx (26.3.1) does not implement `SENS:SWE:TIME?` — it answers -110
  "Command header error" and then never replies, so the unguarded call blocked
  for the full 120 s instrument timeout. It only ever fed a progress estimate,
  so it now uses a 3 s timeout and returns None.
- **2026-08-20** — `--dry-run` suppresses the VNA, not motion. Reason: the name
  invites the opposite reading, and the flag issues real seek commands. Spelled
  out in the help text.
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

**Both instruments verified against real hardware. Only the motion path is untested.**

Rig identity, read off the hardware 2026-08-20:
- VNA — `CMT, A2202-Fx, 26018474, 26.3.1/1`, via the S2VNA socket server on 5025
- Chassis — `ETS Lindgren EMCenter version 4.6.0`
- Slot 1, device A — `ETS-Lindgren, EMControl 7006-001, 2.10.3`
- Slot 1B → `ERROR 305` (no second device); slot 2 answers a *different* error
  (`Error 23`) so some other card is present; slots 3–8 → `ERROR 21` (empty)
- Turntable parked at **90.0°**, speed **100.0%**, `ACC?` 2.0, `ERR?` 0

**VNA path — passing.** `configure()`, `frequencies()` and `measure()` all behave:
101 points spanning exactly the requested 2.000–3.000 GHz, magnitudes −113.8 to
−92.1 dB (noise floor, nothing connected to the ports). Instrument state was
saved and restored around the test, so the chamber's 100 kHz–22 GHz S11 setup is
untouched.

**Positioner path — passing, read-only.** `identity()`, `position()`, `speed()`,
`latched_error()` and `in_motion()` all correct; a bad query raises `RuntimeError`
and the stream stays in sync afterwards. **Nothing has been commanded to move.**

Ten bugs fixed: five found by code review (motion-start race, angle overshoot,
duplicate 360°, silent row truncation, unchecked sweep errors) and five that only
hardware contact could expose (serial framing never applied, wrong slot, position
reply carries units, wrong speed mnemonic, write-reply desync) — plus the
unsupported `SENS:SWE:TIME?`. See Decisions.

**Open — distinguishing simulation from emulation.** *Device Emulation* was
switched on at the front panel; a re-probe confirms it changes nothing about the
command set (identity, `CP?`, `*OPC?`, `SPEED?`, `ERR?` all byte-identical), but
it is a legacy-protocol compatibility feature, not motion suppression. No
read-only query distinguishes "simulating" from "ready to move", so the real
Simulation toggle has to be found on the EMControl device page before
`--dry-run` can be run without turning the tower.

Also unresolved: no over-the-wire way to read the turntable's software limits or
continuous/non-continuous mode on this firmware — confirm on the front panel.

# ToDo

- [ ] Find the EMControl **Simulation** setting (distinct from Device Emulation)
      and switch Device Emulation back off.
- [ ] Run `pattern_measure.py --dry-run --step 5` against simulation mode to
      exercise `seek()` over all 72 angles with nothing turning.
- [ ] Confirm turntable continuous / non-continuous mode and the cable path on
      the EMControl front panel — not readable over the wire.
- [ ] First commanded motion on real mechanics, then a first real cut on a
      reference antenna to sanity-check pattern shape.
- [ ] Add sweep averaging; consider wiring `ACC?`/acceleration into the config.
- [ ] Consider restoring VNA trigger state on exit — a run leaves the instrument
      in `TRIG:SOUR BUS`, which makes the S2VNA GUI look frozen afterwards.
- [x] ~~Install the ETS-Lindgren EMCenter USB/VCP driver~~ — done, now COM16.
- [x] ~~Point `PositionerConfig.resource` at the real port~~ — `ASRL16::INSTR`.
- [x] ~~Cross-check `PositionerCmds` mnemonics~~ — probed directly against the card.
- [x] ~~Resolve the VISA backend~~ — `pyvisa-py` via the `sim` extra.
- [x] ~~Enable the S2VNA socket server~~ — done, listening on 5025.
- [x] ~~Verify the VNA SCPI mnemonics~~ — full path exercised on hardware.
