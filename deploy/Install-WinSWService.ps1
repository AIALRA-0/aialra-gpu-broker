param(
    [Parameter(Mandatory = $true)][string]$WinSWExe,
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [Parameter(Mandatory = $true)][string]$DataDir,
    [string]$InstallDir = (Join-Path $env:ProgramData 'AIALRA\GpuBrokerService'),
    [int]$Port = 18765
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated PowerShell session.'
}
if (Get-Service -Name AIALRAGpuBroker -ErrorAction SilentlyContinue) {
    throw 'AIALRAGpuBroker is already installed. Stop and inspect it before replacing.'
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use. Stop the temporary broker before installing the service."
}
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
$DataDir = (Resolve-Path -LiteralPath $DataDir).Path
$WinSWExe = (Resolve-Path -LiteralPath $WinSWExe).Path
$pythonExe = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw "Missing Python environment: $pythonExe" }
if (-not (Test-Path -LiteralPath (Join-Path $DataDir 'config.json') -PathType Leaf)) { throw 'Broker config.json is missing.' }
if (-not (Test-Path -LiteralPath (Join-Path $DataDir 'tokens.json') -PathType Leaf)) { throw 'Broker tokens.json is missing.' }

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$wrapper = Join-Path $InstallDir 'AIALRAGpuBroker.exe'
$config = Join-Path $InstallDir 'AIALRAGpuBroker.xml'
Copy-Item -LiteralPath $WinSWExe -Destination $wrapper -Force
$xml = @"
<service>
  <id>AIALRAGpuBroker</id>
  <name>AIALRA GPU Broker</name>
  <description>Local GPU admission and monitoring on 127.0.0.1</description>
  <executable>$([System.Security.SecurityElement]::Escape($pythonExe))</executable>
  <arguments>-m gpu_broker serve --data-dir &quot;$([System.Security.SecurityElement]::Escape($DataDir))&quot; --port $Port</arguments>
  <workingdirectory>$([System.Security.SecurityElement]::Escape($RepoRoot))</workingdirectory>
  <startmode>Automatic</startmode>
  <delayedAutoStart>true</delayedAutoStart>
  <hidewindow>true</hidewindow>
  <stoptimeout>20 sec</stoptimeout>
  <onfailure action="restart" delay="30 sec"/>
  <log mode="roll"/>
</service>
"@
[IO.File]::WriteAllText($config, $xml, [Text.UTF8Encoding]::new($false))
& $wrapper install
if ($LASTEXITCODE -ne 0) { throw "WinSW install failed with code $LASTEXITCODE" }
& $wrapper start
if ($LASTEXITCODE -ne 0) { throw "WinSW start failed with code $LASTEXITCODE" }
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Seconds 1
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/health" -TimeoutSec 1
        if ($health.running -eq $true) { break }
    } catch { }
}
if ($null -eq $health -or $health.running -ne $true) {
    throw "Service was installed but failed its health check. Inspect $InstallDir and Windows Event Log."
}
Write-Output "Installed and verified AIALRA GPU Broker: http://127.0.0.1:$Port/"
Write-Output 'Allocation remains in its existing database mode. Check the dashboard before connecting projects.'
