# Windows PowerShell 5.1+. Run as the employee, never as another administrator.
# No execution-policy changes, scheduled tasks, background agents or refresh tokens.
[CmdletBinding()]
param(
    [string]$Portal = 'https://ai-dev.changdar-server.mooo.com',
    [switch]$InstallClaude,
    [switch]$InstallCodex,
    [ValidateSet('Claude', 'Codex', 'Both')]
    [string]$Tools = 'Both',
    [switch]$ReplaceProviderConfig,
    [switch]$RepairPathOnly,
    [switch]$RefreshModelsOnly,
    [string]$InstallerStatusPath,
    [string]$InstallerCancelPath
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$settingsChanged = $false
$acked = $false
$ackAttempted = $false
$previousEnvironment = @{}
$codexEnvironmentSnapshots = [ordered]@{}
$fileSnapshots = [ordered]@{}
$stage = 'initial checks'
$script:canCancel = $true
$script:approval = $null
if ($InstallerStatusPath) { [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false) }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Write-Atomic([string]$Path, [string]$Value) {
    Write-AtomicBytes $Path ((New-Object Text.UTF8Encoding($false)).GetBytes($Value))
}

function Write-AtomicBytes([string]$Path, [byte[]]$Value) {
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllBytes($temporary, $Value)
        # PowerShell converts plain $null to an empty string for a .NET string
        # parameter. File.Replace rejects an empty backup path; NullString
        # passes an actual null and keeps the replacement atomic on Windows 5.1.
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary, $Path, [System.Management.Automation.Language.NullString]::Value) }
        else { [IO.File]::Move($temporary, $Path) }
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}

function Assert-LocalAtomicWrites([string]$Directory) {
    $probe = Join-Path $Directory ([Guid]::NewGuid().ToString('N') + '.write-test')
    try {
        Write-AtomicBytes $probe ([byte[]]@(0, 128, 255))
        # Exercise the existing-file branch before approval or key delivery.
        $expected = [byte[]]@(255, 0, 129, 42)
        Write-AtomicBytes $probe $expected
        if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($probe)) -ne [Convert]::ToBase64String($expected)) {
            throw 'Local atomic file-write verification failed. No enrollment was created.'
        }
    } finally {
        if (Test-Path -LiteralPath $probe) { Remove-Item -LiteralPath $probe -Force }
    }
}

# Only public progress is written to the EXE's private per-run directory.
# Never write proofs, credentials, HTTP bodies or exception text here.
function Write-SetupStatus([string]$Kind = 'progress', [string]$Code = '', [int]$Remaining = -1) {
    if (-not $InstallerStatusPath) { return }
    $value = @{ kind = $Kind; code = $Code; stage = $script:stage; can_cancel = $script:canCancel; remaining = $Remaining }
    if ($script:approval) { $value.approval_url = $script:approval.url; $value.user_code = $script:approval.code }
    try { Write-Atomic $InstallerStatusPath ($value | ConvertTo-Json -Compress) }
    catch [IO.IOException] { } # Progress must not abort an activation transaction.
}

function Set-SetupStage([string]$Value) {
    $script:stage = $Value
    Write-SetupStatus
}

function Assert-SetupNotCancelled {
    if ($script:canCancel -and $InstallerCancelPath -and (Test-Path -LiteralPath $InstallerCancelPath)) {
        throw (New-Object OperationCanceledException('Setup cancelled before activation. Installed tools were preserved.'))
    }
}

function Get-SetupHttpStatus($Failure) {
    $exception = $Failure.Exception
    for ($depth = 0; $exception -and $depth -lt 8; $depth++) {
        if ($exception.PSObject.Properties['Response'] -and $exception.Response -and $exception.Response.PSObject.Properties['StatusCode']) {
            return [int]$exception.Response.StatusCode
        }
        $exception = $exception.InnerException
    }
    return 0
}

function Test-SetupTransientFailure($Failure) {
    $status = Get-SetupHttpStatus $Failure
    if ($status) { return $status -in @(408, 429, 500, 502, 503, 504) }
    $exception = $Failure.Exception
    for ($depth = 0; $exception -and $depth -lt 8; $depth++) {
        if ($exception -is [Net.WebException]) {
            return $exception.Status -in @([Net.WebExceptionStatus]::Timeout, [Net.WebExceptionStatus]::ConnectFailure,
                [Net.WebExceptionStatus]::ConnectionClosed, [Net.WebExceptionStatus]::ReceiveFailure,
                [Net.WebExceptionStatus]::SendFailure, [Net.WebExceptionStatus]::NameResolutionFailure)
        }
        if ($exception -is [TimeoutException] -or $exception -is [System.Threading.Tasks.TaskCanceledException]) { return $true }
        $exception = $exception.InnerException
    }
    return $false
}

function Invoke-SetupRetry([scriptblock]$Operation, [string]$Label) {
    # Use only for reads and proof-bound idempotent poll/ACK. Never retry start
    # or approval, and never turn an HTTP permission/policy error into a retry.
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        Assert-SetupNotCancelled
        try { return (& $Operation) }
        catch {
            if ($attempt -eq 3 -or -not (Test-SetupTransientFailure $_)) { throw }
            Write-Host ($Label + ': temporary connection failure; retry ' + $attempt + '/2.')
            Write-SetupStatus 'retry' 'TEMPORARY_NETWORK'
            for ($pause = 0; $pause -lt (2 * $attempt); $pause++) { Assert-SetupNotCancelled; Start-Sleep -Seconds 1 }
        }
    }
}

