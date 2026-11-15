"""Only explicitly classified input/source errors may return to the model loop."""
from __future__ import annotations

import json

from langchain_core.tools import ToolException


class RecoverableToolError(ToolException):
    def __init__(self, code, message, **details):
        super().__init__(json.dumps({
            'code': code, 'message': message, 'executed': False,
            'retryable_argument_error': code == 'sheet_not_found', **details,
        }, ensure_ascii=False))


def input_error_feedback(error):
    if isinstance(error, RecoverableToolError):
        return str(error)
    raise error
