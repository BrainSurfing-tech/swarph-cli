<#
.SYNOPSIS
Installs the Windows Task Scheduler runner+watchdog pair that supervises a
peer's swarph monitor (board card #644).

.DESCRIPTION
Two tasks, because Task Scheduler's restart-on-failure is EXIT-CODE KEYED:
a monitor that crash-loops with exit 0 is invisible to it — #636's exact
shape (1147 restarts, every one exit 0, invisible to NRestarts, OnFailure
and is-enabled simultaneously on Linux). The runner owns the process
lifetime; the watchdog catches what the runner cannot see.

  RUNNER   "Swarph <Peer> Monitor"         At log on, restart-on-failure
                                           1min x N, MultipleInstances
                                           IgnoreNew, --foreground so the
                                           task OWNS the process lifetime.
  WATCHDOG "Swarph <Peer> Monitor Watchdog" Every -WatchdogIntervalMinutes:
                                           heartbeat-check ALWAYS (gateway
                                           sees OK/DEGRADED/HELD), then
                                           revives the monitor ONLY if it
                                           is down (status exit 2) and no
                                           supervision hold is present.

Also enables the TaskScheduler/Operational event log — the restart-counter
and journal legs are SILENT without it (IsEnabled=False is the default on
this fleet's Windows box, measured 2026-08-27). Enabling needs elevation;
the installer REFUSES to proceed silently without it.

Ownership convention: both launchers set SWARPH_SUPERVISOR to the runner
task's name; the monitor records it in its pidfile and `swarph monitor
status` reads it back. Windows has no pid→task reverse map — this
convention is the ownership query, or there is none.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]*$')]
    [string]$Peer,

    [Parameter(Mandatory)] [string]$StateDir,
    [Parameter(Mandatory)] [string]$Gateway,
    [string]$SwarphBin = (Get-Command swarph.exe -ErrorAction Stop).Source,
    [string[]]$Deliver = @(),
    [ValidateRange(1, 60)] [int]$WatchdogIntervalMinutes = 5,
    [ValidateRange(1, 999)] [int]$RunnerRestartCount = 999,
    [switch]$Start
)

$ErrorActionPreference = 'Stop'

function Resolve-AbsolutePath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path)
}

function Quote-PowerShell([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

$StateDir = Resolve-AbsolutePath $StateDir
$SwarphBin = Resolve-AbsolutePath $SwarphBin

if (-not (Test-Path -LiteralPath $SwarphBin -PathType Leaf)) { throw "Missing swarph executable: $SwarphBin" }

$runnerTask = "Swarph $Peer Monitor"
$watchdogTask = "Swarph $Peer Monitor Watchdog"
$supervisorSpec = "task:$runnerTask"

# ── The operational log is the restart counter and the journal — or it is nothing. ──
$tsLog = Get-WinEvent -ListLog Microsoft-Windows-TaskScheduler/Operational
if (-not $tsLog.IsEnabled) {
    & wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "TaskScheduler/Operational log is DISABLED and enabling it failed (exit $LASTEXITCODE — elevation required). Without it the restart-counter and journal legs are SILENT, which is #644's CANNOT-EVALUATE clause. Re-run elevated, or accept a supervision layer that cannot count restarts."
    }
    Write-Host "Enabled Microsoft-Windows-TaskScheduler/Operational (restart counter + journal leg)."
}

$launcherDir = Join-Path $StateDir 'windows-launchers'
$runnerLauncher = Join-Path $launcherDir 'monitor-runner.ps1'
$watchdogLauncher = Join-Path $launcherDir 'monitor-watchdog.ps1'
$hiddenRunner = Join-Path $launcherDir 'run-hidden.vbs'

if ($WhatIfPreference) {
    Write-Host "Would write launchers under $launcherDir."
    Write-Host "Would register '$runnerTask' (At log on, restart-on-failure 1min x $RunnerRestartCount, IgnoreNew)."
    Write-Host "Would register '$watchdogTask' (every $WatchdogIntervalMinutes min)."
    return
}

New-Item -ItemType Directory -Force -Path $launcherDir | Out-Null

# ── Runner: the task owns the monitor process lifetime (--foreground). ──
$runnerArgsList = @('monitor', 'start', '--foreground', '--as', $Peer, '--state-dir', $StateDir, '--gateway', $Gateway)
foreach ($d in $Deliver) { $runnerArgsList += @('--deliver', $d) }
$runnerArgsLiteral = ($runnerArgsList | ForEach-Object { Quote-PowerShell $_ }) -join ', '
$runnerContent = @"
`$ErrorActionPreference = 'Continue'
`$env:SWARPH_SUPERVISOR = $(Quote-PowerShell $supervisorSpec)
`$swarph = $(Quote-PowerShell $SwarphBin)
`$log = $(Quote-PowerShell (Join-Path $StateDir 'monitor-runner.log'))
`$invokeArgs = @($runnerArgsLiteral)
& `$swarph @invokeArgs *>> `$log
"@
Write-Utf8NoBom $runnerLauncher $runnerContent

# ── Watchdog: report ALWAYS, revive only when DOWN and not HELD. ──
$wdStartArgsList = @('monitor', 'start', '--as', $Peer, '--state-dir', $StateDir, '--gateway', $Gateway)
foreach ($d in $Deliver) { $wdStartArgsList += @('--deliver', $d) }
$wdStartArgsLiteral = ($wdStartArgsList | ForEach-Object { Quote-PowerShell $_ }) -join ', '
$watchdogContent = @"
`$ErrorActionPreference = 'Continue'
`$swarph = $(Quote-PowerShell $SwarphBin)
`$stateDir = $(Quote-PowerShell $StateDir)
`$log = $(Quote-PowerShell (Join-Path $StateDir 'monitor-watchdog.log'))
`$hold = Join-Path `$stateDir 'supervision_hold.json'
# Report first, always: a held or down monitor is still a fact the gateway
# should see. heartbeat-check classifies the cause; it never revives.
& `$swarph monitor heartbeat-check --as $(Quote-PowerShell $Peer) --state-dir `$stateDir --gateway $(Quote-PowerShell $Gateway) *>> `$log
if (Test-Path -LiteralPath `$hold) {
    "watchdog: supervision hold present — deliberate stop, NOT reviving" *>> `$log
    exit 0
}
& `$swarph monitor status --as $(Quote-PowerShell $Peer) --state-dir `$stateDir --brief *> `$null
if (`$LASTEXITCODE -eq 2) {
    # DOWN (not merely hung — a live-but-hung process is REPORTED, not killed:
    # killing a live pid on a heartbeat heuristic is a bigger hammer than this
    # card swings). Detach so this launcher can exit.
    `$env:SWARPH_SUPERVISOR = $(Quote-PowerShell $supervisorSpec)
    `$startArgs = @($wdStartArgsLiteral)
    Start-Process -WindowStyle Hidden -FilePath `$swarph -ArgumentList `$startArgs -RedirectStandardOutput (Join-Path `$stateDir 'monitor.out.log') -RedirectStandardError (Join-Path `$stateDir 'monitor.err.log')
    "watchdog: monitor was DOWN (status exit 2) — restarted detached, supervisor=$supervisorSpec" *>> `$log
}
"@
Write-Utf8NoBom $watchdogLauncher $watchdogContent

$vbs = @'
If WScript.Arguments.Count <> 1 Then WScript.Quit 2
Set shell = CreateObject("WScript.Shell")
scriptPath = WScript.Arguments(0)
command = "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " & Chr(34) & Replace(scriptPath, Chr(34), Chr(34) & Chr(34)) & Chr(34)
WScript.Quit shell.Run(command, 0, True)
'@
Write-Utf8NoBom $hiddenRunner $vbs

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited

$runnerSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount $RunnerRestartCount -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$runnerTrigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$runnerAction = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"{0}" "{1}"' -f $hiddenRunner, $runnerLauncher)

if ($PSCmdlet.ShouldProcess($runnerTask, 'register Task Scheduler runner')) {
    Register-ScheduledTask -TaskName $runnerTask -Action $runnerAction -Trigger $runnerTrigger -Principal $principal -Settings $runnerSettings -Description "Runs the $Peer swarph monitor in the foreground so this task owns its lifetime (#644). Restart-on-failure covers non-zero exits; the watchdog task covers exit-0 loops and hung processes." -Force | Out-Null
    if ($Start) { Start-ScheduledTask -TaskName $runnerTask }
}

$watchdogSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date)
# PS 5.1 cannot express "repeat forever" through -RepetitionDuration; set the
# repetition directly. Empty Duration = indefinitely.
$watchdogTrigger.Repetition.Interval = "PT$($WatchdogIntervalMinutes)M"
$watchdogTrigger.Repetition.Duration = ""
$watchdogAction = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"{0}" "{1}"' -f $hiddenRunner, $watchdogLauncher)

if ($PSCmdlet.ShouldProcess($watchdogTask, 'register Task Scheduler watchdog')) {
    Register-ScheduledTask -TaskName $watchdogTask -Action $watchdogAction -Trigger $watchdogTrigger -Principal $principal -Settings $watchdogSettings -Description "Every $WatchdogIntervalMinutes min: heartbeat-check the $Peer monitor (gateway sees OK/DEGRADED/HELD) and revive it when down unless a supervision hold is present (#644). This task is LOAD-BEARING: restart-on-failure is exit-code keyed, so an exit-0 crash loop is invisible to the runner." -Force | Out-Null
}

Write-Host "Installed $runnerTask."
Write-Host "Installed $watchdogTask."
Write-Host "Ownership query: swarph monitor status --as $Peer  (supervised by: $supervisorSpec)"
if (-not $Start) { Write-Host 'Runner registered but not started. Re-run with -Start, or it begins at next logon.' }
