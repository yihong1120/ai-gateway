# Company-managed Codex home; personal Codex configuration stays in place.
# Run as the enrolled employee. No execution-policy overrides or token arguments.
$ErrorActionPreference = 'Stop'
$previousHome = [Environment]::GetEnvironmentVariable('CODEX_HOME', 'Process')
function Get-CompanyCodexLaunchOverrides([string]$ManagedHome, $Metadata) {
    $catalogPath = Join-Path $ManagedHome 'models.json'
    $catalog = [IO.File]::ReadAllText($catalogPath) | ConvertFrom-Json
    $models = @($catalog.models | ForEach-Object { [string]$_.slug })
    if ($models.Count -ne 3 -or ($models -join ',') -cne 'gemini-3.8-flash,gemini-3.5-flash-lite,gemini-4-argon') {
        throw 'The company model catalog is invalid.'
    }
    $model = $models[0]
    # Respect a saved choice in the managed file, while rejecting project/GPT
    # overrides. This generated file has its model in the root preamble.
    $preamble = ([IO.File]::ReadAllText((Join-Path $ManagedHome 'config.toml')) -split '(?m)^\s*\[', 2)[0]
    $match = [regex]::Match($preamble, '(?m)^model\s*=\s*["' + "'" + ']([^"' + "'" + ']+)["' + "'" + ']\s*(?:#.*)?$')
    if ($match.Success -and $models -contains $match.Groups[1].Value) { $model = $match.Groups[1].Value }
    return @('--config', ('model=' + (ConvertTo-Json -InputObject $model -Compress)),
        '--config', 'model_provider="company_ai"',
        '--config', ('model_catalog_json=' + (ConvertTo-Json -InputObject $catalogPath -Compress)),
        '--config', 'features.daemon_auto_start=false')
}

try {
    $metadata = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'codex-install.json')) | ConvertFrom-Json
    if (-not [IO.Path]::IsPathRooted($metadata.executable) -or -not (Test-Path -LiteralPath $metadata.executable -PathType Leaf)) {
        throw 'changdar-ai Codex executable is unavailable. Run company setup again.'
    }
    $version = & $metadata.executable --version 2>$null
    if ($LASTEXITCODE -ne 0 -or ([string]$version).Trim() -ne ('codex-cli ' + $metadata.release)) {
        throw 'changdar-ai Codex requires the approved CLI release. Run company setup again.'
    }
    $env:CODEX_HOME = Join-Path $PSScriptRoot 'codex'
    $overrides = @(Get-CompanyCodexLaunchOverrides $env:CODEX_HOME $metadata)
    # Explicit user -m/--model and later -c flags still win over these defaults.
    & $metadata.executable @overrides @args
    $result = $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine('changdar-ai Codex could not start. Run the company setup tool again.')
    $result = 1
} finally {
    if ($null -eq $previousHome) {
        [Environment]::SetEnvironmentVariable('CODEX_HOME', [System.Management.Automation.Language.NullString]::Value, 'Process')
    } else { [Environment]::SetEnvironmentVariable('CODEX_HOME', $previousHome, 'Process') }
}
exit $result
