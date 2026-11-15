"""Real CA/CRL crypto; disposable keys and no system trust modifications."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID
from cryptography.x509.oid import NameOID

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'internal_ca', SOURCE / 'examples/company_ai_access/deployment/production/internal_ca.py',
)
pki = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pki)


@pytest.fixture
def ca(tmp_path):
    authority = pki.Authority(tmp_path / 'private', tmp_path / 'public')
    authority.initialize()
    return authority


def csr(subject=pki.SIGNER_NAME, size=3072, extra=None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=size)
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]),
    )
    builder = builder.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=False)
    if extra is not None:
        builder = builder.add_extension(extra, critical=True)
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)


def issue(ca, value=None):
    value = value or csr()
    return ca.issue(value, hashlib.sha256(value).hexdigest())


def test_ca_is_code_signing_only_private_and_idempotent(ca):
    before = ca.load()[0]['root_sha256']
    assert ca.initialize()['root_sha256'] == before
    root = ca.load()[1]
    assert list(
        root.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage,
        ).value,
    ) == [ExtendedKeyUsageOID.CODE_SIGNING]
    basic = root.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert basic.ca and basic.path_length == 0
    assert root.public_key().key_size == 4096
    for path in ca.state.iterdir():
        assert path.stat().st_mode & 0o077 == 0
    assert {path.name for path in ca.public.iterdir()} == {pki.ROOT_FILE, pki.CRL_FILE, 'pki.json'}
    assert b'ENCRYPTED PRIVATE KEY' in (ca.state / 'root-key.pem').read_bytes()


def test_issue_has_exact_usage_and_working_crl(ca):
    result = issue(ca)
    root = ca.load()[1]
    leaf = x509.load_der_x509_certificate((ca.public / pki.SIGNER_FILE).read_bytes())
    root.public_key().verify(leaf.signature, leaf.tbs_certificate_bytes, padding.PKCS1v15(), hashes.SHA256())
    assert not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert list(
        leaf.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage,
        ).value,
    ) == [ExtendedKeyUsageOID.CODE_SIGNING]
    usage = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
    assert usage.digital_signature and not usage.key_cert_sign and not usage.key_encipherment
    assert leaf.extensions.get_extension_for_class(
        x509.CRLDistributionPoints,
    ).value[0].full_name[0].value == pki.CRL_URL
    ca.revoke(result['signer']['serial'])
    revoked = x509.load_der_x509_crl((ca.public / pki.CRL_FILE).read_bytes())
    assert revoked.is_signature_valid(root.public_key())
    assert revoked.get_revoked_certificate_by_serial_number(leaf.serial_number) is not None
    assert not (ca.public / pki.SIGNER_FILE).exists()


@pytest.mark.parametrize('bad', ['small-key', 'subject', 'subordinate-ca', 'san', 'hash', 'size'])
def test_rejects_out_of_scope_or_unconfirmed_requests(ca, bad):
    if bad == 'small-key':
        value = csr(size=2048)
    elif bad == 'subject':
        value = csr(subject='Unapproved publisher')
    elif bad == 'subordinate-ca':
        value = csr(extra=x509.BasicConstraints(ca=True, path_length=0))
    elif bad == 'san':
        value = csr(extra=x509.SubjectAlternativeName([x509.DNSName('example.test')]))
    elif bad == 'size':
        value = b'x' * 16385
    else:
        value = csr()
    digest = '0' * 64 if bad == 'hash' else hashlib.sha256(value).hexdigest()
    with pytest.raises(ValueError):
        ca.issue(value, digest)
    assert not ca.load()[0]['certificates']


def test_active_and_revoked_keys_are_not_silently_replaced(ca):
    request = csr()
    first = issue(ca, request)
    assert issue(ca, request)['signer'] == first['signer']
    with pytest.raises(ValueError):
        issue(ca)
    ca.revoke(first['signer']['serial'])
    with pytest.raises(ValueError):
        issue(ca, request)
    assert issue(ca)['signer']['sha256'] != first['signer']['sha256']


def test_rejects_unsafe_state_or_public_paths(ca, tmp_path):
    (ca.state / 'root-key.pem').chmod(0o644)
    with pytest.raises(ValueError):
        ca.refresh()
    link = tmp_path / 'link'
    link.symlink_to(ca.state, target_is_directory=True)
    with pytest.raises(ValueError):
        pki.Authority(link, tmp_path / 'other')
    with pytest.raises(ValueError):
        pki.Authority(ca.state, ca.state / 'public')


def test_signing_powershell_properties_and_syntax(ca):
    powershell = shutil.which('pwsh')
    if not powershell:
        pytest.skip('Portable PowerShell is needed.')
    result = issue(ca)
    command = r'''
$ErrorActionPreference='Stop'
$files=Get-ChildItem -LiteralPath $env:COMPANYAI_SIGNING_SCRIPTS -Filter '*.ps1'
foreach ($file in $files) {
    $tokens=$null;$errors=$null
    $null=[Management.Automation.Language.Parser]::ParseFile($file.FullName,[ref]$tokens,[ref]$errors)
    if ($errors.Count) {throw ('Invalid syntax: '+$file.Name+': '+($errors.Message -join '; '))}
}
. (Join-Path $env:COMPANYAI_SIGNING_SCRIPTS 'CompanyAI.Signing.ps1')
$root=[Security.Cryptography.X509Certificates.X509Certificate2]::new([IO.File]::ReadAllBytes($env:COMPANYAI_ROOT_CERT))
$leaf=[Security.Cryptography.X509Certificates.X509Certificate2]::new([IO.File]::ReadAllBytes($env:COMPANYAI_LEAF_CERT))
Assert-CompanySigningCertificates $root $leaf $env:COMPANYAI_ROOT_SHA $env:COMPANYAI_LEAF_SHA
$rejected=$false
try {Assert-CompanySigningCertificates $root $leaf ('0'*64) $env:COMPANYAI_LEAF_SHA} catch {$rejected=$true}
if (-not $rejected) {throw 'An incorrect trust fingerprint was accepted.'}
Write-Output 'PASS'
'''
    run = subprocess.run(
        [powershell, '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True, text=True, timeout=30,
        env={
            **os.environ, 'COMPANYAI_SIGNING_SCRIPTS': str(SOURCE / 'examples/company_ai_access/deployment/windows'),
            'COMPANYAI_ROOT_CERT': str(ca.public / pki.ROOT_FILE), 'COMPANYAI_LEAF_CERT': str(ca.public / pki.SIGNER_FILE),
            'COMPANYAI_ROOT_SHA': result['root_sha256'], 'COMPANYAI_LEAF_SHA': result['signer']['sha256'],
        },
    )
    assert run.returncode == 0, run.stderr
    assert 'PASS' in run.stdout


def test_pki_routes_preserve_read_policy_and_restrict_uploads():
    specification = importlib.util.spec_from_file_location(
        'ca_install', SOURCE / 'examples/company_ai_access/deployment/production/install_internal_ca.py',
    )
    installer = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(installer)
    original = '''location = /company-ai/downloads/Get-CompanyAIKey.ps1 {
    deny 192.168.1.1/32;
    allow 127.0.0.0/8;
    allow 192.168.1.0/24;
    allow 10.1.2.3/32;
    deny all;
    proxy_pass http://127.0.0.1:8013;
}
'''
    generated = installer.routes(original, '192.168.1.98')
    assert generated.startswith(original.rstrip())
    assert generated.count('allow 10.1.2.3/32;') == 6
    assert generated.count('limit_except PUT { deny all; }') == 2
    assert generated.count('allow 192.168.1.98/32;') == 2
    assert generated.count('limit_except GET { deny all; }') == 5
    assert 'client_max_body_size 16k;' in generated and 'client_max_body_size 2m;' in generated
    assert 'root-key' not in generated and 'key-password' not in generated
    assert installer.routes(generated, '192.168.1.98') == generated
    with pytest.raises(ValueError):
        installer.routes(original, '0.0.0.0/0')
    with pytest.raises(ValueError):
        installer.routes('', '192.168.1.98')
