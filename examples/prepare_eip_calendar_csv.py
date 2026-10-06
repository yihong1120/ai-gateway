"""Prepare a downloaded calendar CSV for a direct upload in the EIP website."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from official_document_workflow.infrastructure.eip.calendar_csv import parse_csv  # noqa: E402
from official_document_workflow.infrastructure.eip.calendar_csv import export_eip_csv  # noqa: E402


def prepare(payload: bytes) -> tuple[int, bytes]:
    rows, _ = parse_csv(payload)
    # The EIP upload parser applies urldecode; preserve literal % and + once.
    # This file goes directly to EIP, not through the AI adapter's stage().
    return len(rows), export_eip_csv(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    target = args.output or args.source.with_name(args.source.stem+'_EIP直接匯入.csv')
    count, payload = prepare(args.source.read_bytes())
    with target.open('xb') as stream:
        stream.write(payload)
    print(f'已產生 {count} 筆行程：{target.resolve()}')
    print('在 EIP 網站直接上傳此檔，編碼選 UTF-8；%2B 會還原為 +。')


if __name__ == '__main__':
    main()
