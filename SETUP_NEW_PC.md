# Setting up the chamber on a new computer

Windows. About 30 minutes, mostly downloads. Everything below was learned on the
first PC; the traps are listed where you'll hit them.

## 1. Install software

Driver installs need admin rights. Open a **new** terminal afterwards so PATH updates.

| What | Where | Why |
|---|---|---|
| Git for Windows | https://git-scm.com/download/win | get the code |
| uv | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` | Python and its packages |
| Node.js (LTS) | https://nodejs.org/ | the dashboard frontend |
| S2VNA (Copper Mountain) | https://coppermountaintech.com/demo-the-software/ | the only way to talk to the VNA; installs to `C:\VNA\S2VNA`, not Program Files |
| EMCenter USB drivers (ETS-Lindgren) | [EMCenter_USB_Drivers_2.12.36.4_Signed.zip](https://support.ets-lindgren.com/public/other/downloads/get-download?software=other&filename=EMCenter_USB_Drivers_2.12.36.4_Signed.zip&securetype=public&folder=other) | without it the positioner shows in Device Manager but has **no COM port** |

## 2. Get the code

```powershell
cd $HOME
git clone https://github.com/livethisdream/usafa-chamber.git
cd usafa-chamber
```

Sign in to GitHub if prompted. Any folder works, OneDrive included: the Python
environment is kept outside the project folder.

## 3. Run setup

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

Creates the Python environment, installs everything, then checks the rig. Safe to
re-run. If Node wasn't installed yet, install it and run this again.

## 4. Connect the hardware

1. Plug in USB: **VNA** (A2202-Fx), **EMCenter** chassis (power it on), **ACM2202**.
2. Launch **S2VNA**.
3. Turn on its socket server: **System → Misc Setup → Network Setup → Socket Server → On**.
   It is off by default, and nothing works until it's on.
4. Re-check:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup.ps1 -CheckOnly
   ```

Write down two values from that output — you'll use them below:

- **Positioner port**: `virtual COM port ready: COMn` → use `ASRLn::INSTR`.
  It was COM16 on the old PC; it will probably be different here.
- **VNA port**: normally **5025**. Run only **one** copy of S2VNA — a second copy
  takes 5026, and the old PC ended up talking to the wrong one.

## 5. Verify — nothing moves

```powershell
.\.venv-win\Scripts\python.exe rigcheck.py
.\.venv-win\Scripts\python.exe bringup.py --pos ASRLn::INSTR --vna TCPIP0::127.0.0.1::5025::SOCKET
```

`rigcheck.py` needs no hardware; expect `28/28 scenarios passed`. `bringup.py`
stages 0–4 and 7 only read from the instruments; stages 5–6 (motion) are skipped
unless you pass `--allow-motion`.

## 6. Start the dashboard

```powershell
.\start.ps1
```

Brings up S2VNA, the service and the dashboard together, finding the positioner's
COM port itself. Make a desktop shortcut to it and the whole rig starts from one
icon. Ctrl+C stops the service and frontend; S2VNA stays open.

To run the two halves by hand instead — two terminals, both in the project folder
(replace `n` and the VNA port as noted):

```powershell
.\.venv-win\Scripts\python.exe chamber_service.py --no-fallback --vna TCPIP0::127.0.0.1::5025::SOCKET --pos ASRLn::INSTR
```

```powershell
npm run dev --prefix frontend
```

Open **http://localhost:5173**. The badge should read **HARDWARE**. Stop with
Ctrl+C in both terminals.

No instruments attached? `.\.venv-win\Scripts\python.exe chamber_service.py --sim`

## 7. Taking a measurement

- **Calibrate at the exact sweep you'll measure** — start, stop, points, IF BW,
  power. Changing any of them invalidates the cal. The cal is at the cable ends,
  so swapping or rotating the antenna doesn't need a new one.
- **Take the ACM out of the RF path before scanning.** If every angle reads about
  −22 dB and flat, the ACM is still connected.
- **Set zero at boresight**: physically align the tower, then define zero in the
  Turntable section. Negative angles are CCW looking down on the tower.
- **Confirm continuous / non-continuous mode on the EMControl front panel** before
  the first run — the cable goes through the tower and can wind up.
- **Jog back to 0° between scans.** Going +90° → −90° directly is exactly 180°,
  and the controller may take the long way round.
- **Set Cut freq** (e.g. 915 MHz) after the scan starts — it defaults to mid-sweep.
- A scan can stop on a rejected move (serial link glitch). Points already taken
  are saved under `runs/`. Jog to 0° and rerun.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "running scripts is disabled on this system" | Use `powershell -ExecutionPolicy Bypass -File ...` as above |
| "cannot reach the VNA … Nothing is listening" | S2VNA isn't running, or its socket server is off |
| EMCenter in Device Manager, no COM port | Install the ETS-Lindgren driver (step 1) |
| Positioner never answers | Wrong COM number in `--pos` |
| Badge says SIMULATED | The service fell back; restart it with `--no-fallback` to see why |
| "Address already in use" / port 8766 | A previous service is still running — close it first |
| Calibrate… reports no module | Check the ACM2202's USB; `setup.ps1 -CheckOnly` lists it |
