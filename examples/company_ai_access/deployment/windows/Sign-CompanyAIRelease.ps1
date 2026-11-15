# Run on the approved Windows signing computer. Never export the private key.
# Requires a real company code-signing certificate in the Windows certificate store.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{40}$')]
    [string]$CertificateThumbprint,
    [ValidateSet('CurrentUser', 'LocalMachine')]
    [string]$CertificateStore = 'CurrentUser',
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedRootSHA256,
    [Parameter(Mandatory = $true)]
    [string]$TimestampServer,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$')]
    [string]$Version,
    [string]$SourceDirectory = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'windows'),
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Authenticode signing requires the approved Windows signing computer.'
}
# Set-AuthenticodeSignature uses a Windows timestamp API which requires HTTP.
# The returned timestamp is cryptographically signed; require and verify it.
$timestampUri = [Uri]$TimestampServer
if ($timestampUri.Scheme -ne 'http' -or -not $timestampUri.Host -or $timestampUri.UserInfo -or $timestampUri.Fragment) {
    throw 'Use the certificate provider-approved HTTP Authenticode timestamp endpoint.'
}
$certificatePath = 'Cert:\' + $CertificateStore + '\My\' + $CertificateThumbprint
$certificate = Get-Item -LiteralPath $certificatePath
if (-not $certificate.HasPrivateKey) { throw 'The certificate has no available signing private key.' }
if ($certificate.Subject -eq $certificate.Issuer) { throw 'A self-signed certificate is not a company release certificate.' }
$now = Get-Date
if ($now -lt $certificate.NotBefore -or $now -ge $certificate.NotAfter) {
    throw 'The signing certificate is not currently valid.'
}
$codeSigning = @($certificate.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.37' } |
    ForEach-Object { $_.EnhancedKeyUsages } | Where-Object { $_.Value -eq '1.3.6.1.5.5.7.3.3' })
if ($codeSigning.Count -eq 0) { throw 'The certificate is not authorized for code signing.' }
$chain = New-Object Security.Cryptography.X509Certificates.X509Chain
$rootCertificate = $null
try {
    $chain.ChainPolicy.RevocationMode = [Security.Cryptography.X509Certificates.X509RevocationMode]::Online
    $chain.ChainPolicy.RevocationFlag = [Security.Cryptography.X509Certificates.X509RevocationFlag]::ExcludeRoot
    $chain.ChainPolicy.UrlRetrievalTimeout = [TimeSpan]::FromSeconds(20)
    if (-not $chain.Build($certificate)) { throw 'Certificate chain or revocation validation failed.' }
    $rootCertificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new($chain.ChainElements[$chain.ChainElements.Count - 1].Certificate.RawData)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $rootSHA256 = ([BitConverter]::ToString($hash.ComputeHash($rootCertificate.RawData))).Replace('-','') }
    finally { $hash.Dispose() }
    if ($ExpectedRootSHA256 -and $rootSHA256 -ne $ExpectedRootSHA256) { throw 'The signer does not chain to the pinned company root.' }
} finally { $chain.Dispose() }

$source = [IO.Path]::GetFullPath($SourceDirectory)
$output = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $output) { throw 'OutputDirectory must be a new release directory.' }
$parent = Split-Path $output -Parent
if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'Output parent directory must already exist.' }
if ((Get-Item -LiteralPath $parent).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'Output parent must not be a link.'
}
$files = @('Setup-CompanyAI.ps1', 'Get-CompanyAIKey.ps1', 'Start-CompanyCodex.ps1')
foreach ($name in $files) {
    $path = Join-Path $source $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw ('Source file missing: ' + $name) }
    if ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Source scripts must not be links.'
    }
    $tokens = $null; $errors = $null
    $null = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ('Source script syntax is invalid: ' + $name) }
}
$staging = Join-Path $parent ('.company-ai-signing-' + [Guid]::NewGuid().ToString('N'))
$null = [IO.Directory]::CreateDirectory($staging)
try {
    $metadata = @()
    foreach ($name in $files) {
        $original = Join-Path $source $name
        $target = Join-Path $staging $name
        Copy-Item -LiteralPath $original -Destination $target
        $signed = Set-AuthenticodeSignature -LiteralPath $target -Certificate $certificate `
            -HashAlgorithm SHA256 -IncludeChain NotRoot -TimestampServer $TimestampServer
        $verified = Get-AuthenticodeSignature -LiteralPath $target
        if ($signed.Status -ne 'Valid' -or $verified.Status -ne 'Valid' `
                -or -not $verified.SignerCertificate -or -not $verified.TimeStamperCertificate `
                -or $verified.SignerCertificate.Thumbprint -ne $CertificateThumbprint) {
            throw ('Signature, publisher or timestamp verification failed: ' + $name)
        }
        $metadata += [ordered]@{
            name = $name
            sha256 = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
            source_sha256 = (Get-FileHash -LiteralPath $original -Algorithm SHA256).Hash.ToLowerInvariant()
            bytes = (Get-Item -LiteralPath $target).Length
        }
    }
    $manifest = [ordered]@{
        version = $Version
        created_at = [DateTimeOffset]::UtcNow.ToString('o')
        publisher = $certificate.Subject
        signer_thumbprint = $certificate.Thumbprint
        root_sha256 = $rootSHA256
        timestamp_server = $TimestampServer
        files = $metadata
    }
    [IO.File]::WriteAllText((Join-Path $staging 'release.json'), ($manifest | ConvertTo-Json -Depth 8),
        (New-Object Text.UTF8Encoding($false)))
    # Export only the public certificate. No PFX, password or private key.
    $null = Export-Certificate -Cert $certificate -FilePath (Join-Path $staging 'Publisher.cer') -Type CERT
    if ($ExpectedRootSHA256) {
        $null = Export-Certificate -Cert $rootCertificate -FilePath (Join-Path $staging 'RootCA.cer') -Type CERT
    }
    [IO.Directory]::Move($staging, $output)
    Write-Host ('Verified signed release ready: ' + $output)
    Write-Host ('Publisher: ' + $certificate.Subject)
    Write-Host 'Deploy all three PS1 files from this release together, preserving their exact bytes.'
    Write-Host 'No certificate trust settings or execution policies were changed.'
} finally {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
