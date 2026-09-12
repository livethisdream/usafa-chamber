<#
.SYNOPSIS
    First-time setup and readiness check for the USAFA chamber rig.

.DESCRIPTION
    Two halves, either of which can run alone:

      Environment  - out-of-tree venv + junction, Python deps, frontend deps.
      Readiness    - checks the two pieces of instrument software that do not
                     work out of the box, and reports exactly what is wrong.

    Nothing is downloaded or installed on your behalf. Where a driver or an
    application is missing the script says so and prints where to get it; the
    install itself is yours to run, deliberately, with whatever rights it needs.

    The readiness checks encode failure modes that are actively misleading:
    an EMCenter that enumerates and looks healthy while having no COM port at
    all, a VNA that hides under a device class nobody thinks to look in, and a
    socket server that is off by default so nothing listens on 5025.

.PARAMETER CheckOnly
    Run the readiness checks and change nothing. Safe on the acquisition PC
    mid-session.

.PARAMETER SkipFrontend
    Skip npm install. Useful if you only need the Python side.

.EXAMPLE
    .\setup.ps1
    Full first-time setup, then report readiness.

.EXAMPLE
    .\setup.ps1 -CheckOnly
    Diagnose the rig without touching anything.
#>

[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$SkipFrontend
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$VenvName    = 'usafa-chamber'
$LinkName    = '.venv-win'
$RealVenv    = Join-Path $env:LOCALAPPDATA "uv-venvs\$VenvName"

$script:Problems = @()
$script:Actions  = @()

# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

function Write-Head($text) {
    Write-Host ''
    Write-Host "== $text " -ForegroundColor Cyan -NoNewline
    Write-Host ('=' * [Math]::Max(0, 68 - $text.Length)) -ForegroundColor DarkCyan
}
function Write-Ok($text)   { Write-Host '  [ OK ] ' -ForegroundColor Green      -NoNewline; Write-Host $text }
function Write-Warn($text) { Write-Host '  [ !! ] ' -ForegroundColor Yellow     -NoNewline; Write-Host $text }
function Write-Bad($text)  { Write-Host '  [FAIL] ' -ForegroundColor Red        -NoNewline; Write-Host $text }
function Write-Info($text) { Write-Host '         ' -NoNewline; Write-Host $text -ForegroundColor DarkGray }

function Add-Problem($summary, $action) {
    $script:Problems += $summary
    if ($action) { $script:Actions += $action }
}

# --------------------------------------------------------------------------
# Prerequisites
# --------------------------------------------------------------------------

function Test-Prerequisites {
    Write-Head 'Prerequisites'
    $ok = $true

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        $v = (& uv --version)
        Write-Ok "uv        $v"
    } else {
        Write-Bad 'uv not found on PATH'
        Write-Info 'Install: https://docs.astral.sh/uv/getting-started/installation/'
        Add-Problem 'uv missing' 'Install uv, then re-run this script.'
        $ok = $false
    }

    $node = Get-Command node -ErrorAction SilentlyContinue
    if ($node) {
        Write-Ok "node      $(& node --version)"
    } else {
        Write-Warn 'node not found on PATH (frontend cannot be built)'
        Write-Info 'Install: https://nodejs.org/'
        Add-Problem 'node missing' 'Install Node.js if you need the dashboard frontend.'
    }

    return $ok
}

# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

