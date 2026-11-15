"""Resource-limited parser process, NOT a replacement for an OS sandbox."""
from __future__ import annotations

import base64
import json
import resource
import sys

from langchain_core.tools import ToolException

from company_agent.services.office import documents
from company_agent.services.files.errors import RecoverableToolError


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
    from company_agent.services.delivery.validation import validate_file

    funcs = {
        'edit_spreadsheet': edit_spreadsheet,
        'extract': documents.extract,
        'edit': documents.edit,
        'word': documents.create_word,
        'slides': documents.create_slides,
        'spreadsheet': documents.create_spreadsheet,
        'read_spreadsheet': documents.read_spreadsheet,
        'analyze_spreadsheet': documents.analyze_spreadsheet,
        'validate_delivery_file': validate_file,
    }
    try:
        result = funcs[operation](**args)
    except RecoverableToolError as error:
        print(json.dumps({'input_error': json.loads(str(error))}, ensure_ascii=False))
        return
    except ToolException as error:
        if operation != 'validate_delivery_file':
            raise
        print(json.dumps({'input_error': {'code': 'delivery_validation_failed', 'message': str(error)}}, ensure_ascii=False))
        return
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
