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

# Traps

- **Windows keeps a device node for hardware it has ever seen.** A ghost reports
  `Present=False`, `Status=Unknown` and an empty problem code — indistinguishable
  from a failed driver install unless you check `Present` first. Cost: `setup.ps1`
  initially prescribed a driver reinstall for an unplugged chassis.
- **Device Emulation is not simulation.** The EMControl front-panel toggle is
  legacy-protocol compatibility; it does not inhibit the motor, and no read-only
  query distinguishes "simulating" from "ready to move".
- **`--dry-run` means no VNA, not no motion.** It issues real seek commands and
  will turn the tower unless EMControl is genuinely in simulation mode.
- **S2VNA's socket server is off by default** and is a GUI-only toggle
  (System → Misc Setup → Network Setup). Nothing listens on 5025 until it is on,
  and it keeps listening after the VNA is unplugged.
- **Neither instrument is where you would look for it.** S2VNA installs to
  `C:\VNA\`, not Program Files; the A2202-Fx enumerates under device class
  `USBDevice`, so Device Manager files it under *Universal Serial Bus devices*.
- **`grep` block-buffers when not writing to a terminal.** Piping a long-running
  script through it makes a live run look like it produced nothing.

- **A failure path that closes the port without stopping the axis.** Both entry
  points had this. The CLI stopped the positioner only on `KeyboardInterrupt`; the
  service's scan worker caught `Exception`, logged it, and returned. Neither is
  reachable in normal use, which is why it survived hardware bring-up — but an
  exception raised inside `seek()`'s wait loop (a dropped poll, a rejected command,
  a VNA read that dies on the next line) arrives with the tower **still turning**,
  and `finally: pos.close()` then throws away the only means of stopping it. With a
  cable routed through the tower that is the wind-up hazard, reached by a code path
  rather than by a mistake at the panel. Found by `rigcheck.py`, not by the rig.
  Fixed in both; `drop_sweep` and `service_stops` hold them to it.

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
- **2026-08-20** — Speed is left alone by default (`--speed` to override).
  Reason: the real mnemonic is `SPEED <percent>`, not `SP <preset>`; the old
  default would have pushed a preset index into a percentage field.
- **2026-08-20** — `sweep_time_s()` is optional and fails fast. Reason: the
  A2202-Fx (26.3.1) does not implement `SENS:SWE:TIME?` — it answers -110
  "Command header error" and then never replies, so the unguarded call blocked
  for the full 120 s instrument timeout. It only ever fed a progress estimate,
  so it now uses a 3 s timeout and returns None.
- **2026-08-20** — Positioner writes drain their reply, commands retry once after
  a resync, and position replies are validated against the full
  `<number> DEGREES` format. Reason: the chassis answers *every* command, and the
  FTDI link corrupts bytes at a measurable rate. Outgoing corruption produces
  `ERROR 1` (loud); incoming corruption splits a reply so the fragment still
  parses as a plausible angle (silent, and therefore worse). Retries are safe
  because every write is an idempotent absolute instruction.
- **2026-08-20** — Dashboard mirrors the Phaser architecture: WebSocket service
  with a JSON `{cmd, id}` protocol, a transport facade on the frontend, and a
  simulated backend preserving the payload contract. Reason: it is a proven
  shape in this codebase, and the sim makes the UI developable off-site.
- **2026-08-20** — The scan worker owns the backend and STOP sets a flag it
  polls, rather than a second thread writing a stop command. Reason: the
  instruments are single-threaded and stateful; a competing write would land in
  the middle of another thread's request/response exchange. Serial access stays
  single-threaded and stop still responds in ~100 ms via `seek(should_abort=)`.
- **2026-08-20** — Setup scripts detect and instruct; they never download or
  install. Reason: driver installs need admin rights on an ADI-managed machine
  and should be a deliberate act, not a side effect of running setup.

# Plan

✅ **Phase 1 — service, simulator, transport.** `chamber_service.py` plus the Vite
frontend, verified against both backends. Setup scripts (`setup.ps1`, `setup.sh`)
make the environment reproducible. See `README.md` to run either.

**Later:**
- Phase 2 — polish the hardware path in the UI: richer status, jog affordances,
  surfacing link-retry warnings to the operator rather than only stderr.
- Phase 3 — run browser: list/load stored runs, overlay cuts, export.
- Teach the simulator to replay a recorded run (`thru_run/pattern.csv`) instead
  of synthesizing, so the UI can be exercised against real measured data.
- Sweep averaging; wire `ACC?`/acceleration into the config.
- First real cut on a reference antenna once one is mounted.

# Status

Acquisition chain verified end to end on hardware; dashboard phase 1 done. `main`
is pushed to `livethisdream/usafa-chamber` and the tree is clean. See the archive
for the bring-up history.

**Rig identity** — VNA `CMT, A2202-Fx, 26018474, 26.3.1/1`; chassis
`EMCenter 4.6.0`; slot 1A `EMControl 7006-001, 2.10.3` on COM16 at 115200 8N1.
All three devices are currently **unplugged** (`Present=False`); S2VNA is still
running and still holds 5025.

**Reference figure worth keeping:** the thru-line run (`thru_run/`, 72 × 101,
2–3 GHz) is flat to **0.019 dB peak-to-peak across the full rotation**
(σ 0.0029 dB, mean −0.88 dB). That is the noise floor of the whole chain — above
roughly 0.03 dB of angular structure, a real measurement is showing signal.

**Working state.** `pattern_measure.py` is the standalone acquisition path;
`chamber_service.py` + `frontend/` is the dashboard, runnable with no hardware via
`--sim`. Both backends verified, including STOP preempting a live scan.
`HardwareBackend` verified read-only against the rig.

**The drivers are now under test.** `rigcheck.py` runs 17 scenarios against
`mock_instruments.py`, which impersonates pyvisa so `Vna` and `Positioner` execute
unmodified — the `--sim` backend never touched them, so until now every fix from
bring-up was resting on a single manual verification. Each fault is one the rig
actually produced: the split position reply, `ERROR 1` on a valid command, the
unsupported `SENS:SWE:TIME?`. The FTDI corruption can now be reproduced on demand
with no cable involved, which is the closest thing to a handle on that open
concern. One scenario, `split_unguarded`, deletes the guard and asserts the run
breaks — otherwise the guarded case would pass even with the guard gone.

**Open concerns:**
- *FTDI link corruption.* One 72-point run logged 3 `ERROR 1` retries and 2 split
  position reads (~7% of exchanges); a later rotation test logged none. Handled in
  software; the physical cause — cable, hub, or RF pickup — is unaddressed.
- *Parallel implementation on the remote.* Branch
  `claude/vna-antenna-controller-vaa34u` (7 commits) is a different build of this
  same project, written against mock instruments. Unrelated histories, so no clean
  merge; its `project/USAFA-chamber_PROJECT.md` differs from this file only by
  case, which collides on Windows. Its `acquisition/` package is more modular than
  `main` and may hold structure worth lifting.
- *Setup scripts are untested on a fresh machine* — the environment half only ever
  exercised the reuse path, and the hardware half has never seen a healthy
  connected rig or a genuine problem-code-28 device.
- *VNA left on 2–3 GHz / 101 pts / S21*, not its original 100 kHz–22 GHz S11 sweep.

# ToDo

- [ ] Fix the stale line in **Special Instructions** claiming the EMCenter
      mnemonics are unverified — they were probed directly against the card and
      are recorded in `PositionerCmds`. (Section is read-only to `/bye`.)
- [ ] Decide how to reconcile `claude/vna-antenna-controller-vaa34u` with `main`
      — unrelated histories, overlapping scope, one file differing only in case.
- [ ] Chase the FTDI link corruption physically — different cable, no hub, check
      routing relative to the VNA and chamber feed.
- [ ] Exercise `setup.ps1` on a fresh machine and against a connected rig; the
      problem-code-28 branch has never run against a real failed install.
- [ ] Phase 2: hardware-path polish in the UI; surface link-retry warnings to the
      operator instead of only stderr.
- [ ] Phase 3: run browser — list/load stored runs, overlay cuts, export.
- [ ] Teach the simulator to replay `thru_run/pattern.csv` for real data shapes.
- [ ] Confirm continuous / non-continuous mode on the front panel before any run
      with a cable routed through the tower.
- [ ] Switch Device Emulation back off — unused, and an uncharacterized variable.
- [ ] Restore the VNA to its original sweep setup, or set it up fresh next visit.
- [ ] First real cut on a reference antenna; add sweep averaging.
