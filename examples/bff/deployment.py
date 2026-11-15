"""Build and verify the complete identity/BFF source bundle from one source tree."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import zipfile
import tempfile
from types import MappingProxyType
from typing import NamedTuple, Mapping

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ('identity', 'bff', 'db_management', 'deployment/registry', 'shared')
BUNDLE = 'identity-services.zip'
MANIFEST = 'bff-artifact.json'
ENTRY_FILES = ('examples/__init__.py', 'examples/deployment/__init__.py', 'examples/deployment/cli.py')
OBSOLETE_FILES = ('examples/identity/models.py', 'examples/configuration.py')


class VerifiedBundle(NamedTuple):
    manifest: dict
    files: Mapping[str, bytes]

    def stage(self, directory):
        """Publish a new immutable source root without touching a host checkout."""
        directory = Path(directory)
        if directory.is_symlink():
            raise ValueError('Release directory must not be a symlink.')
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / self.manifest['sha256']
        if target.exists():
            self.verify(target)
            return target
        with tempfile.TemporaryDirectory(prefix='.identity-', dir=directory) as temporary:
            temporary = Path(temporary)
            for name, value in self.files.items():
                path = temporary / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
            (temporary / 'release.json').write_text(json.dumps(self.manifest, sort_keys=True, indent=2) + '\n')
            try:
                temporary.rename(target)
            except FileExistsError:
                self.verify(target)
        return target

    def verify(self, target):
        target = Path(target)
        entries = list(target.rglob('*'))
        if target.is_symlink() or any(path.is_symlink() for path in entries):
            raise ValueError('Release tree contains symlinks.')
        if {path.relative_to(target).as_posix() for path in entries if path.is_file()} != set(self.files) | {'release.json'}:
            raise ValueError('Release tree contains unexpected files.')
        if json.loads((target / 'release.json').read_text()) != self.manifest:
            raise ValueError('Release metadata changed.')
        for name, value in self.files.items():
            if (target / name).read_bytes() != value:
                raise ValueError('Release source changed: ' + name)


def allowed(name):
    path = PurePosixPath(name)
    return (
        name != 'examples/identity/models.py' and not path.is_absolute() and '..' not in path.parts and '\\' not in name
        and (name in ENTRY_FILES or (
            path.suffix == '.py' and '__pycache__' not in path.parts
            and any(
                path.is_relative_to(PurePosixPath('examples') / package)
                and 'deployment' not in path.relative_to(PurePosixPath('examples') / package).parts
                for package in PACKAGES
            )
        ))
    )


def build(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    files = [ROOT / name for name in ENTRY_FILES]
    for package in PACKAGES:
        files.extend((ROOT / 'examples' / package).rglob('*.py'))
    payloads = {}
    for path in sorted(files):
        name = path.relative_to(ROOT).as_posix()
        if allowed(name):
            if path.is_symlink():
                raise ValueError('Deployment source must not be a symlink.')
            payloads[name] = path.read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payloads.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    data = buffer.getvalue()
    manifest = {
        'format': 2, 'source': 'examples/bff/proxy.py',
        'entrypoint': 'examples.db_management.app:app',
        'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
        'files': [{'name': name, 'sha256': hashlib.sha256(value).hexdigest(), 'bytes': len(value)}
                  for name, value in sorted(payloads.items())],
        'obsolete_files': list(OBSOLETE_FILES),
    }
    (destination / BUNDLE).write_bytes(data)
    (destination / MANIFEST).write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    return manifest


def read_bundle(destination):
    destination = Path(destination)
    manifest = json.loads((destination / MANIFEST).read_text())
    data = (destination / BUNDLE).read_bytes()
    if (manifest.get('format') != 2 or manifest.get('source') != 'examples/bff/proxy.py'
            or manifest.get('entrypoint') != 'examples.db_management.app:app'
            or manifest.get('obsolete_files') != list(OBSOLETE_FILES)
            or len(data) != manifest.get('bytes')
            or hashlib.sha256(data).hexdigest() != manifest.get('sha256')):
        raise ValueError('Identity/BFF bundle does not match its manifest.')
    entries = manifest.get('files', [])
    expected = {row['name']: row for row in entries}
    if not expected or len(expected) != len(entries) or not all(allowed(name) for name in expected):
        raise ValueError('Invalid identity/BFF manifest paths.')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.namelist()) != len(expected) or set(archive.namelist()) != set(expected):
            raise ValueError('Unexpected identity/BFF bundle contents.')
        payloads = {}
        for name, entry in expected.items():
            value = archive.read(name)
            if len(value) != entry['bytes'] or hashlib.sha256(value).hexdigest() != entry['sha256']:
                raise ValueError('Identity/BFF module integrity check failed.')
            payloads[name] = value
    return VerifiedBundle(manifest, MappingProxyType(payloads))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    print(json.dumps(build(parser.parse_args().destination), sort_keys=True))
