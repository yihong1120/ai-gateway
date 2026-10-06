"""CAD format policy; execution uses the shared offline sandbox."""
from __future__ import annotations

from company_agent.services.sandbox import sandbox_worker

IMAGE = 'ai-gateway/company-cad:1'
NOTICE = (
    'CAD 僅結構分析；未知物件、外參和字型可能不完整。不得據此宣稱已驗證整張施工圖或工程安全。'
)


def cad_worker(operation, data=None, suffix='.dxf', **args):
    if operation not in {'read', 'create', 'edit'} or suffix not in {'.dwg', '.dxf'}:
        raise ValueError('CAD 操作不支援。')
    if suffix == '.dwg' and operation != 'read':
        raise ValueError('DWG 僅提供唯讀分析，不提供重存。')
    if data is not None and (not data or len(data) > 20_000_000):
        raise ValueError('CAD 單檔上限 20 MB。')
    if suffix == '.dwg' and (
        data is None
        or data[:6]
        not in {b'AC1014', b'AC1015', b'AC1018', b'AC1021', b'AC1024', b'AC1027', b'AC1032'}
    ):
        raise ValueError('DWG 檔案簽章或版本不支援。')
    if operation == 'read' and (
        args.get('view', 'summary') not in {'summary', 'texts', 'layers', 'entities'}
        or not 0 <= args.get('start', 0) <= 200000
        or not 1 <= args.get('limit', 10) <= 10
        or len(args.get('query', '')) > 100
    ):
        raise ValueError('CAD 分頁參數不正確。')
    request = {'operation': operation, 'view': 'summary', 'start': 0, 'limit': 10, 'query': '', **args}
    entrypoint = (
        ('/usr/share/dotnet/dotnet', '/app/CadReader.dll') if suffix == '.dwg'
        else ('/opt/cad-python/bin/python', '/app/dxf_worker.py')
    )
    return sandbox_worker(
        IMAGE, request, data=data, suffix=suffix, entrypoint=entrypoint,
        output=None if operation == 'read' else 'result.dxf',
    )


def read_results(receipts):
    """Summarize successful CAD reads by source version."""
    fields = (
        'root', 'path', 'sha256', 'entity_count', 'layer_count',
        'block_count', 'external_reference_count',
    )
    return list({
        (r.get('root'), r.get('path'), r['sha256']): {key: r.get(key) for key in fields}
        for r in receipts if r and r.get('sha256')
    }.values())
