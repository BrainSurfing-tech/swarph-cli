<#
.SYNOPSIS
Installs per-peer Windows Task Scheduler runners for the Swarph Codex Waker.

.DESCRIPTION
The Waker and drainer use separate long-running Task Scheduler jobs because
Windows Task Scheduler does not reliably support the sub-minute repetition
intervals used by the Linux systemd timers. The drainer is opt-in: validate a
Waker envelope before enabling automatic delivery.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]*$')]
    [string]$Peer,

    [Parameter(Mandatory)] [string]$MonitorInboxLog,
    [Parameter(Mandatory)] [string]$WakerStateDir,
    [Parameter(Mandatory)] [string]$WorkspaceDir,
    [Parameter(Mandatory)] [string]$OutboxDir,
    [Parameter(Mandatory)] [string]$Gateway,
    [Parameter(Mandatory)] [string]$TokenFile,
    [string]$SwarphBin = (Get-Command swarph.exe -ErrorAction Stop).Source,
    [ValidateRange(5, 3600)] [int]$WakerIntervalSeconds = 30,
    [ValidateRange(5, 3600)] [int]$DrainerIntervalSeconds = 15,
    [switch]$EnableDrainer,
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

function Test-PathWithin([string]$Child, [string]$Parent) {
    $prefix = $Parent.TrimEnd('\\') + '\\'
    return $Child.Equals($Parent, [StringComparison]::OrdinalIgnoreCase) -or
        $Child.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Write-LoopLauncher([string]$Path, [string]$LogPath, [string[]]$Arguments, [int]$IntervalSeconds) {
    $argumentList = ($Arguments | ForEach-Object { Quote-PowerShell $_ }) -join ', '
    $content = @"
`$ErrorActionPreference = 'Continue'
`$swarph = $(Quote-PowerShell $SwarphBin)
`$invokeArgs = @($argumentList)
`$log = $(Quote-PowerShell $LogPath)
while (`$true) {
    & `$swarph @invokeArgs *>> `$log
    Start-Sleep -Seconds $IntervalSeconds
}
"@
    Write-Utf8NoBom $Path $content
}

$MonitorInboxLog = Resolve-AbsolutePath $MonitorInboxLog
$WakerStateDir = Resolve-AbsolutePath $WakerStateDir
$WorkspaceDir = Resolve-AbsolutePath $WorkspaceDir
$OutboxDir = Resolve-AbsolutePath $OutboxDir
$TokenFile = Resolve-AbsolutePath $TokenFile
$SwarphBin = Resolve-AbsolutePath $SwarphBin

if (-not (Test-Path -LiteralPath $SwarphBin -PathType Leaf)) { throw "Missing swarph executable: $SwarphBin" }
if (-not (Test-Path -LiteralPath $MonitorInboxLog -PathType Leaf)) { throw "Missing monitor inbox log: $MonitorInboxLog" }
if (-not (Test-Path -LiteralPath $TokenFile -PathType Leaf)) { throw "Missing token file: $TokenFile" }
if (Test-PathWithin $WakerStateDir $WorkspaceDir) { throw 'Waker state must be outside the Codex workspace.' }
if (Test-PathWithin $OutboxDir $WakerStateDir) { throw 'Outbox must not be inside Waker state.' }

$launcherDir = Join-Path $WakerStateDir 'windows-launchers'
$wakerLauncher = Join-Path $launcherDir 'waker-loop.ps1'
$drainerLauncher = Join-Path $launcherDir 'drainer-loop.ps1'
$hiddenRunner = Join-Path $launcherDir 'run-hidden.vbs'

if ($WhatIfPreference) {
    Write-Host "Would write launchers under $launcherDir."
    Write-Host "Would register Swarph $Peer Codex Waker."
    if ($EnableDrainer) { Write-Host "Would register Swarph $Peer Codex Outbox Drainer." }
    return
}

New-Item -ItemType Directory -Force -Path $launcherDir, $OutboxDir | Out-Null

Write-LoopLauncher $wakerLauncher (Join-Path $launcherDir 'waker.log') @(
    'codex-waker', '--inbox-log', $MonitorInboxLog, '--state-dir', $WakerStateDir,
    '--self', $Peer, '--cwd', $WorkspaceDir, '--outbox-dir', $OutboxDir, '--timeout-s', '300'
) $WakerIntervalSeconds
Write-LoopLauncher $drainerLauncher (Join-Path $launcherDir 'drainer.log') @(
    'codex-waker', '--state-dir', $WakerStateDir, '--self', $Peer, '--cwd', $WorkspaceDir,
    '--outbox-dir', $OutboxDir, '--drain-outbox', '--gateway', $Gateway, '--token-file', $TokenFile
) $DrainerIntervalSeconds

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
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity

function Register-Runner([string]$TaskName, [string]$Launcher, [string]$Description) {
    $action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"{0}" "{1}"' -f $hiddenRunner, $Launcher)
    if ($PSCmdlet.ShouldProcess($TaskName, 'register Task Scheduler runner')) {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $Description -Force | Out-Null
        if ($Start) { Start-ScheduledTask -TaskName $TaskName }
    }
}

$wakerTask = "Swarph $Peer Codex Waker"
Register-Runner $wakerTask $wakerLauncher "Runs the $Peer Codex Waker every $WakerIntervalSeconds seconds while $identity is logged in."

if ($EnableDrainer) {
    $drainerTask = "Swarph $Peer Codex Outbox Drainer"
    Register-Runner $drainerTask $drainerLauncher "Drains authorized $Peer Codex Waker replies every $DrainerIntervalSeconds seconds while $identity is logged in."
}

Write-Host "Installed $wakerTask."
if ($EnableDrainer) { Write-Host "Installed Swarph $Peer Codex Outbox Drainer." }
if (-not $Start) { Write-Host 'Tasks are registered but not started. Re-run with -Start after validating the Waker envelope.' }
