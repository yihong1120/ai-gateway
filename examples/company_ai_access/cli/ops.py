"""Local operator commands; never expose a key or allow anonymous admin APIs."""
from __future__ import annotations

import argparse
import asyncio
import json

from fastapi import FastAPI

from examples.company_ai_access.config import Settings
from examples.company_ai_access.runtime import runtime


async def run(args):
    async with runtime(FastAPI(), Settings.from_env()) as service:
        if args.command == 'status':
            print(
                json.dumps(
                    {'policy': service.policy(), 'devices': (await service.devices())},
                    ensure_ascii=False,
                ),
            )
        elif args.command == 'maintain':
            print(json.dumps(await service.maintain()))
        elif args.command == 'revoke-user':
            count = 0
            for device in await service.repository.list('device', args.subject):
                await service.revoke(args.subject, device['id'])
                count += 1
            print(json.dumps({'revoked_devices': count}))


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    commands.add_parser('maintain')
    command = commands.add_parser('revoke-user')
    command.add_argument('subject')
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
