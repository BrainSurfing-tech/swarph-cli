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

Session semantics: the pair is registered with -LogonType Interactive and
an AtLogOn trigger — it lives only while this user's session does. That
is workstation supervision, not service semantics: no logon, no monitor.

The supervision HOLD binds BOTH tasks (#344 review): `monitor stop` kills
the runner-owned monitor with exit 15, which is exactly what the runner's
restart-on-failure is built to revive — so the runner launcher checks the
hold BEFORE launching and exits 0 (a completed task does not restart),
and a supervised `monitor start` refuses to clear it. Only an operator's
start (no SWARPH_SUPERVISOR) un-holds. To resume a held monitor:
`swarph monitor start` clears the hold and runs one hand-started; the
runner re-claims supervision at next logon, or immediately when that
instance exits (the watchdog revives THROUGH the runner task, so the
ownership claim stays true).
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
`$hold = $(Quote-PowerShell (Join-Path $StateDir 'supervision_hold.json'))
# THE HOLD BINDS THE RUNNER TOO (#344 review): `monitor stop` kills the
# foreground monitor with exit 15, which is precisely what this task's
# restart-on-failure exists to revive. Without this gate a deliberate stop
# is a <=1min outage followed by an automatic revive — and the hold file
# erased with it. Exit 0: a COMPLETED task does not trigger restart-on-
# failure, so gating here is silent by construction.
if (Test-Path -LiteralPath `$hold) {
    "runner: supervision hold present — deliberate stop, NOT launching" *>> `$log
    exit 0
}
`$invokeArgs = @($runnerArgsLiteral)
& `$swarph @invokeArgs *>> `$log
# THE EXIT CODE IS THE RESTART SIGNAL. powershell -File exits 0 on script
# completion UNLESS told otherwise — measured on metal 2026-08-27: a
# force-killed monitor read "code de retour 0" in the task log and
# restart-on-failure never fired. Propagate or the runner is blind.
exit `$LASTEXITCODE
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
    # card swings). Revive THROUGH THE RUNNER TASK (#344 review): a detached
    # Start-Process from here would run unowned while claiming
    # supervisor=$supervisorSpec — the claim must not overstate. Starting the
    # runner makes the claim true AND gives the revival the runner's restart
    # accounting. IgnoreNew makes this a no-op if the runner is mid-restart.
    try {
        Start-ScheduledTask -TaskName $(Quote-PowerShell $runnerTask) -ErrorAction Stop
        "watchdog: monitor was DOWN (status exit 2) — started runner task '$runnerTask' (revival is runner-owned)" *>> `$log
    } catch {
        # The pair is registered together; a missing runner means someone
        # uninstalled half the supervision. Fall back to a detached start so
        # the monitor is not left dead — the claim still names the runner,
        # and the divergence is exactly what the ownership line is FOR.
        `$env:SWARPH_SUPERVISOR = $(Quote-PowerShell $supervisorSpec)
        `$startArgs = @($wdStartArgsLiteral)
        Start-Process -WindowStyle Hidden -FilePath `$swarph -ArgumentList `$startArgs -RedirectStandardOutput (Join-Path `$stateDir 'monitor.out.log') -RedirectStandardError (Join-Path `$stateDir 'monitor.err.log')
        "watchdog: monitor was DOWN; Start-ScheduledTask '$runnerTask' FAILED (`$(`$_.Exception.Message)) — revived detached as fallback" *>> `$log
    }
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
# PS 5.1 quirk (measured on metal): a trigger's .Repetition is not settable
# property-by-property — build a second trigger that carries the repetition
# and copy the whole Repetition object. 3650 days ≈ indefinitely.
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date)
$repetition = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $WatchdogIntervalMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
$watchdogTrigger.Repetition = $repetition.Repetition
$watchdogAction = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"{0}" "{1}"' -f $hiddenRunner, $watchdogLauncher)

if ($PSCmdlet.ShouldProcess($watchdogTask, 'register Task Scheduler watchdog')) {
    Register-ScheduledTask -TaskName $watchdogTask -Action $watchdogAction -Trigger $watchdogTrigger -Principal $principal -Settings $watchdogSettings -Description "Every $WatchdogIntervalMinutes min: heartbeat-check the $Peer monitor (gateway sees OK/DEGRADED/HELD) and revive it when down unless a supervision hold is present (#644). This task is LOAD-BEARING: restart-on-failure is exit-code keyed, so an exit-0 crash loop is invisible to the runner." -Force | Out-Null
}

Write-Host "Installed $runnerTask."
Write-Host "Installed $watchdogTask."
Write-Host "Ownership query: swarph monitor status --as $Peer  (supervised by: $supervisorSpec)"
if (-not $Start) { Write-Host 'Runner registered but not started. Re-run with -Start, or it begins at next logon.' }
