<#
.SYNOPSIS
    Bring the chamber up on Windows: S2VNA, the service, the dashboard.

.DESCRIPTION
    The Windows twin of start.sh. Checks each piece in the order it is needed
    and stops with the reason when one is missing, rather than letting the
    service fail later with a confusing error.

    Ctrl+C stops the service and the frontend together. S2VNA is left running,
    since restarting it means turning its socket server back on by hand.

    The positioner COM port is discovered from the EMCenter USB identity, so
    this keeps working when Windows renumbers the port. Override with -Pos.

    With no EMCenter attached it comes up VNA-only instead of refusing: sweeps,
    the Smith chart and a 1-port calibration need no tower. It says so loudly,
    and the dashboard disables Start scan, so one icon covers both rigs without
    a pattern run being startable against a tower that is not there.

.PARAMETER Sim
    Run the simulator. No instruments are touched and no rig checks are made.

.PARAMETER NoBrowser
    Do not open the dashboard in a browser.

.PARAMETER Vna
    VISA resource for the VNA. Default TCPIP0::127.0.0.1::5025::SOCKET.

.PARAMETER Pos
    VISA resource for the positioner, e.g. ASRL4::INSTR. Default: auto-detect.

.PARAMETER VnaOnly
    Force the VNA-only rig even when a positioner is present. Not needed just
    because the EMCenter is absent - that is detected, and the script comes up
    VNA-only on its own, saying so. This is for deliberately ignoring a tower
    that is plugged in.

.PARAMETER NoPause
    Do not wait for a keypress when something fails. For scripts; the desktop
    shortcut wants the default so the window stays up long enough to read.

.EXAMPLE
    .\start.ps1
    Real rig.

.EXAMPLE
    .\start.ps1 -Sim
    Simulator, no instruments needed.
#>

[CmdletBinding()]
param(
    [switch]$Sim,
    [switch]$NoBrowser,
    [string]$Vna = 'TCPIP0::127.0.0.1::5025::SOCKET',
    [string]$Pos,
    [switch]$NoPause,
    [switch]$VnaOnly
)

$ProjectRoot  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Py           = Join-Path $ProjectRoot '.venv-win\Scripts\python.exe'
$ServicePort  = 8766
$FrontendPort = 5173
$S2vnaExe     = 'C:\VNA\S2VNA\S2VNA.exe'

# Launched from a shortcut, PATH is whatever the shell inherited. Node installs
# machine-wide, but a session that predates the install will not have it.
$NodeDir = Join-Path $env:ProgramFiles 'nodejs'
if ((Test-Path $NodeDir) -and ($env:Path -notlike "*$NodeDir*")) {
    $env:Path = "$NodeDir;$env:Path"
}

function Write-Head($t) { Write-Host ''; Write-Host "== $t" -ForegroundColor Cyan }
function Write-Ok($t)   { Write-Host '  [ OK ] ' -ForegroundColor Green  -NoNewline; Write-Host $t }
function Write-Warn($t) { Write-Host '  [ !! ] ' -ForegroundColor Yellow -NoNewline; Write-Host $t }
function Write-Bad($t)  { Write-Host '  [FAIL] ' -ForegroundColor Red    -NoNewline; Write-Host $t }
function Write-Info($t) { Write-Host "         $t" -ForegroundColor DarkGray }

