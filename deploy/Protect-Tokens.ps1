param([string]$DataDir = (Join-Path $env:LOCALAPPDATA 'AIALRA\GpuBroker'))
$ErrorActionPreference = 'Stop'
$DataDir = (Resolve-Path -LiteralPath $DataDir).Path
$tokenPath = Join-Path $DataDir 'tokens.json'
if (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf)) { throw "Token file not found: $tokenPath" }
$acl = Get-Acl -LiteralPath $tokenPath
$acl.SetAccessRuleProtection($true, $false)
$current = [Security.Principal.WindowsIdentity]::GetCurrent().User
$identities = @(
    $current,
    [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
    [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
)
foreach ($identity in $identities) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $identity,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $tokenPath -AclObject $acl
Write-Output "Restricted token file ACL: $tokenPath"
