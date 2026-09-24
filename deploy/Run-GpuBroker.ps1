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

# Put the supervisor process in a kill-on-close Job Object before it starts
# Python. Windows automatically includes this process's children in the job,
# so Task Scheduler stopping PowerShell also terminates Python and its children.
if (-not ('Aialra.GpuBrokerSupervisorJob' -as [type])) {
    $jobTypeDefinition = @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace Aialra {
    public static class GpuBrokerSupervisorJob {
        private const int JobObjectExtendedLimitInfoClass = 9;
        private const uint JobObjectLimitKillOnJobClose = 0x00002000;
        private static readonly object SyncRoot = new object();
        // Keep the handle rooted until PowerShell exits. Windows then closes it
        // as part of process teardown and applies KILL_ON_JOB_CLOSE.
        private static IntPtr jobHandle = IntPtr.Zero;

        [StructLayout(LayoutKind.Sequential)]
        private struct JobObjectBasicLimitInformation {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IoCounters {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JobObjectExtendedLimitInformation {
            public JobObjectBasicLimitInformation BasicLimitInformation;
            public IoCounters IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr jobAttributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetInformationJobObject(
            IntPtr job,
            int informationClass,
            ref JobObjectExtendedLimitInformation information,
            uint informationLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll")]
        private static extern IntPtr GetCurrentProcess();

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr handle);

        public static void AttachCurrentProcess() {
            lock (SyncRoot) {
                if (jobHandle != IntPtr.Zero) {
                    return;
                }

                IntPtr newJob = CreateJobObject(IntPtr.Zero, null);
                if (newJob == IntPtr.Zero) {
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject failed");
                }

                try {
                    JobObjectExtendedLimitInformation information = new JobObjectExtendedLimitInformation();
                    information.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
                    if (!SetInformationJobObject(
                        newJob,
                        JobObjectExtendedLimitInfoClass,
                        ref information,
                        (uint)Marshal.SizeOf(typeof(JobObjectExtendedLimitInformation)))) {
                        throw new Win32Exception(Marshal.GetLastWin32Error(), "SetInformationJobObject failed");
                    }

                    if (!AssignProcessToJobObject(newJob, GetCurrentProcess())) {
                        throw new Win32Exception(Marshal.GetLastWin32Error(), "AssignProcessToJobObject failed");
                    }

                    jobHandle = newJob;
                    newJob = IntPtr.Zero;
                } finally {
                    if (newJob != IntPtr.Zero) {
                        CloseHandle(newJob);
                    }
                }
            }
        }
    }
}
'@
    Add-Type -TypeDefinition $jobTypeDefinition -ErrorAction Stop
}

try {
    [Aialra.GpuBrokerSupervisorJob]::AttachCurrentProcess()
    Write-ServiceLog "$(Get-Date -Format o) Supervisor attached to kill-on-close child-process job"
} catch {
    Write-ServiceLog "$(Get-Date -Format o) Failed to establish Broker child-process ownership: $($_ | Out-String)"
    throw
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
        # Windows PowerShell 5 wraps native stderr as ErrorRecord. Redirection
        # directly into the log mixes UTF-16 records with UTF-8 supervisor lines,
        # obscuring tracebacks when the upstream disappears. Convert each record
        # to text before writing it with one encoding.
        $ErrorActionPreference = 'Continue'
        & $pythonExe -m gpu_broker serve --data-dir $DataDir --port $Port 2>&1 |
            ForEach-Object { [string]$_ | Out-File -FilePath $logPath -Append -Encoding utf8 }
        $result = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        Write-ServiceLog "$(Get-Date -Format o) Broker exited with code $result; retrying in $RetrySeconds seconds"
    } catch {
        $ErrorActionPreference = 'Stop'
        Write-ServiceLog "$(Get-Date -Format o) Startup failed; retrying in $RetrySeconds seconds: $($_ | Out-String)"
    }
    Start-Sleep -Seconds $RetrySeconds
}
