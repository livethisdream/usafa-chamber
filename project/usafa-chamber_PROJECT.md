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
- **2026-08-20** — Positioner commands retry once after a resync, and position
  replies are validated against the full `<number> DEGREES` format. Reason: the
  FTDI link corrupts bytes at a measurable rate. Outgoing corruption produces
  `ERROR 1` (loud); incoming corruption splits a reply so the fragment still
  parses as a plausible angle (silent, and therefore worse).
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

**Full chain verified end to end on hardware, including a completed 72-point scan.**

Rig, read off the hardware 2026-08-20:
- VNA — `CMT, A2202-Fx, 26018474, 26.3.1/1` over the S2VNA socket server on 5025
- Chassis — `ETS Lindgren EMCenter version 4.6.0`
- Slot 1A — `ETS-Lindgren, EMControl 7006-001, 2.10.3`; slots 3–8 empty, slot 2
  holds some other card that rejects EMControl mnemonics
- Motion confirmed visually: 90°→150°→90° at 40% speed, ~6.7 deg/s

**Thru-line reference run (`thru_run/`), 72 angles × 101 freqs, 2–3 GHz:**
- S21 flat to **0.019 dB peak-to-peak across the full rotation**; std dev
  0.0029 dB; worst per-frequency angular spread 0.025 dB at 2.040 GHz
- Polar plot is a clean circle — the correct answer for a thru, and a good
  system-stability figure for the whole chain
- Mean level −0.88 dB (cable loss)

**Link quality is the open concern.** That run logged 3 `ERROR 1` retries on
outgoing `SK` commands and 2 corrupted position reads — roughly 7% of exchanges
affected. The corrupted reads decoded as split replies (`'255.0 DEGREES'` →
`'2'` + `'55.0 DEGREES'`), which is why position replies are now format-validated.
All are handled in software now, but the physical cause is unaddressed: candidates
are USB cable quality, a hub in the path, or RF pickup from the VNA in the chamber.

Eleven bugs fixed total: five from code review, five that only hardware contact
could expose, plus the unsupported `SENS:SWE:TIME?` — see Decisions.

# ToDo

- [ ] Chase the FTDI link corruption physically — try a different cable, remove
      any hub from the path, check routing relative to the VNA and chamber feed.
- [ ] Build the browser dashboard (see Plan) — WebSocket service plus Vite
      frontend, mirroring the Phaser architecture and visual language.
- [ ] Confirm turntable continuous / non-continuous mode on the front panel
      before any run with a cable routed through the tower.
- [ ] Switch Device Emulation back off — it changes nothing we depend on, but
      it is an uncharacterized variable.
- [ ] First real cut on a reference antenna to sanity-check pattern shape.
- [ ] Add sweep averaging; consider wiring `ACC?`/acceleration into the config.
- [x] ~~Install the ETS-Lindgren EMCenter USB/VCP driver~~ — done, COM16.
- [x] ~~Cross-check `PositionerCmds` mnemonics~~ — probed against the card.
- [x] ~~Enable the S2VNA socket server~~ — done, listening on 5025.
- [x] ~~Verify the VNA SCPI path~~ — configure/frequencies/measure all exercised.
- [x] ~~Validate `seek()` against real mechanics~~ — visually confirmed.
- [x] ~~First full collection run~~ — 72-point thru reference in `thru_run/`.
