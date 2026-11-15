# Safe regression: synthetic paths/files; Windows registry tests use a unique
# disposable HKCU subkey, never the real user or machine Environment key.
[CmdletBinding()]
param([string]$Source = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'examples\company_ai_access\windows\Setup-CompanyAI.ps1'))
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ('Setup syntax failed: ' + ($errors.Message -join '; ')) }
foreach ($function in $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
    . ([ScriptBlock]::Create($function.Extent.Text))
}
function Assert-Equal($Actual, $Expected, [string]$Name) {
    if ($Actual -cne $Expected) { throw ('Failed: ' + $Name) }
}
$bin = 'C:\Users\Employee With Spaces\.local\bin'
$cases = @(
    @{ before = ''; expected = $bin; name = 'empty PATH' },
    @{ before = 'C:\Existing;D:\Tools'; expected = 'C:\Existing;D:\Tools;' + $bin; name = 'preserve entries and order' },
    @{ before = 'C:\Existing;'; expected = 'C:\Existing;' + $bin; name = 'trailing separator' },
    @{ before = 'C:\Existing;' + $bin; expected = 'C:\Existing;' + $bin; name = 'idempotent' },
    @{ before = 'c:\users\employee with spaces\.LOCAL\BIN\;D:\Tools'; expected = 'c:\users\employee with spaces\.LOCAL\BIN\;D:\Tools'; name = 'case and trailing slash' },
    @{ before = '"' + $bin + '";D:\Tools'; expected = '"' + $bin + '";D:\Tools'; name = 'quoted entry' },
    @{ before = 'C:/Users/Employee With Spaces/.local/bin;D:\Tools'; expected = 'C:/Users/Employee With Spaces/.local/bin;D:\Tools'; name = 'forward slash' },
    @{ before = '%SystemRoot%\Other;D:\Tools'; expected = '%SystemRoot%\Other;D:\Tools;' + $bin; name = 'unrelated variables preserved' },
    @{ before = ('C:\LongEntry;' * 200); expected = ('C:\LongEntry;' * 200) + $bin; name = 'long PATH not truncated' }
)
foreach ($case in $cases) {
    Assert-Equal (Get-PathWithDirectory $case.before $bin) $case.expected $case.name
}
$previousProfile = [Environment]::GetEnvironmentVariable('COMPANY_AI_TEST_PROFILE', 'Process')
try {
    $env:COMPANY_AI_TEST_PROFILE = 'C:\Users\Employee With Spaces'
    $existing = '%COMPANY_AI_TEST_PROFILE%\.local\bin;D:\Tools'
    Assert-Equal (Get-PathWithDirectory $existing $bin) $existing 'expanded equivalent not duplicated'
} finally {
    if ($null -eq $previousProfile) { Remove-Item Env:\COMPANY_AI_TEST_PROFILE -ErrorAction SilentlyContinue }
    else { $env:COMPANY_AI_TEST_PROFILE = $previousProfile }
}
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('CompanyAI PATH regression ' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temporary) | Out-Null
try {
    Assert-LocalAtomicWrites $temporary
    Assert-LocalAtomicWrites $temporary
    if (@(Get-ChildItem -LiteralPath $temporary).Count) { throw 'Atomic write preflight left files.' }
} finally { Remove-Item -LiteralPath $temporary -Recurse -Force }

if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
    $subkey = 'Software\CompanyAI\Tests\Path-' + [Guid]::NewGuid().ToString('N')
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($subkey)
    try {
        $key.SetValue('Unrelated', 'keep')
        Update-UserPathKey $key $bin
        Assert-Equal $key.GetValueKind('Path') ([Microsoft.Win32.RegistryValueKind]::ExpandString) 'new registry PATH type'
        foreach ($kind in @([Microsoft.Win32.RegistryValueKind]::String, [Microsoft.Win32.RegistryValueKind]::ExpandString)) {
            $raw = '%SystemRoot%\Other;C:\Existing'
            $key.SetValue('Path', $raw, $kind)
            Update-UserPathKey $key $bin
            Update-UserPathKey $key $bin
            Assert-Equal $key.GetValueKind('Path') $kind 'existing registry type preserved'
            Assert-Equal ($key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)) ($raw + ';' + $bin) 'raw PATH preserved and idempotent'
        }
        $key.SetValue('Path', 42, [Microsoft.Win32.RegistryValueKind]::DWord)
        $rejected = $false
        try { Update-UserPathKey $key $bin } catch { $rejected = $true }
        if (-not $rejected) { throw 'Non-string registry PATH was accepted.' }
        Assert-Equal ($key.GetValue('Path')) 42 'invalid registry PATH untouched'
        Assert-Equal ($key.GetValue('Unrelated')) 'keep' 'unrelated registry value preserved'
    } finally {
        $key.Dispose()
        [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKey($subkey)
    }
    Write-Host 'PASS: Windows disposable registry preservation/idempotency checks.'
} else { Write-Host 'SKIP: Windows registry and notification require Windows.' }
Write-Host 'PASS: PowerShell parser, 10 PATH merge cases, repeated atomic-write preflight; no real device or credential changes.'