function Get-SetupFailureCode($Failure) {
    if ($Failure.Exception -is [OperationCanceledException]) { return 'CANCELLED' }
    if ($script:ackAttempted -and -not $script:acked) { return 'ACTIVATION_UNCERTAIN' }
    $status = Get-SetupHttpStatus $Failure
    if ($script:stage -eq 'browser approval and device polling') {
        if ($status -eq 410 -or [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -ge $script:deadline) { return 'APPROVAL_EXPIRED' }
        return 'APPROVAL_FAILED'
    }
    if ($script:stage -eq 'reading existing Claude configuration') { return 'PROVIDER_CONFIG' }
    if ($script:stage -eq 'verifying installed Codex CLI release') { return 'CODEX_VERSION' }
    if ($script:stage -eq 'company VPN/DNS check') { return 'COMPANY_NETWORK' }
    if ($script:stage -match 'Claude Code installation|official Codex CLI installer') { return 'TOOL_INSTALL' }
    if ($status -or (Test-SetupTransientFailure $Failure)) { return 'NETWORK_FAILED' }
    return 'SETUP_FAILED'
}

function Protect-Directory([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        if ((Get-Item -LiteralPath $Path).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'changdar-ai directory must not be a link.'
        }
    } else { [IO.Directory]::CreateDirectory($Path) | Out-Null }
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
    $acl.SetOwner($sid)
    # Persist only the modified access/owner sections. Set-Acl on Windows
    # PowerShell can also request SACL privileges ordinary employees do not have.
    ([IO.DirectoryInfo]$Path).SetAccessControl($acl)
}

function Assert-InferenceNetwork {
    Write-Host 'Checking company LAN/VPN access before creating an enrollment...'
    try {
        $null = Invoke-SetupRetry { Invoke-WebRequest -Uri ($Portal + '/company-ai/v1/models') `
            -UseBasicParsing -TimeoutSec 15 -MaximumRedirection 0 -ErrorAction Stop } 'Company network check'
    } catch [Net.WebException] {
        $response = $_.Exception.Response
        if ($null -ne $response) {
            $status = [int]$response.StatusCode
            $response.Close()
            # This probe deliberately has NO credential: 401 proves that the
            # private network gate passed and the API now requires a device key.
            if ($status -eq 401) { return }
            if ($status -eq 403) {
                throw 'changdar-ai network access denied. Connect the approved company VPN and verify private DNS. No enrollment was created.'
            }
        }
        throw 'changdar-ai private endpoint is unreachable. Check the company VPN, DNS and HTTPS connection. No enrollment was created.'
    }
    throw 'Unexpected unauthenticated model endpoint response. Contact IT; no enrollment was created.'
}

function Post-Json([string]$Path, $Body) {
    Invoke-RestMethod -Uri ($Portal + $Path) -Method Post -ContentType 'application/json' `
        -Body ($Body | ConvertTo-Json -Depth 30 -Compress) -TimeoutSec 40 -MaximumRedirection 0
}

function Set-Property($Object, [string]$Name, $Value) {
    $Object | Add-Member -MemberType NoteProperty -Name $Name -Value $Value -Force
}

function Set-CompanyModelSettings($Settings, [string[]]$Models, [string]$DefaultModel) {
    if (-not $Models.Count -or $Models -notcontains $DefaultModel -or (@($Models | Select-Object -Unique).Count -ne $Models.Count)) {
        throw 'Company model policy is invalid; settings were not saved.'
    }
    foreach ($model in $Models) {
        if ($model -notmatch '^[A-Za-z0-9._/-]{1,200}$') { throw 'Unexpected company model identifier.' }
    }
    if (-not $Settings.env) { Set-Property $Settings 'env' ([PSCustomObject]@{}) }
    if ($Settings.env -isnot [PSCustomObject]) { throw 'Claude env settings must be a JSON object.' }
    $names = @{ 'gemini-3.8-flash' = 'Gemini 3.8 Flash'; 'gemini-3.5-flash-lite' = 'Gemini 3.5 Flash Lite'; 'gemini-4-argon' = 'Gemini 4 Argon' }
    $options = @()
    foreach ($model in $Models) {
        $label = $model
        if ($names.ContainsKey($model)) { $label = $names[$model] }
        $options += @{ model = $model; label = $label }
    }
    Set-Property $Settings 'model' $DefaultModel
    Set-Property $Settings 'availableModels' @($Models)
    Set-Property $Settings 'modelPicker' @{ replaceBuiltInOptions = $true; options = $options }
    # A pinned ANTHROPIC_MODEL overrides saved /model choices on every launch.
    # Store a default instead, so employees can select and keep another model.
    $Settings.env.PSObject.Properties.Remove('ANTHROPIC_MODEL')
    Set-Property $Settings.env 'ANTHROPIC_DEFAULT_MODEL' $DefaultModel
    $tiers = @{ SONNET = $DefaultModel; OPUS = $DefaultModel; HAIKU = $DefaultModel }
    if ($Models -contains 'gemini-4-argon') { $tiers.OPUS = 'gemini-4-argon' }
    if ($Models -contains 'gemini-3.5-flash-lite') { $tiers.HAIKU = 'gemini-3.5-flash-lite' }
    foreach ($tier in $tiers.Keys) { Set-Property $Settings.env ('ANTHROPIC_DEFAULT_' + $tier + '_MODEL') $tiers[$tier] }
}

function Get-CompanyMcpPolicyValue($Envelope) {
    if ($null -ne $Envelope -and $null -ne $Envelope.PSObject.Properties['mcp']) { return $Envelope.mcp }
    return $null
}

function Assert-CompanyMcpPolicy($Policy) {
    if ($null -eq $Policy) { return }
    $endpoint = $null
    if (-not [Uri]::TryCreate([string]$Policy.url, [UriKind]::Absolute, [ref]$endpoint) -or
        $endpoint.Scheme -ne 'https' -or $endpoint.UserInfo -or $endpoint.Query -or $endpoint.Fragment -or
        $endpoint.AbsolutePath -cne '/company-mcp' -or [int]$Policy.version -ne 1 -or
        [string]$Policy.scope -cne 'company.resources' -or [string]$Policy.resource_mode -cne 'all_owned_bindings' -or
        [int]$Policy.codex_callback_port -ne 18431 -or [int]$Policy.claude_callback_port -ne 18432 -or
        [string]$Policy.codex_client_id -cnotmatch '^[A-Za-z0-9._-]{1,100}$' -or
        [string]$Policy.claude_client_id -cnotmatch '^[A-Za-z0-9._-]{1,100}$' -or
        [string]$Policy.codex_client_id -ceq [string]$Policy.claude_client_id) {
        throw 'Company MCP policy is invalid. No resource credentials were requested.'
    }
}

function Get-CompanyMcpCodexConfig($Policy) {
    if ($null -eq $Policy) { return '' }
    Assert-CompanyMcpPolicy $Policy
    $url = ConvertTo-TomlString $Policy.url
    $client = ConvertTo-TomlString $Policy.codex_client_id
    return @"

[mcp_servers.changdar_company]
url = $url
scopes = ["company.resources"]
startup_timeout_sec = 5
tool_timeout_sec = 120

[mcp_servers.changdar_company.oauth]
client_id = $client
callback_url = "http://127.0.0.1:18431/callback"
"@
}

function Write-CompanyMcpClaudeConfig($Policy) {
    Assert-CompanyMcpPolicy $Policy
    $path = Join-Path $env:USERPROFILE '.claude.json'
    if ($null -eq $Policy -and -not (Test-Path -LiteralPath $path)) { return }
    Save-FileSnapshot $path
    $settings = [pscustomobject]@{}
    if (Test-Path -LiteralPath $path) { $settings = [IO.File]::ReadAllText($path) | ConvertFrom-Json }
    if ($settings -isnot [pscustomobject]) { throw 'Claude user configuration must be a JSON object.' }
    if (-not $settings.mcpServers) { Set-Property $settings 'mcpServers' ([pscustomobject]@{}) }
    if ($settings.mcpServers -isnot [pscustomobject]) { throw 'Claude MCP configuration must be an object.' }
    if ($null -eq $Policy) { $settings.mcpServers.PSObject.Properties.Remove('changdar_company') }
    else {
        Set-Property $settings.mcpServers 'changdar_company' ([pscustomobject]@{
            type = 'http'; url = [string]$Policy.url
            oauth = [pscustomobject]@{ clientId = [string]$Policy.claude_client_id; callbackPort = 18432; scopes = 'company.resources' }
        })
    }
    Write-Atomic $path ($settings | ConvertTo-Json -Depth 100)
}

function Get-OtherMcpConfig([string]$Current) {
    # Preserve user-added MCP sections in the managed home. Company settings
    # remain generated, and the fixed company server is replaced by policy.
    $blocks = [regex]::Split($Current, '(?m)(?=^\s*\[[^\r\n]+\]\s*(?:#.*)?$)')
    return (($blocks | Where-Object {
        $_ -match '^\s*\[mcp_servers\.' -and $_ -notmatch '^\s*\[mcp_servers\.changdar_company(?:\.|\])'
    }) -join "`n")
}

function Update-InstalledCompanyModels([string]$Origin) {
    $originUri = [Uri]$Origin
    $root = Join-Path $env:LOCALAPPDATA 'CompanyAI'
    $stateDirectory = Join-Path $root $originUri.Host
    $claudeDirectory = Join-Path $env:USERPROFILE '.claude'
    $deviceFile = Join-Path $stateDirectory 'device.json'
    $credentialFile = Join-Path $stateDirectory 'credential.dpapi'
    $helperFile = Join-Path $stateDirectory 'Get-CompanyAIKey.ps1'
    $settingsFile = Join-Path $claudeDirectory 'settings.json'
    $backupFile = Join-Path $stateDirectory 'models.backup.dpapi'
    foreach ($path in @($root, $stateDirectory, $claudeDirectory, $deviceFile, $credentialFile, $helperFile, $settingsFile)) {
        if (-not (Test-Path -LiteralPath $path)) { throw 'Existing company setup is incomplete. Run normal setup first.' }
        if ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Company settings and state must not be links.' }
    }
    if ((Test-Path -LiteralPath $backupFile) -and ((Get-Item -LiteralPath $backupFile).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Company model settings backup must not be a link.'
    }
    $device = [IO.File]::ReadAllText($deviceFile) | ConvertFrom-Json
    if ($device.portal -ne $Origin -or [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -ge [long]$device.confirmation_due_at) {
        throw 'Company setup belongs to another portal or browser confirmation is due. Run normal setup.'
    }
    $previous = [IO.File]::ReadAllText($settingsFile)
    $settings = $previous | ConvertFrom-Json
    $expectedHelper = 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy AllSigned -File "' + $helperFile + '"'
    $legacyHelper = $expectedHelper.Replace(' -ExecutionPolicy AllSigned', '')
    if ($settings.apiKeyHelper -notin @($expectedHelper, $legacyHelper) -or $settings.env.ANTHROPIC_BASE_URL -ne ($Origin + '/company-ai') `
        -or $settings.env.ANTHROPIC_API_KEY -or $settings.env.ANTHROPIC_AUTH_TOKEN) {
        throw 'Existing provider configuration is not the installed company configuration. It was preserved.'
    }
    $secure = ConvertTo-SecureString ([IO.File]::ReadAllText($credentialFile))
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $raw = $null
    try {
        $raw = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        $catalog = Invoke-RestMethod -Uri ($Origin + '/company-ai/v1/models') `
            -Headers @{ Authorization = ('Bearer ' + $raw) } -TimeoutSec 30 -MaximumRedirection 0
    } finally {
        $raw = $null
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $secure.Dispose()
    }
    $models = @($catalog.data | ForEach-Object { $_.id })
    if (-not $models.Count) { throw 'No company models are available. Existing settings were preserved.' }
    Write-CompanyMcpClaudeConfig (Get-CompanyMcpPolicyValue $catalog)
    Set-CompanyModelSettings $settings $models $models[0]
    $backup = ConvertTo-SecureString $previous -AsPlainText -Force
    try { Write-Atomic $backupFile (ConvertFrom-SecureString $backup) }
    finally { $backup.Dispose() }
    Write-Atomic $settingsFile ($settings | ConvertTo-Json -Depth 100)
    Write-Host ('Company model settings updated. Default: ' + $models[0])
    Write-Host ('Available models: ' + ($models -join ', '))
    Write-Host 'No device enrollment or credential change was performed. Restart Claude Code and use /model to choose a model.'
}

function Get-PathWithDirectory([AllowEmptyString()][string]$PathValue, [string]$Directory) {
    $target = $Directory.TrimEnd('\', '/')
    foreach ($entry in ($PathValue -split ';')) {
        $expanded = [Environment]::ExpandEnvironmentVariables($entry.Trim().Trim('"')).Replace('/', '\').TrimEnd('\')
        if ([StringComparer]::OrdinalIgnoreCase.Equals($expanded, $target)) { return $PathValue }
    }
    if (-not $PathValue) { return $Directory }
    if ($PathValue.EndsWith(';')) { return $PathValue + $Directory }
    return $PathValue + ';' + $Directory
}

function Get-PreferredPathWithDirectory([AllowEmptyString()][string]$PathValue, [string]$Directory) {
    $target = [Environment]::ExpandEnvironmentVariables($Directory).Replace('/', '\').TrimEnd('\')
    $kept = @()
    if ($PathValue) {
        foreach ($entry in ($PathValue -split ';')) {
            $expanded = [Environment]::ExpandEnvironmentVariables($entry.Trim().Trim('"')).Replace('/', '\').TrimEnd('\')
            if (-not [StringComparer]::OrdinalIgnoreCase.Equals($expanded, $target)) { $kept += $entry }
        }
    }
    if (-not $kept.Count) { return $Directory }
    return $Directory + ';' + ($kept -join ';')
}

function Send-EnvironmentChange {
    if (-not ('CompanyAI.EnvironmentNotification' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace CompanyAI {
    public static class EnvironmentNotification {
        [DllImport("user32.dll", EntryPoint = "SendMessageTimeoutW", CharSet = CharSet.Unicode, ExactSpelling = true)]
        public static extern IntPtr SendMessageTimeout(IntPtr window, uint message, UIntPtr wParam,
            string lParam, uint flags, uint timeout, out UIntPtr result);
    }
}
'@
    }
    $result = [UIntPtr]::Zero
    $sent = [CompanyAI.EnvironmentNotification]::SendMessageTimeout([IntPtr]0xffff, 0x001a,
        [UIntPtr]::Zero, 'Environment', 2, 2000, [ref]$result)
    if ($sent -eq [IntPtr]::Zero) { throw 'Environment notification timed out.' }
}

function Update-UserPathKey([Microsoft.Win32.RegistryKey]$Key, [string]$Directory, [bool]$Prefer = $false) {
    $exists = $Key.GetValueNames() -contains 'Path'
    $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
    if ($exists) { $kind = $Key.GetValueKind('Path') }
    if ($kind -notin @([Microsoft.Win32.RegistryValueKind]::String, [Microsoft.Win32.RegistryValueKind]::ExpandString)) {
        throw 'User PATH is not a string. Contact IT; it was not changed.'
    }
    $before = [string]$Key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    $after = Get-PathWithDirectory $before $Directory
    if ($Prefer) { $after = Get-PreferredPathWithDirectory $before $Directory }
    if ($after -cne $before) { $Key.SetValue('Path', $after, $kind) }
}

function Enable-ClaudeNativePath {
    $bin = Join-Path $env:USERPROFILE '.local\bin'
    if (-not (Test-Path -LiteralPath (Join-Path $bin 'claude.exe') -PathType Leaf)) {
        throw 'Native Claude Code was not found in this Windows account. Install it first; PATH was not changed.'
    }
    # Read the unexpanded value and preserve REG_SZ/REG_EXPAND_SZ. Using setx
    # or merging the full process PATH could truncate/copy machine entries.
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    try { Update-UserPathKey $key $bin }
    finally { $key.Dispose() }
    # Also make claude available in this PowerShell process immediately.
    $env:Path = Get-PathWithDirectory ([string]$env:Path) $bin
    try { Send-EnvironmentChange }
    catch { Write-Warning 'User PATH was saved. Fully close and reopen Windows Terminal if new tabs still use the old PATH.' }
    Write-Host 'Claude Code user PATH is configured. Existing entries and the system PATH were preserved.'
}

function Get-CompanyTools([string]$Selection) {
    if ($Selection -eq 'Both') { return @('Claude', 'Codex') }
    return @($Selection)
}

function Get-CompanyToolPlan([string]$Selection, [System.Collections.IDictionary]$Parameters) {
    # Retain legacy explicit install switches; a plain setup now selects both.
    if ($Parameters.Keys -notcontains 'Tools' -and ($Parameters['InstallClaude'] -or $Parameters['InstallCodex'])) {
        if ($Parameters['InstallClaude'] -and $Parameters['InstallCodex']) { $Selection = 'Both' }
        elseif ($Parameters['InstallCodex']) { $Selection = 'Codex' }
        else { $Selection = 'Claude' }
    }
    $tools = @(Get-CompanyTools $Selection)
    if (($Parameters['InstallClaude'] -and $tools -notcontains 'Claude') -or
        ($Parameters['InstallCodex'] -and $tools -notcontains 'Codex')) {
        throw 'Installation switches must match -Tools Claude, Codex or Both.'
    }
    $maintenance = $Parameters['RepairPathOnly'] -or $Parameters['RefreshModelsOnly']
    return [PSCustomObject]@{
        tools = $tools
        install_claude = (-not $maintenance -and $tools -contains 'Claude' -and
            ($Parameters.Keys -notcontains 'InstallClaude' -or [bool]$Parameters['InstallClaude']))
        install_codex = (-not $maintenance -and $tools -contains 'Codex' -and
            ($Parameters.Keys -notcontains 'InstallCodex' -or [bool]$Parameters['InstallCodex']))
    }
}

function Find-CompanyClaude {
    $command = Get-Command claude -CommandType Application, ExternalScript -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    $native = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
    if (Test-Path -LiteralPath $native -PathType Leaf) { return $native }
    return $null
}

function Install-CompanyClaude([string]$StateDirectory, [bool]$Allowed) {
    Set-SetupStage 'Claude Code installation'
    $executable = Find-CompanyClaude
    if ($executable) {
        Write-Host 'Claude Code is already installed. Skipping download and installation.'
    } else {
        if (-not $Allowed) { throw 'Install Claude Code first, or re-run with -InstallClaude.' }
        $installer = Join-Path $StateDirectory ([Guid]::NewGuid().ToString('N') + '.install-claude.ps1')
        try {
            Invoke-SetupRetry {
                Invoke-WebRequest 'https://claude.ai/install.ps1' -UseBasicParsing -OutFile $installer -TimeoutSec 60 -ErrorAction Stop
            } 'Claude installer download' | Out-Null
            Assert-SetupNotCancelled
            & powershell.exe -NoProfile -NonInteractive -File $installer | Out-Host
            if ($LASTEXITCODE -ne 0) { throw 'Official Claude Code installation failed.' }
        } finally { Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue }
        $executable = Find-CompanyClaude
        if (-not $executable) { throw 'Claude Code installation could not be verified.' }
    }
    if (Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.local\bin\claude.exe') -PathType Leaf) { Enable-ClaudeNativePath }
    return $executable
}

function Save-FileSnapshot([string]$Path) {
    if ($script:fileSnapshots.Contains($Path)) { return }
    $before = $null
    if (Test-Path -LiteralPath $Path) {
        if ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Company configuration files must not be links.' }
        $before = [IO.File]::ReadAllBytes($Path)
    }
    $script:fileSnapshots[$Path] = $before
}

function Restore-FileSnapshots {
    $failed = $false
    foreach ($path in $script:fileSnapshots.Keys) {
        try {
            if ($null -eq $script:fileSnapshots[$path]) {
                if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
            } else { Write-AtomicBytes $path $script:fileSnapshots[$path] }
        } catch { $failed = $true }
    }
    if ($failed) { throw 'Some configuration files could not be restored. Contact IT.' }
}

function Enable-CompanyPath([string]$Directory, [switch]$Prefer) {
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    try { Update-UserPathKey $key $Directory ([bool]$Prefer) }
    finally { $key.Dispose() }
    if ($Prefer) { $env:Path = Get-PreferredPathWithDirectory ([string]$env:Path) $Directory }
    else { $env:Path = Get-PathWithDirectory ([string]$env:Path) $Directory }
    try { Send-EnvironmentChange }
    catch { Write-Warning 'User PATH was saved. Fully close and reopen Windows Terminal to refresh it.' }
}

function Get-CodexEnvironmentValue([string]$Target) {
    return [Environment]::GetEnvironmentVariable('CODEX_HOME', $Target)
}

function Set-CodexEnvironmentValue([AllowNull()][string]$Value, [string]$Target) {
    if ($null -eq $Value -or $Value -eq '') {
        [Environment]::SetEnvironmentVariable('CODEX_HOME', [System.Management.Automation.Language.NullString]::Value, $Target)
    } else { [Environment]::SetEnvironmentVariable('CODEX_HOME', $Value, $Target) }
}

function Restore-CodexEnvironment {
    $failed = $false
    foreach ($target in $script:codexEnvironmentSnapshots.Keys) {
        try { Set-CodexEnvironmentValue $script:codexEnvironmentSnapshots[$target] $target }
        catch { $failed = $true }
    }
    if ($script:codexEnvironmentSnapshots.Count) {
        try { Send-EnvironmentChange } catch { Write-Warning 'Reopen Windows Terminal to reload the restored Codex environment.' }
    }
    if ($failed) { throw 'Some Codex environment settings could not be restored. Contact IT.' }
}

function Enable-CompanyCodexCommand([string]$StateDirectory, [string]$Executable) {
    $homeDirectory = Join-Path $StateDirectory 'codex'
    foreach ($path in @($Executable, (Join-Path $homeDirectory 'config.toml'), (Join-Path $homeDirectory 'models.json'))) {
        if (-not [IO.Path]::IsPathRooted($path) -or -not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw 'changdar-ai Codex installation is incomplete. Run normal setup first.'
        }
    }
    # The Windows entry point selects the company home on every invocation,
    # including terminals opened before installation. The signed launcher also
    # gives managed defaults precedence over a trusted ~/.codex project layer.
    Enable-CompanyPath (Split-Path $Executable -Parent)
    $expectedCommand = $Executable
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        $binDirectory = Join-Path $StateDirectory 'bin'
        Protect-Directory $binDirectory
        $expectedCommand = Join-Path $binDirectory 'codex.cmd'
        Save-FileSnapshot $expectedCommand
        $launcher = Join-Path $StateDirectory 'Start-CompanyCodex.ps1'
        if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw 'Company Codex launcher is missing. Run setup again.' }
        Write-Atomic $expectedCommand "@echo off`r`n`"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`" -NoProfile -NonInteractive -ExecutionPolicy AllSigned -File `"%~dp0..\Start-CompanyCodex.ps1`" %*`r`nexit /b %ERRORLEVEL%`r`n"
        Enable-CompanyPath $binDirectory -Prefer
    }
    $command = Get-Command codex -ErrorAction SilentlyContinue
    if (-not $command -or $command.CommandType -notin @('Application', 'ExternalScript') -or
        -not [StringComparer]::OrdinalIgnoreCase.Equals($command.Source, $expectedCommand)) {
        throw 'Another codex command takes precedence. Remove the conflicting shell alias or PATH entry, then run -Tools Codex -RepairPathOnly.'
    }
    # Older installations can migrate through -RepairPathOnly without a new
    # enrollment. Retain their original home before selecting the company one.
    $installFile = Join-Path $StateDirectory 'codex-install.json'
    if (Test-Path -LiteralPath $installFile) {
        $installed = [IO.File]::ReadAllText($installFile) | ConvertFrom-Json
        if ($installed.PSObject.Properties.Name -notcontains 'previous_user_home') {
            Save-FileSnapshot $installFile
            Set-Property $installed 'previous_user_home' (Get-CodexEnvironmentValue 'User')
            Write-Atomic $installFile ($installed | ConvertTo-Json)
        }
    }
    foreach ($target in @('User', 'Process')) {
        if (-not $script:codexEnvironmentSnapshots.Contains($target)) {
            $script:codexEnvironmentSnapshots[$target] = Get-CodexEnvironmentValue $target
        }
        Set-CodexEnvironmentValue $homeDirectory $target
    }
    try { Send-EnvironmentChange }
    catch { Write-Warning 'changdar-ai Codex settings were saved. Fully close and reopen Windows Terminal to refresh them.' }
}

function Assert-CodexPolicy($Policy, [string]$Origin) {
    $models = @($Policy.models)
    if (-not $models.Count -or $models -notcontains $Policy.default_model -or (@($models | Select-Object -Unique).Count -ne $models.Count)) {
        throw 'Invalid company Codex model policy.'
    }
    foreach ($model in $models) {
        if ($model -notmatch '^[A-Za-z0-9._/-]{1,200}$') { throw 'Unexpected company Codex model identifier.' }
    }
    if ($Policy.base_url -ne ($Origin + '/company-ai/v1') -or $Policy.release -notmatch '^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$') {
        throw 'Unexpected Codex gateway or CLI release.'
    }
    $slugs = @($Policy.catalog.models | ForEach-Object { $_.slug })
    if ($slugs.Count -ne $models.Count -or ($slugs -join ',') -cne ($models -join ',')) { throw 'Codex catalog does not match the ordered company model policy.' }
    if ([Text.Encoding]::UTF8.GetByteCount(($Policy.catalog | ConvertTo-Json -Depth 100 -Compress)) -gt 1MB) {
        throw 'Codex catalog exceeds 1 MiB.'
    }
}

function Assert-CompanyCodexAvailability($Started) {
    if ($Started.PSObject.Properties.Name -notcontains 'codex_enabled') {
        throw 'The company backend is older than this Codex setup script. IT must deploy the Codex backend and all three PS1 downloads together. No browser approval or device credential was requested.'
    }
    if ($Started.codex_enabled -isnot [bool] -or -not $Started.codex_enabled) {
        throw 'changdar-ai Codex is not enabled on the server. IT must configure AI_ACCESS_CODEX_RELEASE and AI_ACCESS_CODEX_CATALOG and restart the access service. No browser approval or device credential was requested.'
    }
}

function Get-CompanyCodexBootstrapPolicy([string]$Origin) {
    try {
        $policy = Invoke-SetupRetry { Invoke-RestMethod -Uri ($Origin + '/company-ai/v1/codex/install-policy') -TimeoutSec 20 -MaximumRedirection 0 } 'Codex installation policy'
    } catch {
        if ($_.Exception -is [OperationCanceledException]) { throw }
        throw 'Could not retrieve the company installation policy. Check company LAN/VPN and the backend deployment before enrolling again.' }
    if ($policy.allowed -isnot [bool] -or -not $policy.allowed) { throw 'Company installation policy did not confirm LAN/VPN access.' }
    Assert-CompanyCodexAvailability $policy
    if ($policy.codex_release -notmatch '^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$') {
        throw 'The company backend did not return an approved Codex CLI version. IT must update the backend and PS1 together.'
    }
    return $policy
}

function Prepare-CompanyCodex([string]$Origin, [string]$StateDirectory, [bool]$Allowed) {
    # Complete large downloads before starting the 10-minute approval window
    # or the 5-minute key-delivery/ACK window. No enrollment proof/key exists yet.
    Set-SetupStage 'checking Codex installation policy before enrollment'
    $policy = Get-CompanyCodexBootstrapPolicy $Origin
    return Install-CompanyCodex $StateDirectory $policy.codex_release $Allowed
}

function Start-CompanyEnrollment([string]$Origin, [string]$StateDirectory, [bool]$Codex,
    [bool]$InstallAllowed, [string]$ComputerName, [string]$Challenge, $ReplaceId) {
    $executable = $null
    if ($Codex) {
        Write-Host 'Preparing Codex before browser approval. Large downloads do not consume the activation window.'
        $executable = Prepare-CompanyCodex $Origin $StateDirectory $InstallAllowed
    }
    Set-SetupStage 'creating device enrollment'
    Assert-SetupNotCancelled
    $started = Post-Json '/api/company-ai/device/start' @{
        device_name = $ComputerName; code_challenge = $Challenge; replace_device_id = $ReplaceId
    }
    if ($Codex) {
        Set-SetupStage 'checking Codex backend availability before browser approval'
        Assert-CompanyCodexAvailability $started
    }
    return @{ started = $started; executable = $executable }
}

function Get-CodexPolicyFailureMessage([int]$Status, [string]$Code) {
    # Never include raw response bodies, exception details or server strings.
    if ($Status -eq 503 -and $Code -ceq 'ai_access_codex_not_enabled') {
        return 'changdar-ai Codex is not enabled on the server. IT must configure AI_ACCESS_CODEX_RELEASE and AI_ACCESS_CODEX_CATALOG and restart the access service.'
    }
    if ($Status -eq 403 -and $Code -ceq 'ai_access_codex_enrollment_required') {
        return 'This device was approved without Codex access. After IT enables Codex, run normal setup and browser approval again.'
    }
    if ($Status -eq 404) { return 'The company Codex configuration endpoint was not found (HTTP 404). IT must update the backend and its reverse-proxy route together.' }
    if ($Status -eq 401) { return 'The device credential was rejected by the Codex endpoint (HTTP 401). Contact IT to check backend deployment consistency before enrolling again.' }
    if ($Status -eq 403) { return 'The Codex endpoint denied access (HTTP 403). Check company LAN/VPN and have IT verify the endpoint network and device policies.' }
    if ($Status -eq 503) { return 'The Codex endpoint is unavailable (HTTP 503). IT must check the access service and its dependencies.' }
    return 'Could not retrieve the company Codex policy. Check company VPN access and ask IT to verify the backend and reverse-proxy deployment.'
}

function Get-CompanyCodexPolicy([string]$Origin, $Headers) {
    try {
        $policy = Invoke-SetupRetry { Invoke-RestMethod -Uri ($Origin + '/company-ai/v1/codex/config') -Headers $Headers -TimeoutSec 20 -MaximumRedirection 0 } 'Codex model policy'
    } catch {
        if ($_.Exception -is [OperationCanceledException]) { throw }
        $failure = $_
        $status = 0
        $code = ''
        try { if ($failure.Exception.Response) { $status = [int]$failure.Exception.Response.StatusCode } } catch { }
        try { $code = [string](($failure.ErrorDetails.Message | ConvertFrom-Json).detail.code) } catch { }
        throw (Get-CodexPolicyFailureMessage $status $code)
    }
    try { Assert-CodexPolicy $policy $Origin }
    catch { throw 'The company Codex policy is invalid. IT must verify the approved release, company gateway URL and ordered three-model catalog.' }
    return $policy
}

function ConvertTo-TomlString([string]$Value) {
    # JSON basic string escaping is also valid for these TOML strings/paths.
    return ConvertTo-Json -InputObject $Value -Compress
}

function New-CompanyCodexConfig($Policy, [string]$Helper, [string]$Catalog, [string]$PowerShell) {
    $model = ConvertTo-TomlString $Policy.default_model
    $base = ConvertTo-TomlString $Policy.base_url
    $catalogValue = ConvertTo-TomlString $Catalog
    $command = ConvertTo-TomlString $PowerShell
    $helperValue = ConvertTo-TomlString $Helper
    $mcp = Get-CompanyMcpCodexConfig (Get-CompanyMcpPolicyValue $Policy)
    $mcpCallback = ''
    if ($mcp) { $mcpCallback = 'mcp_oauth_callback_port = 18431' }
    $generated = @"
# Generated by changdar-ai setup. Personal Codex configuration is preserved.
model = $model
model_provider = "company_ai"
model_catalog_json = $catalogValue
web_search = "disabled"
approval_policy = "on-request"
sandbox_mode = "workspace-write"
$mcpCallback

# The managed Windows home may exceed the daemon's Unix socket path limit.
# Keep native `codex` usable without relocating credentials or session history.
[features]
daemon_auto_start = false

[model_providers.company_ai]
name = "changdar-ai"
base_url = $base
wire_api = "responses"
supports_websockets = false

[model_providers.company_ai.auth]
command = $command
args = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "AllSigned", "-File", $helperValue]
timeout_ms = 10000
refresh_interval_ms = 300000
"@
    return $generated + "`n" + $mcp
}

function Find-CompanyCodex {
    $command = Get-Command codex -All -CommandType Application, ExternalScript -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notmatch '[\\/]CompanyAI[\\/][^\\/]+[\\/]bin[\\/]codex\.cmd$' } | Select-Object -First 1
    if ($command) { return $command.Source }
    $native = Join-Path $env:LOCALAPPDATA 'Programs\OpenAI\Codex\bin\codex.exe'
    if (Test-Path -LiteralPath $native -PathType Leaf) { return $native }
    return $null
}

function Confirm-CodexRelease([string]$Executable, [string]$Release) {
    $version = & $Executable --version 2>$null
    if ($LASTEXITCODE -ne 0 -or ([string]$version).Trim() -ne ('codex-cli ' + $Release)) {
        throw 'Installed Codex does not match the company-approved CLI release. Contact IT to update it; existing installations were preserved.'
    }
}

function Assert-OfficialCodexInstallerUri([Uri]$Uri) {
    if (-not $Uri.IsAbsoluteUri -or $Uri.Scheme -cne 'https' -or $Uri.Port -ne 443 -or
        @('chatgpt.com', 'releases.openai.com') -cnotcontains $Uri.Host -or
        $Uri.AbsolutePath -cne '/codex/install.ps1' -or $Uri.UserInfo -or $Uri.Query -or $Uri.Fragment) {
        throw 'Official Codex installer redirected to an unapproved address. Contact IT; no installer was executed.'
    }
}

function Get-OfficialCodexInstallerHop([Uri]$Uri) {
    # Download without any device/browser credential. Follow redirects only in
    # Save-OfficialCodexInstaller, after checking every destination explicitly.
    $request = [Net.HttpWebRequest][Net.WebRequest]::Create($Uri)
    $request.AllowAutoRedirect = $false
    $request.UseDefaultCredentials = $false
    $request.Timeout = 120000
    $request.ReadWriteTimeout = 120000
    $request.UserAgent = 'CompanyAI-Windows-Setup'
    $response = $null
    $stream = $null
    $buffer = $null
    try {
        $response = $request.GetResponse()
        $status = [int]$response.StatusCode
        if ($status -in @(301, 302, 303, 307, 308)) {
            return @{ status = $status; location = $response.Headers['Location'] }
        }
        if ($status -ne 200 -or $response.ContentLength -gt 1MB) { throw 'Unexpected installer response.' }
        $stream = $response.GetResponseStream()
        $buffer = New-Object IO.MemoryStream
        $chunk = New-Object byte[] 8192
        while (($count = $stream.Read($chunk, 0, $chunk.Length)) -gt 0) {
            if ($buffer.Length + $count -gt 1MB) { throw 'Installer exceeds the download limit.' }
            $buffer.Write($chunk, 0, $count)
        }
        return @{ status = 200; bytes = $buffer.ToArray() }
    } catch {
        throw (New-Object InvalidOperationException('Could not download the official Codex installer. Check HTTPS/proxy access to chatgpt.com and releases.openai.com. No installer was executed.', $_.Exception))
    } finally {
        if ($buffer) { $buffer.Dispose() }
        if ($stream) { $stream.Dispose() }
        if ($response) { $response.Close() }
    }
}

function Save-OfficialCodexInstaller([string]$Path) {
    $uri = [Uri]'https://chatgpt.com/codex/install.ps1'
    for ($redirects = 0; $redirects -le 3; $redirects++) {
        Assert-OfficialCodexInstallerUri $uri
        $hop = Get-OfficialCodexInstallerHop $uri
        if ($hop.status -eq 200) {
            if ($hop.bytes -isnot [byte[]] -or -not $hop.bytes.Length -or $hop.bytes.Length -gt 1MB) {
                throw 'The official Codex installer download is empty or too large. No installer was executed.'
            }
            Write-AtomicBytes $Path $hop.bytes
            return
        }
        if ($hop.status -notin @(301, 302, 303, 307, 308) -or -not $hop.location) {
            throw 'The official Codex installer returned an unexpected response. No installer was executed.'
        }
        if ($redirects -eq 3) { throw 'The official Codex installer exceeded three redirects. No installer was executed.' }
        $next = $null
        if (-not [Uri]::TryCreate($uri, [string]$hop.location, [ref]$next)) {
            throw 'The official Codex installer returned an invalid redirect. No installer was executed.'
        }
        $uri = $next
    }
}

function Install-CompanyCodex([string]$StateDirectory, [string]$Release, [bool]$Allowed) {
    Set-SetupStage 'locating existing Codex CLI'
    $executable = Find-CompanyCodex
    if ($executable) { Write-Host 'Codex CLI is already installed. Skipping download and installation; checking the approved version.' }
    if (-not $executable) {
        if (-not $Allowed) { throw 'Install the approved Codex CLI first, or re-run with -InstallCodex.' }
        $installer = Join-Path $StateDirectory ([Guid]::NewGuid().ToString('N') + '.codex-install.ps1')
        $previousNonInteractive = [Environment]::GetEnvironmentVariable('CODEX_NON_INTERACTIVE', 'Process')
        try {
            Set-SetupStage 'downloading official Codex CLI installer'
            Invoke-SetupRetry { Save-OfficialCodexInstaller $installer } 'Codex installer download'
            Assert-SetupNotCancelled
            $env:CODEX_NON_INTERACTIVE = '1'
            Set-SetupStage 'running official Codex CLI installer'
            & powershell.exe -NoProfile -NonInteractive -File $installer -Release $Release | Out-Host
            if ($LASTEXITCODE -ne 0) { throw 'Official Codex CLI installation failed.' }
        } finally {
            if ($null -eq $previousNonInteractive) {
                [Environment]::SetEnvironmentVariable('CODEX_NON_INTERACTIVE', [System.Management.Automation.Language.NullString]::Value, 'Process')
            } else { [Environment]::SetEnvironmentVariable('CODEX_NON_INTERACTIVE', $previousNonInteractive, 'Process') }
            Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
        }
        Set-SetupStage 'locating newly installed Codex CLI'
        $executable = Find-CompanyCodex
        if (-not $executable) { throw 'Codex CLI installation could not be verified.' }
    }
    Set-SetupStage 'verifying installed Codex CLI release'
    Confirm-CodexRelease $executable $Release
    return $executable
}

function Write-CompanyCodexSettings($Policy, [string]$StateDirectory, [string]$Helper, [string]$Executable) {
    $homeDirectory = Join-Path $StateDirectory 'codex'
    $binDirectory = Join-Path $StateDirectory 'bin'
    Protect-Directory $homeDirectory
    Protect-Directory $binDirectory
    $configFile = Join-Path $homeDirectory 'config.toml'
    $catalogFile = Join-Path $homeDirectory 'models.json'
    $installFile = Join-Path $StateDirectory 'codex-install.json'
    $wrapperFile = Join-Path $binDirectory 'company-codex.cmd'
    Save-FileSnapshot (Join-Path $binDirectory 'codex.cmd')
    foreach ($path in @($configFile, $catalogFile, $installFile, $wrapperFile)) { Save-FileSnapshot $path }
    # Preserve the original user home across upgrades and model refreshes. It
    # contains only a path; personal configuration files are never modified.
    $previousUserHome = Get-CodexEnvironmentValue 'User'
    if (Test-Path -LiteralPath $installFile) {
        $installed = [IO.File]::ReadAllText($installFile) | ConvertFrom-Json
        if ($installed.PSObject.Properties.Name -contains 'previous_user_home') { $previousUserHome = $installed.previous_user_home }
    }
    $shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    # Put the catalog in place before a client can load its configuration.
    Write-Atomic $catalogFile ($Policy.catalog | ConvertTo-Json -Depth 100)
    $otherMcp = ''
    if (Test-Path -LiteralPath $configFile) { $otherMcp = Get-OtherMcpConfig ([IO.File]::ReadAllText($configFile)) }
    Write-Atomic $configFile ((New-CompanyCodexConfig $Policy $Helper $catalogFile $shell) + "`n" + $otherMcp)
    Write-Atomic $installFile (@{ executable = $Executable; release = $Policy.release; previous_user_home = $previousUserHome; models = @($Policy.models); default_model = $Policy.default_model } | ConvertTo-Json)
    Write-Atomic $wrapperFile "@echo off`r`n`"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`" -NoProfile -NonInteractive -ExecutionPolicy AllSigned -File `"%~dp0..\Start-CompanyCodex.ps1`" %*`r`nexit /b %ERRORLEVEL%`r`n"
}

function Sync-CompanyCodexLauncher([string]$Origin, [string]$StateDirectory) {
    $target = Join-Path $StateDirectory 'Start-CompanyCodex.ps1'
    $download = Join-Path $StateDirectory ([Guid]::NewGuid().ToString('N') + '.launcher-download')
    Save-FileSnapshot $target
    try {
        Invoke-SetupRetry { Invoke-WebRequest ($Origin + '/company-ai/downloads/Start-CompanyCodex.ps1') -UseBasicParsing -TimeoutSec 20 -MaximumRedirection 0 -OutFile $download } 'Company Codex launcher download' | Out-Null
        Write-AtomicBytes $target ([IO.File]::ReadAllBytes($download))
    } finally { Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue }
}

function Update-InstalledCodexModels([string]$Origin, [string]$StateDirectory) {
    $deviceFile = Join-Path $StateDirectory 'device.json'
    $helperFile = Join-Path $StateDirectory 'Get-CompanyAIKey.ps1'
    foreach ($path in @((Split-Path $StateDirectory -Parent), $StateDirectory, $deviceFile, $helperFile, (Join-Path $StateDirectory 'codex-install.json'), (Join-Path $StateDirectory 'Start-CompanyCodex.ps1'))) {
        if (-not (Test-Path -LiteralPath $path) -or ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Existing company Codex setup is incomplete or uses links. Run normal setup first.'
        }
    }
    $device = [IO.File]::ReadAllText($deviceFile) | ConvertFrom-Json
    if ($device.portal -ne $Origin -or [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -ge [long]$device.confirmation_due_at) { throw 'Run normal company setup to renew browser confirmation.' }
    $token = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy AllSigned -File $helperFile
    if ($LASTEXITCODE -ne 0 -or -not $token) { throw 'Company credential is unavailable.' }
    try {
        $policy = Get-CompanyCodexPolicy $Origin @{ Authorization = ('Bearer ' + $token) }
    } finally { $token = $null }
    $installed = [IO.File]::ReadAllText((Join-Path $StateDirectory 'codex-install.json')) | ConvertFrom-Json
    Confirm-CodexRelease $installed.executable $policy.release
    Write-CompanyCodexSettings $policy $StateDirectory $helperFile $installed.executable
    Sync-CompanyCodexLauncher $Origin $StateDirectory
    Enable-CompanyCodexCommand $StateDirectory $installed.executable
    Write-Host ('changdar-ai Codex models updated: ' + ($policy.models -join ', ') + '. Open a new terminal and run codex.')
}

function Show-ApprovalInstructions([Uri]$Verification, [string]$Code) {
    Write-Host ''
    Write-Host ('Verify this code in your browser: ' + $Code)
    Write-Host 'Open this approval URL (click if supported, or copy into your browser):'
    Write-Host $Verification.AbsoluteUri
    Write-Host 'Sign in to the company AI portal and approve only if the code matches.'
    Write-Host 'Opening the page alone does not approve this computer. After signing in, click the approval button beside the matching code.'
    try { Start-Process -FilePath $Verification.AbsoluteUri -ErrorAction Stop }
    catch { Write-Warning 'Could not open the browser automatically. Open the approval URL above to continue.' }
    Write-Host 'Waiting for browser approval (up to 10 minutes). Keep this window open.'
}

try {
    $uri = [Uri]$Portal
    $hosts = @('ai-dev.changdar-server.mooo.com', 'ai.changdar-server.mooo.com', 'changdar-server.mooo.com')
    if ($uri.Scheme -ne 'https' -or $uri.Port -ne 443 -or $hosts -notcontains $uri.Host `
        -or $uri.AbsolutePath -ne '/' -or $uri.Query -or $uri.Fragment -or $uri.UserInfo) {
        throw 'Use an approved company HTTPS portal origin.'
    }
    $Portal = $uri.GetLeftPart([UriPartial]::Authority)
    $admin = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if ($admin.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run setup in a normal employee PowerShell window, not an elevated administrator window.'
    }
    $toolPlan = Get-CompanyToolPlan $Tools $PSBoundParameters
    $selected = @($toolPlan.tools)
    $stateRoot = Join-Path $env:LOCALAPPDATA 'CompanyAI'
    $state = Join-Path $stateRoot $uri.Host
    if ($RepairPathOnly) {
        if ($InstallClaude -or $InstallCodex -or $ReplaceProviderConfig -or $RefreshModelsOnly) { throw 'Use -RepairPathOnly without other action switches.' }
        Set-SetupStage 'repairing selected tool PATH'
        if ($selected -contains 'Claude') { Enable-ClaudeNativePath }
        if ($selected -contains 'Codex') {
            $installFile = Join-Path $state 'codex-install.json'
            if (-not (Test-Path -LiteralPath $installFile)) { throw 'Run normal company Codex setup first.' }
            $installed = [IO.File]::ReadAllText($installFile) | ConvertFrom-Json
            Confirm-CodexRelease $installed.executable $installed.release
            $settingsChanged = $true
            Sync-CompanyCodexLauncher $Portal $state
            Enable-CompanyCodexCommand $state $installed.executable
        }
        Write-Host 'Selected tool PATH repair completed. Open a new terminal; run claude or codex for the selected tools.'
        return
    }
    if ($RefreshModelsOnly) {
        if ($InstallClaude -or $InstallCodex -or $ReplaceProviderConfig) { throw 'Use -RefreshModelsOnly without installation or provider replacement switches.' }
        Set-SetupStage 'refreshing installed company model settings'
        # Capture both clients so a failed combined refresh restores both.
        if ($selected -contains 'Claude') { Save-FileSnapshot (Join-Path $env:USERPROFILE '.claude\settings.json') }
        $settingsChanged = $true
        if ($selected -contains 'Codex') { Update-InstalledCodexModels $Portal $state }
        if ($selected -contains 'Claude') { Update-InstalledCompanyModels $Portal }
        return
    }
    Set-SetupStage 'company VPN/DNS check'
    Assert-InferenceNetwork
    Set-SetupStage 'protecting local employee state'
    Protect-Directory $stateRoot
    Protect-Directory $state
    $helperPath = Join-Path $state 'Get-CompanyAIKey.ps1'
    $devicePath = Join-Path $state 'device.json'
    $credentialPath = Join-Path $state 'credential.dpapi'
    foreach ($name in @('credential.dpapi', 'device.json', 'Get-CompanyAIKey.ps1', 'Start-CompanyCodex.ps1', 'settings.backup.dpapi')) {
        Save-FileSnapshot (Join-Path $state $name)
    }
    Set-SetupStage 'local file-write check'
    Assert-LocalAtomicWrites $state
    $helperCommand = 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy AllSigned -File "' + $helperPath + '"'
    if ($selected -contains 'Claude') {
        $settingsDirectory = Join-Path $env:USERPROFILE '.claude'
        if ((Test-Path -LiteralPath $settingsDirectory) -and ((Get-Item -LiteralPath $settingsDirectory).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Claude settings directory must not be a link.' }
        [IO.Directory]::CreateDirectory($settingsDirectory) | Out-Null
        Assert-LocalAtomicWrites $settingsDirectory
        Set-SetupStage 'reading existing Claude configuration'
        $settingsPath = Join-Path $settingsDirectory 'settings.json'
        Save-FileSnapshot $settingsPath
        $previous = '{}'
        if (Test-Path -LiteralPath $settingsPath) { $previous = [IO.File]::ReadAllText($settingsPath) }
        $settings = $previous | ConvertFrom-Json
        if ($settings -isnot [PSCustomObject]) { throw 'Claude settings must be a JSON object.' }
        if (-not $settings.env) { Set-Property $settings 'env' ([PSCustomObject]@{}) }
        if ($settings.env -isnot [PSCustomObject]) { throw 'Claude env settings must be a JSON object.' }
        $conflicts = $settings.env.ANTHROPIC_AUTH_TOKEN -or $settings.env.ANTHROPIC_API_KEY `
            -or ($settings.apiKeyHelper -and $settings.apiKeyHelper -notin @($helperCommand, $helperCommand.Replace(' -ExecutionPolicy AllSigned', ''))) `
            -or ($settings.env.ANTHROPIC_BASE_URL -and $settings.env.ANTHROPIC_BASE_URL -ne ($Portal + '/company-ai'))
        foreach ($name in @('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY')) {
            $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'User')
            if ([Environment]::GetEnvironmentVariable($name, 'Process') -and -not $ReplaceProviderConfig) { $conflicts = $true }
            if ([Environment]::GetEnvironmentVariable($name, 'Machine')) { throw 'A machine-wide Anthropic credential takes precedence. Contact IT to remove it.' }
            if ([Environment]::GetEnvironmentVariable($name, 'User') -and -not $ReplaceProviderConfig) { $conflicts = $true }
        }
        if ($conflicts -and -not $ReplaceProviderConfig) { throw 'Existing provider credentials were preserved. Use -ReplaceProviderConfig for an intentional switch to changdar-ai.' }
        $null = Install-CompanyClaude $state $toolPlan.install_claude
    }
    if ($selected -contains 'Codex') {
        # Perform local filesystem preflight before creating a browser enrollment.
        Protect-Directory (Join-Path $state 'codex')
        Protect-Directory (Join-Path $state 'bin')
        foreach ($name in @('codex\config.toml', 'codex\models.json', 'codex-install.json', 'bin\company-codex.cmd')) { Save-FileSnapshot (Join-Path $state $name) }
        Assert-LocalAtomicWrites (Join-Path $state 'codex')
        Assert-LocalAtomicWrites (Join-Path $state 'bin')
    }
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $verifier = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = $sha.ComputeHash([Text.Encoding]::ASCII.GetBytes($verifier)) } finally { $sha.Dispose() }
    $challenge = [Convert]::ToBase64String($hash).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    $replace = $null
    $installedTools = @()
    if (Test-Path -LiteralPath $devicePath) {
        $old = [IO.File]::ReadAllText($devicePath) | ConvertFrom-Json
        if ($old.portal -ne $Portal) { throw 'Company credential belongs to another portal.' }
        $replace = $old.device_id
        if ($old.tools) { $installedTools = @($old.tools) }
        else { $installedTools = @('Claude') }
    }
    $prepared = Start-CompanyEnrollment $Portal $state ($selected -contains 'Codex') $toolPlan.install_codex $env:COMPUTERNAME $challenge $replace
    $started = $prepared.started
    $codexExecutable = $prepared.executable
    $verification = [Uri]$started.verification_uri_complete
    if ($verification.GetLeftPart([UriPartial]::Authority) -ne $Portal) { throw 'Unexpected verification origin.' }
    Set-SetupStage 'browser approval and device polling'
    $deadline = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() + 600
    $script:approval = @{ url = $verification.AbsoluteUri; code = [string]$started.user_code }
    Write-SetupStatus 'approval' '' 600
    Show-ApprovalInstructions $verification $started.user_code
    $proof = @{ request_id = $started.request_id; code_verifier = $verifier }
    $ready = $null
    $ProgressPreference = 'Continue'
    while ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -lt $deadline) {
        Write-Progress -Activity 'changdar-ai setup' -Status 'Waiting for approval in your browser' `
            -SecondsRemaining ([int]($deadline - [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()))
        Write-SetupStatus 'approval' '' ([Math]::Max(0, [int]($deadline - [DateTimeOffset]::UtcNow.ToUnixTimeSeconds())))
        for ($pause = 0; $pause -lt 3; $pause++) { Assert-SetupNotCancelled; Start-Sleep -Seconds 1 }
        $result = Invoke-SetupRetry { Post-Json '/api/company-ai/device/poll' $proof } 'Browser approval check'
        if ($result.status -eq 'ready') { $ready = $result; break }
    }
    Write-Progress -Activity 'changdar-ai setup' -Completed
    $ProgressPreference = 'SilentlyContinue'
    if (-not $ready) { throw 'Approval expired. Run setup again.' }
    # Finish configuration + ACK as one protected phase; cancellation waits.
    $script:canCancel = $false
    $script:approval = $null
    if ($ready.base_url -ne ($Portal + '/company-ai')) { throw 'Unexpected inference origin.' }
    Set-SetupStage 'verifying private model access'
    $headers = @{ Authorization = ('Bearer ' + $ready.key) }
    if ($selected -contains 'Claude') {
        $catalog = Invoke-SetupRetry { Invoke-RestMethod -Uri ($ready.base_url + '/v1/models') -Headers $headers -TimeoutSec 20 -MaximumRedirection 0 } 'Company model catalog'
        Write-CompanyMcpClaudeConfig (Get-CompanyMcpPolicyValue $catalog)
        Set-CompanyModelSettings $settings @($catalog.data | ForEach-Object { $_.id }) $ready.model
    }
    if ($selected -contains 'Codex') {
        Set-SetupStage 'verifying Codex server policy'
        $codexPolicy = Get-CompanyCodexPolicy $Portal $headers
        Set-SetupStage 'verifying prepared Codex CLI against approved policy'
        Confirm-CodexRelease $codexExecutable $codexPolicy.release
    }
    $headers = $null
    Set-SetupStage 'downloading company client helpers'
    $helpers = @('Get-CompanyAIKey.ps1')
    if ($selected -contains 'Codex') { $helpers += 'Start-CompanyCodex.ps1' }
    # Snapshot restoration covers helpers too, including existing signed bytes.
    $settingsChanged = $true
    foreach ($name in $helpers) {
        $download = Join-Path $state ([Guid]::NewGuid().ToString('N') + '.helper-download')
        try {
            Invoke-SetupRetry { Invoke-WebRequest ($Portal + '/company-ai/downloads/' + $name) -UseBasicParsing -TimeoutSec 20 -MaximumRedirection 0 -OutFile $download } 'Company client helper download' | Out-Null
            Write-AtomicBytes (Join-Path $state $name) ([IO.File]::ReadAllBytes($download))
        } finally { Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue }
    }
    Set-SetupStage 'saving protected employee configuration'
    $secure = ConvertTo-SecureString $ready.key -AsPlainText -Force
    try { $encrypted = ConvertFrom-SecureString $secure } finally { $secure.Dispose() }
    Write-Atomic $credentialPath $encrypted
    if ($selected -contains 'Claude') {
        $backup = ConvertTo-SecureString $previous -AsPlainText -Force
        try { Write-Atomic (Join-Path $state 'settings.backup.dpapi') (ConvertFrom-SecureString $backup) }
        finally { $backup.Dispose() }
        $settings.env.PSObject.Properties.Remove('ANTHROPIC_AUTH_TOKEN')
        $settings.env.PSObject.Properties.Remove('ANTHROPIC_API_KEY')
        if ($ReplaceProviderConfig) {
            foreach ($name in @('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY')) { [Environment]::SetEnvironmentVariable($name, $null, 'User') }
        }
        Set-Property $settings 'apiKeyHelper' $helperCommand
        Set-Property $settings.env 'ANTHROPIC_BASE_URL' $ready.base_url
        Write-Atomic $settingsPath ($settings | ConvertTo-Json -Depth 100)
    }
    if ($selected -contains 'Codex') {
        Write-CompanyCodexSettings $codexPolicy $state $helperPath $codexExecutable
        Enable-CompanyCodexCommand $state $codexExecutable
        # Keep the legacy command available for existing shortcuts.
        Enable-CompanyPath (Join-Path $state 'bin')
    }
    $installedTools = @(($installedTools + $selected) | Select-Object -Unique)
    Write-Atomic $devicePath (@{ portal = $Portal; device_id = $ready.device_id; confirmation_due_at = $ready.confirmation_due_at; tools = $installedTools } | ConvertTo-Json)
    Set-SetupStage 'acknowledging activation'
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        $ackAttempted = $true
        try { $null = Post-Json '/api/company-ai/device/ack' $proof; $acked = $true; break }
        catch { Start-Sleep -Seconds 2 }
    }
    if (-not $acked) { throw 'Activation was not confirmed. Re-run setup.' }
    $ready.key = $null
    Write-SetupStatus 'complete' 'COMPLETED'
    Write-Host 'changdar-ai setup completed. Open a new terminal.'
    if ($selected -contains 'Claude') { Write-Host 'Claude Code: run claude' }
    if ($selected -contains 'Codex') { Write-Host 'Codex: run codex (company CODEX_HOME is selected; personal configuration files are preserved)' }
    Write-Host 'The company renews this credential up to the browser-confirmation deadline. No key copying is needed.'
} catch {
    $failure = $_
    Write-Progress -Activity 'changdar-ai setup' -Completed
    if ($settingsChanged -and -not $acked -and -not $ackAttempted) {
        try { Restore-FileSnapshots }
        catch { [Console]::Error.WriteLine('Some configuration files could not be restored. Contact IT before retrying.') }
        try { Restore-CodexEnvironment }
        catch { [Console]::Error.WriteLine('The previous Codex environment could not be fully restored. Contact IT before retrying.') }
        foreach ($name in $previousEnvironment.Keys) { [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'User') }
    }
    if ($ackAttempted -and -not $acked) {
        [Console]::Error.WriteLine('Activation result is uncertain. The new configuration was kept. Reconnect and run setup again; an old key may already be revoked.')
    }
    # HTTP bodies and native-tool errors can contain tokens; never print them.
    if ($stage -eq 'browser approval and device polling') {
        $pollStatus = 0
        try { if ($_.Exception.Response) { $pollStatus = [int]$_.Exception.Response.StatusCode } } catch { }
        if ($pollStatus -eq 410 -or [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -ge $deadline) {
            [Console]::Error.WriteLine('Browser approval expired after 10 minutes. Run setup again, sign in on the new approval page, compare the code, then click Approve. Installed CLIs will be reused.')
        } else {
            [Console]::Error.WriteLine('Browser approval was not completed. Check company LAN/VPN, sign in on the approval page and click Approve for the matching code. If setup has stopped, run it again to get a new code.')
        }
    } elseif ($_.Exception -is [Net.WebException] -or $stage -in @('verifying private model access', 'downloading company client helpers')) {
        [Console]::Error.WriteLine('Check company VPN access, browser approval and the approved client release. Contact IT if the problem persists.')
    } else { [Console]::Error.WriteLine($_.Exception.Message) }
    Write-SetupStatus 'failed' (Get-SetupFailureCode $failure)
    Write-Error ('changdar-ai setup failed at ' + $stage + '.')
    exit 1
} finally {
    if ($ready) { $ready.key = $null }
    $headers = $null
    $verifier = $null
    $proof = $null
}
