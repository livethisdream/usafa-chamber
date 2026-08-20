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
- **2026-08-20** — Dashboard mirrors the Phaser architecture: WebSocket service
  with a JSON `{cmd, id}` protocol, a transport facade on the frontend, and a
  simulated backend preserving the payload contract. Reason: it is a proven
  shape in this codebase, and the sim makes the UI developable off-site.
- **2026-08-20** — The scan worker owns the backend and STOP sets a flag it
  polls, rather than a second thread writing a stop command. Reason: the
  instruments are single-threaded and stateful; a competing write would land in
  the middle of another thread's request/response exchange. Serial access stays
  single-threaded and stop still responds in ~100 ms via `seek(should_abort=)`.
- **2026-08-20** — Repo hosted under OneDrive at `USAFA/usafa-chamber` (sibling to
  `USAFA/ece444`), mounted into cdocker, remote `livethisdream/usafa-chamber`.

# Plan

**Phase 1 (done): service, simulator, transport.** `chamber_service.py` plus the
Vite frontend, verified against both backends. See `README.md` to run either.

**Later:**
- Phase 2 — polish the hardware path in the UI: richer status, jog affordances,
  surfacing link-retry warnings to the operator rather than only stderr.
- Phase 3 — run browser: list/load stored runs, overlay cuts, export.
- Teach the simulator to replay a recorded run (`thru_run/pattern.csv`) instead
  of synthesizing, so the UI can be exercised against real measured data.
- Sweep averaging; wire `ACC?`/acceleration into the config.
- First real cut on a reference antenna once one is mounted.

# Status

**Full acquisition chain verified on hardware, and dashboard phase 1 is complete.**

Rig — VNA `CMT, A2202-Fx, 26018474, 26.3.1/1`; chassis `EMCenter 4.6.0`; slot 1A
`EMControl 7006-001, 2.10.3`. Motion confirmed visually more than once
(90°→150°→90° at 40% speed, ~6.7 deg/s, exact arrivals, no latched errors).

**Thru-line reference (`thru_run/`), 72 angles × 101 freqs, 2–3 GHz:** S21 flat to
**0.019 dB peak-to-peak across the full rotation** (σ 0.0029 dB), mean −0.88 dB.
The polar plot is a clean circle — the correct answer for a thru, and a strong
stability figure for the whole chain. Anything above ~0.03 dB of angular
structure in a real measurement is therefore signal, not instrumentation.

**Dashboard phase 1 (`chamber_service.py` + `frontend/`).** Verified against the
simulator: every command, unknown-command rejection, live push frames,
concurrent-scan refusal, mid-scan jog refusal, STOP preempting a running scan,
run persistence and reload, and polar trace closure on a full rotation versus
staying open on a partial arc. `HardwareBackend` separately verified read-only
against the real rig — identity, `configure()` from a `ScanRequest` (51 pts over
exactly 2.4–2.6 GHz), `measure()`, and `get_state()` reporting `mode: hw`.

**Link quality remains the open hardware concern.** One 72-point run logged 3
`ERROR 1` retries and 2 corrupted position reads, ~7% of exchanges; a later
rotation test logged none. Both failure modes are handled in software now, but
the physical cause is unaddressed — cable, hub, or RF pickup are the candidates.

Twelve bugs fixed: five from code review, five that only hardware contact could
expose, the unsupported `SENS:SWE:TIME?`, and the split-reply position bug.

**Instrument state left behind:** the VNA is on 2–3 GHz / 101 pts / S21 from the
reference scan, not the 100 kHz–22 GHz S11 sweep it held at session start.

# ToDo

- [ ] Chase the FTDI link corruption physically — different cable, no hub, check
      routing relative to the VNA and chamber feed.
- [ ] Phase 2: hardware-path polish in the UI; surface link-retry warnings to the
      operator instead of only stderr.
- [ ] Phase 3: run browser — list/load stored runs, overlay cuts, export.
- [ ] Teach the simulator to replay `thru_run/pattern.csv` for real data shapes.
- [ ] Confirm continuous / non-continuous mode on the front panel before any run
      with a cable routed through the tower.
- [ ] Switch Device Emulation back off — unused, and an uncharacterized variable.
- [ ] Restore the VNA to its original sweep setup, or set it up fresh next visit.
- [ ] First real cut on a reference antenna; add sweep averaging.
- [x] ~~Install the EMCenter USB/VCP driver~~ — done, COM16.
- [x] ~~Cross-check `PositionerCmds` mnemonics~~ — probed against the card.
- [x] ~~Enable the S2VNA socket server~~ — listening on 5025.
- [x] ~~Verify the VNA SCPI path~~ — configure/frequencies/measure exercised.
- [x] ~~Validate `seek()` against real mechanics~~ — visually confirmed.
- [x] ~~First full collection run~~ — 72-point thru reference.
- [x] ~~Dashboard phase 1~~ — service, sim backend, transport, frontend.
- [x] ~~Verify `HardwareBackend` against the real rig~~ — read-only check passed.
