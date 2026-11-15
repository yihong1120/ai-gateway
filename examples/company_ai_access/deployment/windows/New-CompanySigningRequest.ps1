# Run on CHANGDAR (the approved Windows signing VM) as its administrator.
# Creates a non-exportable machine signing key. No trust or execution-policy changes.
[CmdletBinding()]
param(
    [string]$Portal = 'https://changdar-server.mooo.com',
    [string]$OutputDirectory = (Join-Path $env:LOCALAPPDATA 'CompanyAI-Signing'),
    [string]$ExpectedComputer = 'CHANGDAR'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or $env:COMPUTERNAME -ne $ExpectedComputer) {
    throw 'Run this request tool only on the approved Windows signing VM.'
}
if ($Portal -ne 'https://changdar-server.mooo.com') { throw 'Use the exact company production HTTPS origin.' }
$identity = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open PowerShell as administrator on the signing VM; machine key creation requires it.'
}
$directory = [IO.Path]::GetFullPath($OutputDirectory)
for ($path = $directory; $path; $path = Split-Path $path -Parent) {
    if ((Test-Path -LiteralPath $path) -and ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Signing request paths must not contain links.'
    }
}
[IO.Directory]::CreateDirectory($directory) | Out-Null
$requestFile = Join-Path $directory 'CompanyAI-Signer.req'
$metadataFile = Join-Path $directory 'request.json'
$inf = Join-Path $directory 'request.inf'
if (-not (Test-Path -LiteralPath $requestFile)) {
    if ((Test-Path -LiteralPath $metadataFile) -or (Test-Path -LiteralPath $inf)) {
        throw 'Partial request state exists. Preserve it and contact the operator instead of replacing a key.'
    }
    $definition = @'
[Version]
Signature="$Windows NT$"
[NewRequest]
Subject="CN=Changdar Company AI Code Signing"
KeyAlgorithm=RSA
KeyLength=3072
KeySpec=AT_NONE
KeyUsage=0x80
KeyUsageProperty=NCRYPT_ALLOW_SIGNING_FLAG
HashAlgorithm=SHA256
ProviderName="Microsoft Software Key Storage Provider"
MachineKeySet=TRUE
Exportable=FALSE
ExportableEncrypted=FALSE
UserProtected=FALSE
Silent=TRUE
RequestType=PKCS10
[EnhancedKeyUsageExtension]
OID=1.3.6.1.5.5.7.3.3
'@
    [IO.File]::WriteAllText($inf, $definition, [Text.Encoding]::ASCII)
    & certreq.exe -new -machine -q $inf $requestFile | Out-Host
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $requestFile)) {
        throw 'Windows could not create the non-exportable signing key/request. Preserve the request directory.'
    }
}
$hash = (Get-FileHash -LiteralPath $requestFile -Algorithm SHA256).Hash.ToLowerInvariant()
if (Test-Path -LiteralPath $metadataFile) {
    $old = [IO.File]::ReadAllText($metadataFile) | ConvertFrom-Json
    if ($old.csr_sha256 -ne $hash -or $old.computer -ne $env:COMPUTERNAME) { throw 'Existing signing request identity changed.' }
}
$metadata = [ordered]@{ computer=$env:COMPUTERNAME; csr_sha256=$hash; key_exportable=$false }
[IO.File]::WriteAllText($metadataFile, ($metadata | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
try {
    Invoke-WebRequest -UseBasicParsing -Method Put -Uri ($Portal + '/company-ai/signing/request.csr') `
        -InFile $requestFile -ContentType 'application/pkcs10' -MaximumRedirection 0 -TimeoutSec 30 | Out-Null
} catch {
    throw 'CSR upload failed. Check the VM company DNS/LAN connection and the source-IP restriction. The same local request is retained for retry.'
}
$metadata['uploaded'] = $true
$metadata | ConvertTo-Json
