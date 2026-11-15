"""Overlay verified Windows signatures onto an immutable reviewed source release."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization

SCRIPTS = ('Setup-CompanyAI.ps1', 'Get-CompanyAIKey.ps1', 'Start-CompanyCodex.ps1')
PUBLIC_FILES = {*SCRIPTS, 'release.json', 'Publisher.cer', 'RootCA.cer'}
ORIGIN = 'https://changdar-server.mooo.com'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_zip(path, expected, *, public=False):
    if sha(path.read_bytes()) != expected.lower():
        raise ValueError('Archive checksum does not match its independently recorded value.')
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or (public and set(names) != PUBLIC_FILES):
            raise ValueError('Duplicate or unexpected archive entries.')
        if sum(e.file_size for e in archive.infolist()) > 8 * 1024 * 1024:
            raise ValueError('Archive expands beyond the release limit.')
        for entry in archive.infolist():
            path = Path(entry.filename)
            if (
                path.is_absolute() or '..' in path.parts or '\\' in entry.filename or
                not entry.filename or entry.is_dir() or entry.flag_bits & 1 or
                (entry.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError('Unsafe archive entry.')
        return {name: archive.read(name) for name in names}


def assert_source_unchanged(original, signed):
    if signed.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = signed.decode('utf-16')
    else:
        text = signed.decode('utf-8-sig')
    marker = '# SIG # Begin signature block'
    if text.count(marker) != 1 or text.count('# SIG # End signature block') != 1:
        raise ValueError('Expected one native PowerShell Authenticode signature block.')
    before, signature = text.split(marker)
    if not signature.rstrip().endswith('# SIG # End signature block'):
        raise ValueError('Unexpected data following the signature.')
    before = before.replace('\r\n', '\n')
    expected = original.decode('utf-8-sig').replace('\r\n', '\n')
    if before not in (expected, expected + '\n', expected + '\n\n'):
        raise ValueError('Signed script differs from the reviewed executable source.')


def assert_executable_unchanged(original, signed):
    """Authenticode changes only checksum/security directory and appends certificates."""
    def offsets(data):
        if data[:2] != b'MZ':
            raise ValueError('Expected a Windows PE executable.')
        pe = struct.unpack_from('<I', data, 0x3c)[0]
        if data[pe:pe+4] != b'PE\0\0':
            raise ValueError('Invalid PE signature.')
        optional = pe+24
        magic = struct.unpack_from('<H', data, optional)[0]
        if magic not in (0x10b, 0x20b):
            raise ValueError('Invalid PE optional header.')
        return optional+64, optional+(112 if magic == 0x20b else 96)+32
    old_checksum, old_security = offsets(original)
    checksum, security = offsets(signed)
    if (old_checksum, old_security) != (checksum, security):
        raise ValueError('PE headers changed.')
    if struct.unpack_from('<II', original, security) != (0, 0):
        raise ValueError('Expected an unsigned build.')
    start, size = struct.unpack_from('<II', signed, security)
    if start != (len(original)+7)//8*8 or size == 0 or start+size != len(signed):
        raise ValueError('Unexpected PE certificate location.')
    before = bytearray(signed[:len(original)])
    expected = bytearray(original)
    for data in (before, expected):
        data[checksum:checksum+4] = bytes(4)
        data[security:security+8] = bytes(8)
    if before != expected or any(signed[len(original):start]):
        raise ValueError('Signed executable differs from the reviewed build.')


def build(args):
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
        raise ValueError('Use a new stable source release version.')
    source = read_zip(args.source, args.source_sha256)
    source_manifest = json.loads(source['release.json'])
    if source_manifest['version'] != args.source_version or source_manifest['portal'] != ORIGIN:
        raise ValueError('Expected the reviewed production source version.')
    if {e['name'] for e in source_manifest['files']} != set(source) - {'release.json', 'README-release.txt'}:
        raise ValueError('Source manifest does not cover the archive.')
    for entry in source_manifest['files']:
        data = source[entry['name']]
        if len(data) != entry['bytes'] or sha(data) != entry['sha256']:
            raise ValueError('Reviewed source manifest differs.')
    signed = read_zip(args.signed, args.signed_sha256, public=True)
    manifest = json.loads(signed['release.json'])
    root = x509.load_der_x509_certificate(signed['RootCA.cer'])
    publisher = x509.load_der_x509_certificate(signed['Publisher.cer'])
    if (
        root.fingerprint(hashes.SHA256()).hex() != args.root_sha256.lower() or
        publisher.fingerprint(hashes.SHA256()).hex() != args.publisher_sha256.lower() or
        manifest['root_sha256'].lower() != args.root_sha256.lower() or
        manifest['signer_thumbprint'].lower() != publisher.fingerprint(hashes.SHA1()).hex() or
        manifest['version'] != args.windows_version or
        len(manifest['files']) != 3 or {e['name'] for e in manifest['files']} != set(SCRIPTS)
    ):
        raise ValueError('Signed publisher, anchor or release identity differs.')
    verification = {}
    with tempfile.TemporaryDirectory(prefix='company-ai-signature-') as temporary:
        work = Path(temporary)
        (work / 'root.pem').write_bytes(root.public_bytes(serialization.Encoding.PEM))
        crl = x509.load_der_x509_crl(args.crl.read_bytes())
        if not crl.is_signature_valid(root.public_key()):
            raise ValueError('Company CRL signature differs from the pinned root.')
        (work / 'crl.pem').write_bytes(crl.public_bytes(serialization.Encoding.PEM))
        for entry in manifest['files']:
            name = entry['name']
            original_path = 'examples/company_ai_access/windows/' + name
            original = source[original_path]
            if (
                sha(original) != entry['source_sha256'] or sha(signed[name]) != entry['sha256'] or
                len(signed[name]) != entry['bytes']
            ):
                raise ValueError('Signed artifact/source manifest mismatch.')
            assert_source_unchanged(original, signed[name])
            (work / name).write_bytes(signed[name])
            command = [
                str(args.verifier), 'verify', '-CAfile', str(work / 'root.pem'),
                '-CRLfile', str(work / 'crl.pem'), '-TSA-CAfile', str(args.tsa_ca),
                '-require-leaf-hash', 'sha256:' + args.publisher_sha256,
                '-in', str(work / name),
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=90)
            verification[name] = {'exit_code': result.returncode, 'output': result.stdout + result.stderr}
            if result.returncode:
                raise ValueError(
                    'Linux signature/timestamp/CRL verification failed: ' +
                    name + '\n' + verification[name]['output'],
                )
            source[original_path] = signed[name]
        if args.installer:
            if not args.unsigned_installer or not args.installer_sha256 or not args.unsigned_installer_sha256:
                raise ValueError('Installer requires independently recorded signed and unsigned hashes.')
            binary = args.installer.read_bytes()
            original = args.unsigned_installer.read_bytes()
            if sha(binary) != args.installer_sha256 or sha(original) != args.unsigned_installer_sha256:
                raise ValueError('Installer build/upload checksum mismatch.')
            assert_executable_unchanged(original, binary)
            name = 'changdar-ai-setup.exe'
            (work/name).write_bytes(binary)
            command = [
                str(args.verifier), 'verify', '-CAfile', str(work/'root.pem'), '-CRLfile', str(work/'crl.pem'),
                '-TSA-CAfile', str(args.tsa_ca), '-require-leaf-hash', 'sha256:' +
                args.publisher_sha256, '-in', str(work/name),
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=90)
            verification[name] = {'exit_code': result.returncode, 'output': result.stdout+result.stderr}
            if result.returncode:
                raise ValueError('EXE signature/timestamp/CRL validation failed: '+verification[name]['output'])
            source['examples/company_ai_access/windows/'+name] = binary
    provenance = 'docs/company-ai-internal-signing/'
    for name in ('release.json', 'RootCA.cer', 'Publisher.cer'):
        source[provenance + name] = signed[name]
    source[provenance + 'linux-verification.json'] = (json.dumps(verification, indent=2) + '\n').encode()
    source['README-release.txt'] = (
        b'Company AI signed source release. All three Windows scripts were signed and\n'
        b'timestamped on the dedicated Windows VM and independently verified on Linux.\n'
        b'IT must provision the pinned internal root and publisher through its existing\n'
        b'endpoint management process. Preserve signed bytes, encoding and line endings.\n'
        b'No runtime credentials or private certificate keys are included.\n'
    )
    del source['release.json']
    output_manifest = dict(
        source_manifest, version=args.version, signed=True,
        signing={
            'source_version': args.source_version, 'source_archive_sha256': args.source_sha256,
            'signed_archive_sha256': args.signed_sha256, 'root_sha256': args.root_sha256,
            'publisher_sha256': args.publisher_sha256, 'windows_version': manifest['version'],
            'windows_native_verified': True, 'linux_verified': True,
        },
        files=[{'name': n, 'sha256': sha(d), 'bytes': len(d)} for n, d in sorted(source.items())],
    )
    source['release.json'] = (json.dumps(output_manifest, indent=2) + '\n').encode()
    with zipfile.ZipFile(args.output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(source.items()):
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    return {
        'version': args.version, 'sha256': sha(args.output.read_bytes()), 'signed': True,
        'files': len(output_manifest['files']), 'signing': output_manifest['signing'],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'signed', 'crl', 'verifier', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    for name in ('source-sha256', 'signed-sha256', 'root-sha256', 'publisher-sha256', 'version'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--tsa-ca', type=Path, default=Path('/etc/ssl/certs/ca-certificates.crt'))
    parser.add_argument('--source-version', default='1.1.8')
    parser.add_argument('--windows-version', default='1.1.8-internal.1')
    parser.add_argument('--installer', type=Path)
    parser.add_argument('--unsigned-installer', type=Path)
    parser.add_argument('--installer-sha256')
    parser.add_argument('--unsigned-installer-sha256')
    print(json.dumps(build(parser.parse_args()), indent=2))


if __name__ == '__main__':
    main()
