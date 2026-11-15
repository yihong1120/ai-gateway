"""The public catalog, split by service and independent of server registration."""
from types import MappingProxyType
import hashlib
import json

from .common import TOOLS as COMMON
from .documents import TOOLS as DOCUMENTS
from .eip import TOOLS as EIP
from .nas import TOOLS as NAS

TOOLS = MappingProxyType({**COMMON, **NAS, **EIP, **DOCUMENTS})


def contract_digest():
    return hashlib.sha256(json.dumps({name: [schema.model_json_schema(), description]
        for name, (schema, description) in TOOLS.items()}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
