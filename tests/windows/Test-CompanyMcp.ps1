# Public configuration only: no trust changes, provider calls or real credentials.
[CmdletBinding()]
param([string]$Source=(Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'examples/company_ai_access/windows/Setup-CompanyAI.ps1'))
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Company setup syntax failure'}
foreach($f in $ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)){
    . ([ScriptBlock]::Create($f.Extent.Text))
}
$root=Join-Path ([IO.Path]::GetTempPath()) ('Company MCP test '+[Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($root)|Out-Null
$previousProfile=$env:USERPROFILE
$env:USERPROFILE=$root
$script:fileSnapshots=[ordered]@{}
# File data writes are the only platform substitute; the real snapshot,
# merge, generation and policy validation functions above execute unchanged.
function Write-Atomic([string]$Path,[string]$Value){[IO.File]::WriteAllText($Path,$Value)}
try{
    $policy=[pscustomobject]@{
        version=1;url='https://company.example/company-mcp';issuer='https://identity.example/realms/company';
        scope='company.resources';resource_mode='all_owned_bindings';
        codex_client_id='company-codex';claude_client_id='company-claude';codex_callback_port=18431;claude_callback_port=18432
    }
    Assert-CompanyMcpPolicy $policy
    $legacy=New-CompanyCodexConfig ([pscustomobject]@{default_model='gemini-3.8-flash';base_url='http://127.0.0.1:9/v1'}) 'helper.ps1' 'models.json' 'powershell.exe'
    if($legacy -match 'mcp_servers'){throw 'Disabled MCP policy unexpectedly enabled a server'}
    $generated=New-CompanyCodexConfig ([pscustomobject]@{default_model='gemini-3.8-flash';base_url='http://127.0.0.1:9/v1';mcp=$policy}) 'helper.ps1' 'models.json' 'powershell.exe'
    foreach($value in @('[mcp_servers.changdar_company]','[mcp_servers.changdar_company.oauth]','client_id = "company-codex"','mcp_oauth_callback_port = 18431')){
        if(-not $generated.Contains($value)){throw 'Codex public MCP configuration is missing'}
    }
    $path=Join-Path $root '.claude.json'
    [IO.File]::WriteAllText($path,'{"theme":"dark","mcpServers":{"personal":{"type":"http","url":"https://personal.example/mcp"}}}')
    Write-CompanyMcpClaudeConfig $policy
    $claude=[IO.File]::ReadAllText($path)|ConvertFrom-Json
    if($claude.theme -ne 'dark' -or $claude.mcpServers.personal.url -ne 'https://personal.example/mcp' -or
       $claude.mcpServers.changdar_company.oauth.clientId -ne 'company-claude'){throw 'Claude merge or public client ID is incorrect'}
    if([IO.File]::ReadAllText($path) -match 'password|Bearer|clientSecret|apiKey'){throw 'MCP settings contain credential fields'}
    Write-CompanyMcpClaudeConfig $null
    $claude=[IO.File]::ReadAllText($path)|ConvertFrom-Json
    if($null -ne $claude.mcpServers.PSObject.Properties['changdar_company'] -or -not $claude.mcpServers.personal){throw 'MCP removal damaged personal settings'}
    $other=Get-OtherMcpConfig "[features]`nfoo=true`n[mcp_servers.personal]`nurl=`"https://personal.example/mcp`"`n[mcp_servers.changdar_company]`nurl=`"https://old.example/mcp`"`n[mcp_servers.changdar_company.oauth]`nclient_id=`"old`"`n"
    if($other -notmatch 'mcp_servers.personal' -or $other -match 'changdar_company|features'){throw 'Managed TOML merge damaged other MCP sections'}
    foreach($url in @('http://evil.example/company-mcp','https://evil.example/company-mcp?token=secret','https://user:pass@evil.example/company-mcp')){
        $bad=$policy|ConvertTo-Json|ConvertFrom-Json;$bad.url=$url;$rejected=$false
        try{Assert-CompanyMcpPolicy $bad}catch{$rejected=$true}
        if(-not $rejected){throw 'Invalid MCP endpoint was accepted'}
    }
    Write-Host 'PASS: public MCP policy, legacy policy, Codex generation, Claude merge/removal, personal entries and endpoint validation.'
}finally{
    $env:USERPROFILE=$previousProfile
    Remove-Item -LiteralPath $root -Recurse -Force
}
