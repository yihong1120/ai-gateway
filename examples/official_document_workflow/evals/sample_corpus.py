"""Inspect a local corpus; --execute explicitly enables configured OCR/model calls."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from examples.official_document_workflow.infrastructure.office.office_input import (
    SUFFIXES,
)


class CaseDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, pattern=r'\S')
    files: list[str] = Field(min_length=1)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def dump(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def cases(root, manifest=None):
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Input root must be a directory")
    entries = (
        json.loads(manifest.read_text(encoding="utf-8"))
        if manifest
        else [
            {"name": p.stem, "files": [p.name]}
            for p in sorted(root.iterdir())
            if p.is_file() and p.suffix.lower() in SUFFIXES
        ]
    )
    if not isinstance(entries, list) or not entries:
        raise ValueError("Corpus must contain at least one case")
    result = []
    for entry in entries:
        try:
            case = CaseDefinition.model_validate(entry)
        except ValidationError:
            raise ValueError('Every case needs a name and a nonempty list of file paths') from None
        paths = []
        for filename in case.files:
            path = (root / filename).resolve(strict=True)
            if (
                not path.is_relative_to(root)
                or not path.is_file()
                or path.suffix.lower() not in SUFFIXES
            ):
                raise ValueError("Input must be a supported document inside input root")
            paths.append(path)
        result.append((case.name, paths))
    return result


def select_cases(value, count):
    if value is None:
        return set(range(1, count + 1))
    try:
        selected = {int(part) for part in value.split(',')}
    except ValueError:
        raise ValueError('Unknown or empty case selection') from None
    if not selected or selected - set(range(1, count + 1)):
        raise ValueError('Unknown or empty case selection')
    return selected


def inventory(available, selected=None):
    return [
        {
            "index": i,
            "name": name,
            "files": [
                {"path": str(p), "bytes": p.stat().st_size, "sha256": sha(p)}
                for p in paths
            ],
        }
        for i, (name, paths) in enumerate(available, 1)
        if selected is None or i in selected
    ]


def settings(path):
    if path:
        value = json.loads(path.read_text(encoding="utf-8"))
    else:
        import psycopg

        with psycopg.connect(
            os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on"
        ) as db:
            row = db.execute(
                "SELECT valves FROM function WHERE id=%s",
                ("official_document_processor",),
            ).fetchone()
            if not row:
                raise ValueError("Document function settings not found")
            value = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
    if not isinstance(value, dict):
        raise TypeError("Settings must be a JSON object")
    return value


async def execute(args, available, selected):
    # Import service/host dependencies only after the explicit execution flag.
    from .runner import evaluate

    valves = settings(args.settings)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    dump(args.output / "inventory.json", inventory(available, selected))
    package = Path(__file__).resolve().parents[1]
    dump(
        args.output / "code-hashes.json",
        {str(p.relative_to(package)): sha(p) for p in sorted(package.rglob("*.py"))},
    )
    results = []
    for i, (name, paths) in enumerate(available, 1):
        if i not in selected:
            continue
        # Only generated ordinal names become paths; display names stay in JSON.
        result = await evaluate(i, name, paths, args.output, valves)
        results.append(result)
        dump(args.output / "execution.json", results)
        if (
            {result.get("error", {}).get("type")}
            | {c.get("type") for c in result["model_calls"]}
        ) & {
            "CloudRateLimited",
            "CloudTemporaryFailure",
            "CloudExtractionDeferred",
        }:
            break
    return int(
        len(results) != len(selected)
        or any(
            not r["completed"]
            or not r["output_checks_passed"]
            or not r["source_files_unchanged"]
            for r in results
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument(
        "--manifest", type=Path, help="JSON list of {name, files: [relative paths]}"
    )
    parser.add_argument("--cases", help="Comma-separated inventory ordinals")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--output", type=Path, help="New private directory, required with --execute"
    )
    parser.add_argument(
        "--settings",
        type=Path,
        help="Valves JSON; otherwise read WebUI PostgreSQL settings",
    )
    parser.add_argument(
        "--config", type=Path, help="Explicit environment file, execution only"
    )
    args = parser.parse_args()
    try:
        available = cases(args.input_root, args.manifest)
        selected = select_cases(args.cases, len(available))
    except ValueError as error:
        parser.error(str(error))
    except OSError:
        parser.error('Unable to read the input root or corpus manifest')
    if not args.execute:
        print(
            json.dumps(
                inventory(available, selected),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not args.output:
        parser.error("--execute requires --output NEW_DIRECTORY")
    if args.config:
        from examples.shared.config import read_service

        for key, value in read_service('webui', args.config).items():
            os.environ.setdefault(key, value)
    os.umask(0o077)
    return asyncio.run(execute(args, available, selected))


if __name__ == "__main__":
    raise SystemExit(main())
