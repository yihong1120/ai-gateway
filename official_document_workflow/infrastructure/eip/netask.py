"""Adapter based on the deployed Netask UI's RPC contract (2026-09-05).

No Javascript evaluation, arbitrary URL fetching, sign/approve, or mail APIs.
CM_Import parses CSV; only commit() performs calendar writes.
"""
from __future__ import annotations

import html
import json
import re
import tempfile
from contextlib import AsyncExitStack
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Literal
from typing import NotRequired
from typing import TypedDict
from urllib.parse import quote
from urllib.parse import urljoin
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from . import calendar_attachments
from . import calendar_read
from . import calendar_verification
from official_document_workflow.domain.calendar.csv_schema import protect_eip_csv
from official_document_workflow.infrastructure.office import office_input

ORIGIN = 'http://eip.jiedar.com.tw'
ORIGIN_URL = urlsplit(ORIGIN)
MAX_BYTES = 30 * 1024 * 1024
DocumentFolder = Literal['inbox', 'sign', 'notify', 'sent']
DOCUMENT_FOLDERS: tuple[DocumentFolder, ...] = ('inbox', 'sign', 'notify', 'sent')


class DocumentRecord(TypedDict):
    id: str
    subject: str
    date: str
    folder: DocumentFolder
    due_date: NotRequired[str]
    flow_number: NotRequired[str]
    unread: bool | None
    status: str


class ProjectRecord(TypedDict):
    id: str
    name: str
    members: list[str]


class DocumentField(TypedDict):
    label: str
    value: str


class DocumentDetail(TypedDict):
    fields: list[DocumentField]
    attachments: list[dict[str, str]]


_JS_NUMBER = re.compile(r'-?(?:0|[1-9]\d*)(?:\.\d+)?')
_STRICT_DECODER = json.JSONDecoder(strict=True)
_DOCUMENT_FIELD_LABELS = {
    'Text7': '工區',
    'Text1': '發文單位',
    'Text6': '發文字號',
    'TextArea1': '本文主旨',
    'TextArea2': '說明',
    'TextArea3': '備註',
}


def parse_rpc(text: str, success: int = 0, *, strict=False) -> list:
    match = re.search(r'\bvar\s+data\s*=\s*', text)
    if not match:
        raise ValueError('EIP 登入已失效或回應格式改變，請重新連結帳號。')
    data, _ = json.JSONDecoder(strict=strict).raw_decode(text[match.end():])
    if str(data[0][0]) != str(success):
        raise ValueError()
    return data


class JSArrayResponseParser:
    """Parse PM_List's sparse literal arrays without JavaScript evaluation."""

    def __init__(self, text: str):
        match = re.search(r'\bvar\s+data\s*=\s*', text)
        if not match:
            raise ValueError('EIP 登入已失效或回應格式改變，請重新連結帳號。')
        self.source = text[match.end():]
        self.index = 0

    def spaces(self):
        while self.index < len(self.source) and self.source[self.index] in ' \t\r\n':
            self.index += 1

    def value(self):
        self.spaces()
        if self.index >= len(self.source):
            raise ValueError()
        if self.source[self.index] == '[':
            self.index += 1
            result: list[object] = []
            expecting = True
            while True:
                self.spaces()
                if self.index >= len(self.source):
                    raise ValueError()
                if self.source[self.index] == ']':
                    self.index += 1
                    return result
                if self.source[self.index] == ',':
                    # A sparse element is represented by an empty slot. It is
                    # retained as None only so row offsets remain stable.
                    if expecting:
                        result.append(None)
                    self.index += 1
                    expecting = True
                    continue
                result.append(self.value())
                self.spaces()
                if (
                    self.index >= len(self.source)
                    or self.source[self.index] not in ',]'
                ):
                    raise ValueError()
                if self.source[self.index] == ']':
                    self.index += 1
                    return result
                self.index += 1
                expecting = True
        if self.source[self.index] == '"':
            parsed, used = _STRICT_DECODER.raw_decode(self.source, self.index)
            self.index = used
            return parsed
        for literal, parsed in (('true', True), ('false', False), ('null', None)):
            if self.source.startswith(literal, self.index):
                self.index += len(literal)
                return parsed
        numeric = _JS_NUMBER.match(self.source, self.index)
        if numeric:
            token = numeric[0]
            self.index = numeric.end()
            return int(token) if '.' not in token else float(token)
        raise ValueError()

    def parse(self, success: int = 0) -> list:
        parsed = self.value()
        self.spaces()
        # Ignore unrelated legacy script only after the explicit data boundary.
        if self.index < len(self.source) and self.source[self.index] == ';':
            self.index += 1
        elif self.index != len(self.source):
            raise ValueError()
        if str(parsed[0][0]) != str(success):
            raise ValueError()
        return parsed


