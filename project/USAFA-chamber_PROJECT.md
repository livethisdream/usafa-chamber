# USAFA Chamber — Custom Antenna Pattern Controller

Replacing EMQuest with a purpose-built step-and-measure controller for the
chamber, driving the VNA and turntable directly.

## Hardware

| Role | Device | Interface |
| --- | --- | --- |
| VNA | Copper Mountain A2202-Fx | SCPI over TCP (S2VNA socket server, default port 5025) |
| Positioner | ETS-Lindgren EMControl 7006-001 card in an EMCenter chassis | Ethernet (VISA/socket) or serial |

Prior workflow: EMQuest with older equipment. The goal here is a scriptable
acquisition path we control end to end — sweep config, motion, data layout,
and plotting.

## TODO

- [ ] **Provide the phase GUI repo** — the UI language for this project should
      follow the phase GUI already in progress, rather than inventing a new
      one. That repo is not yet attached to this session, so the front end is
      blocked on it. Needed: the repo (owner/name) so it can be added as a
      source, and a pointer to whichever parts carry the design language worth
      reusing — layout and navigation structure, component set, color and
      typography tokens, and the conventions for live instrument state
      (connection status, run progress, abort/stop affordances).

      Access: candidate URL is https://github.com/livethisdream/phaser, but
      attaching it fails with "you don't have access", and it does not appear
      in a repo listing. Other private repos under `livethisdream` *are*
      visible to the session, so this is not a general private-repo problem —
      it is specific to `phaser`. Likely causes, in order:

      1. The Claude GitHub App is installed with "only select repositories"
         and `phaser` is not among them. Fix in GitHub → Settings →
         Applications → Claude → Configure.
      2. The repo actually lives under a work organization rather than the
         personal account, in which case an org admin grants access at
         https://claude.ai/admin-settings/claude-in-slack.
      3. The owner or name is off, or the repo was renamed.

      Fallback if access can't be granted: lift the design language by hand —
      export the token/theme files and a few representative screens.

## Acquisition script status

`acquisition/pattern_measure.py` — reviewed, fixed, and covered by a scenario
suite (`acquisition/dryrun.py`, 9/9 passing against mock instruments). All six
review findings are closed:

- [x] Angle grid overshot the requested stop when the span wasn't an integer
      multiple of the step (`--step 30` measured 360°). Now floors, drops a
      duplicate full-circle endpoint, and reports the shortfall.
- [x] A mid-scan exception left the turntable moving — only `KeyboardInterrupt`
      issued a stop. `STOP` now runs on every exit path, before the socket
      closes.
- [x] A position mismatch only warned, then recorded the sweep at the commanded
      angle. Now retries, then fails loudly; the plot uses readback angles.
- [x] A truncated VNA response crashed on array assignment. Now length-checked
      and re-read once.
- [x] The polar plot closed the trace unconditionally, fabricating data across
      the unmeasured span of a partial cut. Now closes only on a full
      revolution, and the radial axis fits the data instead of clipping at
      −40 dB.
- [x] `seek()` could return before motion started. Completion now requires
      motion-complete *and* an in-tolerance readback on consecutive polls.

Also addressed: serial line parameters are set for ASRL resources; sweep type,
averaging, and smoothing are written explicitly rather than inherited (without
`SYST:PRES`, which would clear the calibration); correction state is queried and
reported; a warm-up sweep runs before the frequency vector is trusted; transient
VISA failures are retried at the query layer; run metadata is written to
`run_meta.json`.

Added on top of the fixes: end-of-run closure check (re-measures the start
angle to quantify drift, on by default), optional backlash takeup, and optional
aux-parameter capture (`--aux-param S11`).

Still unverified against hardware — the EMCenter mnemonics in `PositionerCmds`,
per ETS-Lindgren manual 399342.

## Open questions

- Cable wrap: is there a rotary joint, or does the scan range need to stay
  within ±180°?
- Which S-parameters to capture per angle — S21 only, or S21 + S11 to catch a
  connection change mid-scan?
