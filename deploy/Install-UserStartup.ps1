param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'AIALRA\GpuBroker'),
    [int]$Port = 18765,
    [string]$TaskName = 'AIALRA-GpuBroker'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
$DataDir = (Resolve-Path -LiteralPath $DataDir).Path
$pythonExe = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Missing isolated Python runtime: $pythonExe"
}
if (-not (Test-Path -LiteralPath (Join-Path $DataDir 'config.json') -PathType Leaf)) {
    throw 'Initialize the broker before installing startup.'
}
if (-not (Test-Path -LiteralPath (Join-Path $DataDir 'tokens.json') -PathType Leaf)) {
    throw 'Project tokens are missing.'
}
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Invalid port.' }

$launcher = Join-Path $PSScriptRoot 'Run-GpuBroker.ps1'
$powershellExe = Join-Path $PSHOME 'powershell.exe'
if (-not (Test-Path -LiteralPath $powershellExe -PathType Leaf)) {
    $powershellExe = (Get-Command powershell.exe).Source
}
$arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -RepoRoot "{1}" -DataDir "{2}" -Port {3}' -f $launcher, $RepoRoot, $DataDir, $Port
$action = New-ScheduledTaskAction -Execute $powershellExe -Argument $arguments -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Local GPU admission and monitoring; listens only on 127.0.0.1' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
$health = $null
for ($attempt = 0; $attempt -lt 10; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/health" -TimeoutSec 1
        if ($health.running -eq $true -and (Get-ScheduledTask -TaskName $TaskName).State -eq 'Running') { break }
    } catch { }
}
if ($null -eq $health -or $health.running -ne $true -or (Get-ScheduledTask -TaskName $TaskName).State -ne 'Running') {
    $lastResult = (Get-ScheduledTaskInfo -TaskName $TaskName).LastTaskResult
    throw "Task startup did not pass the health check (last result $lastResult). The registered supervisor will keep retrying; inspect service.log."
}
Write-Output "Installed and started user-logon task: $TaskName"
Write-Output "Dashboard: http://127.0.0.1:$Port/"
Write-Output "Data: $DataDir"