def js_escape(value: str) -> str:
    """Netask UTF8_escape is Javascript escape(), NOT encodeURIComponent."""
    safe = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@*_+-./'
    out = []
    raw = value.encode('utf-16-be', errors='strict')
    for i in range(0, len(raw), 2):
        n = int.from_bytes(raw[i: i + 2], 'big')
        out.append(
            chr(n) if chr(n) in safe else f"%{n:02X}" if n < 256 else f"%u{n:04X}",
        )
    return ''.join(out)


def csv_minutes(value: str, *, allow_end_of_day=False) -> str:
    if not value:
        return ''
    if allow_end_of_day and value.replace(' ', '') == '上午24:00:00':
        return '1440'  # Official single-day editor's midnight end boundary.
    m = re.fullmatch(r'(上午|下午)(\d{2}):(\d{2}):(\d{2})', value.replace(' ', ''))
    if not m or not 1 <= int(m[2]) <= 12 or int(m[3]) > 59 or m[4] != '00':
        raise ValueError('EIP 時間格式無法安全轉換，請先調整 CSV（精確到分鐘）。')
    return str((int(m[2]) % 12 + (12 if m[1] == '下午' else 0)) * 60 + int(m[3]))


def import_line(row: list, user_id: str) -> str:
    """Mirror cm_import_sel.htm; personal scope only, never company sharing."""
    if len(row) != 29 or not user_id.isdecimal():
        raise ValueError('EIP 匯入欄位格式不符，已停止匯入。')
    d = [str(x) if x is not None else '' for x in row]
    if d[28] not in ('', '0'):
        if d[28] == '1':
            raise ValueError('EIP 拒絕匯入：行事曆主旨超過 600 字元限制；未送出寫入。')
        raise ValueError('EIP 檢查到不合格的行程欄位，請修正 CSV。')
    if d[5] not in ('真', '假') or (d[5] == '假' and (not d[2] or not d[4])):
        raise ValueError('EIP 未解析出完整行程時間，未執行寫入。')
    for idx in (0, 13, 18, 19, 21):
        d[idx] = js_escape(
            d[idx].replace('%0D%0A', '\r\n') if idx == 19 else d[idx],
        ).replace('+', '%2B')
    for idx in (1, 3, 7):
        d[idx] = d[idx].replace('/', '-')
    for idx in (2, 4, 8):
        d[idx] = csv_minutes(d[idx], allow_end_of_day=idx == 4)
    for idx in (5, 6, 14):
        d[idx] = '1' if d[idx] == '真' else '0'
    d[14] = '1'  # Private flag; d[6] is the reminder flag, not privacy.
    d[20] = {'高': '2', '普通': '1', '低': '0', '': '0'}.get(d[20], '')
    if not d[20] or any(',' in x or '\n' in x or '\r' in x for x in d):
        raise ValueError('EIP 匯入序列含不支援的值，未執行寫入。')
    # One record per commit: Netask persists a bare LF in the last field and
    # emits it unescaped into the Detail JavaScript, breaking the detail page.
    return ','.join(d) + f",Ru{user_id},,Ru{user_id}"


