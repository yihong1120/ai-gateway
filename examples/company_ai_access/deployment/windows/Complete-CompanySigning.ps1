# Run on the dedicated Windows signing VM after B has issued its CSR.
# Installs only this VM's explicitly pinned company trust and signs the release.
[CmdletBinding()]
param([string]$StateDirectory=(Join-Path $env:LOCALAPPDATA 'CompanyAI-Signing'))
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or $env:COMPUTERNAME -ne 'CHANGDAR') {
    throw 'Run completion only on the dedicated CHANGDAR Windows signing VM.'
}
$identity=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'The signing VM machine certificate requires its administrator session.' }
. (Join-Path $PSScriptRoot 'CompanyAI.Signing.ps1')
$settings=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'signing-settings.json')) | ConvertFrom-Json
if ($settings.portal -ne 'https://changdar-server.mooo.com') { throw 'Unexpected signing portal.' }
$request=[IO.File]::ReadAllText((Join-Path $StateDirectory 'request.json')) | ConvertFrom-Json
if ($request.computer -ne $env:COMPUTERNAME) { throw 'Request belongs to another signing VM.' }
$pki=Invoke-RestMethod -Uri ($settings.portal + '/company-ai/pki/pki.json') -MaximumRedirection 0 -TimeoutSec 30
if ($pki.root_sha256 -ne $settings.root_sha256 -or -not $pki.signer -or $pki.signer.csr_sha256 -ne $request.csr_sha256) {
    throw 'B has not yet issued this exact signing request, or the pinned CA identity differs.'
}
$rootFile=Join-Path $StateDirectory 'RootCA.cer'
$publisherFile=Join-Path $StateDirectory 'Publisher.cer'
Invoke-WebRequest -UseBasicParsing -Uri ($settings.portal + '/company-ai/pki/CompanyAI-CodeSigning-Root.cer') -OutFile $rootFile -MaximumRedirection 0 -TimeoutSec 30
Invoke-WebRequest -UseBasicParsing -Uri ($settings.portal + '/company-ai/pki/CompanyAI-CodeSigning-Publisher.cer') -OutFile $publisherFile -MaximumRedirection 0 -TimeoutSec 30
$root=[Security.Cryptography.X509Certificates.X509Certificate2]::new([IO.File]::ReadAllBytes($rootFile))
$publisher=[Security.Cryptography.X509Certificates.X509Certificate2]::new([IO.File]::ReadAllBytes($publisherFile))
Assert-CompanySigningCertificates $root $publisher $settings.root_sha256 $pki.signer.sha256
# This explicit VM provisioning step is separate from employee setup.
Install-CompanyPublicTrust $root $publisher 'LocalMachine'
$installed=Get-Item -LiteralPath ('Cert:\LocalMachine\My\' + $publisher.Thumbprint) -ErrorAction SilentlyContinue
if (-not $installed -or -not $installed.HasPrivateKey) {
    & certreq.exe -accept -machine -q $publisherFile | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Windows could not associate the issued certificate with its original machine key.' }
    $installed=Get-Item -LiteralPath ('Cert:\LocalMachine\My\' + $publisher.Thumbprint)
}
$privateKey=[Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($installed)
try {
    if (-not $installed.HasPrivateKey -or $privateKey -isnot [Security.Cryptography.RSACng] -or [int]$privateKey.Key.ExportPolicy -ne 0) {
        throw 'Expected the original non-exportable Windows CNG signing key.'
    }
} finally { if ($privateKey) { $privateKey.Dispose() } }
$source=Join-Path $PSScriptRoot 'scripts'
foreach ($entry in $settings.files) {
    if ($entry.name -notin @('Setup-CompanyAI.ps1','Get-CompanyAIKey.ps1','Start-CompanyCodex.ps1')) { throw 'Unexpected signing input.' }
    if ((Get-FileHash -LiteralPath (Join-Path $source $entry.name) -Algorithm SHA256).Hash -ne $entry.sha256) { throw 'Reviewed signing source hash changed.' }
}
if (@($settings.files).Count -ne 3) { throw 'All three reviewed scripts are required.' }
$output=Join-Path $StateDirectory $settings.signed_version
if (-not (Test-Path -LiteralPath $output)) {
    & (Join-Path $PSScriptRoot 'Sign-CompanyAIRelease.ps1') -CertificateThumbprint $publisher.Thumbprint `
        -CertificateStore LocalMachine -ExpectedRootSHA256 $settings.root_sha256 `
        -TimestampServer $settings.timestamp_server -Version $settings.signed_version -SourceDirectory $source -OutputDirectory $output
}
$manifest=[IO.File]::ReadAllText((Join-Path $output 'release.json')) | ConvertFrom-Json
if ($manifest.version -ne $settings.signed_version -or $manifest.root_sha256 -ne $settings.root_sha256 -or
    $manifest.signer_thumbprint -ne $publisher.Thumbprint -or @($manifest.files).Count -ne 3) { throw 'Existing signed release identity differs.' }
$seen=@{}
foreach ($entry in $manifest.files) {
    if ($seen.ContainsKey($entry.name) -or $entry.name -notin @('Setup-CompanyAI.ps1','Get-CompanyAIKey.ps1','Start-CompanyCodex.ps1')) { throw 'Invalid signed manifest filenames.' }
    $seen[$entry.name]=$true
    $file=Join-Path $output $entry.name
    $signature=Get-AuthenticodeSignature -LiteralPath $file
    if ($signature.Status -ne 'Valid' -or -not $signature.TimeStamperCertificate -or $signature.SignerCertificate.Thumbprint -ne $publisher.Thumbprint -or
        (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $entry.sha256 -or
        (Get-FileHash -LiteralPath (Join-Path $source $entry.name) -Algorithm SHA256).Hash -ne $entry.source_sha256) { throw 'Windows signature, timestamp or source/hash verification failed.' }
}
$archive=Join-Path $StateDirectory ($settings.signed_version + '.zip')
if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
$files=@('Setup-CompanyAI.ps1','Get-CompanyAIKey.ps1','Start-CompanyCodex.ps1','release.json','Publisher.cer','RootCA.cer') | ForEach-Object {Join-Path $output $_}
Compress-Archive -LiteralPath $files -DestinationPath $archive
try {
    Invoke-WebRequest -UseBasicParsing -Method Put -Uri ($settings.portal + '/company-ai/signing/signed-release.zip') `
        -InFile $archive -ContentType 'application/zip' -MaximumRedirection 0 -TimeoutSec 60 | Out-Null
} catch { throw 'Signed artifact upload failed. The signed files and ZIP are retained for retry.' }
[ordered]@{computer=$env:COMPUTERNAME;version=$settings.signed_version;root_sha256=$settings.root_sha256;
    signer_thumbprint=$publisher.Thumbprint;signatures_valid=$true;timestamps_present=$true;
    signed_zip_sha256=(Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant();uploaded=$true} | ConvertTo-Json
