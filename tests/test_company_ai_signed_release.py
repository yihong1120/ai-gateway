"""Signed release upload boundaries and reviewed executable source preservation."""
import hashlib
import importlib.util
from pathlib import Path
import zipfile
import struct

import pytest

spec = importlib.util.spec_from_file_location('signed_release', Path(__file__).resolve().parents[1] /
    'examples/company_ai_access/deployment/production/signed_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

SIGNATURE = '\r\n# SIG # Begin signature block\r\n# ABC\r\n# SIG # End signature block\r\n'


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'utf-16'])
def test_native_signature_encoding_and_added_separator_preserve_source(encoding):
    original = b"Write-Output 'reviewed'\n"
    signed = (original.decode().replace('\n', '\r\n') + SIGNATURE).encode(encoding)
    release.assert_source_unchanged(original, signed)


@pytest.mark.parametrize('change', ['source', 'after-signature', 'duplicate-signature'])
def test_rejects_executable_changes_or_ambiguous_signature(change):
    original = b"Write-Output 'reviewed'\n"
    text = original.decode() + SIGNATURE
    if change == 'source': text = text.replace('reviewed', 'unreviewed')
    elif change == 'after-signature': text += 'Write-Output unreviewed'
    else: text += SIGNATURE
    with pytest.raises(ValueError): release.assert_source_unchanged(original, text.encode())


@pytest.mark.parametrize('name', ['../escape.ps1', '/absolute.ps1', 'nested\\escape.ps1'])
def test_archive_rejects_unsafe_paths_before_extracting(tmp_path, name):
    archive = tmp_path / 'upload.zip'
    with zipfile.ZipFile(archive, 'w') as z: z.writestr(name, b'unsafe')
    with pytest.raises(ValueError):
        release.read_zip(archive, hashlib.sha256(archive.read_bytes()).hexdigest())


def test_upload_requires_independent_hash_and_exact_public_file_set(tmp_path):
    archive = tmp_path / 'upload.zip'
    with zipfile.ZipFile(archive, 'w') as z: z.writestr('private-key.pem', b'unexpected')
    with pytest.raises(ValueError): release.read_zip(archive, '0' * 64)
    with pytest.raises(ValueError):
        release.read_zip(archive, hashlib.sha256(archive.read_bytes()).hexdigest(), public=True)


def test_exe_signing_allows_only_authenticode_header_changes_and_appended_certificate():
    original=bytearray(512);original[:2]=b'MZ';struct.pack_into('<I',original,0x3c,128)
    original[128:132]=b'PE\0\0';struct.pack_into('<H',original,152,0x10b)
    original[400:408]=b'reviewed'
    signed=bytearray(original)+bytearray(32)
    struct.pack_into('<I',signed,216,12345)
    struct.pack_into('<II',signed,280,len(original),32)
    release.assert_executable_unchanged(bytes(original),bytes(signed))
    signed[400]=ord('X')
    with pytest.raises(ValueError):release.assert_executable_unchanged(bytes(original),bytes(signed))