class Netask:
    async def calendar(self, start: str, end: str):
        data = await self.rpc(
            'List', ap_name='cm', start_date=start, end_date=end, __tag='event_list',
        )
        return calendar_read.occurrences(data[1:], start, end)

    async def calendar_projects(self) -> list[ProjectRecord]:
        """Read this user's current EIP participation-project choices.

        Project IDs are validated EIP data and are never supplied by the LLM.
        The list is intentionally fetched at import time, so renamed/newly
        assigned projects are reflected without a hard-coded mapping.
        """

        payload = {
            '__rpcname': 'List',
            '__style': 'html',
            'charset': 'utf8',
            'u_str': self.ustr,
            'ap_name': 'pm',
            'readtype': '1',
            'with_fmsize': '1',
            '__tag': 'PM_List',
        }
        raw = await self.request(
            'POST', '/rpc2.php', data=payload, limit=4 * 1024 * 1024,
        )
        text = raw.decode('utf-8', errors='replace')
        if re.search(r"<input\b[^>]*name=[\"\']_token[\"\']", text, re.I) and re.search(
            r'<form\b', text, re.I,
        ):
            raise ConnectionError('EIP 已返回登入頁，需要重新建立連線。')
        data = JSArrayResponseParser(text).parse()
        projects: list[ProjectRecord] = []
        ids: set[str] = set()
        records = data[1:]
        for record in records:
            if len(record) <= 6:
                raise ValueError()
            project_id, name = (
                str(record[4]),
                calendar_read.unescape(str(record[6])).strip(),
            )
            if (
                not project_id.isdecimal()
                or int(project_id) <= 0
                or not name
                or len(name) > 200
                or any(ord(c) < 32 for c in name)
                or project_id in ids
            ):
                raise ValueError()
            ids.add(project_id)
            members = str(record[12]) if len(record) > 12 else ''
            if not re.fullmatch(r'Ru\d+(?:,Ru\d+)*', members):
                raise ValueError()
            projects.append(
                {
                    'id': project_id,
                    'name': name,
                    'members': sorted(set(members.split(','))),
                },
            )
        return projects

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False,
        )
        self.ustr = ''
        self.user_id = ''
        # The official attachment uploader authenticates with the rolling
        # Authorization response header.  It is session-only and is never
        # persisted with the EIP binding.
        self.upload_token = ''

    async def close(self):
        self.ustr = ''
        self.upload_token = ''
        await self.client.aclose()

    async def request(
        self, method: str, path: str, *, limit=MAX_BYTES, spool=False, **kwargs,
    ):
        url = urljoin(ORIGIN, path)
        parsed_url = urlsplit(url)
        if (
            parsed_url.netloc != ORIGIN_URL.netloc
            or parsed_url.scheme != 'http'
        ):
            raise ValueError('拒絕讀取 EIP 以外的附件網址。')
        async with AsyncExitStack() as cleanup:
            response = await cleanup.enter_async_context(
                self.client.stream(method, url, **kwargs),
            )
            token = response.headers.get('Authorization', '')
            if token:
                self.upload_token = token.removeprefix('Bearer ').strip()
            destination = urlsplit(urljoin(url, response.headers.get('location', '')))
            if response.status_code == 401 or (
                response.status_code in {301, 302, 303, 307, 308}
                and destination.netloc == urlsplit(ORIGIN).netloc
                and destination.path.rstrip('/') == '/login'
            ):
                raise ConnectionError('EIP 登入已失效，需要重新建立連線。')
            if response.status_code in (408, 429, 500, 502, 503, 504):
                raise TimeoutError(
                    'EIP 暫時忙碌或服務不可用；安全讀取將延至下輪，寫入結果不明不重送。',
                )
            if response.status_code != 200:
                raise ValueError('EIP 回應異常或登入失效，請稍後重試／重新連結。')
            chunks = bytearray()
            output = (
                cleanup.enter_context(
                    tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024, mode='w+b'),
                )
                if spool
                else None
            )
            total = 0
            async for block in response.aiter_bytes():
                total += len(block)
                if output is not None:
                    output.write(block)
                    if len(chunks) < 65536:
                        chunks.extend(block[: 65536 - len(chunks)])
                else:
                    chunks.extend(block)
                if limit is not None and total > limit:
                    raise ValueError('EIP 回傳資料超過大小限制，請縮小範圍。')
            prefix = bytes(chunks[:65536]).lstrip().lower()
            if (
                parsed_url.path != '/login'
                and prefix.startswith((b'<!doctype html', b'<html'))
                and re.search(rb"<input\b[^>]*name=[\"\']_token[\"\']", prefix)
                and re.search(rb'<form\b', prefix)
            ):
                raise ConnectionError('EIP 已返回登入頁，需要重新建立連線。')
            if output is not None:
                output.seek(0)
                cleanup.pop_all()
                return output
            return bytes(chunks)

    async def rpc(
        self, name: str, *, success=0, files=None, strict_json=False, **params,
    ) -> list:
        payload = {
            '__rpcname': name,
            '__style': 'html',
            'charset': 'utf8',
            'u_str': self.ustr,
            **params,
        }
        raw = await self.request(
            'POST', '/rpc2.php', data=payload, files=files, limit=4 * 1024 * 1024,
        )
        text = raw.decode('utf-8', errors='replace')
        # Known login form, not arbitrary malformed RPC/permission errors.
        if (
            name != 'Office3Login'
            and re.search(r"<input\b[^>]*name=[\"\']_token[\"\']", text, re.I)
            and re.search(r'<form\b', text, re.I)
        ):
            raise ConnectionError('EIP 已返回登入頁，需要重新建立連線。')
        return parse_rpc(text, success, strict=strict_json)

    async def login(self, account: str, password: str):
        page = (await self.request('GET', '/login')).decode('utf-8', errors='replace')
        token = re.search(r"name=[\"\']_token[\"\'][^>]*value=[\"\']([^\"\']+)", page)
        if not token:
            raise ValueError('EIP 登入頁格式已變更，請聯絡管理員。')
        data = await self.rpc(
            'Office3Login',
            success=1,
            login_name=account,
            login_passwd=password,
            GroupName='imageol',
            ProjName='WEBOFFICE3',
        )
        self.ustr = data[2][1]
        match = re.search(r'u(\d+),', self.ustr)
        self.user_id = match[1] if match else ''
        if not self.user_id:
            raise ValueError()
        landing = (
            await self.request(
                'POST',
                '/tw/findex.php',
                data={
                    '_token': html.unescape(token[1]),
                    'ustr': self.ustr,
                    'lang': 'tw',
                    'sms_open': '',
                },
            )
        ).decode('utf-8', errors='replace')
        # The official browser stores this short-lived JWT in localStorage and
        # sends it only to /tw/fm/att_upload.php.  Keep it in memory for the
        # same authenticated adapter; never write it to the automation DB.
        stored = re.search(
            r"localStorage\.setItem\(\s*['\"]token['\"]\s*,\s*JSON\.stringify\(\s*['\"]([^'\"]+)",
            landing,
        )
        if stored:
            self.upload_token = stored[1]
        self.client.cookies.set(
            'u_str', self.ustr, domain='eip.jiedar.com.tw', path='/',
        )

    async def documents(
        self, folder: DocumentFolder, start: str | None, end: str | None, page: int = 0,
    ) -> list[DocumentRecord]:
        if folder not in DOCUMENT_FOLDERS:
            raise ValueError('不支援此文件匣。')
        if (start is None or end is None) and (
            folder != 'inbox' or start is not None or end is not None
        ):
            raise ValueError('只有待處理文件匣可不指定日期範圍。')
        data = await self.rpc(
            'List',
            ap_name='wf',
            __tag='flow_list',
            curfolder=folder,
            folder_id='0',
            start_date=start + ' 00:00:00' if start is not None else '',
            end_date=end + ' 23:59:59' if end is not None else '',
            language='tw',
            page=str(page),
        )
        # wf_close.htm / wf_receive.htm use the first row's empty ID as the
        # no-record sentinel; the row still carries date-range filter values.
        if len(data) == 2 and len(data[1]) >= 18 and data[1][0] == '':
            return []
        rows: list[DocumentRecord] = []
        for row in data[1:]:
            if len(row) < 20 or not str(row[0]).isdecimal():
                raise ValueError('EIP 文件清單格式改變，已停止處理。')
            # wf_receive.htm: subject=data[3], received=data[7], bold/unread=data[9].
            # Read state is display metadata, not document-processing completion.
            # wf_left.htm maps UI listFilter('send',0) to RPC folder 'sent'.
            # wf_sendbk.htm uses row[7] for sent time, NOT row[8] (planned end).
            # Its row[9] is a due/status flag, not the inbox unread flag.
            unread = (
                None if folder == 'sent' else {'0': False, '1': True}.get(str(row[9]))
            )
            due_date = str(row[8]) if folder == 'inbox' else ''
            if due_date:
                datetime.fromisoformat(due_date)
            rows.append(
                {
                    'id': str(row[0]),
                    'subject': str(row[3]),
                    'date': str(row[7]),
                    'folder': folder,
                    'unread': unread,
                    'status': str(row[10]),
                    'due_date': due_date,
                    'flow_number': str(row[18]),
                },
            )
        return rows

    @staticmethod
    def received_since(doc: DocumentRecord, scan_start: float) -> bool:
        """Validate folder record time (sent time for sent), not document deadlines."""
        value = doc['date']
        if value is None:
            raise ValueError('missing document record time')
        raw = value.strip().replace('/', '-')
        # Date-only metadata cannot establish an exact 24-hour boundary.
        if len(raw) <= 10:
            raise ValueError()
        received = datetime.fromisoformat(raw)
        if received.tzinfo is None:
            received = received.replace(tzinfo=ZoneInfo('Asia/Taipei'))
        return received.timestamp() >= scan_start

    @staticmethod
    def automatic_eligible(doc: DocumentRecord, scan_start: float) -> bool:
        return doc.get('folder') == 'inbox' or Netask.received_since(doc, scan_start)

    async def automatic_documents(
        self,
        scan_start: float,
        folders: tuple[DocumentFolder, ...] = DOCUMENT_FOLDERS,
    ) -> list[DocumentRecord]:
        """Snapshot the user's supported folders, with inbox-only reads using the same implementation."""
        start = (
            datetime.fromtimestamp(scan_start, ZoneInfo('Asia/Taipei'))
            .date()
            .isoformat()
        )
        end = datetime.now(ZoneInfo('Asia/Taipei')).date().isoformat()
        rows: dict[str, DocumentRecord] = {}
        for folder in folders:
            seen_pages = set()
            for page in range(50):
                batch = await self.documents(
                    folder,
                    None if folder == 'inbox' else start,
                    None if folder == 'inbox' else end,
                    page,
                )
                signature = tuple(doc['id'] for doc in batch)
                if batch and signature in seen_pages:
                    raise ValueError(
                        'EIP 分頁重複，已停止本輪掃描；請人工檢查文件清單。',
                    )
                seen_pages.add(signature)
                for doc in batch:
                    record = {**doc, 'folder': folder}
                    if self.automatic_eligible(record, scan_start):
                        # Prefer inbox when the same flow exists in several folders.
                        rows.setdefault(doc['id'], record)
                if len(batch) < 20:
                    break
            else:
                raise ValueError(
                    f"EIP {folder} 文件匣達到 1000 筆掃描上限，已停止本輪；請人工確認範圍。",
                )
        return list(rows.values())

    async def document_detail(self, document_id: str, folder: str) -> DocumentDetail:
        data = await self.rpc(
            'Detail',
            ap_name='wf',
            __tag='flow_detail',
            flow_id=document_id,
            folder=folder,
            status='',
        )
        count = int(data[1][17] or 0)
        if not 0 <= count <= 100 or len(data) < count + 2:
            raise ValueError()
        values: dict[str, str] = {}
        encoded_fields = str(data[1][13])
        if len(encoded_fields) > 500_000:
            raise ValueError('EIP 表單內容超過安全顯示上限。')
        for encoded_field in encoded_fields.split('\x01'):
            name, separator, encoded_value = encoded_field.partition('=')
            if separator and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', name):
                values[name] = calendar_read.unescape(encoded_value)
        fields: list[DocumentField] = []
        for name, label in _DOCUMENT_FIELD_LABELS.items():
            value = values.get(name, '').strip()
            if value:
                fields.append({'label': label, 'value': value})
        for year_name, month_name, day_name, label, position in (
            ('Select1', 'Select2', 'Select3', '收文日期', 1),
            ('Select12', 'Select13', 'Select14', '發文日期', 3),
        ):
            year = values.get(year_name, '').strip()
            month = values.get(month_name, '').strip()
            day = values.get(day_name, '').strip()
            if year and month and day:
                fields.insert(
                    position,
                    {'label': label, 'value': f"{year}年{month}月{day}日"},
                )
        attachments = [
            {'name': str(r[2]), 'url': str(r[3])}
            for r in data[2: count + 2]
            if len(r) >= 7 and Path(str(r[2])).suffix.lower() in office_input.SUFFIXES
        ]
        return {'fields': fields, 'attachments': attachments}

    async def attachments(self, document_id: str, folder: str):
        return (await self.document_detail(document_id, folder))['attachments']

    async def download(self, url: str, name: str = 'attachment.pdf'):
        parsed = urlsplit(urljoin(ORIGIN, url))
        if (
            parsed.username
            or parsed.password
            or not parsed.path.startswith(('/fo/', '/fo1/'))
        ):
            raise ValueError('附件不在允許的 EIP 文件路徑。')
        payload = await self.request(
            'GET', url.replace('fo1/v', 'fo1/d'), limit=None, spool=True,
        )

        with ExitStack() as cleanup:
            cleanup.callback(payload.close)
            office_input.validate(payload, name)
            cleanup.pop_all()
            return payload

    async def stage(self, csv_bytes: bytes):
        # CM_Import applies urldecode to uploaded CSV cells (confirmed against
        # the deployed parser). Protect literal percent/plus once at this
        # boundary: chainage 0K+100 must not become 0K 100, nor %2B become +.
        # Original downloads and expected rows remain byte/text unchanged.
        transport = protect_eip_csv(csv_bytes)
        rows = (
            await self.rpc(
                'Common',
                ap_name='CM_Import',
                __tag='import',
                encoding='UTF-8',
                files={'importfile': ('calendar.csv', transport, 'text/csv')},
            )
        )[1:]
        # Deployed parser emits one empty row for a trailing CSV newline.
        return [
            r
            for r in rows
            if not (len(r) == 29 and not any(r[:28]) and str(r[28]) in ('', '0'))
        ]

    async def commit(self, line: str):

        calendar_verification.validate_wire(line, self.user_id)
        await self.rpc(
            'Update',
            ap_name='cm',
            subitem='import',
            __tag='import_ready',
            language='tw',
            import_data=line,
        )
        # Do not declare success merely because Update returned success.
        # Failure here leaves the reserved journal entry unknown, never resent.
        return await calendar_verification.verify_import(self, line)

    async def calendar_detail(self, event_id):
        if not re.fullmatch(
            r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?;\d+', event_id,
        ):
            raise ValueError('行事曆識別碼格式不符。')
        return (
            await self.rpc(
                'Detail',
                ap_name='cm',
                event_id=event_id,
                __tag='viewdtl',
                strict_json=True,
            )
        )[1:]

    async def calendar_site_id(self):
        """Read the current account's attachment site ID from the EIP contract."""
        data = await self.rpc(
            'PerManDepList',
            ProjName='WEBOFFICE3',
            update_cache='0',
            __tag='isDepLst',
            strict_json=True,
        )
        site_id = str(data[1][4])
        if not site_id.isdecimal():
            raise ValueError('無法確認 EIP 附件站台，未上傳檔案。')
        return site_id

    async def upload_calendar_attachment(self, filename: str, payload: bytes):
        """Upload a private temporary file through EIP's documented UI endpoint.

        Uploading alone does not attach the file to an event.  Callers must
        reserve a write receipt first, then update and read back the event.
        """

        calendar_attachments.validate_upload(filename, payload)
        if not self.upload_token:
            raise ValueError('EIP 未提供附件上傳授權，未上傳。')
        raw = await self.request(
            'POST',
            '/tw/fm/att_upload.php?filename=' + quote(filename, safe=''),
            content=payload,
            headers={
                'Authorization': 'Bearer ' + self.upload_token,
                'Content-Type': 'application/octet-stream',
            },
            limit=512 * 1024,
        )
        result = json.loads(raw.decode('utf-8'))
        item = result['data']
        file_id, returned_name, url, size = (
            str(item['id']),
            str(item['filename']),
            str(item['url']),
            int(item['size']),
        )
        if (
            result.get('status') is not True
            or returned_name != filename
            or not file_id
            or size != len(payload)
            or not url.startswith(('/fo/', '/fo1/'))
        ):
            raise ValueError('EIP 附件上傳未通過回應核對，請人工核對；未重送。')
        return {'id': file_id, 'filename': returned_name, 'url': url, 'size': size}
