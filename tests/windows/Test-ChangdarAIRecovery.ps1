# Fault injection against production functions. No enrollment or credential access.
[CmdletBinding()]
param([string]$Source = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'examples\company_ai_access\windows\Setup-CompanyAI.ps1'))
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Setup parse failure'}
foreach($definition in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)){. ([ScriptBlock]::Create($definition.Extent.Text))}
$temporary=Join-Path ([IO.Path]::GetTempPath()) ('changdar-ai recovery '+[Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temporary)|Out-Null
$InstallerStatusPath=Join-Path $temporary 'status.json';$InstallerCancelPath=Join-Path $temporary 'cancel.request'
$script:canCancel=$true;$script:approval=$null;$script:ackAttempted=$false;$script:acked=$false
$script:sleeps=0
function Start-Sleep {param([int]$Seconds) $script:sleeps+=$Seconds}
function Assert-Condition([bool]$Condition,[string]$Message){if(-not $Condition){throw $Message}}
try {
    $script:calls=0
    $value=Invoke-SetupRetry { $script:calls++;if($script:calls -lt 3){throw (New-Object Net.WebException('synthetic', [Net.WebExceptionStatus]::Timeout))};return 'ok' } 'Synthetic download'
    Assert-Condition ($value -eq 'ok' -and $script:calls -eq 3 -and $script:sleeps -eq 6) 'Transient retry did not recover within bound'
    $script:calls=0;$rejected=$false
    try{Invoke-SetupRetry {$script:calls++;throw (New-Object Net.WebException('synthetic',[Net.WebExceptionStatus]::Timeout))} 'Synthetic timeout'}catch{$rejected=$true}
    Assert-Condition ($rejected -and $script:calls -eq 3) 'Repeated timeout was not bounded'
    $script:calls=0;$rejected=$false
    try{Invoke-SetupRetry {$script:calls++;throw 'Invalid configuration'} 'Synthetic policy'}catch{$rejected=$true}
    Assert-Condition ($rejected -and $script:calls -eq 1) 'Permanent failure was retried'
    foreach($status in @(401,403,404,410,408,429,500,502,503,504)) {
        $failure=[pscustomobject]@{Exception=[pscustomobject]@{Response=[pscustomobject]@{StatusCode=$status};InnerException=$null}}
        Assert-Condition ((Test-SetupTransientFailure $failure) -eq ($status -in @(408,429,500,502,503,504))) 'HTTP retry classification failed'
    }
    [IO.File]::WriteAllText($InstallerCancelPath,'cancel');$script:calls=0;$cancelled=$false
    try{Invoke-SetupRetry {$script:calls++;'unexpected'} 'Cancelled read'}catch{$cancelled=$_.Exception -is [OperationCanceledException]}
    Assert-Condition ($cancelled -and $script:calls -eq 0) 'Cancelled operation still contacted endpoint'
    $script:canCancel=$false
    Assert-Condition ((Invoke-SetupRetry {'ack allowed'} 'Atomic activation') -eq 'ack allowed') 'Cancellation interrupted protected activation'
    Remove-Item -LiteralPath $InstallerCancelPath
    $script:canCancel=$true
    Set-SetupStage 'browser approval and device polling'
    $script:approval=@{url='https://changdar-server.mooo.com/company-ai/setup?request_id=00000000-0000-0000-0000-000000000001&user_code=ABCD-1234';code='ABCD-1234'}
    Write-SetupStatus 'approval' '' 598
    $status=Get-Content -LiteralPath $InstallerStatusPath -Raw|ConvertFrom-Json
    Assert-Condition ($status.kind -eq 'approval' -and $status.remaining -eq 598 -and $status.user_code -eq 'ABCD-1234') 'Public approval status failed'
    $script:deadline=0
    try{throw (New-Object Net.WebException('sensitive-key-must-not-be-written'))}catch{$code=Get-SetupFailureCode $_;Write-SetupStatus 'failed' $code}
    $json=Get-Content -LiteralPath $InstallerStatusPath -Raw
    Assert-Condition ($code -eq 'APPROVAL_EXPIRED' -and -not $json.Contains('sensitive-key')) 'Expiry or safe diagnostic failed'
    $script:ackAttempted=$true
    try{throw 'synthetic'}catch{$code=Get-SetupFailureCode $_}
    Assert-Condition ($code -eq 'ACTIVATION_UNCERTAIN') 'Indeterminate ACK lost its recovery classification'
    Write-Host 'PASS: transient recovery, retry bound, permanent HTTP errors, cancellation, protected ACK, approval countdown and redacted failure status.'
}finally{Remove-Item -LiteralPath $temporary -Recurse -Force}
