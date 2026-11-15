# Called by Claude apiKeyHelper or Codex provider.auth. Stdout is only the key.
$ErrorActionPreference = 'Stop'
try {
    $metadata = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'device.json')) | ConvertFrom-Json
    if ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -ge [long]$metadata.confirmation_due_at) {
        [Console]::Error.WriteLine('Company AI browser confirmation is due. Run the company setup tool again.')
        exit 1
    }
    $path = Join-Path $PSScriptRoot 'credential.dpapi'
    $secure = ConvertTo-SecureString ([IO.File]::ReadAllText($path))
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        [Console]::Out.WriteLine([Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer))
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $secure.Dispose()
    }
} catch {
    [Console]::Error.WriteLine('Company AI credential is unavailable. Run the company setup tool again.')
    exit 1
}
