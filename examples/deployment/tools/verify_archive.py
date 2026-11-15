"""Check versioned archive metadata, or explicitly verify local archive bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

ARCHIVE = Path(__file__).resolve().parents[1] / 'archive'
INDEX = ARCHIVE / 'integrity.json'
PREFIX = PurePosixPath('examples/deployment/archive')


def manifest_entries():
    entries = json.loads(INDEX.read_text())
    if not isinstance(entries, dict) or not entries:
        raise ValueError('Archive manifest must be a nonempty object.')
    checked = []
    for name, expected in entries.items():
        if not isinstance(name, str):
            raise ValueError('Invalid archive entry.')
        relative = PurePosixPath(name)
        if (
            relative.as_posix() != name
            or '..' in relative.parts
            or '\\' in name
            or not relative.is_relative_to(PREFIX)
        ):
            raise ValueError('Invalid archive entry: ' + name)
        local = relative.relative_to(PREFIX)
        if len(local.parts) < 2 or local.parts[0] not in {'dev', 'releases'}:
            raise ValueError('Invalid archive section: ' + name)
        if (
            not isinstance(expected, str)
            or re.fullmatch(r'[0-9a-f]{64}', expected) is None
        ):
            raise ValueError('Invalid archive SHA-256: ' + name)
        checked.append((local, expected))
    return checked


def verify_manifest():
    """A clean checkout needs metadata, not an untracked local archive."""
    return len(manifest_entries())


def verify(archive_dir=None):
    """Strict mode: missing, modified, or symlinked archives are errors."""
    root = (Path(archive_dir) if archive_dir is not None else ARCHIVE).resolve()
    entries = manifest_entries()
    for local, expected in entries:
        path = root.joinpath(*local.parts)
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Unsafe archive path: ' + local.as_posix())
        if not path.is_file():
            raise ValueError('Archive file missing: ' + local.as_posix())
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected:
            raise ValueError(
                'Archive integrity verification failed: ' + local.as_posix()
            )
    return len(entries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        '--manifest-only',
        action='store_true',
        help='Check metadata without reading archive files.',
    )
    modes.add_argument(
        '--archive-dir',
        type=Path,
        help='Directory containing restored dev/ and releases/ archives.',
    )
    args = parser.parse_args()
    if args.manifest_only:
        print(
            f'Validated {verify_manifest()} archive manifest entries; archive bytes were not checked.'
        )
    else:
        print(
            f'Verified {verify(args.archive_dir)} immutable deployment archive files.'
        )


if __name__ == '__main__':
    main()
