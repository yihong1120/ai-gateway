#!/usr/bin/env python3
"""Small, single-process PaddleOCR HTTP service.

This intentionally bypasses PaddleX's generic ``paddlex --serve`` wrapper.
PaddleX uses Paddle's process-global device selection, so the model is loaded
and its ``predict`` generator is consumed in the same Uvicorn event-loop
thread. Do not run this application with more than one Uvicorn worker: every
worker would load another copy of the GPU models.

The ``/layout-parsing`` request and success response match the subset of the
PaddleX PaddleOCR-VL serving API used by
``openwebui/functions/official_document_processor_pipe.py``. In particular it
accepts a *raw base64* PDF with ``fileType: 0`` and returns
``result.layoutParsingResults[*].prunedResult`` plus Markdown.

The default backend is PaddleX's conventional GPU ``OCR`` pipeline
(PP-OCRv6-medium). It is deliberately not the much slower native
PaddleOCR-VL backend. ``maxNewTokens`` and layout/VLM request fields remain
accepted for compatibility but have no effect on conventional OCR.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from paddlex.inference.serving.basic_serving._pipeline_apps._common import (
    common as serving_common,
)
from paddlex.inference.serving.infra import utils as serving_utils
from paddlex.inference.serving.schemas.paddleocr_vl import (
    InferRequest as PaddleOCRVLInferRequest,
)
# These are the exact request schema and output conversion helpers used by
# PaddleX's bundled ``paddlex --serve`` apps. We only replace its threaded
# PipelineWrapper and adapt conventional OCR output to the layout API contract.


LOGGER = logging.getLogger('paddleocr_direct_server')
HERE = Path(__file__).resolve().parent
# The default was benchmarked successfully on this host.  A custom YAML can
# still be selected deliberately with PADDLEOCR_PIPELINE_CONFIG, but the
# checked-in PaddleOCR-VL YAML is never selected implicitly.
PIPELINE_NAME = os.environ.get('PADDLEOCR_PIPELINE', 'OCR')
PIPELINE_CONFIG_VALUE = os.environ.get('PADDLEOCR_PIPELINE_CONFIG')
DEVICE = os.environ.get(
    'PADDLEOCR_DEVICE', os.environ.get('PADDLEOCR_VL_DEVICE', 'gpu:0'),
)
MAX_INPUT_BYTES = int(
    os.environ.get(
        'PADDLEOCR_MAX_INPUT_BYTES',
        os.environ.get('PADDLEOCR_VL_MAX_INPUT_BYTES', 30 * 1024 * 1024),
    ),
)
MAX_INPUT_PAGES = int(
    os.environ.get(
        'PADDLEOCR_MAX_INPUT_PAGES',
        os.environ.get('PADDLEOCR_VL_MAX_INPUT_PAGES', 10),
    ),
)
MAX_OUTPUT_IMAGE_SIZE = (2000, 2000)


class LayoutParsingRequest(PaddleOCRVLInferRequest):
    """PaddleOCR-VL wire schema plus optional conventional-OCR controls."""

    useTextlineOrientation: bool | None = None
    textDetLimitSideLen: int | None = None
    textDetLimitType: str | None = None
    textDetThresh: float | None = None
    textDetBoxThresh: float | None = None
    textDetUnclipRatio: float | None = None
    textRecScoreThresh: float | None = None
    returnWordBox: bool | None = None


def _log_id(value: str | None = None) -> str:
    """Keep a caller-supplied PaddleX log ID when present."""
    return value or str(uuid.uuid4())


def _error_response(status_code: int, detail: str, log_id: str | None = None) -> JSONResponse:
    """Return PaddleX's documented error envelope without leaking input data."""
    return JSONResponse(
        status_code=status_code,
        content={
            'logId': _log_id(log_id),
            'errorCode': status_code,
            'errorMsg': detail,
        },
    )