function Test-Listening($port) {
    $null -ne (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

function Wait-Port($port, $seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Listening $port) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

# npm spawns node, and stopping the launcher orphans the child that is actually
# holding the port. Kill the whole tree.
function Stop-Tree($proc) {
    if ($proc -and -not $proc.HasExited) {
        & taskkill /T /F /PID $proc.Id 2>&1 | Out-Null
    }
}

$script:Children = @()

function Stop-Children {
    if ($script:Children.Count -eq 0) { return }
    Write-Host ''
    Write-Info 'stopping service and frontend'
    foreach ($c in $script:Children) { Stop-Tree $c }
    $script:Children = @()
}

function Invoke-Fail($msg) {
    Write-Bad $msg
    Stop-Children
    if (-not $NoPause) {
        Write-Host ''
        Read-Host 'Press Enter to close'
    }
    exit 1
}

Set-Location $ProjectRoot

try {
    # ----------------------------------------------------------------------
    Write-Head 'Environment'

    if (-not (Test-Path $Py)) {
        Write-Info 'Run: powershell -ExecutionPolicy Bypass -File .\setup.ps1'
        Invoke-Fail 'Python environment missing'
    }
    Write-Ok 'Python environment'

    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Info 'Install Node.js from https://nodejs.org/ then re-run setup.ps1'
        Invoke-Fail 'npm not found'
    }
    if (-not (Test-Path (Join-Path $ProjectRoot 'frontend\node_modules\.bin\vite.cmd'))) {
        Write-Info 'Run: powershell -ExecutionPolicy Bypass -File .\setup.ps1'
        Invoke-Fail 'frontend dependencies missing'
    }
    Write-Ok "node $(& node --version)"

    foreach ($port in @($ServicePort, $FrontendPort)) {
        if (Test-Listening $port) {
            Write-Info 'Close the other copy first, or it will hold the instruments.'
            Invoke-Fail "port $port is already in use - is the chamber already running?"
        }
    }

    $serviceArgs = @('chamber_service.py', '--port', "$ServicePort")

    if ($Sim) {
        $serviceArgs += '--sim'
        Write-Warn 'simulator - no instruments will be used'
    } else {
        # ------------------------------------------------------------------
        Write-Head 'VNA (S2VNA)'

        $vnaPort = 5025
        if ($Vna -match '::(\d+)::SOCKET$') { $vnaPort = [int]$Matches[1] }

        if (-not (Test-Listening $vnaPort)) {
            if (Get-Process -Name 'S2VNA' -ErrorAction SilentlyContinue) {
                Write-Warn "S2VNA is running but nothing is listening on $vnaPort"
            } elseif (Test-Path $S2vnaExe) {
                Write-Info "launching $S2vnaExe"
                Start-Process -FilePath $S2vnaExe -WorkingDirectory (Split-Path $S2vnaExe) | Out-Null
            } else {
                Write-Info 'Install it from https://coppermountaintech.com/demo-the-software/'
                Invoke-Fail 'S2VNA is not running and was not found'
            }
            Write-Info "waiting for port $vnaPort. If S2VNA is open, turn its socket server on:"
            Write-Info '  System -> Misc Setup -> Network Setup -> Socket Server -> On'
            if (-not (Wait-Port $vnaPort 180)) {
                Invoke-Fail "nothing started listening on $vnaPort"
            }
        }
        Write-Ok "listening on $vnaPort"

        if ($vnaPort -eq 5025 -and (Test-Listening 5026)) {
            Write-Warn 'port 5026 is also open - a second copy of S2VNA is running'
            Write-Info 'Close the extra copy so the service cannot talk to the wrong one.'
        }

        # ------------------------------------------------------------------
        Write-Head 'Positioner (EMCenter)'

        # One entry point for both rigs. A missing tower is a reason to come up
        # without one, not a reason to refuse: the sweep, the Smith chart and a
        # 1-port calibration all work on a bench with only the VNA, and that is
        # a whole way of using this that should not need a different icon.
        #
        # Safe to infer because the service refuses rather than pretends. With
        # no positioner, Start scan is disabled and says why, so a pattern run
        # cannot quietly become 72 sweeps of the same angle - which is the only
        # thing that made inferring it dangerous.
        $vnaOnlyBecause = ''
        if ($VnaOnly) {
            $vnaOnlyBecause = 'asked for with -VnaOnly'
        } elseif ($Pos) {
            Write-Ok "using $Pos"
        } else {
            # Windows renumbers COM ports per machine and per USB socket, so
            # the number is not worth hardcoding. The USB identity is stable.
            $emc = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
                   Where-Object { $_.Name -match 'EMCenter.*\(COM\d+\)' } |
                   Select-Object -First 1
            if ($emc) {
                $null = $emc.Name -match '\(COM(\d+)\)'
                $Pos = "ASRL$($Matches[1])::INSTR"
                Write-Ok "$($emc.Name) -> $Pos"
            } else {
                $chassis = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
                           Where-Object { $_.Name -match 'EMCenter' }
                $vnaOnlyBecause = if ($chassis) {
                    'the chassis is on USB but has no COM port - the ETS-Lindgren driver is missing'
                } else {
                    'no EMCenter found on USB'
                }
            }
        }

        if ($vnaOnlyBecause) {
            Write-Warn "starting VNA-only: $vnaOnlyBecause"
            Write-Info 'Sweeps, the Smith chart and calibration all work.'
            Write-Info 'Start scan is disabled in the dashboard and says why.'
            if (-not $VnaOnly) {
                Write-Info 'If you meant to scan a pattern: check the EMCenter is'
                Write-Info 'powered and cabled, then setup.ps1 -CheckOnly tells the'
                Write-Info 'difference between unplugged and a failed driver install.'
            }
            $serviceArgs += @('--no-fallback', '--no-positioner', '--vna', $Vna)
        } else {
            $serviceArgs += @('--no-fallback', '--vna', $Vna, '--pos', $Pos)
        }
    }

    # ----------------------------------------------------------------------
    Write-Head 'Service'

    $env:PYTHONUNBUFFERED = '1'
    $service = Start-Process -FilePath $Py -ArgumentList $serviceArgs `
                             -WorkingDirectory $ProjectRoot -NoNewWindow -PassThru
    $script:Children += $service

    while (-not (Test-Listening $ServicePort)) {
        if ($service.HasExited) {
            Start-Sleep -Milliseconds 300   # let its last output through
            Invoke-Fail 'the service exited during startup (reason above)'
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Ok "ws://localhost:$ServicePort"

    # ----------------------------------------------------------------------
    Write-Head 'Frontend'

    # --strictPort: fail rather than drift to 5174 while the browser opens 5173.
    $frontend = Start-Process -FilePath 'npm.cmd' `
                              -ArgumentList @('run', 'dev', '--prefix', 'frontend', '--', '--strictPort') `
                              -WorkingDirectory $ProjectRoot -NoNewWindow -PassThru
    $script:Children += $frontend

    while (-not (Test-Listening $FrontendPort)) {
        if ($frontend.HasExited) {
            Start-Sleep -Milliseconds 300
            Invoke-Fail 'the frontend exited during startup (reason above)'
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Ok "http://localhost:$FrontendPort"

    if (-not $NoBrowser) {
        Start-Process "http://localhost:$FrontendPort" | Out-Null
    }

    Write-Head 'Running'
    Write-Info 'Ctrl+C stops the service and frontend. S2VNA stays open.'

    # Either one dying takes the other down, so a dead service never leaves a
    # dashboard up that still looks alive.
    while ($true) {
        if ($service.HasExited -or $frontend.HasExited) {
            Write-Host ''
            Write-Bad 'a process exited - shutting down'
            Stop-Children
            if (-not $NoPause) { Write-Host ''; Read-Host 'Press Enter to close' }
            exit 1
        }
        Start-Sleep -Milliseconds 500
    }
} finally {
    Stop-Children
}
