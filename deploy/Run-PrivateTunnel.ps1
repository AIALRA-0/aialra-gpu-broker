param(
    [string]$SshHost = 'gpu-gateway',
    [int]$RemotePort = 18767,
    [int]$LocalPort = 18765,
    [int]$RetrySeconds = 10
)

$ErrorActionPreference = 'Continue'
while ($true) {
    & ssh.exe -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ConnectTimeout=10 -N -T -R "127.0.0.1:$($RemotePort):127.0.0.1:$LocalPort" $SshHost
    Start-Sleep -Seconds $RetrySeconds
}
