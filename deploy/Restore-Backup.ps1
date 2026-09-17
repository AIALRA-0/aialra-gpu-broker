param(
    [Parameter(Mandatory = $true)][string]$BackupPath,
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'AIALRA\GpuBroker'),
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$DataDir = (Resolve-Path -LiteralPath $DataDir).Path
$BackupPath = (Resolve-Path -LiteralPath $BackupPath).Path
$backupRoot = (Resolve-Path -LiteralPath (Join-Path $DataDir 'backups')).Path
$backupPrefix = $backupRoot.TrimEnd('\') + '\'
if (-not $BackupPath.StartsWith($backupPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The backup must be inside this broker data directory.'
}
$dbPath = Join-Path $DataDir 'broker.sqlite3'
if (-not (Test-Path -LiteralPath $dbPath -PathType Leaf)) { throw "Database not found: $dbPath" }
$running = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like '*gpu_broker serve*' -and $_.CommandLine -like "*$DataDir*"
}
if ($running) { throw 'Stop the broker service or process before restoring the database.' }
$pythonExe = Join-Path (Resolve-Path -LiteralPath $RepoRoot).Path '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw "Python runtime not found: $pythonExe" }
$checkScript = 'import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); result=db.execute("PRAGMA integrity_check").fetchone()[0]; db.close(); print(result); sys.exit(0 if result=="ok" else 1)'
& $pythonExe -c $checkScript $BackupPath
if ($LASTEXITCODE -ne 0) { throw 'Backup integrity check failed.' }
$timestamp = Get-Date -Format 'yyyyMMddTHHmmss'
$oldPath = Join-Path $DataDir "broker.sqlite3.pre-restore-$timestamp"
Copy-Item -LiteralPath $dbPath -Destination $oldPath -ErrorAction Stop
Copy-Item -LiteralPath $BackupPath -Destination $dbPath -Force -ErrorAction Stop
& $pythonExe -c $checkScript $dbPath
if ($LASTEXITCODE -ne 0) {
    Copy-Item -LiteralPath $oldPath -Destination $dbPath -Force
    throw 'Restored database failed integrity check; original database was put back.'
}
Write-Output "Database restored from: $BackupPath"
Write-Output "Previous database retained at: $oldPath"
Write-Output 'Restart the broker, reconcile uncertain backend states, then consider enabling admission.'
