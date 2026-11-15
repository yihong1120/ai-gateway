"""Verified post-import attachment bridge for private EIP calendar events.

The EIP CSV importer cannot express attachments.  The browser's own calendar
editor first uploads a file to ``/tw/fm/att_upload.php`` and then submits its
returned file tuple in an ``Update/cm/edit_event`` RPC.  This module mirrors
that two-step contract without changing scheduling, sharing, reminders, or
report status.
"""
from __future__ import annotations

import json
from typing import TypedDict

from examples.official_document_workflow.domain.limits import MAX_EIP_ATTACHMENTS

from . import calendar_repair, calendar_verification


class UploadedAttachment(TypedDict):
    id: str
    filename: str
    url: str
    size: int


def validate_upload(filename: str, payload: bytes):
    """One contract for local preflight and the final upload boundary."""
    if (
        not filename or len(filename.encode('utf-8')) > 240
        or '/' in filename or '\\' in filename or '\x00' in filename
    ):
        raise ValueError('行事曆附件檔名不符合 EIP 限制，未上傳。')
    if not payload or len(payload) > 100_000_000:
        raise ValueError('行事曆附件大小不符合 EIP 限制，未上傳。')


def attachment_parameters(detail, line, user_id, event_id, site_id, attachments, *, project_id='', sharing=None):
    """Preserve an imported event exactly, adding verified attachments."""
    if not 1 <= len(attachments) <= MAX_EIP_ATTACHMENTS:
        raise ValueError('行事曆附件數量不符合限制，未更新行程。')
    for attachment in attachments:
        if (
            any(key not in attachment for key in ('id', 'filename', 'url', 'size'))
            or not str(attachment['id']) or not str(attachment['filename'])
            or not str(attachment['url']).startswith(('/fo/', '/fo1/'))
            or attachment['size'] is True or attachment['size'] is False or attachment['size'] <= 0
        ):
            raise ValueError('EIP 附件上傳資料不符合安全格式，未更新行程。')
    if not site_id.isdecimal():
        raise ValueError('EIP 附件站台資料不正確，未更新行程。')
    # Imported events are eligible only while their existing attachment set is
    # empty.  This avoids overwriting human/manual attachments.  One scheduler
    # run may add several files by reading back after each Update.
    params = calendar_repair.repair_parameters(
        detail, line, user_id, event_id, calendar_repair.fingerprint(detail), project_id=project_id, sharing=sharing,
    )
    for index, attachment in enumerate(attachments):
        params.update({
            f'att_ownerid[{index}]': user_id,
            f'att_id[{index}]': str(attachment['id']),
            f'att_siteid[{index}]': site_id,
            f'att_name[{index}]': str(attachment['filename']),
            f'att_url[{index}]': str(attachment['url']),
            # Local uploader results have no network-drive object id. The
            # official browser serializes JavaScript's undefined value here.
            f'att_oid[{index}]': 'undefined',
        })
    # The deployed editor records attachment-set changes in its history flag.
    params['update_history'] = '1'
    return params


async def attach_event(adapter, event_id, line, files, *, project_id='', sharing=None):
    """Attach a bounded set of files and return a verified receipt."""
    fields = calendar_verification.validate_wire(line, adapter.user_id)
    if not event_id:
        raise ValueError('缺少已驗證的 EIP 行程識別碼，未上傳附件。')
    before = await adapter.calendar_detail(event_id)
    # Prove this is still exactly the private event the importer created before
    # uploading an otherwise unattached temporary file.
    calendar_verification.verify_detail(
        before, fields, adapter.user_id, event_id,
        project_id=project_id, sharing=sharing,
    )
    if not 1 <= len(files) <= MAX_EIP_ATTACHMENTS:
        raise ValueError('行事曆附件數量不符合限制，未上傳。')
    # Validate the edit scope before creating any temporary upload.
    calendar_repair.repair_parameters(
        before, line, adapter.user_id, event_id,
        calendar_repair.fingerprint(before), project_id=project_id, sharing=sharing,
    )
    uploaded = [
        await adapter.upload_calendar_attachment(filename, payload)
        for filename, payload in files
    ]
    params = attachment_parameters(
        before, line, adapter.user_id, event_id,
        await adapter.calendar_site_id(), uploaded, project_id=project_id, sharing=sharing,
    )
    await adapter.rpc('Update', **params)
    after = await adapter.calendar_detail(event_id)
    calendar_verification.verify_detail(
        after, fields, adapter.user_id, event_id,
        project_id=project_id, sharing=sharing,
    )
    encoded = json.dumps(after[1:], ensure_ascii=False, separators=(',', ':'))
    if not all(
        json.dumps(str(item['id']), ensure_ascii=False) in encoded
        and json.dumps(str(item['filename']), ensure_ascii=False) in encoded for item in uploaded
    ):
        raise ValueError('EIP 行程附件回讀不符，請人工核對；未重送。')
    track = getattr(adapter, 'calendar_managed_update', None)
    if track is not None:
        await track(before, after)
    return {
        'event_id': event_id,
        'files': [{'filename': item['filename'], 'size': item['size']} for item in uploaded],
        'attachment_verified': True,
    }
