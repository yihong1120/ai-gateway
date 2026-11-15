"""Check public PKI and isolated handoff routes; never read/export a CA private key."""
import argparse
import hashlib
import http.client
import io
import json
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import zipfile

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root-sha256', required=True)
    parser.add_argument('--toolkit-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    original_lookup = socket.getaddrinfo
    def lookup(host, port, *a, **kw):
        return original_lookup('127.0.0.1' if host == 'changdar-server.mooo.com' else host, port, *a, **kw)
    socket.getaddrinfo = lookup
    checks = {}
    def request(method, path, status=200, body=None, source='127.0.0.1', headers=None):
        connection = http.client.HTTPSConnection('changdar-server.mooo.com', timeout=20,
            context=ssl.create_default_context(), source_address=(source,0))
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
            assert response.status == status, method + ' ' + path + ': ' + str(response.status)
            return data
        finally:
            connection.close()
    root = x509.load_der_x509_certificate(request('GET','/company-ai/pki/CompanyAI-CodeSigning-Root.cer'))
    assert root.fingerprint(__import__('cryptography.hazmat.primitives.hashes', fromlist=['SHA256']).SHA256()).hex().upper() == args.root_sha256
    assert list(root.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == [ExtendedKeyUsageOID.CODE_SIGNING]
    assert root.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length == 0
    checks['public_root_is_exact_pinned_code_signing_only_authority'] = True
    crl = x509.load_der_x509_crl(request('GET','/company-ai/pki/CompanyAI-CodeSigning.crl'))
    assert crl.is_signature_valid(root.public_key())
    metadata = json.loads(request('GET','/company-ai/pki/pki.json'))
    assert metadata['root_sha256'] == args.root_sha256
    checks['live_crl_signature_and_public_metadata'] = True
    toolkit = request('GET','/company-ai/signing/CompanyAI-SigningTools.zip')
    assert hashlib.sha256(toolkit).hexdigest() == args.toolkit_sha256
    with zipfile.ZipFile(io.BytesIO(toolkit)) as source:
        settings = json.loads(source.read('signing-settings.json'))
        assert settings['root_sha256'] == args.root_sha256
        for entry in settings['files']:
            assert source.read('scripts/' + entry['name']) == request('GET','/company-ai/downloads/' + entry['name'])
    checks['toolkit_hash_and_reviewed_sources_match_current_downloads'] = True
    request('PUT','/company-ai/pki/CompanyAI-CodeSigning-Root.cer',403,body=b'denied')
    request('GET','/company-ai/signing/request.csr',403)
    request('POST','/company-ai/signing/request.csr',403,body=b'denied')
    request('PUT','/company-ai/signing/request.csr',403,body=b'denied',source='127.0.0.2',
            headers={'X-Forwarded-For':'192.168.1.98'})
    request('PUT','/company-ai/signing/request.csr',413,body=b'x'*16385)
    checks['public_pki_read_only_and_upload_method_size_source_and_spoofing_gates'] = True
    incoming = Path('/var/lib/company-ai-signing-incoming/request.csr')
    if not incoming.exists():
        probe = ('public handoff smoke ' + secrets.token_hex(20)).encode()
        try:
            request('PUT','/company-ai/signing/request.csr',201,body=probe,headers={'Content-Type':'application/pkcs10'})
            assert incoming.read_bytes() == probe
            checks['bounded_native_upload_reaches_only_fixed_incoming_file'] = True
        finally:
            if incoming.exists() and incoming.read_bytes() == probe:
                incoming.unlink()
    else:
        checks['existing_vm_request_preserved_without_smoke_overwrite'] = True
    assert set(p.name for p in Path('/var/lib/company-ai-code-signing-public').iterdir()) <= {
        'CompanyAI-CodeSigning-Root.cer','CompanyAI-CodeSigning-Publisher.cer','CompanyAI-CodeSigning.crl','pki.json'}
    for user in ('www-data','changdar02'):
        subprocess.run(['runuser','-u',user,'--','python3','-c',
            "import os;assert not os.access('/var/lib/company-ai-code-signing/root-key.pem',os.R_OK)"],check=True,capture_output=True)
    checks['private_ca_material_not_web_readable_or_app_user_readable'] = True
    for unit in ('company-ai-access.service','phase5-bff-production.service','company-ai-code-signing-crl.timer'):
        subprocess.run(['systemctl','is-active','--quiet',unit],check=True)
    subprocess.run(['systemctl','is-enabled','--quiet','company-ai-code-signing-crl.timer'],check=True)
    checks['existing_services_active_and_daily_crl_timer_enabled'] = True
    result = {'checks':checks,'root_sha256':args.root_sha256,'root_thumbprint':metadata['root_thumbprint'],
              'toolkit_sha256':args.toolkit_sha256,'crl_next_update':metadata['crl_next_update'],
              'signer_issued':bool(metadata['signer']),'windows_vm_enrollment':'pending' if not metadata['signer'] else 'issued',
              'employee_trust_installed':False,'production_scripts_signed':False}
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    main()
