param(
    [string]$TaskName = 'AIALRA-GpuBroker-PrivateTunnel',
    [string]$SshHost = 'gpu-gateway',
    [int]$RemotePort = 18767,
    [int]$LocalPort = 18765
)

$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'Run-PrivateTunnel.ps1'
$powershellExe = (Get-Command powershell.exe).Source
$arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -SshHost "{1}" -RemotePort {2} -LocalPort {3}' -f $scriptPath, $SshHost, $RemotePort, $LocalPort
$action = New-ScheduledTaskAction -Execute $powershellExe -Argument $arguments -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Private outbound SSH connection for the GPU broker' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "Started private tunnel task: $TaskName"
