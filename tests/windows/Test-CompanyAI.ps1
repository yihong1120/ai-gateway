# Run on an approved Windows test machine as a normal employee user.
# Uses a synthetic credential only. Does not enroll devices or call models.
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'DPAPI acceptance requires Windows.' }
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$source = Join-Path $root 'examples\company_ai_access\windows'
$temporary = Join-Path $env:TEMP ('CompanyAI helper test ' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temporary) | Out-Null
function Invoke-Helper([string]$Path) {
    $process = New-Object Diagnostics.Process
    $process.StartInfo = New-Object Diagnostics.ProcessStartInfo
    $process.StartInfo.FileName = 'powershell.exe'
    $process.StartInfo.Arguments = '-NoProfile -NonInteractive -File "' + $Path + '"'
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    try {
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return @{ code = $process.ExitCode; stdout = $stdout.Trim(); stderr = $stderr }
    } finally { $process.Dispose() }
}
try {
    foreach ($name in @('Setup-CompanyAI.ps1', 'Get-CompanyAIKey.ps1', 'Start-CompanyCodex.ps1')) {
        $tokens = $null; $errors = $null
        $ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $source $name), [ref]$tokens, [ref]$errors)
        if ($errors.Count) { throw ('PowerShell syntax failed: ' + $name) }
        if ($name -eq 'Setup-CompanyAI.ps1') {
            foreach ($function in $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
                . ([ScriptBlock]::Create($function.Extent.Text))
            }
        }
    }
    & (Join-Path $PSScriptRoot 'Test-CompanyAIPath.ps1') -Source (Join-Path $source 'Setup-CompanyAI.ps1')
    & (Join-Path $PSScriptRoot 'Test-CompanyAIModels.ps1') -Source (Join-Path $source 'Setup-CompanyAI.ps1')
    & (Join-Path $PSScriptRoot 'Test-CompanyAIInstall.ps1') -Source (Join-Path $source 'Setup-CompanyAI.ps1')
    & (Join-Path $PSScriptRoot 'Test-CompanyAICodex.ps1') -Source (Join-Path $source 'Setup-CompanyAI.ps1')
    & (Join-Path $PSScriptRoot 'Test-CompanyAICodexDownload.ps1') -Source (Join-Path $source 'Setup-CompanyAI.ps1')
    Protect-Directory $temporary
    Assert-LocalAtomicWrites $temporary
    $probeDirectory = Join-Path $temporary 'atomic replacement with spaces'
    [IO.Directory]::CreateDirectory($probeDirectory) | Out-Null
    Assert-LocalAtomicWrites $probeDirectory
    $binaryPath = Join-Path $probeDirectory 'existing helper.ps1'
    Write-AtomicBytes $binaryPath ([byte[]]@(0, 255, 128))
    $replacementBytes = [byte[]]@(255, 0, 129, 42)
    Write-AtomicBytes $binaryPath $replacementBytes
    if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($binaryPath)) -ne [Convert]::ToBase64String($replacementBytes)) {
        throw 'Existing binary helper replacement failed.'
    }
    if (@(Get-ChildItem -LiteralPath $probeDirectory -Filter '*.tmp').Count) { throw 'Atomic replacement left temporary files.' }
    if (@(Get-ChildItem -LiteralPath $probeDirectory -Filter '*.write-test').Count) { throw 'Local preflight left probe files.' }
    $helper = Join-Path $temporary 'Get-CompanyAIKey.ps1'
    Copy-Item -LiteralPath (Join-Path $source 'Get-CompanyAIKey.ps1') -Destination $helper
    $synthetic = 'sk-synthetic-test-' + [Guid]::NewGuid().ToString('N')
    $secure = ConvertTo-SecureString $synthetic -AsPlainText -Force
    try { $encrypted = ConvertFrom-SecureString $secure } finally { $secure.Dispose() }
    if ($encrypted.Contains($synthetic)) { throw 'Credential was not encrypted.' }
    Write-Atomic (Join-Path $temporary 'credential.dpapi') $encrypted
    $metadata = @{ confirmation_due_at = [DateTimeOffset]::UtcNow.AddDays(1).ToUnixTimeSeconds() }
    Write-Atomic (Join-Path $temporary 'device.json') ($metadata | ConvertTo-Json)
    $result = Invoke-Helper $helper
    if ($result.code -ne 0 -or $result.stdout -ne $synthetic) { throw 'DPAPI helper roundtrip failed. Check approved script policy.' }
    $metadata.confirmation_due_at = 0
    Write-Atomic (Join-Path $temporary 'device.json') ($metadata | ConvertTo-Json)
    $result = Invoke-Helper $helper
    if ($result.code -eq 0 -or $result.stdout) { throw 'Expired helper emitted a key.' }
    if ($result.stderr.Contains($synthetic)) { throw 'A key leaked to stderr.' }
    $settings = '{"permissions":{"allow":["Read"]},"env":{"UNRELATED":"keep"}}' | ConvertFrom-Json
    Set-Property $settings 'apiKeyHelper' 'powershell.exe -NoProfile -File "C:\path with spaces\helper.ps1"'
    Set-Property $settings.env 'ANTHROPIC_BASE_URL' 'https://ai-dev.changdar-server.mooo.com/company-ai'
    $path = Join-Path $temporary 'settings.json'
    Write-Atomic $path ($settings | ConvertTo-Json -Depth 100)
    Write-Atomic $path ($settings | ConvertTo-Json -Depth 100)
    $loaded = [IO.File]::ReadAllText($path) | ConvertFrom-Json
    if ($loaded.env.UNRELATED -ne 'keep' -or $loaded.permissions.allow[0] -ne 'Read') { throw 'Existing settings were lost.' }
    if ([IO.File]::ReadAllText($path).Contains($synthetic)) { throw 'A key was written to settings.json.' }
    Write-Host 'Passed: Windows syntax, local-write preflight, existing binary helper replacement, DPAPI helper, expired-key denial, atomic settings and unrelated settings preservation.'
} finally {
    $synthetic = $null
    Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
}
