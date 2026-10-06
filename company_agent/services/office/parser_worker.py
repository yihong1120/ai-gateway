"""Resource-limited parser process, NOT a replacement for an OS sandbox."""
from __future__ import annotations

import base64
import json
import resource
import sys

from company_agent.services.office import documents


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_AS, (1_500_000_000, 1_500_000_000))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    request = json.loads(sys.stdin.buffer.read(70_000_001))
    operation = request['operation']
    args = request['args']
    if 'data' in args:
        args['data'] = base64.b64decode(args['data'], validate=True)
    from company_agent.services.office.spreadsheet_edit import edit_spreadsheet

    funcs = {
        'edit_spreadsheet': edit_spreadsheet,
        'extract': documents.extract,
        'edit': documents.edit,
        'word': documents.create_word,
        'slides': documents.create_slides,
        'spreadsheet': documents.create_spreadsheet,
        'read_spreadsheet': documents.read_spreadsheet,
        'analyze_spreadsheet': documents.analyze_spreadsheet,
    }
    result = funcs[operation](**args)
    value = (
        {'binary': base64.b64encode(result).decode()}
        if isinstance(result, bytes)
        else {'blocks': result}
    )
    payload = json.dumps(value, ensure_ascii=False)
    if len(payload) > 30_000_000:
        raise ValueError('解析結果過大；請縮小文件。')
    print(payload)


if __name__ == '__main__':
    main()
