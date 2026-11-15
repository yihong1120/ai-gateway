# Run on the dedicated, already provisioned signing VM. No trust/policy changes.
[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
if ($env:COMPUTERNAME -ne 'CHANGDAR') { throw 'Use the approved signing VM.' }
$settings=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'signing-input.json') -Raw | ConvertFrom-Json
$exe=Join-Path $PSScriptRoot 'changdar-ai-setup.exe'
if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -ne $settings.unsigned_sha256) { throw 'Unsigned EXE hash differs from the reviewed build.' }
$cert=Get-Item -LiteralPath ('Cert:\LocalMachine\My\'+$settings.signer_thumbprint)
$chain=New-Object Security.Cryptography.X509Certificates.X509Chain
try {
    $chain.ChainPolicy.RevocationMode='Online';$chain.ChainPolicy.RevocationFlag='ExcludeRoot'
    if (-not $chain.Build($cert)) {throw 'Signing chain or revocation validation failed.'}
    $hash=[Security.Cryptography.SHA256]::Create()
    try {
        $rootSHA=([BitConverter]::ToString($hash.ComputeHash($chain.ChainElements[$chain.ChainElements.Count-1].Certificate.RawData))).Replace('-','')
        $leafSHA=([BitConverter]::ToString($hash.ComputeHash($cert.RawData))).Replace('-','')
    } finally {$hash.Dispose()}
    if ($rootSHA -ne $settings.root_sha256 -or $leafSHA -ne $settings.publisher_sha256) {throw 'Signer identity differs.'}
} finally {$chain.Dispose()}
$signed=Set-AuthenticodeSignature -LiteralPath $exe -Certificate $cert -HashAlgorithm SHA256 -IncludeChain NotRoot -TimestampServer 'http://timestamp.digicert.com'
$verified=Get-AuthenticodeSignature -LiteralPath $exe
if ($signed.Status -ne 'Valid' -or $verified.Status -ne 'Valid' -or -not $verified.TimeStamperCertificate -or $verified.SignerCertificate.Thumbprint -ne $settings.signer_thumbprint) {throw 'EXE signature/timestamp validation failed.'}
$report=Join-Path $PSScriptRoot 'installer-signing.json'
$result=[ordered]@{version=$settings.version;unsigned_sha256=$settings.unsigned_sha256;signed_sha256=(Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant();signatures_valid=$true;timestamp_present=$true;signer_thumbprint=$cert.Thumbprint}
[IO.File]::WriteAllText($report,($result|ConvertTo-Json),(New-Object Text.UTF8Encoding($false)))
$zip=Join-Path $PSScriptRoot 'changdar-ai-installer-signed.zip'
Compress-Archive -LiteralPath @($exe,$report) -DestinationPath $zip
Invoke-WebRequest -UseBasicParsing -Method Put -Uri 'https://changdar-server.mooo.com/company-ai/signing/signed-release.zip' -InFile $zip -ContentType 'application/zip' -MaximumRedirection 0 -TimeoutSec 60 | Out-Null
$result['signed_zip_sha256']=(Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
$result|ConvertTo-Json