def _decode_base64_input(request: LayoutParsingRequest) -> tuple[list[Any], Any]:
    """Decode one base64 PDF/image exactly once, then use PaddleX's renderer."""
    if request.fileType not in (0, 1):
        raise HTTPException(
            status_code=422,
            detail='fileType is required: use 0 for a PDF or 1 for an image',
        )

    # Reject oversized input before allocating a very large decoded byte string.
    max_base64_chars = ((MAX_INPUT_BYTES + 2) // 3) * 4
    if len(request.file) > max_base64_chars:
        raise HTTPException(
            status_code=413,
            detail=f"Input exceeds the {MAX_INPUT_BYTES // (1024 * 1024)} MiB limit",
        )

    try:
        payload = base64.b64decode(request.file, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail='file must be raw base64 data') from exc

    if len(payload) > MAX_INPUT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Input exceeds the {MAX_INPUT_BYTES // (1024 * 1024)} MiB limit",
        )
    if request.fileType == 0 and not payload.startswith(b'%PDF-'):
        raise HTTPException(status_code=422, detail='fileType 0 requires a PDF')

    try:
        return serving_utils.file_to_images(
            payload,
            'PDF' if request.fileType == 0 else 'IMAGE',
            max_num_imgs=MAX_INPUT_PAGES,
        )
    except serving_utils.ImageTooLargeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        # Do not return decoder implementation details or document contents.
        LOGGER.info('Could not decode PaddleOCR input: %s', type(exc).__name__)
        raise HTTPException(status_code=422, detail='Invalid input PDF or image') from exc


def _run_pipeline(
    pipeline: Any, images: list[Any], request: LayoutParsingRequest,
) -> list[Any]:
    """Run the OCR generator in this thread; never hand Paddle's device to a worker."""
    predictions = pipeline.predict(
        images,
        use_doc_orientation_classify=request.useDocOrientationClassify,
        use_doc_unwarping=request.useDocUnwarping,
        use_textline_orientation=request.useTextlineOrientation,
        text_det_limit_side_len=request.textDetLimitSideLen,
        text_det_limit_type=request.textDetLimitType,
        text_det_thresh=request.textDetThresh,
        text_det_box_thresh=request.textDetBoxThresh,
        text_det_unclip_ratio=request.textDetUnclipRatio,
        text_rec_score_thresh=request.textRecScoreThresh,
        return_word_box=request.returnWordBox,
    )

    results: list[Any] = []
    # PaddleX returns a generator.  Explicitly close it on a client disconnect
    # or a downstream conversion failure, matching its bundled server wrapper.
    with contextlib.closing(predictions) as iterator:
        for item in iterator:
            if isinstance(item, dict) and set(item) == {'error'}:
                raise RuntimeError(item['error'])
            results.append(item)

    return results


def _encode_images(
    images: dict[str, Any] | None,
    log_id: str,
    filename_template: str,
) -> dict[str, str]:
    """Match PaddleX's base64 image response without configuring file storage."""
    if not images:
        return {}
    return serving_common.postprocess_images(
        images,
        log_id,
        filename_template=filename_template,
        file_storage=None,
        return_urls=False,
        max_img_size=MAX_OUTPUT_IMAGE_SIZE,
    )


def _result_entry(
    item: Any,
    input_image: Any,
    request: LayoutParsingRequest,
    log_id: str,
    page_index: int,
) -> dict[str, Any]:
    """Map an OCR result into the layout-parsing shape consumed by Open WebUI."""
    ocr_result = serving_common.prune_result(item.json['res'])
    texts = ocr_result.get('rec_texts', [])
    page_text = '\n'.join(
        text.strip() for text in texts if isinstance(text, str) and text.strip()
    )
    height, width = input_image.shape[:2]

    # The Open WebUI Pipe consumes this field, not PaddleX's normal ``rec_texts``
    # response. One full-page OCR block retains all detected text in reading
    # order and avoids losing lines to a layout/VLM postprocessor.
    pruned_result = {
        'parsing_res_list': [
            {
                'block_label': 'ocr',
                'block_content': page_text,
                'block_bbox': [0, 0, int(width), int(height)],
                'block_id': 0,
                'block_order': 1,
                'group_id': 0,
            },
        ],
        # Retain the native recognition details for diagnostics without making
        # consumers depend on the non-layout PaddleX response schema.
        'ocrResult': ocr_result,
    }

    entry: dict[str, Any] = {
        'prunedResult': pruned_result,
        'markdown': {
            'text': page_text,
            # Conventional OCR has no Markdown image asset map. ``images`` is
            # optional in the PaddleOCR-VL response model and Open WebUI only
            # consumes text blocks, so this stays wire-compatible.
            'images': None,
        },
    }

    if request.visualize:
        visualized = _encode_images(
            {'input_img': input_image, **item.img},
            log_id,
            f"{{key}}_{page_index}.jpg",
        )
        entry['inputImage'] = visualized.get('input_img')
        entry['outputImages'] = {
            key: value for key, value in visualized.items() if key != 'input_img'
        }
    return entry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load precisely one local GPU OCR pipeline before accepting requests."""
    if PIPELINE_CONFIG_VALUE:
        pipeline_source = str(Path(PIPELINE_CONFIG_VALUE).expanduser().resolve())
        if not Path(pipeline_source).is_file():
            raise RuntimeError(f"PaddleOCR pipeline config does not exist: {pipeline_source}")
    else:
        pipeline_source = PIPELINE_NAME

    # Import only while starting up so importing this module does not load a
    # model. PaddleX propagates ``device`` to each PP-OCR model.
    from paddlex import create_pipeline

    LOGGER.info('Loading PaddleOCR pipeline=%s device=%s', pipeline_source, DEVICE)
    app.state.pipeline = create_pipeline(pipeline_source, device=DEVICE)
    app.state.device = DEVICE
    app.state.pipeline_source = pipeline_source
    try:
        yield
    finally:
        # Process shutdown frees the Paddle allocation.  Removing the reference
        # also prevents a stale object from being reported as healthy.
        app.state.pipeline = None


app = FastAPI(title='PaddleOCR direct layout-compatible service', lifespan=lifespan)


@app.get('/health', include_in_schema=False)
async def health() -> dict[str, str]:
    if getattr(app.state, 'pipeline', None) is None:
        raise HTTPException(status_code=503, detail='PaddleOCR is not ready')
    return {
        'status': 'ok',
        'device': app.state.device,
        'pipeline': app.state.pipeline_source,
    }


@app.post('/layout-parsing', response_model_exclude_none=True)
async def layout_parsing(request: LayoutParsingRequest) -> dict[str, Any]:
    """Process one base64 document with the GPU-resident PP-OCR pipeline."""
    if request.outputFormats:
        raise HTTPException(
            status_code=422,
            detail='This direct service does not implement outputFormats; use Markdown/prunedResult',
        )
    if request.restructurePages:
        raise HTTPException(
            status_code=422,
            detail='This direct OCR service does not implement restructurePages',
        )

    pipeline = getattr(app.state, 'pipeline', None)
    if pipeline is None:
        raise HTTPException(status_code=503, detail='PaddleOCR is not ready')

    log_id = _log_id(request.logId)
    images, data_info = _decode_base64_input(request)
    if not images:
        raise HTTPException(status_code=422, detail='Input contains no renderable pages')

    # This is intentionally synchronous inside an async route.  There is no
    # await between model creation and predict(), so a one-worker Uvicorn server
    # serializes requests without moving native Paddle inference to another
    # thread (the behavior this service is meant to test).
    results = _run_pipeline(pipeline, images, request)
    if not results:
        raise HTTPException(status_code=500, detail='PaddleOCR returned no pages')

    entries = [
        _result_entry(item, image, request, log_id, index)
        for index, (image, item) in enumerate(zip(images, results))
    ]
    return {
        'logId': log_id,
        'result': {
            'layoutParsingResults': entries,
            'dataInfo': data_info,
        },
        'errorCode': 0,
        'errorMsg': 'Success',
    }


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
    return _error_response(422, 'Invalid request')


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else 'Request failed'
    return _error_response(exc.status_code, detail)


@app.exception_handler(Exception)
async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    LOGGER.exception('PaddleOCR request failed: %s', type(exc).__name__)
    return _error_response(500, 'Internal server error')


def main() -> None:
    """Run one loopback-only Uvicorn worker for the GPU-resident OCR model."""
    import uvicorn

    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO').upper())
    uvicorn.run(
        app,
        host=os.environ.get(
            'PADDLEOCR_HOST', os.environ.get('PADDLEOCR_VL_HOST', '127.0.0.1'),
        ),
        port=int(
            os.environ.get(
                'PADDLEOCR_PORT', os.environ.get('PADDLEOCR_VL_PORT', '8113'),
            ),
        ),
        workers=1,
    )


if __name__ == '__main__':
    main()