function Initialize-Venv {
    Write-Head 'Python environment'
    $linkPath = Join-Path $ProjectRoot $LinkName

    # A real directory here means someone made an in-tree venv. That is the
    # thing this project deliberately avoids: OneDrive syncs venv contents,
    # which is slow and can corrupt the venv outright.
    if (Test-Path $linkPath) {
        $item = Get-Item $linkPath -Force
        $isLink = ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
        if (-not $isLink) {
            Write-Bad "$LinkName exists as a real directory, not a junction"
            Write-Info 'Venvs must stay out of the OneDrive tree. Remove it and re-run:'
            Write-Info "  Remove-Item -Recurse -Force `"$linkPath`""
            Add-Problem "$LinkName is a real directory" "Remove $LinkName and re-run setup."
            return $false
        }
        Write-Ok "$LinkName junction present"
    }

    if (Test-Path $RealVenv) {
        $originFile = Join-Path $RealVenv '.origin'
        if (Test-Path $originFile) {
            $origin = (Get-Content $originFile -Raw).Trim()
            if ($origin -ne $ProjectRoot) {
                Write-Bad "venv at $RealVenv belongs to another project"
                Write-Info "  its .origin: $origin"
                Write-Info "  this project: $ProjectRoot"
                Add-Problem 'venv name collision' "Remove $RealVenv or rename this project."
                return $false
            }
            Write-Ok "venv reused  $RealVenv"
        } else {
            Write-Bad "venv at $RealVenv has no .origin marker"
            Add-Problem 'unmarked venv' "Remove $RealVenv and re-run setup."
            return $false
        }
    } else {
        Write-Info "creating venv at $RealVenv"
        & uv venv $RealVenv
        if (-not $?) { Write-Bad 'uv venv failed'; Add-Problem 'uv venv failed' $null; return $false }
        Set-Content -Path (Join-Path $RealVenv '.origin') -Value $ProjectRoot -Encoding utf8 -NoNewline
        Write-Ok "venv created $RealVenv"
    }

    if (-not (Test-Path $linkPath)) {
        & cmd /c mklink /J "$linkPath" "$RealVenv" | Out-Null
        if (-not (Test-Path $linkPath)) {
            Write-Bad 'junction creation failed'
            Add-Problem 'junction failed' $null
            return $false
        }
        Write-Ok "$LinkName -> $RealVenv"
    }

    Write-Info 'syncing Python dependencies (with the sim extra)'
    $env:UV_PROJECT_ENVIRONMENT = $LinkName
    Push-Location $ProjectRoot
    try {
        & uv sync --extra sim
        if (-not $?) { Write-Bad 'uv sync failed'; Add-Problem 'uv sync failed' $null; return $false }
    } finally {
        Pop-Location
    }
    Write-Ok 'Python dependencies installed'
    return $true
}

function Initialize-Frontend {
    if ($SkipFrontend) { return $true }
    Write-Head 'Frontend'
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Warn 'npm unavailable, skipping'
        return $true
    }
    $frontend = Join-Path $ProjectRoot 'frontend'
    if (-not (Test-Path (Join-Path $frontend 'package.json'))) {
        Write-Warn 'frontend/package.json not found, skipping'
        return $true
    }
    Push-Location $frontend
    try {
        & npm install --no-fund --no-audit
        if (-not $?) { Write-Bad 'npm install failed'; Add-Problem 'npm install failed' $null; return $false }
    } finally {
        Pop-Location
    }
    Write-Ok 'frontend dependencies installed'
    return $true
}

function Test-Tooling {
    Write-Head 'Verification tooling'
    $py = Join-Path $ProjectRoot "$LinkName\Scripts\python.exe"
    if (-not (Test-Path $py)) {
        Write-Warn 'venv not built yet; run without -CheckOnly first'
        return
    }

    # rigcheck and bringup run on the base dependencies, so if the venv built at
    # all they are usable. Say so explicitly - they are the two things worth
    # running before trusting a new machine with the rig.
    Write-Ok 'rigcheck.py / bringup.py ready (no extra dependencies)'
    Write-Info 'rigcheck.py          - drivers vs fake instruments, no hardware'
    Write-Info 'bringup.py           - staged bring-up; stages 5-6 need --allow-motion'

    # uicheck needs playwright plus a downloaded browser, neither of which is in
    # the default install. Report rather than install: the browser download is
    # large and unexpected as a side effect of running setup.
    #
    # The probe prints a token and never writes to stderr. Redirecting a native
    # executable's stderr in Windows PowerShell wraps each line in a
    # NativeCommandError and trips $ErrorActionPreference='Stop', so a missing
    # optional package would abort the whole script instead of being reported.
    # Single-quoted inside Python on purpose: PowerShell strips double quotes
    # when handing an argument to a native executable, so "playwright" would
    # reach Python as a bare name and raise NameError.
    $probe = @'
import importlib.util as u, sys
if u.find_spec('playwright') is None:
    sys.stdout.write('nomodule'); raise SystemExit
try:
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    try:
        p.chromium.executable_path
        sys.stdout.write('ready')
    finally:
        p.stop()
except Exception:
    sys.stdout.write('nobrowser')
'@
    $result = (& $py -c $probe | Out-String).Trim()

    switch ($result) {
        'ready' { Write-Ok 'uicheck.py ready (playwright + chromium present)' }
        'nobrowser' {
            Write-Warn 'playwright installed but its browser is missing'
            Write-Info "  $LinkName\Scripts\playwright.exe install chromium"
            Add-Problem 'playwright browser missing' 'Run: .venv-win\Scripts\playwright.exe install chromium'
        }
        default {
            Write-Warn 'uicheck.py unavailable - playwright not installed (optional)'
            Write-Info '  uv sync --extra ui   then   .venv-win\Scripts\playwright.exe install chromium'
        }
    }
}

# --------------------------------------------------------------------------
# Hardware readiness
# --------------------------------------------------------------------------

$DriverUrl = 'https://support.ets-lindgren.com/public/other/downloads/get-download?software=other&filename=EMCenter_USB_Drivers_2.12.36.4_Signed.zip&securetype=public&folder=other'
$S2vnaUrl  = 'https://coppermountaintech.com/demo-the-software/'

function Test-Positioner {
    Write-Head 'Positioner (ETS-Lindgren EMCenter)'

    # Windows keeps a registry node for every device it has ever seen, so a
    # filter that ignores Present matches hardware that was unplugged months
    # ago. Those ghosts report Status=Unknown and an empty problem code, which
    # looks exactly like a failed driver install - and telling someone to
    # reinstall drivers when the real fault is an unplugged cable is worse than
    # saying nothing. Presence is checked first, every time.
    $usbAll     = @(Get-PnpDevice -ErrorAction SilentlyContinue |
                    Where-Object { $_.InstanceId -like '*VID_0403&PID_8570*' })
    $usbPresent = @($usbAll | Where-Object { $_.Present })
    $childAll     = @(Get-PnpDevice -ErrorAction SilentlyContinue |
                      Where-Object { $_.InstanceId -like 'FTDIBUS*PID_8570*' })
    $childPresent = @($childAll | Where-Object { $_.Present })

    if ($usbAll.Count -eq 0) {
        Write-Warn 'EMCenter has never been seen on this machine'
        Write-Info 'Connect and power the chassis, then re-run.'
        Write-Info "Drivers, if needed: $DriverUrl"
        Add-Problem 'EMCenter never connected' 'Connect and power the EMCenter chassis.'
        return
    }

    if ($usbPresent.Count -eq 0) {
        Write-Warn 'EMCenter is not connected right now'
        # A leftover Ports-class child means the driver package did install at
        # some point, which is worth saying: it turns "reinstall everything"
        # into "plug the cable back in".
        $ghostPort = @($childAll | Where-Object { $_.Class -eq 'Ports' })
        if ($ghostPort.Count -gt 0) {
            Write-Info "Drivers are installed - it previously enumerated as: $($ghostPort[0].FriendlyName)"
            Write-Info 'Reconnect and power the chassis; no reinstall needed.'
            Add-Problem 'EMCenter unplugged' 'Reconnect and power the EMCenter chassis.'
        } else {
            Write-Info 'No virtual COM port was ever created for it.'
            Write-Info "Connect it, then install: $DriverUrl"
            Add-Problem 'EMCenter unplugged, drivers unconfirmed' 'Connect the chassis, then check drivers.'
        }
        return
    }

    Write-Ok "chassis connected: $($usbPresent[0].FriendlyName)"

    # The real trap, only meaningful once the device is actually present: the
    # parent enumerates and reports healthy while the virtual COM port child
    # fails to install, so Device Manager looks fine and nothing can open it.
    # Stock FTDI drivers do not claim ETS-Lindgren's PID 8570.
    if ($childPresent.Count -eq 0) {
        Write-Bad 'connected, but it has no working virtual COM port'
        Write-Info 'The chassis looks healthy in Device Manager yet cannot be opened.'
        Write-Info 'Stock FTDI drivers do not claim ETS-Lindgren PID 8570.'
        Write-Info "Install: $DriverUrl"
        Add-Problem 'EMCenter has no COM port' 'Install the ETS-Lindgren EMCenter USB drivers.'
        return
    }

    $child = $childPresent[0]
    $problem = $null
    try {
        $prop = Get-PnpDeviceProperty -InstanceId $child.InstanceId `
                    -KeyName 'DEVPKEY_Device_ProblemCode' -ErrorAction Stop
        if ($null -ne $prop -and $null -ne $prop.Data) { $problem = [int]$prop.Data }
    } catch { }

    if ($child.Status -ne 'OK' -or $problem -eq 28) {
        $shown = '(none reported)'
        if ($null -ne $problem) { $shown = $problem }
        Write-Bad "virtual COM port failed to install (problem code $shown)"
        Write-Info "Install: $DriverUrl"
        Add-Problem "EMCenter COM port problem code $shown" 'Install the ETS-Lindgren EMCenter USB drivers.'
        return
    }

    $port = $null
    if ($child.FriendlyName -match '\((COM(\d+))\)') { $port = $Matches[2] }
    if ($port) {
        Write-Ok "virtual COM port ready: COM$port"
        Write-Info "Set PositionerConfig.resource to 'ASRL$port::INSTR'"
        Write-Info 'Framing is 115200 8N1 (the documented 9600,7,Odd,1 is the legacy rear port).'
    } else {
        Write-Ok 'virtual COM port present'
        Write-Info "Device: $($child.FriendlyName)"
    }
}

function Test-Vna {
    Write-Head 'VNA (Copper Mountain A2202-Fx)'

    # Device class is USBDevice, so this appears under "Universal Serial Bus
    # devices" in Device Manager - not Ports, and nothing VNA-named. Presence
    # is checked separately from existence for the same ghost-node reason as
    # the positioner.
    $vnaAll     = @(Get-PnpDevice -ErrorAction SilentlyContinue |
                    Where-Object { $_.InstanceId -like '*VID_36BF*' -or $_.FriendlyName -like '*A2202*' })
    $vnaPresent = @($vnaAll | Where-Object { $_.Present })
    $vnaConnected = $vnaPresent.Count -gt 0

    if ($vnaConnected) {
        Write-Ok "connected: $($vnaPresent[0].FriendlyName.Trim())"
        Write-Info 'Listed under "Universal Serial Bus devices" in Device Manager.'
    } elseif ($vnaAll.Count -gt 0) {
        Write-Warn 'A2202-Fx is not connected right now'
        Write-Info "Known to this machine as: $($vnaAll[0].FriendlyName.Trim())"
        Write-Info 'Reconnect it to measure; not needed for simulator work.'
        Add-Problem 'VNA unplugged' 'Reconnect the A2202-Fx if you intend to measure.'
    } else {
        Write-Warn 'A2202-Fx has never been seen on this machine'
        Write-Info 'Check the cable; not needed for simulator work.'
        Add-Problem 'VNA never connected' 'Connect the A2202-Fx if you intend to measure.'
    }

    $exe = 'C:\VNA\S2VNA\S2VNA.exe'
    if (Test-Path $exe) {
        Write-Ok "S2VNA installed: $exe"
    } else {
        Write-Bad 'S2VNA not found at C:\VNA\S2VNA\S2VNA.exe'
        Write-Info "It installs outside Program Files. Download: $S2vnaUrl"
        Add-Problem 'S2VNA not installed' 'Install S2VNA from Copper Mountain.'
        return
    }

    $running = Get-Process -Name 'S2VNA' -ErrorAction SilentlyContinue
    if (-not $running) {
        Write-Warn 'S2VNA is not running'
        Write-Info 'Nothing serves SCPI on 5025 unless S2VNA is open.'
        Add-Problem 'S2VNA not running' 'Launch S2VNA.'
        return
    }
    Write-Ok "S2VNA running (pid $($running[0].Id))"

    # The socket server is off by default; this is the check that saves an hour.
    $listening = @(Get-NetTCPConnection -State Listen -LocalPort 5025 -ErrorAction SilentlyContinue)
    if ($listening.Count -gt 0) {
        Write-Ok 'socket server listening on 5025'
        # S2VNA serves the socket whether or not an instrument is attached, so
        # a bare "listening" tick would imply more readiness than there is.
        if (-not $vnaConnected) {
            Write-Warn 'but no VNA is attached - the socket will answer with no instrument behind it'
            Add-Problem 'socket up without a VNA' 'Reconnect the A2202-Fx if you intend to measure.'
        }
    } else {
        Write-Bad 'nothing listening on TCP 5025'
        Write-Info 'The socket server is OFF by default and is a GUI toggle:'
        Write-Info '  S2VNA -> System -> Misc Setup -> Network Setup -> Socket Server'
        Add-Problem 'socket server disabled' 'Enable the socket server in S2VNA (System > Misc Setup > Network Setup).'
    }
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

Write-Host ''
Write-Host '  USAFA Chamber - setup and readiness' -ForegroundColor White
Write-Host "  $ProjectRoot" -ForegroundColor DarkGray

$envOk = $true
if ($CheckOnly) {
    Write-Head 'Environment'
    Write-Info 'skipped (-CheckOnly)'
} else {
    if (-not (Test-Prerequisites)) {
        Write-Host ''
        Write-Bad 'Prerequisites missing; stopping before touching the environment.'
        exit 1
    }
    $envOk = (Initialize-Venv) -and (Initialize-Frontend)
}

Test-Tooling
Test-Positioner
Test-Vna

Write-Head 'Summary'
if ($script:Problems.Count -eq 0) {
    Write-Ok 'Everything checked out. The rig is ready.'
} else {
    Write-Host "  $($script:Problems.Count) item(s) need attention:" -ForegroundColor Yellow
    foreach ($p in $script:Problems) { Write-Host "    - $p" -ForegroundColor Yellow }
    if ($script:Actions.Count -gt 0) {
        Write-Host ''
        Write-Host '  What to do:' -ForegroundColor White
        $i = 1
        foreach ($a in ($script:Actions | Select-Object -Unique)) {
            Write-Host "    $i. $a"
            $i++
        }
    }
}

Write-Host ''
Write-Host '  Run the dashboard:' -ForegroundColor White
Write-Host '    python chamber_service.py --sim        # no hardware needed'
Write-Host '    npm run dev --prefix frontend          # http://localhost:5173'
Write-Host ''

# Environment failures are actionable and worth a non-zero exit for CI or a
# wrapper script. Hardware findings are informational: a machine with no rig
# attached is a perfectly valid simulator development box.
if (-not $envOk) { exit 1 }
exit 0
