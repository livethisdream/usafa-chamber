---
name: "#usafa-chamber"
dateCreated: 2026-08-20
dateModified: 2026-08-20
---

Archive for `usafa-chamber_PROJECT.md`. Superseded decisions, closed work, and
session history live here so the hot note stays a picture of the present.

# Session Log

## 2026-08-20 — bring-up, verification, dashboard phase 1

Project created from scratch. `pattern_measure.py` arrived as an unreviewed
script; by end of session the full chain was verified against real hardware, a
reference measurement was banked, and a browser dashboard existed.

**Twelve bugs fixed**, in three groups:

*Found by code review, before any hardware contact (5):* motion-start race in
`seek()`; `angles()` overshooting the stop angle on a non-divisible step;
duplicate measurement at 0°/360°; `zip()` silently truncating rows on a
point-count mismatch; VNA errors checked only after `configure()`.

*Only reachable with hardware attached (5):* serial framing never applied in
`Positioner.__init__` (so pyvisa's 9600 8N1 default was in use and every query
timed out); card in slot 1 not slot 5; `CP?` replying `'90.0 DEGREES'` with
units where `float()` was called directly; speed mnemonic `SPEED <percent>` not
`SP <preset>`; writes not draining their reply, desynchronizing later queries.

*Found by measurement (2):* `SENS:SWE:TIME?` unsupported on A2202-Fx firmware
26.3.1, blocking for the full 120 s instrument timeout; the split-reply position
bug — `'255.0 DEGREES'` arriving as `'2'` + `'55.0 DEGREES'`, recording 2.0° and
29.0° for cuts commanded to 255° and 295°.

The two that mattered most both failed *silently* and produced measurements that
looked entirely plausible: the motion-start race and the split reply.

**Verification milestones:** EMCenter USB driver installed (cleared problem code
28, produced COM16); S2VNA socket server enabled on 5025; every SCPI mnemonic
probed directly against both instruments; motion confirmed visually
(90°→150°→90° at 40% speed, ~6.7 deg/s); 72-point thru-line reference run;
dashboard phase 1 built and verified against sim and hardware backends.

**Setup scripts** (`setup.ps1`, `setup.sh`) added at end of session. Writing them
surfaced a bug of the same family as the measurement ones: the first version
matched Windows ghost device nodes and confidently prescribed a driver reinstall
for a chassis that was merely unplugged.

# Rotated Decisions

- **2026-08-20** — Positioner writes drain their reply and queries raise on an
  `ERROR n` response. *Superseded by the retry-after-resync decision in the hot
  note, which subsumes this rationale.* Kept as a stub because the drain itself
  is still what makes the retry possible.
- **2026-08-20** — Repo hosted under OneDrive at `USAFA/usafa-chamber` (sibling
  to `USAFA/ece444`), mounted into cdocker, remote `livethisdream/usafa-chamber`.
  *Closed setup decision; the layout is now recorded in `README.md` and the vault
  stub.*

# Closed ToDo

- Install the ETS-Lindgren EMCenter USB/VCP driver — done, cleared problem code 28, COM16.
- Point `PositionerConfig.resource` at the real port — `ASRL16::INSTR`.
- Cross-check `PositionerCmds` mnemonics — probed directly against the card.
- Resolve the VISA backend — `pyvisa-py` via the `sim` extra.
- Enable the S2VNA socket server — listening on 5025.
- Verify the VNA SCPI path — `configure`/`frequencies`/`measure` all exercised.
- Validate `seek()` against real mechanics — visually confirmed twice.
- First full collection run — 72-point thru reference in `thru_run/`.
- Dashboard phase 1 — service, sim backend, transport, frontend.
- Verify `HardwareBackend` against the real rig — read-only check passed.

# Ruled Out

- **`SK` rejected as a bad mnemonic.** A mid-scan `SK 5.0` returned `ERROR 1`,
  which read as a wrong command. It is not: `SK` answers `OK` on success and the
  same command succeeds on demand and in a traced replay of the identical
  sequence. The `ERROR 1` was link corruption, not rejection.
- **Device Emulation as a stand-in for simulation mode.** Switching it on changes
  nothing about the command set (identity, `CP?`, `*OPC?`, `SPEED?`, `ERR?` all
  byte-identical) and does not inhibit the motor. It is legacy-protocol
  compatibility, not motion suppression.
- **`np.fromstring(sep=",")` as a deprecation risk.** Tested with
  `DeprecationWarning` promoted to error on numpy 2.5.2; only the binary form
  (`sep=''`) is deprecated. The VNA parse path is safe as written.
