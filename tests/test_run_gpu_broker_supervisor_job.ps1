$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$supervisorSourcePath = Join-Path $repoRoot 'deploy\Run-GpuBroker.ps1'
$supervisorSource = [IO.File]::ReadAllText($supervisorSourcePath)
$typeMarker = '$jobTypeDefinition = @'''
$typeStart = $supervisorSource.IndexOf($typeMarker, [StringComparison]::Ordinal)
if ($typeStart -lt 0) {
    throw 'Could not find the supervisor Job Object type definition.'
}
$typeBodyStart = $supervisorSource.IndexOf("`n", $typeStart, [StringComparison]::Ordinal) + 1
$typeBodyEnd = $supervisorSource.IndexOf("`n'@", $typeBodyStart, [StringComparison]::Ordinal)
if ($typeBodyStart -le 0 -or $typeBodyEnd -lt 0) {
    throw 'Could not extract the supervisor Job Object type definition.'
}
$jobTypeDefinition = $supervisorSource.Substring($typeBodyStart, $typeBodyEnd - $typeBodyStart).TrimEnd("`r")

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("GpuBrokerSupervisorJobTest-{0}" -f [guid]::NewGuid().ToString('N'))
$childScript = Join-Path $testRoot 'sleep-child.ps1'
$markerPath = Join-Path $testRoot 'child.pid'
$runnerPath = Join-Path $testRoot 'supervisor.ps1'
$runnerStdoutPath = Join-Path $testRoot 'supervisor.stdout.log'
$runnerStderrPath = Join-Path $testRoot 'supervisor.stderr.log'
$powershellExe = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$testRootFullPath = [IO.Path]::GetFullPath($testRoot).TrimEnd([char[]]"\/")
$tempParentFullPath = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([char[]]"\/")
$supervisorProcess = $null
$childProcessId = 0
$previousChildScript = $env:GPU_BROKER_JOB_TEST_CHILD_SCRIPT
$previousMarkerPath = $env:GPU_BROKER_JOB_TEST_MARKER

try {
    New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
    [IO.File]::WriteAllText($childScript, 'Start-Sleep -Seconds 300', [Text.Encoding]::UTF8)

    $runnerSource = @"
`$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
$jobTypeDefinition
'@
[Aialra.GpuBrokerSupervisorJob]::AttachCurrentProcess()
`$child = Start-Process -FilePath (Join-Path `$PSHOME 'powershell.exe') -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f `$env:GPU_BROKER_JOB_TEST_CHILD_SCRIPT)) -PassThru -WindowStyle Hidden
`$child.Id | Set-Content -LiteralPath `$env:GPU_BROKER_JOB_TEST_MARKER -Encoding ascii
while (`$true) { Start-Sleep -Seconds 1 }
"@
    [IO.File]::WriteAllText($runnerPath, $runnerSource, [Text.Encoding]::UTF8)

    $env:GPU_BROKER_JOB_TEST_CHILD_SCRIPT = $childScript
    $env:GPU_BROKER_JOB_TEST_MARKER = $markerPath
    $runnerArgument = '"{0}"' -f $runnerPath
    $supervisorProcess = Start-Process -FilePath $powershellExe -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $runnerArgument
    ) -PassThru -WindowStyle Hidden -RedirectStandardOutput $runnerStdoutPath -RedirectStandardError $runnerStderrPath
    $env:GPU_BROKER_JOB_TEST_CHILD_SCRIPT = $previousChildScript
    $env:GPU_BROKER_JOB_TEST_MARKER = $previousMarkerPath

    $startupDeadline = [DateTime]::UtcNow.AddSeconds(15)
    while (-not (Test-Path -LiteralPath $markerPath)) {
        $supervisorProcess.Refresh()
        if ($supervisorProcess.HasExited) {
            $runnerOutput = if (Test-Path -LiteralPath $runnerStderrPath) { [IO.File]::ReadAllText($runnerStderrPath) } else { '' }
            throw "Test supervisor exited before starting its child (code $($supervisorProcess.ExitCode)): $runnerOutput"
        }
        if ([DateTime]::UtcNow -ge $startupDeadline) {
            throw 'Timed out waiting for the test supervisor to start its child.'
        }
        Start-Sleep -Milliseconds 100
    }

    $childProcessId = [int](Get-Content -LiteralPath $markerPath -Raw)
    $null = Get-Process -Id $childProcessId -ErrorAction Stop

    Stop-Process -Id $supervisorProcess.Id -Force
    if (-not $supervisorProcess.WaitForExit(5000)) {
        throw 'Test supervisor did not exit after being stopped.'
    }

    $stopDeadline = [DateTime]::UtcNow.AddSeconds(5)
    $childStillRunning = $true
    while ($childStillRunning -and [DateTime]::UtcNow -lt $stopDeadline) {
        try {
            $null = Get-Process -Id $childProcessId -ErrorAction Stop
            Start-Sleep -Milliseconds 100
        } catch {
            $childStillRunning = $false
        }
    }
    if ($childStillRunning) {
        throw "Child process $childProcessId survived supervisor termination."
    }

    Write-Output 'PASS: stopping the supervisor terminates its child process.'
} finally {
    $env:GPU_BROKER_JOB_TEST_CHILD_SCRIPT = $previousChildScript
    $env:GPU_BROKER_JOB_TEST_MARKER = $previousMarkerPath

    if ($null -ne $supervisorProcess) {
        $supervisorProcess.Refresh()
        if (-not $supervisorProcess.HasExited) {
            Stop-Process -Id $supervisorProcess.Id -Force -ErrorAction SilentlyContinue
        }
    }
    if ($childProcessId -gt 0) {
        $childDetails = Get-CimInstance Win32_Process -Filter "ProcessId=$childProcessId" -ErrorAction SilentlyContinue
        if ($childDetails -and $childDetails.CommandLine -and
            $childDetails.CommandLine.Contains($childScript)) {
            Stop-Process -Id $childProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    if (Test-Path -LiteralPath $testRoot) {
        $resolvedCleanupRoot = (Resolve-Path -LiteralPath $testRoot).Path.TrimEnd([char[]]"\/")
        $resolvedCleanupParent = [IO.Path]::GetDirectoryName($resolvedCleanupRoot).TrimEnd([char[]]"\/")
        if (-not [string]::Equals($resolvedCleanupRoot, $testRootFullPath, [StringComparison]::OrdinalIgnoreCase) -or
            -not [string]::Equals($resolvedCleanupParent, $tempParentFullPath, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove unexpected test directory: $resolvedCleanupRoot"
        }
        Remove-Item -LiteralPath $resolvedCleanupRoot -Recurse -Force
    }
}
