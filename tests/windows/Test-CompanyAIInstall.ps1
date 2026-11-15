# Default selection and idempotent installation; synthetic installers, no network.
[CmdletBinding()]
param([string]$Source = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'examples\company_ai_access\windows\Setup-CompanyAI.ps1'))
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ('Setup syntax failed: ' + ($errors.Message -join '; ')) }
foreach ($function in $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
    . ([ScriptBlock]::Create($function.Extent.Text))
}
# Bind the shipped parameter declaration before exercising the production plan.
$binding = [ScriptBlock]::Create($ast.ParamBlock.Extent.Text + "`nGet-CompanyToolPlan `$Tools `$PSBoundParameters")
function Assert-Plan([hashtable]$Arguments, [string]$Tools, [bool]$Claude, [bool]$Codex) {
    $plan = & $binding @Arguments
    if (($plan.tools -join ',') -ne $Tools -or $plan.install_claude -ne $Claude -or $plan.install_codex -ne $Codex) {
        throw ('Unexpected tool plan: ' + ($Arguments | ConvertTo-Json -Compress))
    }
}
Assert-Plan @{} 'Claude,Codex' $true $true
Assert-Plan @{Tools='Both'} 'Claude,Codex' $true $true
Assert-Plan @{Tools='Claude'} 'Claude' $true $false
Assert-Plan @{Tools='Codex'} 'Codex' $false $true
Assert-Plan @{InstallClaude=$true} 'Claude' $true $false
Assert-Plan @{InstallCodex=$true} 'Codex' $false $true
Assert-Plan @{InstallClaude=$true;InstallCodex=$true} 'Claude,Codex' $true $true
Assert-Plan @{InstallCodex=$false} 'Claude,Codex' $true $false
Assert-Plan @{InstallClaude=$false} 'Claude,Codex' $false $true
Assert-Plan @{RefreshModelsOnly=$true} 'Claude,Codex' $false $false
Assert-Plan @{RepairPathOnly=$true} 'Claude,Codex' $false $false
foreach ($arguments in @(@{Tools='Claude';InstallCodex=$true}, @{Tools='Codex';InstallClaude=$true})) {
    $rejected = $false
    try { $null = & $binding @arguments } catch { $rejected = $true }
    if (-not $rejected) { throw 'Conflicting tool/installation selection was accepted.' }
}
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('Company AI install ' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temporary) | Out-Null
$previousProfile = $env:USERPROFILE
$env:USERPROFILE = Join-Path $temporary 'User With Spaces'
[IO.Directory]::CreateDirectory($env:USERPROFILE) | Out-Null
function Find-CompanyClaude { if ($script:present.Claude) { return 'C:\Synthetic CLI\claude.exe' } }
function Find-CompanyCodex { if ($script:present.Codex) { return 'C:\Synthetic CLI\codex.exe' } }
function Invoke-WebRequest {
    param([string]$Uri, [switch]$UseBasicParsing, [string]$OutFile, [int]$TimeoutSec)
    if ($Uri -ne 'https://claude.ai/install.ps1') { throw 'Unexpected download.' }
    $script:downloads.Claude++
    [IO.File]::WriteAllText($OutFile, '# synthetic installer')
}
function Save-OfficialCodexInstaller([string]$Path) {
    $script:downloads.Codex++
    [IO.File]::WriteAllText($Path, '# synthetic installer')
}
function powershell.exe {
    $installer = [string]$args[[Array]::IndexOf($args, '-File') + 1]
    if ($installer.EndsWith('.install-claude.ps1')) { $script:present.Claude = $true; $script:installs.Claude++ }
    elseif ($installer.EndsWith('.codex-install.ps1')) { $script:present.Codex = $true; $script:installs.Codex++ }
    else { throw 'Unexpected installer execution.' }
    $script:LASTEXITCODE = 0
    Write-Output 'Synthetic installer progress'
}
function Confirm-CodexRelease([string]$Executable, [string]$Release) {
    if ($Executable -ne 'C:\Synthetic CLI\codex.exe' -or $Release -ne '0.164.0') { throw 'Approved release validation was bypassed.' }
    $script:versionChecks++
}
try {
    foreach ($claudePresent in @($false, $true)) {
        foreach ($codexPresent in @($false, $true)) {
            $script:present = @{Claude=$claudePresent;Codex=$codexPresent}
            $script:downloads = @{Claude=0;Codex=0}
            $script:installs = @{Claude=0;Codex=0}
            $script:versionChecks = 0
            $plan = & $binding
            for ($run = 0; $run -lt 2; $run++) {
                $claude = @(Install-CompanyClaude $temporary $plan.install_claude)
                $codex = @(Install-CompanyCodex $temporary '0.164.0' $plan.install_codex)
                if ($claude.Count -ne 1 -or $claude[0] -ne 'C:\Synthetic CLI\claude.exe' -or
                    $codex.Count -ne 1 -or $codex[0] -ne 'C:\Synthetic CLI\codex.exe') { throw 'Installers lost executable paths or mixed progress into their results.' }
            }
            foreach ($tool in @('Claude','Codex')) {
                $expected = 1
                if (($tool -eq 'Claude' -and $claudePresent) -or ($tool -eq 'Codex' -and $codexPresent)) { $expected = 0 }
                if ($script:downloads[$tool] -ne $expected -or $script:installs[$tool] -ne $expected) {
                    throw ('Existing tool was reinstalled, or missing tool was skipped: ' + $tool)
                }
            }
            if ($script:versionChecks -ne 2) { throw 'Existing Codex release was not verified.' }
            if (@(Get-ChildItem -LiteralPath $temporary -Filter '*.ps1').Count) { throw 'Temporary installer was not cleaned up.' }
        }
    }
    Write-Host 'PASS: default both, explicit selection, legacy switches, maintenance, opt-out, all four installed states and repeat-run installation skips.'
} finally {
    if ($null -eq $previousProfile) { [Environment]::SetEnvironmentVariable('USERPROFILE', [Management.Automation.Language.NullString]::Value, 'Process') }
    else { $env:USERPROFILE = $previousProfile }
    Remove-Item -LiteralPath $temporary -Recurse -Force
}
