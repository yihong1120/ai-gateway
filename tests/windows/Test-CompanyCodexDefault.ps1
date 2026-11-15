# Managed launch precedence: no network, credentials, enrollment or user settings.
[CmdletBinding()]
param([string]$Source=(Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'examples\company_ai_access\windows\Start-CompanyCodex.ps1'))
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Launcher syntax failure'}
foreach($f in $ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)){. ([ScriptBlock]::Create($f.Extent.Text))}
$setup=[Management.Automation.Language.Parser]::ParseFile((Join-Path (Split-Path $Source -Parent) 'Setup-CompanyAI.ps1'),[ref]$tokens,[ref]$errors)
foreach($f in $setup.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-PreferredPathWithDirectory'},$true)){. ([ScriptBlock]::Create($f.Extent.Text))}
$preferred='C:\CompanyAI\bin'
foreach($path in @('C:\Native;C:\System', 'C:\Native;"c:/companyai/bin/";C:\System', 'C:\CompanyAI\bin;C:\Native;C:\System;C:\CompanyAI\bin')) {
    $actual=Get-PreferredPathWithDirectory $path $preferred
    if($actual -ne 'C:\CompanyAI\bin;C:\Native;C:\System'){throw 'Managed launcher did not take precedence while preserving other PATH entries'}
    if((Get-PreferredPathWithDirectory $actual $preferred) -ne $actual){throw 'Preferred PATH merge is not idempotent'}
}
if((Get-PreferredPathWithDirectory '' $preferred) -ne $preferred){throw 'Empty preferred PATH failed'}
$root=Join-Path ([IO.Path]::GetTempPath()) ('Company model '+[Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($root)|Out-Null
try {
    $models=@('gemini-3.8-flash','gemini-3.5-flash-lite','gemini-4-argon')
    [IO.File]::WriteAllText((Join-Path $root 'models.json'),(@{models=@($models|ForEach-Object{@{slug=$_}})}|ConvertTo-Json -Depth 10))
    foreach($model in $models+@('gpt-5.6-sol')) {
        [IO.File]::WriteAllText((Join-Path $root 'config.toml'),("model = `"$model`"`n[features]`ndaemon_auto_start = false`n"))
        $flags=@(Get-CompanyCodexLaunchOverrides $root @{})
        $expected=$model;if($model -eq 'gpt-5.6-sol'){$expected=$models[0]}
        if($flags.Count -ne 8 -or $flags[1] -ne ('model="'+$expected+'"') -or $flags[3] -ne 'model_provider="company_ai"' -or $flags[5] -ne ('model_catalog_json='+ (ConvertTo-Json (Join-Path $root 'models.json') -Compress)) -or $flags[7] -ne 'features.daemon_auto_start=false'){throw 'Managed launch defaults were not pinned'}
        $forward=@($flags)+@('-m','gemini-4-argon','argument with spaces')
        if($forward[9] -ne 'gemini-4-argon' -or $forward[10] -ne 'argument with spaces'){throw 'Explicit model or literal arguments were lost'}
    }
    [IO.File]::WriteAllText((Join-Path $root 'models.json'),'{"models":[{"slug":"gpt-5.6-sol"}]}')
    $rejected=$false
    try{$null=Get-CompanyCodexLaunchOverrides $root @{}}catch{$rejected=$true}
    if(-not $rejected){throw 'An unexpected model catalog was accepted'}
    Write-Host 'PASS: company Flash default, all three saved choices, rejected GPT fallback, catalog/provider/daemon precedence and explicit model argument forwarding.'
}finally{Remove-Item -LiteralPath $root -Recurse -Force}
