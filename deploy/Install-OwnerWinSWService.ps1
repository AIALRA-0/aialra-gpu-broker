[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$WinSWExe,
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [Parameter(Mandatory = $true)][string]$DataDir,
    [string]$InstallDir = (Join-Path $env:ProgramData 'AIALRA\GpuOwnerService'),
    [ValidateRange(1, 65535)][int]$Port = 18767,
    [switch]$Start
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated PowerShell session.'
}
if (Get-Service -Name AIALRAGpuOwner -ErrorAction SilentlyContinue) {
    throw 'AIALRAGpuOwner is already installed. Inspect it before replacing.'
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use."
}

$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
$DataDir = (Resolve-Path -LiteralPath $DataDir).Path
$WinSWExe = (Resolve-Path -LiteralPath $WinSWExe).Path
$pythonExe = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Missing isolated Python runtime: $pythonExe"
}
if (-not (Test-Path -LiteralPath (Join-Path $DataDir 'owner.json') -PathType Leaf)) {
    throw 'Owner configuration is missing. Measure idle GPU use before installing.'
}
if (-not (Test-Path -LiteralPath (Join-Path $DataDir 'tokens.json') -PathType Leaf)) {
    throw 'Owner project credentials are missing.'
}
& $pythonExe -m gpu_broker.owner_cli check-config --data-dir $DataDir
if ($LASTEXITCODE -ne 0) {
    throw 'Owner configuration validation failed; the service was not installed.'
}

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$wrapper = Join-Path $InstallDir 'AIALRAGpuOwner.exe'
$config = Join-Path $InstallDir 'AIALRAGpuOwner.xml'
if ((Test-Path -LiteralPath $wrapper) -or (Test-Path -LiteralPath $config)) {
    throw 'Owner service files already exist. Inspect the prior installation before replacing them.'
}
Copy-Item -LiteralPath $WinSWExe -Destination $wrapper
$xml = @"
<service>
  <id>AIALRAGpuOwner</id>
  <name>AIALRA GPU Owner</name>
  <description>Local RTX 4080 ownership coordination on 127.0.0.1</description>
  <executable>$([System.Security.SecurityElement]::Escape($pythonExe))</executable>
  <arguments>-m gpu_broker.owner_cli serve --data-dir &quot;$([System.Security.SecurityElement]::Escape($DataDir))&quot; --port $Port</arguments>
  <workingdirectory>$([System.Security.SecurityElement]::Escape($RepoRoot))</workingdirectory>
  <startmode>Manual</startmode>
  <hidewindow>true</hidewindow>
  <stoptimeout>20 sec</stoptimeout>
  <onfailure action="restart" delay="15 sec"/>
  <log mode="roll"/>
</service>
"@
[IO.File]::WriteAllText($config, $xml, [Text.UTF8Encoding]::new($false))
& $wrapper install
if ($LASTEXITCODE -ne 0) {
    throw "WinSW Owner service install failed with code $LASTEXITCODE"
}

if ($Start) {
    & $wrapper start
    if ($LASTEXITCODE -ne 0) {
        throw "WinSW Owner service start failed with code $LASTEXITCODE"
    }
    $healthy = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Seconds 1
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/health" -TimeoutSec 1
            if ($health.running -eq $true) {
                $healthy = $true
                break
            }
        } catch { }
    }
    if (-not $healthy) {
        throw 'Owner process did not pass liveness. Inspect WinSW logs; liveness does not prove GPU admission.'
    }
}

Write-Output "Installed AIALRA GPU Owner service: $InstallDir"
if ($Start) {
    Write-Output 'Process liveness passed. Verify all three observers and real task handoffs before production cutover.'
} else {
    Write-Output 'Service remains stopped. Start only after all three GPU entries and idle calibration are ready.'
}
