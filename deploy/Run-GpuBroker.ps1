param(
    [Parameter(Mandatory = $true)][string]$RepoRoot,
    [Parameter(Mandatory = $true)][string]$DataDir,
    [int]$Port = 18765
)

$ErrorActionPreference = 'Stop'
$logPath = Join-Path $DataDir 'service.log'
try {
    "$(Get-Date -Format o) Starting broker as $([Security.Principal.WindowsIdentity]::GetCurrent().Name)" | Out-File -FilePath $logPath -Append -Encoding utf8
    $pythonExe = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    Set-Location -LiteralPath $RepoRoot
    # Windows PowerShell 5 wraps a native program's stderr as an ErrorRecord.
    # Uvicorn writes normal lifecycle messages there, so do not treat them as
    # PowerShell exceptions. The native exit code still controls task failure.
    $ErrorActionPreference = 'Continue'
    & $pythonExe -m gpu_broker serve --data-dir $DataDir --port $Port *>> $logPath
    $result = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    "$(Get-Date -Format o) Broker exited with code $result" | Out-File -FilePath $logPath -Append -Encoding utf8
    exit $result
} catch {
    "$(Get-Date -Format o) Startup failed: $($_ | Out-String)" | Out-File -FilePath $logPath -Append -Encoding utf8
    exit 1
}
