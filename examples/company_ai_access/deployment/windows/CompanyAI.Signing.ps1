# Public-certificate validation shared by VM completion and the explicit IT trust tool.
function Get-CompanyCertificateSHA256($Certificate) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash($Certificate.RawData))).Replace('-', '') }
    finally { $hash.Dispose() }
}

function Assert-CompanySigningCertificates($Root, $Publisher, [string]$RootSHA256, [string]$PublisherSHA256) {
    if ($RootSHA256 -notmatch '^[A-Fa-f0-9]{64}$' -or $PublisherSHA256 -notmatch '^[A-Fa-f0-9]{64}$' -or
        (Get-CompanyCertificateSHA256 $Root) -ne $RootSHA256 -or
        (Get-CompanyCertificateSHA256 $Publisher) -ne $PublisherSHA256) { throw 'Public certificate fingerprints do not match the independently supplied values.' }
    if ($Root.HasPrivateKey -or $Publisher.HasPrivateKey) { throw 'Only public CER certificates may be distributed.' }
    foreach ($certificate in @($Root,$Publisher)) {
        if ([DateTime]::UtcNow -lt $certificate.NotBefore.ToUniversalTime() -or [DateTime]::UtcNow -ge $certificate.NotAfter.ToUniversalTime()) {
            throw 'A company signing certificate is not currently valid.'
        }
        $eku = @($certificate.Extensions | Where-Object {$_.Oid.Value -eq '2.5.29.37'})
        if ($eku.Count -ne 1) { throw 'A Code Signing-only EKU is required.' }
        $usage = New-Object Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension($eku[0],$eku[0].Critical)
        if ($usage.EnhancedKeyUsages.Count -ne 1 -or $usage.EnhancedKeyUsages[0].Value -ne '1.3.6.1.5.5.7.3.3') {
            throw 'Company trust must be limited to Code Signing.'
        }
    }
    $rootBasic = @($Root.Extensions | Where-Object {$_.Oid.Value -eq '2.5.29.19'})
    $leafBasic = @($Publisher.Extensions | Where-Object {$_.Oid.Value -eq '2.5.29.19'})
    if ($rootBasic.Count -ne 1 -or $leafBasic.Count -ne 1) { throw 'Explicit CA/leaf constraints are required.' }
    $rootConstraints = New-Object Security.Cryptography.X509Certificates.X509BasicConstraintsExtension($rootBasic[0],$rootBasic[0].Critical)
    $leafConstraints = New-Object Security.Cryptography.X509Certificates.X509BasicConstraintsExtension($leafBasic[0],$leafBasic[0].Critical)
    if (-not $rootConstraints.CertificateAuthority -or -not $rootConstraints.HasPathLengthConstraint -or
        $rootConstraints.PathLengthConstraint -ne 0 -or $leafConstraints.CertificateAuthority -or
        $Root.Subject -ne $Root.Issuer -or $Publisher.Issuer -ne $Root.Subject) { throw 'Expected a dedicated root and one non-CA signing leaf.' }
    if (@($Publisher.Extensions | Where-Object {$_.Oid.Value -eq '2.5.29.31'}).Count -ne 1) {
        throw 'The company publisher must have an online revocation endpoint.'
    }
    $rootKey = [Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPublicKey($Root)
    $leafKey = [Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPublicKey($Publisher)
    try {
        if (-not $rootKey -or -not $leafKey -or ($rootKey.ExportParameters($false).Modulus.Length * 8) -lt 4096 -or
            ($leafKey.ExportParameters($false).Modulus.Length * 8) -lt 3072) {
            throw 'Company CA/publisher RSA key sizes are invalid.'
        }
    } finally {
        if ($rootKey) { $rootKey.Dispose() }
        if ($leafKey) { $leafKey.Dispose() }
    }
}

function Assert-CompanySigningChain($Root, $Publisher, [string]$RootSHA256) {
    $chain = New-Object Security.Cryptography.X509Certificates.X509Chain
    try {
        # The explicitly pinned root must be installed before Windows can check
        # its leaf's revocation. Every chain/revocation error remains fatal.
        $chain.ChainPolicy.VerificationFlags = [Security.Cryptography.X509Certificates.X509VerificationFlags]::NoFlag
        $chain.ChainPolicy.RevocationMode = [Security.Cryptography.X509Certificates.X509RevocationMode]::Online
        $chain.ChainPolicy.RevocationFlag = [Security.Cryptography.X509Certificates.X509RevocationFlag]::ExcludeRoot
        $chain.ChainPolicy.UrlRetrievalTimeout = [TimeSpan]::FromSeconds(30)
        $chain.ChainPolicy.ApplicationPolicy.Add((New-Object Security.Cryptography.Oid('1.3.6.1.5.5.7.3.3')))
        $null = $chain.ChainPolicy.ExtraStore.Add($Root)
        if (-not $chain.Build($Publisher) -or $chain.ChainElements.Count -ne 2 -or
            (Get-CompanyCertificateSHA256 $chain.ChainElements[1].Certificate) -ne $RootSHA256) {
            throw ('Pinned company chain/revocation validation failed: ' + (($chain.ChainStatus | ForEach-Object {$_.Status.ToString()}) -join ', '))
        }
    } finally { $chain.Dispose() }
}

function Install-CompanyPublicTrust($Root, $Publisher, [string]$Scope) {
    # Caller explicitly selects this operation. The employee setup PS1 never calls it.
    $rootStore = New-Object Security.Cryptography.X509Certificates.X509Store('Root',$Scope)
    $publisherStore = New-Object Security.Cryptography.X509Certificates.X509Store('TrustedPublisher',$Scope)
    $rootAdded = $false
    try {
        $rootStore.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
        $publisherStore.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
        $existing = $rootStore.Certificates.Find([Security.Cryptography.X509Certificates.X509FindType]::FindByThumbprint,$Root.Thumbprint,$false)
        if ($existing.Count -eq 0) {
            $rootStore.Add($Root)
            $rootAdded = $true
        }
        # Windows skips CRL retrieval for an untrusted anchor. Provision only the
        # independently pinned root first, then require full online validation
        # before trusting the publisher. Roll back a newly added root on failure.
        Assert-CompanySigningChain $Root $Publisher (Get-CompanyCertificateSHA256 $Root)
        $publisherStore.Add($Publisher)
    } catch {
        if ($rootAdded) { $rootStore.Remove($Root) }
        throw
    } finally { $rootStore.Close(); $publisherStore.Close() }
}
