param(
    [Parameter(Mandatory = $true)][string]$RepoRoot,
    [Parameter(Mandatory = $true)][string]$DataDir,
    [int]$Port = 18765,
    [ValidateRange(5, 300)][int]$RetrySeconds = 10
)

$ErrorActionPreference = 'Stop'
$logPath = Join-Path $DataDir 'service.log'
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null

function Write-ServiceLog([string]$Message) {
    try {
        $Message | Out-File -FilePath $logPath -Append -Encoding utf8 -ErrorAction Stop
    } catch {
        # A full log volume must not terminate the supervisor retry loop.
        Write-Warning $Message
    }
}

# Keep an unexpected interpreter exit from turning the public reverse proxy into
# a permanent 502 until the next user logon. Task Scheduler still owns final stop.
while ($true) {
    try {
        if ((Test-Path -LiteralPath $logPath) -and
            (Get-Item -LiteralPath $logPath).Length -gt 10MB) {
            $archive = Join-Path $DataDir ("service-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
            Move-Item -LiteralPath $logPath -Destination $archive
        }
        Write-ServiceLog "$(Get-Date -Format o) Starting broker as $([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
        $pythonExe = Join-Path $RepoRoot '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
            throw "Missing isolated Python runtime: $pythonExe"
        }
        Set-Location -LiteralPath $RepoRoot
        # Windows PowerShell 5 wraps a native program's stderr as an ErrorRecord.
        # Uvicorn writes normal lifecycle messages there, so the native exit code
        # is recorded and the supervisor retries instead of treating stderr as fatal.
        $ErrorActionPreference = 'Continue'
        & $pythonExe -m gpu_broker serve --data-dir $DataDir --port $Port *>> $logPath
        $result = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        Write-ServiceLog "$(Get-Date -Format o) Broker exited with code $result; retrying in $RetrySeconds seconds"
    } catch {
        $ErrorActionPreference = 'Stop'
        Write-ServiceLog "$(Get-Date -Format o) Startup failed; retrying in $RetrySeconds seconds: $($_ | Out-String)"
    }
    Start-Sleep -Seconds $RetrySeconds
}
