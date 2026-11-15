"""Explicitly download one owned EIP document's attachments for local acceptance.

Credentials are read from EIP_SAMPLE_ACCOUNT / EIP_SAMPLE_PASSWORD and the
expected identity from EIP_SAMPLE_IDENTITY. No WebUI or calendar writes.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
from datetime import date
from pathlib import Path

from .sample_corpus import dump, sha


async def download(args, adapter):
    """An injected session owns login, access checks and bounded download validation."""
    folder = args.output
    try:
        identity = os.environ["EIP_SAMPLE_IDENTITY"]
        if not identity:
            raise ValueError("Expected EIP identity is required")
        await adapter.login(
            os.environ["EIP_SAMPLE_ACCOUNT"], os.environ["EIP_SAMPLE_PASSWORD"]
        )
        if adapter.user_id != identity:
            raise ValueError("EIP identity mismatch")
        documents = await adapter.documents(args.folder, args.start, args.end)
        if not any(str(d["id"]) == args.document_id for d in documents):
            raise ValueError(
                "Document absent from the selected owned document list/page"
            )
        attachments = await adapter.attachments(args.document_id, args.folder)
        if not attachments:
            raise ValueError("Document contains no attachments")
        names = [a["name"] for a in attachments]
        if len(set(names)) != len(names) or any(
            not n or Path(n).name != n or n in {".", ".."} or "\\" in n for n in names
        ):
            raise ValueError("Unsafe or duplicate attachment names")
        folder.mkdir(mode=0o700, parents=True, exist_ok=False)
        inputs = folder / "inputs"
        inputs.mkdir(mode=0o700)
        for attachment in attachments:
            stream = await adapter.download(attachment["url"], attachment["name"])
            try:
                with (inputs / attachment["name"]).open("xb") as target:
                    shutil.copyfileobj(stream, target)
            finally:
                stream.close()
        # Written only after every download succeeds. Partial directories are
        # retained for diagnosis; they never become an accepted corpus manifest.
        dump(folder / "manifest.json", [{"name": "eip-document", "files": names}])
        dump(
            folder / "downloads.json",
            [
                {
                    "name": n,
                    "sha256": sha(inputs / n),
                    "bytes": (inputs / n).stat().st_size,
                }
                for n in names
            ],
        )
    finally:
        await adapter.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument(
        "--folder", choices=["inbox", "sign", "notify", "sent"], required=True
    )
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not follow --end")
    args.start, args.end = args.start.isoformat(), args.end.isoformat()
    if args.config:
        from examples.shared.config import read_service

        for key, value in read_service('eip_samples', args.config).items():
            os.environ.setdefault(key, value)
    os.umask(0o077)
    from examples.official_document_workflow.infrastructure.eip.netask import Netask

    asyncio.run(download(args, Netask()))
    print(
        f"下載完成：{args.output}。以 sample_corpus --input-root {args.output / 'inputs'} "
        f"--manifest {args.output / 'manifest.json'} 檢查；加 --execute 才會呼叫 OCR／模型。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
