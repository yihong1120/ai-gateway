"""Private local output sink and format evidence, without host storage dependencies."""

from __future__ import annotations

import csv
import hashlib
import io
import uuid
import zipfile
from types import SimpleNamespace

import pymupdf

from examples.official_document_workflow.domain.calendar.csv_schema import (
    CALENDAR_CSV_FORMAT,
    CALENDAR_HEADERS,
)
from examples.official_document_workflow.infrastructure.eip.calendar_csv import (
    decode_csv,
    parse_csv,
)

from .sample_corpus import dump


class LocalOutputs:
    def __init__(self, folder):
        self.folder = folder
        self.files = []

    async def store(self, **kwargs):
        folder, files = self.folder, self.files
        filename = kwargs["display_name"]
        path = folder / filename
        if path.parent != folder or filename in {".", ".."} or "\\" in filename:
            raise ValueError("Unsafe output filename")
        payload = kwargs["payload"]
        if not isinstance(payload, bytes):
            payload.seek(0)
            payload = payload.read()
        with path.open("xb") as stream:
            stream.write(payload)
        meta = kwargs.get("extra_metadata") or {}
        entry = {
            "name": filename,
            "kind": kwargs["kind"],
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "metadata": meta,
            "filename_within_limit": len(filename.encode()) <= 200,
        }
        if filename.endswith(".pdf"):
            with pymupdf.open(path) as pdf:
                entry["pages"] = len(pdf)
            entry["page_count_matches_metadata"] = entry["pages"] == len(
                meta.get("official_document_output_pages", [])
            )
        if filename.endswith(".docx"):
            from docx import Document

            doc = Document(path)
            entry["editable_characters"] = sum(
                len(node.text or "")
                for node in doc.element.iter(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
                )
            )
            with zipfile.ZipFile(path) as z:
                entry["zip_ok"] = z.testzip() is None
        if filename.endswith(".csv"):
            raw = list(
                csv.DictReader(
                    io.StringIO(
                        decode_csv(payload, CALENDAR_CSV_FORMAT).decode("utf-8-sig")
                    )
                )
            )
            entry.update(
                rows=len(raw),
                headers_complete=all(list(r) == CALENDAR_HEADERS for r in raw),
                all_rows_complete=all(
                    r["Subject"]
                    and r["Start Date"]
                    and r["End Date"]
                    and (
                        r["All day event"] == "真" or r["Start Time"] and r["End Time"]
                    )
                    for r in raw
                ),
            )
            parse_csv(payload, csv_format=CALENDAR_CSV_FORMAT)
            dump(folder / "physical-csv-rows.json", raw)
        files.append(entry)
        return SimpleNamespace(
            id=str(uuid.uuid4()), filename=filename, path=str(path), meta=meta
        )
