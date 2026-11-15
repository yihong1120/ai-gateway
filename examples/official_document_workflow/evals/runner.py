"""Local acceptance runner. Imported only after explicit live execution consent."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from langsmith import tracing_context

from examples.official_document_workflow.application.upload_batch import (
    UploadBatch,
    unique_uploads,
)
from examples.official_document_workflow.domain.documents import PreparedDocument
from examples.official_document_workflow.infrastructure.office.office_input import to_pdf

from .local_outputs import LocalOutputs
from .sample_corpus import dump, sha


async def evaluate(index, name, paths, output, valves):
    folder = output / f"{index:02d}"
    folder.mkdir(mode=0o700)
    from examples.official_document_workflow.adapters.webui import LegacyPipeOperations
    from examples.official_document_workflow.application.pending import PendingClarificationStore
    from examples.official_document_workflow.services.processor import DocumentProcessor

    class EvaluationProcessor(LegacyPipeOperations, DocumentProcessor):
        pass

    pipe = EvaluationProcessor(pending=PendingClarificationStore())
    pipe.valves = pipe.Valves(**valves)
    pipe.valves.document_analysis_cache_enabled = False
    pipe.valves.eip_large_sources = True
    pipe.valves.rename_original_attachment = False
    started = time.monotonic()
    sources = []
    files = []
    batch = None
    calendar_rows = []
    calls = []
    manifest = [
        {"path": str(p.resolve()), "bytes": p.stat().st_size, "sha256": sha(p)}
        for p in paths
    ]
    dump(folder / "inputs.json", manifest)
    original_semantic = pipe.ocr.semantic_source

    def semantic(path, *args, **kwargs):
        value = original_semantic(path, *args, **kwargs)
        (folder / f"source-{len(sources) + 1}.txt").write_text(value.text)
        sources.append(
            {"pdf_sha256": sha(path), "kind": value.kind, "characters": len(value.text)}
        )
        return value

    pipe.ocr.semantic_source = semantic
    original_request = pipe.transport.analyze_text

    async def request(source, **kwargs):
        n = len(calls) + 1
        calls.append({"call": n, "state": "running"})
        if n > 15:
            raise RuntimeError("Live model call cap exceeded")
        dump(
            folder / f"request-{n}.json",
            {"source": source.text, "prompt": kwargs.get("prompt")},
        )
        t = time.monotonic()
        try:
            value = await original_request(source, **kwargs)
            dump(folder / f"response-{n}.json", value)
            calls[n - 1].update(
                state="returned", seconds=round(time.monotonic() - t, 2)
            )
            return value
        except Exception as e:
            calls[n - 1].update(
                state="failed",
                type=type(e).__name__,
                seconds=round(time.monotonic() - t, 2),
            )
            raise

    pipe.transport.analyze_text = request
    original_csv = pipe.values.calendar_csv

    def calendar(rows):
        calendar_rows.extend(rows)
        dump(folder / "calendar-rows.json", calendar_rows)
        return original_csv(rows)

    pipe.values.calendar_csv = calendar

    outputs = LocalOutputs(folder)
    files = outputs.files
    pipe.exports.store_output = outputs.store

    async def emit(event):
        message = event.get("data", {}).get("description", "")
        print(
            json.dumps({"case": index, "progress": message}, ensure_ascii=False),
            flush=True,
        )

    report = {
        "case": name,
        "completed": False,
        "index": index,
        "inputs": manifest,
        "model": pipe.valves.cloud_ocr_text_model,
        "real_ocr": True,
        "real_model_calls": True,
        "storage": "Local test directory only",
        "calendar_written": False,
    }
    try:
        prepared = []
        for n, p in enumerate(paths, 1):
            path = p
            if p.suffix.lower() != ".pdf":
                with p.open("rb") as stream:
                    normalized, _ = await to_pdf(stream, p.name)
                path = folder / f"normalized-input-{n}.pdf"
                try:
                    path.write_bytes(normalized.getvalue())
                finally:
                    normalized.close()
            prepared.append(
                PreparedDocument(
                    kind="pdf" if p.suffix.lower() == ".pdf" else "office",
                    source_files=(
                        SimpleNamespace(
                            id="sample-" + str(index) + "-" + str(n), filename=p.name
                        ),
                    ),
                    pdf_path=path,
                    original_path=p,
                )
            )
        with (
            tracing_context(enabled=False),
            tempfile.TemporaryDirectory(prefix="pages-", dir=folder) as directory,
        ):
            directory = Path(directory)
            unique, origins = await asyncio.to_thread(
                unique_uploads, prepared, directory
            )
            batch = UploadBatch(
                pipe, "local-sample-acceptance", unique, origins, directory, emit
            )
            result = await batch.run()
        (folder / "message.md").write_text(result)
        report["completed"] = bool(files) and not (
            batch.failed_groups
            or batch.pending_groups
            or batch.calendar_pending
            or batch.unread
            or batch.export_failed
            or batch.csv_failed
        )
    except Exception as e:  # noqa: BLE001 — persist failed-case diagnostics before continuing
        report.update(
            completed=False, error={"type": type(e).__name__, "message": str(e)}
        )
        print(
            json.dumps({"case": index, "error": report["error"]}, ensure_ascii=False),
            flush=True,
        )
    finally:
        dump(folder / "analysis.json", batch.payload if batch is not None else {})
        report.update(
            seconds=round(time.monotonic() - started, 2),
            sources=sources,
            files=files,
            model_calls=calls,
            source_files_unchanged=all(
                sha(p) == m["sha256"] for p, m in zip(paths, manifest)
            ),
            csv_rows=len(calendar_rows),
            pending_groups=batch.pending_groups if batch is not None else 0,
            calendar_pending=batch.calendar_pending if batch is not None else [],
            failed_groups=batch.failed_groups if batch is not None else [],
        )
        report["output_checks_passed"] = bool(files) and all(
            f["filename_within_limit"]
            and f.get("page_count_matches_metadata", True)
            and f.get("zip_ok", True)
            and f.get("editable_characters", 1) > 0
            and f.get("headers_complete", True)
            and f.get("all_rows_complete", True)
            for f in files
        )
        dump(folder / "result.json", report)
        print(
            json.dumps(
                {
                    "case": index,
                    "name": name,
                    "completed": report["completed"],
                    "seconds": report["seconds"],
                    "files": len(files),
                    "csv_rows": report["csv_rows"],
                    "checks_passed": report["output_checks_passed"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return report
