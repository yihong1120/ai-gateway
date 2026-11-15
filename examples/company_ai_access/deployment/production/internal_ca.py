"""Private, code-signing-only CA. Run locally as B's root; no signing HTTP API."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ExtensionOID


ORIGIN = 'https://changdar-server.mooo.com'
CRL_URL = ORIGIN + '/company-ai/pki/CompanyAI-CodeSigning.crl'
ROOT_NAME = 'Changdar Company AI Code Signing Root'
SIGNER_NAME = 'Changdar Company AI Code Signing'
ROOT_FILE = 'CompanyAI-CodeSigning-Root.cer'
SIGNER_FILE = 'CompanyAI-CodeSigning-Publisher.cer'
CRL_FILE = 'CompanyAI-CodeSigning.crl'
UTC = timezone.utc


def now():
    return datetime.now(UTC)


def fingerprint(cert, algorithm=None):
    return cert.fingerprint(algorithm or hashes.SHA256()).hex().upper()


def check_path(path, directory=False):
    path = Path(path)
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError('CA paths must not contain symbolic links.')
    if path.exists() and directory and not path.is_dir():
        raise ValueError('Expected a CA directory.')
    return path


def atomic(path, value, mode):
    path = check_path(path)
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(12))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, 'wb') as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


class Authority:
    def __init__(self, state, public):
        self.state, self.public = check_path(state, True), check_path(public, True)
        if self.state == self.public or self.state in self.public.parents or self.public in self.state.parents:
            raise ValueError('Public and private CA directories must be separate.')

    @contextmanager
    def locked(self):
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state.stat().st_uid != os.geteuid() or self.state.stat().st_mode & 0o077:
            raise ValueError('The private CA directory must be owned by the operator and mode 0700.')
        fd = os.open(self.state / 'operator.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def save(self, record):
        atomic(self.state / 'registry.json', json_bytes(record), 0o600)

    def load(self):
        for name in ('registry.json', 'root-key.pem', 'key-password', 'root.cer'):
            path = check_path(self.state / name)
            if not path.is_file() or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077:
                raise ValueError('CA state is missing or has unsafe ownership/permissions.')
        record = json.loads((self.state / 'registry.json').read_text())
        root = x509.load_der_x509_certificate((self.state / 'root.cer').read_bytes())
        key = serialization.load_pem_private_key((self.state / 'root-key.pem').read_bytes(),
                                                (self.state / 'key-password').read_bytes())
        if not isinstance(key, rsa.RSAPrivateKey) or key.public_key().public_numbers() != root.public_key().public_numbers():
            raise ValueError('The CA key and certificate do not match.')
        if fingerprint(root) != record['root_sha256'] or now() >= root.not_valid_after_utc:
            raise ValueError('CA identity or validity check failed; never silently replace this root.')
        return record, root, key

    def initialize(self):
        with self.locked():
            if (self.state / 'registry.json').exists():
                return self._publish(*self.load())
            if any((self.state / name).exists() for name in ('root-key.pem', 'key-password', 'root.cer')):
                raise ValueError('Partial CA state exists; preserve it for operator recovery.')
            key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ROOT_NAME)])
            root = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now() - timedelta(minutes=10)).not_valid_after(now() + timedelta(days=1825))
                    .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                    .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
                    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=True)
                    .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                    .sign(key, hashes.SHA256()))
            password = secrets.token_bytes(48)
            atomic(self.state / 'key-password', password, 0o600)
            atomic(self.state / 'root-key.pem', key.private_bytes(serialization.Encoding.PEM,
                   serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(password)), 0o600)
            atomic(self.state / 'root.cer', root.public_bytes(serialization.Encoding.DER), 0o600)
            record = {'version': 1, 'root_sha256': fingerprint(root), 'crl_number': 0,
                      'created_at': now().isoformat(), 'certificates': [], 'revoked': []}
            self.save(record)
            return self._publish(record, root, key)

    def _publish(self, record, root, key):
        self.public.mkdir(mode=0o755, parents=True, exist_ok=True)
        if self.public.stat().st_uid != os.geteuid() or self.public.stat().st_mode & 0o022:
            raise ValueError('Only the CA operator may write the public PKI directory.')
        self.public.chmod(0o755)
        record['crl_number'] += 1
        builder = (x509.CertificateRevocationListBuilder().issuer_name(root.subject)
                   .last_update(now() - timedelta(minutes=5))
                   .next_update(min(now() + timedelta(days=7), root.not_valid_after_utc))
                   .add_extension(x509.CRLNumber(record['crl_number']), critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False))
        for entry in record['revoked']:
            revoked = (x509.RevokedCertificateBuilder().serial_number(int(entry['serial'], 16))
                       .revocation_date(datetime.fromisoformat(entry['at']))
                       .add_extension(x509.CRLReason(x509.ReasonFlags.key_compromise), critical=False).build())
            builder = builder.add_revoked_certificate(revoked)
        crl = builder.sign(key, hashes.SHA256())
        key.public_key().verify(crl.signature, crl.tbs_certlist_bytes, padding.PKCS1v15(), hashes.SHA256())
        summary = {'root_sha256': fingerprint(root), 'root_thumbprint': fingerprint(root, hashes.SHA1()),
                   'root_subject': root.subject.rfc4514_string(), 'root_expires_at': root.not_valid_after_utc.isoformat(),
                   'crl_url': CRL_URL, 'crl_number': record['crl_number'],
                   'crl_next_update': crl.next_update_utc.isoformat(), 'signer': None}
        active = [entry for entry in record['certificates'] if entry['serial'] not in {r['serial'] for r in record['revoked']}]
        if active:
            entry = active[-1]
            certificate = x509.load_der_x509_certificate((self.state / entry['filename']).read_bytes())
            atomic(self.public / SIGNER_FILE, certificate.public_bytes(serialization.Encoding.DER), 0o644)
            summary['signer'] = {name: entry[name] for name in ('sha256', 'thumbprint', 'csr_sha256', 'serial', 'expires_at')}
        else:
            (self.public / SIGNER_FILE).unlink(missing_ok=True)
        self.save(record)
        atomic(self.public / ROOT_FILE, root.public_bytes(serialization.Encoding.DER), 0o644)
        atomic(self.public / CRL_FILE, crl.public_bytes(serialization.Encoding.DER), 0o644)
        atomic(self.public / 'pki.json', json_bytes(summary), 0o644)
        return summary

    def refresh(self):
        with self.locked():
            return self._publish(*self.load())

    def issue(self, csr_bytes, expected_sha256):
        if len(csr_bytes) > 16384 or hashlib.sha256(csr_bytes).hexdigest().lower() != expected_sha256.lower():
            raise ValueError('CSR size/hash must match the independently reported VM request.')
        request = x509.load_pem_x509_csr(csr_bytes)
        key = request.public_key()
        expected = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SIGNER_NAME)])
        if (not request.is_signature_valid or request.subject != expected or not isinstance(key, rsa.RSAPublicKey)
                or key.key_size < 3072 or request.signature_hash_algorithm.name not in ('sha256', 'sha384', 'sha512')):
            raise ValueError('CSR must be a valid SHA256+ RSA3072+ request for the dedicated company signer.')
        for extension in request.extensions:
            if extension.oid == ExtensionOID.BASIC_CONSTRAINTS:
                if extension.value.ca:
                    raise ValueError('Subordinate CA requests are forbidden.')
            elif extension.oid == ExtensionOID.EXTENDED_KEY_USAGE:
                if set(extension.value) != {ExtendedKeyUsageOID.CODE_SIGNING}:
                    raise ValueError('Only the Code Signing EKU may be requested.')
            elif extension.oid == ExtensionOID.KEY_USAGE:
                usage = extension.value
                if usage.key_cert_sign or usage.crl_sign or usage.key_encipherment or usage.key_agreement:
                    raise ValueError('Only a signing leaf is permitted.')
            elif extension.oid != ExtensionOID.SUBJECT_KEY_IDENTIFIER:
                raise ValueError('Unexpected requested certificate extension.')
        with self.locked():
            record, root, root_key = self.load()
            revoked = {entry['serial'] for entry in record['revoked']}
            for entry in record['certificates']:
                if entry['csr_sha256'] == expected_sha256.lower() and entry['serial'] in revoked:
                    raise ValueError('A revoked signer key cannot be reissued; generate a fresh VM key.')
                if entry['csr_sha256'] == expected_sha256.lower() and entry['serial'] not in revoked:
                    return self._publish(record, root, root_key)
            if any(entry['serial'] not in revoked for entry in record['certificates']):
                raise ValueError('A different signer is already active; explicit revocation is required before replacement.')
            certificate = (x509.CertificateBuilder().subject_name(expected).issuer_name(root.subject)
                           .public_key(key).serial_number(x509.random_serial_number())
                           .not_valid_before(now() - timedelta(minutes=10))
                           .not_valid_after(min(now() + timedelta(days=365), root.not_valid_after_utc))
                           .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                           .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
                           .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=True)
                           .add_extension(x509.SubjectKeyIdentifier.from_public_key(key), critical=False)
                           .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
                           .add_extension(x509.CRLDistributionPoints([x509.DistributionPoint(
                               full_name=[x509.UniformResourceIdentifier(CRL_URL)], relative_name=None,
                               reasons=None, crl_issuer=None)]), critical=False)
                           .sign(root_key, hashes.SHA256()))
            serial = format(certificate.serial_number, 'X')
            filename = 'issued-' + serial + '.cer'
            atomic(self.state / filename, certificate.public_bytes(serialization.Encoding.DER), 0o600)
            record['certificates'].append({'serial': serial, 'filename': filename,
                'csr_sha256': expected_sha256.lower(), 'sha256': fingerprint(certificate),
                'thumbprint': fingerprint(certificate, hashes.SHA1()), 'issued_at': now().isoformat(),
                'expires_at': certificate.not_valid_after_utc.isoformat()})
            self.save(record)
            return self._publish(record, root, root_key)

    def revoke(self, serial):
        serial = format(int(serial, 16), 'X')
        with self.locked():
            record, root, key = self.load()
            if not any(entry['serial'] == serial for entry in record['certificates']):
                raise ValueError('Unknown certificate serial.')
            if not any(entry['serial'] == serial for entry in record['revoked']):
                record['revoked'].append({'serial': serial, 'at': now().isoformat()})
                self.save(record)
            return self._publish(record, root, key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=Path('/var/lib/company-ai-code-signing'))
    parser.add_argument('--public', type=Path, default=Path('/var/lib/company-ai-code-signing-public'))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init')
    commands.add_parser('refresh-crl')
    issue = commands.add_parser('issue')
    issue.add_argument('--csr', type=Path, required=True)
    issue.add_argument('--expected-sha256', required=True)
    revoke = commands.add_parser('revoke')
    revoke.add_argument('--serial', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Production CA administration requires root on B.')
    ca = Authority(args.state, args.public)
    if args.command == 'init':
        result = ca.initialize()
    elif args.command == 'refresh-crl':
        result = ca.refresh()
    elif args.command == 'issue':
        if args.csr.stat().st_size > 16384:
            raise ValueError('CSR exceeds the fixed limit.')
        result = ca.issue(args.csr.read_bytes(), args.expected_sha256)
    else:
        result = ca.revoke(args.serial)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError):
        raise SystemExit('CA operation failed. Check input/permissions and preserve existing CA state; no private material is printed.') from None
