# Explicit IT operation, separate from employee setup. Public certificates only.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$RootCertificate,
    [Parameter(Mandatory=$true)][string]$PublisherCertificate,
    [Parameter(Mandatory=$true)][ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$RootSHA256,
    [Parameter(Mandatory=$true)][ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$PublisherSHA256,
    [ValidateSet('CurrentUser','LocalMachine')][string]$Scope='CurrentUser'
)
$ErrorActionPreference='Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Windows certificate trust provisioning requires Windows.' }
if ($Scope -eq 'LocalMachine') {
    $identity=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'LocalMachine trust requires IT administrator access.' }
}
. (Join-Path $PSScriptRoot 'CompanyAI.Signing.ps1')
$root=[Security.Cryptography.X509Certificates.X509Certificate2]::new([IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $RootCertificate).Path))
$publisher=[Security.Cryptography.X509Certificates.X509Certificate2]::new([IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $PublisherCertificate).Path))
Assert-CompanySigningCertificates $root $publisher $RootSHA256 $PublisherSHA256
Install-CompanyPublicTrust $root $publisher $Scope
[ordered]@{scope=$Scope;root_sha256=$RootSHA256;publisher_sha256=$PublisherSHA256;execution_policy_changed=$false} | ConvertTo-Json
