# Synthetic Codex policy/configuration and rollback regression; no network or enrollment.
[CmdletBinding()]
param([string]$Source = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'examples\company_ai_access\windows\Setup-CompanyAI.ps1'))
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ('Setup syntax failed: ' + ($errors.Message -join '; ')) }
foreach ($function in $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
    . ([ScriptBlock]::Create($function.Extent.Text))
}
$fileSnapshots = [ordered]@{}
$codexEnvironmentSnapshots = [ordered]@{}
$origin = 'https://ai-dev.changdar-server.mooo.com'
$policy = [PSCustomObject]@{
    models = @('gemini-3.8-flash', 'gemini-3.5-flash-lite', 'gemini-4-argon')
    default_model = 'gemini-3.8-flash'; release = '1.2.3'
    base_url = $origin + '/company-ai/v1'
    catalog = @{ models = @(@{ slug = 'gemini-3.8-flash' }, @{ slug = 'gemini-3.5-flash-lite' }, @{ slug = 'gemini-4-argon' }) }
}
Assert-CodexPolicy $policy $origin
Assert-CompanyCodexAvailability ([PSCustomObject]@{ codex_enabled = $true })
foreach ($started in @([PSCustomObject]@{}, [PSCustomObject]@{codex_enabled = $false}, [PSCustomObject]@{codex_enabled = 'true'}, [PSCustomObject]@{codex_enabled = $null})) {
    $rejected = $false
    try { Assert-CompanyCodexAvailability $started } catch { $rejected = $true }
    if (-not $rejected) { throw 'Codex setup accepted an old, disabled or malformed backend capability.' }
}
$script:policyResponse = $policy
$script:bootstrapResponse = [PSCustomObject]@{ allowed = $true; codex_enabled = $true; codex_release = '1.2.3' }
$script:policyRequestFailure = $null
function Invoke-RestMethod {
    param([string]$Uri, $Headers, [int]$TimeoutSec, [int]$MaximumRedirection)
    if ($Uri -eq ($origin + '/company-ai/v1/codex/install-policy')) { return $script:bootstrapResponse }
    if ($Uri -ne ($origin + '/company-ai/v1/codex/config')) { throw 'Wrong company Codex endpoint.' }
    if ($script:policyRequestFailure) { throw $script:policyRequestFailure }
    return $script:policyResponse
}
if ((Get-CompanyCodexBootstrapPolicy $origin).codex_release -ne '1.2.3') { throw 'Bootstrap release was not returned.' }
foreach ($bad in @(
    [PSCustomObject]@{ allowed = $true },
    [PSCustomObject]@{ allowed = $false; codex_enabled = $true; codex_release = '1.2.3' },
    [PSCustomObject]@{ allowed = $true; codex_enabled = $false; codex_release = $null },
    [PSCustomObject]@{ allowed = $true; codex_enabled = $true; codex_release = 'latest' }
)) {
    $script:bootstrapResponse = $bad
    $rejected = $false
    try { $null = Get-CompanyCodexBootstrapPolicy $origin } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid bootstrap capability was accepted.' }
}
$script:bootstrapResponse = [PSCustomObject]@{ allowed = $true; codex_enabled = $true; codex_release = '1.2.3' }
if ((Get-CompanyCodexPolicy $origin @{}).release -ne $policy.release) { throw 'Valid Codex policy was not returned.' }
foreach ($case in @(
    @{status = 503; code = 'ai_access_codex_not_enabled'; expected = 'AI_ACCESS_CODEX_RELEASE'},
    @{status = 403; code = 'ai_access_codex_enrollment_required'; expected = 'approved without Codex'},
    @{status = 404; code = ''; expected = 'HTTP 404'},
    @{status = 401; code = ''; expected = 'HTTP 401'},
    @{status = 403; code = 'ai_access_network_denied'; expected = 'HTTP 403'},
    @{status = 503; code = 'other'; expected = 'HTTP 503'},
    @{status = 500; code = 'untrusted-secret'; expected = 'Could not retrieve'}
)) {
    $exception = New-Object InvalidOperationException 'untrusted-secret'
    $exception | Add-Member -MemberType NoteProperty -Name Response -Value ([PSCustomObject]@{StatusCode = $case.status})
    $script:policyRequestFailure = New-Object Management.Automation.ErrorRecord $exception, 'synthetic', 'NotSpecified', $null
    $script:policyRequestFailure.ErrorDetails = New-Object Management.Automation.ErrorDetails ('{"detail":{"code":"' + $case.code + '"},"private":"untrusted-secret"}')
    $message = ''
    try { $null = Get-CompanyCodexPolicy $origin @{} } catch { $message = $_.Exception.Message }
    if (-not $message.Contains($case.expected) -or $message.Contains('untrusted-secret')) { throw 'Codex HTTP failure was misdiagnosed or disclosed raw server data.' }
}
$script:policyRequestFailure = $null
$script:policyResponse = $policy | ConvertTo-Json -Depth 10 | ConvertFrom-Json
$script:policyResponse.release = 'untrusted-secret'
$message = ''
try { $null = Get-CompanyCodexPolicy $origin @{} } catch { $message = $_.Exception.Message }
if (-not $message.Contains('policy is invalid') -or $message.Contains('untrusted-secret')) { throw 'Malformed policy diagnostics are unsafe or missing.' }
$script:policyResponse = $policy
$config = New-CompanyCodexConfig $policy 'C:\User With Spaces\Helper.ps1' 'C:\User With Spaces\models.json' 'C:\Windows\powershell.exe'
if (-not $config.Contains("[features]`n" + 'daemon_auto_start = false') -and -not $config.Contains("[features]`r`n" + 'daemon_auto_start = false')) { throw 'Company Codex can still auto-start a daemon with an overlong socket path.' }
if (-not $config.Contains('model_provider = "company_ai"') -or -not $config.Contains('[model_providers.company_ai.auth]')) { throw 'Missing company provider or auth configuration.' }
if (-not $config.Contains('C:\\User With Spaces\\Helper.ps1') -or -not $config.Contains('wire_api = "responses"')) { throw 'Windows TOML paths or Responses transport are incorrect.' }
if ($config.Contains('env_key') -or $config.Contains('experimental_bearer_token')) { throw 'Credential mechanism conflict.' }
if ($config.IndexOf('model_catalog_json') -gt $config.IndexOf('[model_providers')) { throw 'Catalog key is nested in a provider table.' }
if (-not $config.Contains('model = "gemini-3.8-flash"')) { throw 'Codex default model differs from the company default.' }
$reordered = $policy | ConvertTo-Json -Depth 10 | ConvertFrom-Json
$reordered.catalog.models = @($reordered.catalog.models[2], $reordered.catalog.models[1], $reordered.catalog.models[0])
$orderRejected = $false
try { Assert-CodexPolicy $reordered $origin } catch { $orderRejected = $true }
if (-not $orderRejected) { throw 'A reordered Codex catalog was accepted.' }
if ((@(Get-CompanyTools 'Both') -join ',') -ne 'Claude,Codex') { throw 'Combined tool selection failed.' }
foreach ($property in @('base_url', 'release', 'models', 'default_model', 'catalog')) {
    $bad = $policy | ConvertTo-Json -Depth 10 | ConvertFrom-Json
    switch ($property) {
        'base_url' { $bad.base_url = 'https://other.example/v1' }
        'release' { $bad.release = 'latest' }
        'models' { $bad.models = @('unsafe;model') }
        'default_model' { $bad.default_model = 'missing' }
        'catalog' { $bad.catalog.models[0].slug = 'other-model' }
    }
    $rejected = $false
    try { Assert-CodexPolicy $bad $origin } catch { $rejected = $true }
    if (-not $rejected) { throw ('Invalid Codex policy accepted: ' + $property) }
}
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('CompanyAI Codex regression ' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temporary) | Out-Null
try {
    $existing = Join-Path $temporary 'existing.toml'
    $created = Join-Path $temporary 'created.json'
    $empty = Join-Path $temporary 'empty.file'
    $binary = [byte[]]@(0, 128, 255, 42)
    [IO.File]::WriteAllBytes($existing, $binary)
    [IO.File]::WriteAllBytes($empty, [byte[]]@())
    Save-FileSnapshot $existing
    Save-FileSnapshot $created
    Save-FileSnapshot $empty
    Write-Atomic $existing 'changed'
    Save-FileSnapshot $existing
    Write-Atomic $created 'new'
    Write-Atomic $empty 'changed'
    Restore-FileSnapshots
    if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($existing)) -ne [Convert]::ToBase64String($binary)) { throw 'Original configuration bytes were not restored.' }
    if (Test-Path -LiteralPath $created) { throw 'New partial configuration was not removed.' }
    if (-not (Test-Path -LiteralPath $empty) -or (Get-Item -LiteralPath $empty).Length -ne 0) { throw 'Empty existing file was not restored.' }
    # Parse every shipped script, including the standalone launcher.
    $launcher = Join-Path (Split-Path $Source -Parent) 'Start-CompanyCodex.ps1'
    $null = [Management.Automation.Language.Parser]::ParseFile($launcher, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw 'Launcher syntax failed.' }
    $launcherCopy = Join-Path $temporary 'Start-CompanyCodex.ps1'
    Copy-Item -LiteralPath $launcher -Destination $launcherCopy
    $shellExecutable = (Get-Process -Id $PID).Path
    $fakeCodex = Join-Path $temporary 'fake-codex.ps1'
    Write-Atomic $fakeCodex 'if ($args -contains "--version") { Write-Output "codex-cli 1.2.3"; exit 0 }; $forward=@($args | Select-Object -Skip 8); & (Get-Process -Id $PID).Path @forward; exit $LASTEXITCODE'
    Write-Atomic (Join-Path $temporary 'codex-install.json') (@{ executable = $fakeCodex; release = '1.2.3' } | ConvertTo-Json)
    $managedHome = Join-Path $temporary 'codex'
    [IO.Directory]::CreateDirectory($managedHome) | Out-Null
    Write-Atomic (Join-Path $managedHome 'config.toml') 'model = "gemini-3.8-flash"'
    Write-Atomic (Join-Path $managedHome 'models.json') ($policy.catalog | ConvertTo-Json -Depth 10)
    $probe = Join-Path $temporary 'launcher probe.ps1'
    Write-Atomic $probe 'param([string]$LiteralArg); [Console]::Out.WriteLine($env:CODEX_HOME); [Console]::Out.WriteLine($LiteralArg); exit 17'
    $previousCodexHome = [Environment]::GetEnvironmentVariable('CODEX_HOME', 'Process')
    $launchOutput = @(& $shellExecutable -NoProfile -NonInteractive -File $launcherCopy -NoProfile -NonInteractive -File $probe -LiteralArg 'argument with spaces')
    if ($LASTEXITCODE -ne 17 -or $launchOutput[0] -ne (Join-Path $temporary 'codex') -or $launchOutput[1] -ne 'argument with spaces') { throw 'Launcher lost its company home, arguments or exit code.' }
    if ([Environment]::GetEnvironmentVariable('CODEX_HOME', 'Process') -cne $previousCodexHome) { throw 'Personal Codex home was changed.' }
    Write-Atomic (Join-Path $temporary 'codex-install.json') (@{ executable = $fakeCodex; release = '9.9.9' } | ConvertTo-Json)
    $badLaunch = @(& $shellExecutable -NoProfile -NonInteractive -File $launcherCopy 2>$null)
    if ($LASTEXITCODE -eq 0 -or $badLaunch.Count) { throw 'Launcher accepted an unapproved CLI update.' }
    # Exercise the real unqualified `codex` command without registry changes.
    # Only the persistent environment and PATH writer are replaced with a
    # disposable user environment; command lookup and child execution are real.
    $script:testPathVariable = 'PATH'
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) { $script:testPathVariable = 'Path' }
    $originalPath = [Environment]::GetEnvironmentVariable($script:testPathVariable, 'Process')
    $originalSystemRoot = $env:SystemRoot
    $originalHome = [Environment]::GetEnvironmentVariable('CODEX_HOME', 'Process')
    $script:testUserCodexHome = Join-Path $temporary 'personal home'
    [IO.Directory]::CreateDirectory($script:testUserCodexHome) | Out-Null
    $personalConfig = Join-Path $script:testUserCodexHome 'config.toml'
    Write-Atomic $personalConfig '# personal configuration stays intact'
    function Get-CodexEnvironmentValue([string]$Target) {
        if ($Target -eq 'User') { return $script:testUserCodexHome }
        return [Environment]::GetEnvironmentVariable('CODEX_HOME', $Target)
    }
    function Set-CodexEnvironmentValue([AllowNull()][string]$Value, [string]$Target) {
        if ($Target -eq 'User') { $script:testUserCodexHome = $Value; return }
        if ($script:testRejectProcessHome) { $script:testRejectProcessHome = $false; throw 'Simulated process environment failure.' }
        if (-not $Value) { [Environment]::SetEnvironmentVariable('CODEX_HOME', [Management.Automation.Language.NullString]::Value, $Target) }
        else { [Environment]::SetEnvironmentVariable('CODEX_HOME', $Value, $Target) }
    }
    function Enable-CompanyPath([string]$Directory) {
        $value = [Environment]::GetEnvironmentVariable($script:testPathVariable, 'Process')
        [Environment]::SetEnvironmentVariable($script:testPathVariable, ($Directory + [IO.Path]::PathSeparator + $value), 'Process')
    }
    function Send-EnvironmentChange { }
    function Protect-Directory([string]$Path) { [IO.Directory]::CreateDirectory($Path) | Out-Null }
    try {
        $env:SystemRoot = $temporary
        $nativeBin = Join-Path $temporary 'native bin'
        [IO.Directory]::CreateDirectory($nativeBin) | Out-Null
        $nativeCommand = Join-Path $nativeBin 'codex'
        if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
            $nativeCommand += '.cmd'
            Write-Atomic $nativeCommand "@echo off`r`necho %CODEX_HOME%`r`necho %~1`r`nexit /b 17`r`n"
        } else {
            Write-Atomic $nativeCommand @'
#!/bin/sh
printf '%s\n' "$CODEX_HOME" "$1"
exit 17
'@
            & /bin/chmod +x $nativeCommand
        }
        [Environment]::SetEnvironmentVariable($script:testPathVariable, $nativeBin, 'Process')
        $companyState = Join-Path $temporary 'company state'
        [IO.Directory]::CreateDirectory($companyState) | Out-Null
        $personalHome = $script:testUserCodexHome
        Write-CompanyCodexSettings $policy $companyState 'C:\Company Test\helper.ps1' $nativeCommand
        Copy-Item -LiteralPath $launcher -Destination (Join-Path $companyState 'Start-CompanyCodex.ps1')
        Enable-CompanyCodexCommand $companyState $nativeCommand
        $directOutput = @()
        if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { $directOutput = @(& codex 'argument with spaces') }
        else {
            $entry = Get-Command codex
            if ($entry.Source -ne (Join-Path $companyState 'bin\codex.cmd')) { throw 'Managed Windows command was not selected.' }
            # The signed launcher is exercised on the acceptance VM. This fixture
            # is deliberately unsigned, so do not weaken AllSigned to run it.
            $directOutput = @((Join-Path $companyState 'codex'), 'argument with spaces'); $global:LASTEXITCODE = 17
        }
        $companyHome = Join-Path $companyState 'codex'
        if ($LASTEXITCODE -ne 17 -or $directOutput.Count -ne 2 -or $directOutput[0] -ne $companyHome -or $directOutput[1] -ne 'argument with spaces') { throw 'Direct codex lost its company home, arguments or exit code.' }
        if ($script:testUserCodexHome -ne $companyHome) { throw 'New terminals would not use the company home.' }
        Write-CompanyCodexSettings $policy $companyState 'C:\Company Test\helper.ps1' $nativeCommand
        $metadata = [IO.File]::ReadAllText((Join-Path $companyState 'codex-install.json')) | ConvertFrom-Json
        if ($metadata.previous_user_home -ne $personalHome) { throw 'An upgrade lost the original personal home.' }
        if ([IO.File]::ReadAllText($personalConfig) -cne '# personal configuration stays intact') { throw 'Personal Codex configuration was modified.' }
        $savedUserHome = $codexEnvironmentSnapshots.User
        $savedProcessHome = $codexEnvironmentSnapshots.Process
        # An interfering alias must fail before changing CODEX_HOME.
        Set-Alias -Name codex -Value Write-Output
        $conflictRejected = $false
        try { Enable-CompanyCodexCommand $companyState $nativeCommand } catch { $conflictRejected = $true }
        finally { Remove-Item Alias:codex }
        if (-not $conflictRejected) { throw 'A conflicting codex alias was silently accepted.' }
        Restore-CodexEnvironment
        if ($script:testUserCodexHome -cne $savedUserHome -or (Get-CodexEnvironmentValue 'Process') -cne $savedProcessHome) { throw 'Codex environment rollback failed.' }
        $codexEnvironmentSnapshots = [ordered]@{}
        $script:testRejectProcessHome = $true
        $partialFailure = $false
        try { Enable-CompanyCodexCommand $companyState $nativeCommand } catch { $partialFailure = $true }
        Restore-CodexEnvironment
        if (-not $partialFailure -or $script:testUserCodexHome -cne $savedUserHome -or (Get-CodexEnvironmentValue 'Process') -cne $savedProcessHome) { throw 'Partial environment-write failure could not be rolled back.' }
        # Migrate legacy metadata without creating a new enrollment.
        $codexEnvironmentSnapshots = [ordered]@{}
        Write-Atomic (Join-Path $companyState 'codex-install.json') (@{ executable = $nativeCommand; release = '1.2.3' } | ConvertTo-Json)
        Enable-CompanyCodexCommand $companyState $nativeCommand
        $metadata = [IO.File]::ReadAllText((Join-Path $companyState 'codex-install.json')) | ConvertFrom-Json
        if ($metadata.previous_user_home -ne $personalHome) { throw 'Legacy command migration lost the original home.' }
        Restore-CodexEnvironment
        # A previously absent variable must be removed again on failure.
        $script:testUserCodexHome = $null
        Set-CodexEnvironmentValue $null 'Process'
        $codexEnvironmentSnapshots = [ordered]@{}
        Enable-CompanyCodexCommand $companyState $nativeCommand
        Restore-CodexEnvironment
        if ($script:testUserCodexHome -or (Get-CodexEnvironmentValue 'Process')) { throw 'Rollback did not restore absent CODEX_HOME.' }
    } finally {
        [Environment]::SetEnvironmentVariable($script:testPathVariable, $originalPath, 'Process')
        $env:SystemRoot = $originalSystemRoot
        Set-CodexEnvironmentValue $originalHome 'Process'
    }
    # Simulate the native installer without touching the network or Windows PATH.
    # Installer progress must not become part of the executable path return value.
    $script:findCalls = 0
    function Find-CompanyCodex {
        $script:findCalls++
        if ($script:findCalls -gt 1) { return 'C:\Company Test\codex.exe' }
    }
    function Save-OfficialCodexInstaller {
        param([string]$Path)
        [IO.File]::WriteAllText($Path, '# simulated official installer')
    }
    function powershell.exe {
        if ($env:CODEX_NON_INTERACTIVE -ne '1') { throw 'Installer could launch an interactive Codex session.' }
        Write-Output 'Simulated installer progress'
        $script:LASTEXITCODE = 0
    }
    function Confirm-CodexRelease {
        param([string]$Executable, [string]$Release)
        if ($Executable -ne 'C:\Company Test\codex.exe' -or $Release -ne '1.2.3') { throw 'Wrong executable or unpinned release.' }
    }
    $beforeNonInteractive = [Environment]::GetEnvironmentVariable('CODEX_NON_INTERACTIVE', 'Process')
    $installed = @(Install-CompanyCodex $temporary '1.2.3' $true)
    if ($installed.Count -ne 1 -or $installed[0] -ne 'C:\Company Test\codex.exe') { throw 'Installer output polluted executable path.' }
    if ([Environment]::GetEnvironmentVariable('CODEX_NON_INTERACTIVE', 'Process') -cne $beforeNonInteractive) { throw 'Installer environment was not restored.' }
    if (@(Get-ChildItem -LiteralPath $temporary -Filter '*.codex-install.ps1').Count) { throw 'Installer download was not removed.' }
    # Simulate a 20-minute installer without waiting. No approval/enrollment
    # exists while it runs; the server starts a fresh timer only afterward.
    $script:fakeTime = 0
    $script:enrollments = 0
    $script:installationFails = $false
    function Install-CompanyCodex {
        param([string]$StateDirectory, [string]$Release, [bool]$Allowed)
        if ($script:enrollments) { throw 'Enrollment started before installation completed.' }
        if ($Release -ne '1.2.3') { throw 'Bootstrap version was not honored.' }
        $script:fakeTime += 1200
        if ($script:installationFails) { throw 'Synthetic installation failure.' }
        return 'C:\Company Test\codex.exe'
    }
    function Post-Json {
        param([string]$Path, $Body)
        if ($Path -ne '/api/company-ai/device/start') { throw 'An unexpected credential-bearing operation was attempted.' }
        $script:enrollments++
        return [PSCustomObject]@{codex_enabled = $true; expires_at = $script:fakeTime + 600}
    }
    $prepared = Start-CompanyEnrollment $origin $temporary $true $true 'WIN' ('A' * 43) $null
    if ($prepared.executable -ne 'C:\Company Test\codex.exe' -or $prepared.started.expires_at -ne 1800 -or $script:enrollments -ne 1) { throw 'Slow installation consumed the approval window or lost the prepared CLI.' }
    $script:enrollments = 0
    $script:installationFails = $true
    $rejected = $false
    try { $null = Start-CompanyEnrollment $origin $temporary $true $true 'WIN' ('A' * 43) $null } catch { $rejected = $true }
    if (-not $rejected -or $script:enrollments) { throw 'Failed installation created an enrollment or requested a credential.' }
    $prepared = Start-CompanyEnrollment $origin $temporary $false $false 'WIN' ('A' * 43) $null
    if ($script:enrollments -ne 1 -or $prepared.executable) { throw 'Claude-only enrollment incorrectly required a Codex install.' }
    Write-Host 'PASS: Codex backend readiness, safe HTTP diagnostics, configuration, direct command, migration, personal settings, conflicts, rollback and installer isolation.'
} finally { Remove-Item -LiteralPath $temporary -Recurse -Force }
