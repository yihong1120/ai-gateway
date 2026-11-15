# Pure PowerShell regression. Synthetic settings only; no Windows state/network.
[CmdletBinding()]
param([string]$Source = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'examples\company_ai_access\windows\Setup-CompanyAI.ps1'))
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ('Setup syntax failed: ' + ($errors.Message -join '; ')) }
foreach ($function in $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
    . ([ScriptBlock]::Create($function.Extent.Text))
}
$models = @('gemini-3.8-flash', 'gemini-3.5-flash-lite', 'gemini-4-argon')
$original = '{"permissions":{"allow":["Read"]},"env":{"UNRELATED":"keep","ANTHROPIC_MODEL":"claude-sonnet-5","ANTHROPIC_BASE_URL":"https://example.test/company-ai"},"apiKeyHelper":"synthetic-helper"}'
$settings = $original | ConvertFrom-Json
Set-CompanyModelSettings $settings $models $models[0]
$roundtrip = $settings | ConvertTo-Json -Depth 100 | ConvertFrom-Json
if ($roundtrip.model -ne $models[0]) { throw 'Default model not configured.' }
if ($roundtrip.env.ANTHROPIC_MODEL) { throw 'Pinned model would override saved selections.' }
if ($roundtrip.env.ANTHROPIC_DEFAULT_MODEL -ne $models[0]) { throw 'Default fallback not configured.' }
if (($roundtrip.availableModels -join ',') -ne ($models -join ',')) { throw 'Model allowlist changed order or content.' }
if (-not $roundtrip.modelPicker.replaceBuiltInOptions -or ($roundtrip.modelPicker.options.model -join ',') -ne ($models -join ',')) { throw 'Picker is not the exact three requested models.' }
if ($roundtrip.modelPicker.options[1].label -ne 'Gemini 3.5 Flash Lite') { throw 'Picker labels missing.' }
if ($roundtrip.env.ANTHROPIC_DEFAULT_HAIKU_MODEL -ne 'gemini-3.5-flash-lite' -or $roundtrip.env.ANTHROPIC_DEFAULT_OPUS_MODEL -ne 'gemini-4-argon') { throw 'Background/planning tiers not mapped to allowed models.' }
if ($roundtrip.env.UNRELATED -ne 'keep' -or $roundtrip.permissions.allow[0] -ne 'Read' -or $roundtrip.apiKeyHelper -ne 'synthetic-helper' -or $roundtrip.env.ANTHROPIC_BASE_URL -ne 'https://example.test/company-ai') { throw 'Unrelated settings or provider credentials were changed.' }
foreach ($case in @(
    @{ models = @(); default = 'missing' },
    @{ models = $models; default = 'missing' },
    @{ models = @('duplicate', 'duplicate'); default = 'duplicate' },
    @{ models = @('unsafe;model'); default = 'unsafe;model' }
)) {
    $candidate = $original | ConvertFrom-Json
    $before = $candidate | ConvertTo-Json -Depth 100 -Compress
    $rejected = $false
    try { Set-CompanyModelSettings $candidate $case.models $case.default } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid model policy was accepted.' }
    if (($candidate | ConvertTo-Json -Depth 100 -Compress) -cne $before) { throw 'Invalid policy modified settings.' }
}
Write-Host 'PASS: default, three model picker entries, saved-choice behavior, tier mapping, settings preservation and four invalid-policy denials.'
