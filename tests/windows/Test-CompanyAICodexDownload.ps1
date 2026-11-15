# Official installer redirects, without executing an installer or enrolling.
[CmdletBinding()]
param(
    [string]$Source = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'examples\company_ai_access\windows\Setup-CompanyAI.ps1'),
    [switch]$Live
)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Setup syntax failed.' }
$definitions = @{}
foreach ($function in $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
    $definitions[$function.Name] = $function.Extent.Text
    . ([ScriptBlock]::Create($function.Extent.Text))
}
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('CompanyAI installer download ' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temporary) | Out-Null
$file = Join-Path $temporary 'official installer.ps1'
$script:requests = @()
$script:responses = @()
function Get-OfficialCodexInstallerHop([Uri]$Uri) {
    $script:requests += $Uri.AbsoluteUri
    if (-not $script:responses.Count) { throw 'Unexpected additional network hop.' }
    $response = $script:responses[0]
    $script:responses = @($script:responses | Select-Object -Skip 1)
    return $response
}
$bytes = [byte[]]@(239, 187, 191, 35, 32, 0, 128, 255)
try {
    foreach ($status in @(301, 302, 303, 307, 308)) {
        $script:requests = @()
        $script:responses = @(@{status = $status; location = 'https://releases.openai.com/codex/install.ps1'}, @{status = 200; bytes = $bytes})
        Save-OfficialCodexInstaller $file
        if (($script:requests -join ',') -ne 'https://chatgpt.com/codex/install.ps1,https://releases.openai.com/codex/install.ps1') { throw 'Official redirect was not followed exactly once.' }
        if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($file)) -ne [Convert]::ToBase64String($bytes)) { throw 'Installer bytes changed during download.' }
    }
    foreach ($destination in @(
        'http://releases.openai.com/codex/install.ps1',
        'https://example.com/codex/install.ps1',
        'https://releases.openai.com.example.com/codex/install.ps1',
        'https://user@releases.openai.com/codex/install.ps1',
        'https://releases.openai.com:8443/codex/install.ps1',
        'https://releases.openai.com/other.ps1',
        'https://releases.openai.com/codex/install.ps1?token=untrusted',
        'https://releases.openai.com/codex/install.ps1#fragment'
    )) {
        Remove-Item -LiteralPath $file
        $script:requests = @()
        $script:responses = @(@{status = 302; location = $destination})
        $rejected = $false
        try { Save-OfficialCodexInstaller $file } catch { $rejected = $true }
        if (-not $rejected -or $script:requests.Count -ne 1 -or (Test-Path -LiteralPath $file)) { throw 'Unapproved redirect was contacted or written to disk.' }
        Write-AtomicBytes $file $bytes
    }
    foreach ($response in @(@{status = 200; bytes = [byte[]]@()}, @{status = 200; bytes = (New-Object byte[] (1MB + 1))}, @{status = 500}, @{status = 302; location = ''})) {
        Remove-Item -LiteralPath $file
        $script:requests = @()
        $script:responses = @($response)
        $rejected = $false
        try { Save-OfficialCodexInstaller $file } catch { $rejected = $true }
        if (-not $rejected -or (Test-Path -LiteralPath $file)) { throw 'Invalid or partial installer was accepted.' }
        Write-AtomicBytes $file $bytes
    }
    $script:requests = @()
    $script:responses = @(@{status = 302; location = '/codex/install.ps1'}) * 4
    $rejected = $false
    try { Save-OfficialCodexInstaller $file } catch { $rejected = $true }
    if (-not $rejected -or $script:requests.Count -ne 4) { throw 'Redirect loop was not bounded to three redirects.' }
    if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($file)) -ne [Convert]::ToBase64String($bytes)) { throw 'Failed download modified an existing file.' }
    Write-Host 'PASS: official redirects, exact bytes, blocked destinations, payload limits, failures and redirect-loop bound.'
    if ($Live) {
        . ([ScriptBlock]::Create($definitions['Get-OfficialCodexInstallerHop']))
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Save-OfficialCodexInstaller $file
        $text = [IO.File]::ReadAllText($file)
        if (-not $text.Contains('[CmdletBinding()]') -or -not $text.Contains('[string]$Release')) { throw 'Official endpoint did not return the expected installer.' }
        $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
        Write-Host ('PASS: live official HTTPS installer downloaded without execution; bytes=' + (Get-Item -LiteralPath $file).Length + '; sha256=' + $hash)
    }
} finally { Remove-Item -LiteralPath $temporary -Recurse -Force }
