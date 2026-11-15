"""Generate complete candidate settings; runtime never resolves inheritance."""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def merge(base, overrides):
    result = deepcopy(base)
    for key, value in overrides.items():
        result[key] = (
            merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else deepcopy(value)
        )
    return result


def build(check=False):
    spec = json.loads((ROOT / 'presets.json').read_text())
    base = merge(
        spec['defaults'],
        {
            'role_models': {role: spec['role_defaults'] for role in spec['roles']},
        },
    )
    stale = []
    for name, overrides in spec['candidates'].items():
        destination = ROOT / name
        if destination.parent != ROOT or destination.suffix != '.json':
            raise ValueError('Candidate names must be JSON filenames.')
        result = merge(base, overrides)
        if check:
            if not destination.exists() or json.loads(destination.read_text()) != result:
                stale.append(name)
        else:
            destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    if stale:
        raise ValueError('Regenerate candidate settings: ' + ', '.join(stale))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    build(parser.parse_args().check)
