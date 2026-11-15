"""Explicit configuration, command and Compose operations for repository deployments."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import dotenv_values

from examples.shared.config import config_path
from examples.shared.config import ConfigurationError
from examples.shared.config import DEFAULT_NAME
from examples.shared.config import ENVIRONMENT_KEY
from examples.shared.config import execution_environment
from examples.shared.config import read_configuration

ARGUMENT_KEY = re.compile(r'\{([A-Z][A-Z0-9_]*)\}')

# Profile, isolated settings section, existing project name, startup requirements.
COMPOSE_PROFILES = {
    'litellm': ('litellm', 'ai-gateway', ('LITELLM_DATABASE_URL', 'LITELLM_MASTER_KEY', 'VERTEX_CREDENTIALS')),
    'local-model': ('local_inference', 'local-model', ('LOCAL_INFERENCE_IMAGE', 'LOCAL_INFERENCE_MODEL_PATH', 'LOCAL_INFERENCE_CONFIG_PATH')),
    'ocr': ('ocr', 'paddleocr-vl16', ('OCR_INFERENCE_IMAGE', 'OCR_MODEL_PATH', 'OCR_INFERENCE_CONFIG_PATH')),
    'files': ('fileserver', 'ai-gateway', ('FILESERVER_ROOT',)),
    'dev-db': (None, 'ai-gateway-dev', ('DEV_CONFIG_DIR',)),
    'images': (None, 'ai-gateway-build', ()),
}


def _write_private(path, sections):
    lines = ['# Private service configuration. Do not commit.\n']
    for service, values in sections.items():
        lines.append(f'\n[{service}]\n')
        lines.extend(f'{key} = {json.dumps(value, ensure_ascii=False)}\n' for key, value in values.items())
    # Exclusive creation protects an existing configuration and any symlink.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.writelines(lines)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink()
        raise


def migrate(root, destination=None):
    """One-time import; existing private env files and deployed services stay intact."""

    root = Path(root).resolve()
    destination = Path(destination) if destination else root / DEFAULT_NAME
    sources = {
        'sdk': root / '.env',
        'webui': root / '.env.openwebui',
        'litellm': root / 'examples/litellm/.env',
        'ai_usage': root / '.env.ai_usage',
        'company_ai_access': root / 'examples/company_ai_access/.env',
    }
    sections = {}
    for service, source in sources.items():
        if source.is_file():
            values = dotenv_values(source)
            if any(value is None for value in values.values()):
                raise ConfigurationError('Legacy files must assign a value to every key.')
            if any(not ENVIRONMENT_KEY.fullmatch(key) for key in values):
                raise ConfigurationError('Legacy environment contains an invalid key.')
            if service == 'litellm':
                for old, current in {
                    'GEMMA4_API_BASE': 'LOCAL_MODEL_API_BASE',
                    'GEMMA4_API_KEY': 'LOCAL_MODEL_API_KEY',
                    'GEMMA4_DEV_API_BASE': 'LOCAL_MODEL_DEV_API_BASE',
                }.items():
                    if old in values:
                        value = values.pop(old)
                        if current in values and values[current] != value:
                            raise ConfigurationError('Legacy model connection settings conflict.')
                        values[current] = value
            sections[service] = dict(values)
    if not sections:
        raise ConfigurationError('No legacy environment files found.')
    _write_private(destination, sections)
    try:
        restored = read_configuration(destination)
        if restored != sections:
            raise ConfigurationError('Configuration import verification failed.')
    except BaseException:
        destination.unlink()
        raise
    return {service: len(values) for service, values in sections.items()}


def compose_command(profile, command, *, project=None, path=None):
    """Run one profile from the checkout, without exporting other sections."""
    section, default_project, required = COMPOSE_PROFILES[profile]
    source = Path.cwd() / 'docker-compose.yml'
    if not source.is_file():
        raise ConfigurationError('Run Compose from the repository root containing docker-compose.yml.')
    if not command or command[0].startswith('-'):
        raise ConfigurationError(
            'Place the Docker Compose command immediately after the profile, such as up -d or build.',
        )
    env = execution_environment(section, path) if section else {**os.environ, 'PYTHON_DOTENV_DISABLED': '1'}
    if command[0] in {'up', 'run', 'create', 'start', 'restart'}:
        missing = [name for name in required if not env.get(name, '').strip()]
        if missing:
            raise ConfigurationError('Missing selected Compose settings: ' + ', '.join(missing))
    return (
        [
            'docker', 'compose', '--env-file', '/dev/null', '-f', str(source),
            '-p', project or default_project, '--profile', profile, *command,
        ],
        env,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--config', type=Path,
        help='Private TOML file; defaults to AI_GATEWAY_CONFIG or config.local.toml.',
    )
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('check', help='Validate structure without displaying values.')
    migration = actions.add_parser('migrate', help='Import legacy env files without changing the originals.')
    migration.add_argument('--root', type=Path, default=Path.cwd())
    execute = actions.add_parser('exec', help='Run a command with one service section.')
    execute.add_argument('service')
    execute.add_argument('command', nargs=argparse.REMAINDER)
    compose = actions.add_parser('compose', help='Use the root Compose with one profile and its settings only.')
    compose.add_argument('--project', help='Override the profile project name for an independent instance.')
    compose.add_argument('profile', choices=COMPOSE_PROFILES)
    compose.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.action == 'migrate':
            counts = migrate(args.root, args.config)
            print('Imported service key counts: ' + json.dumps(counts, sort_keys=True))
        elif args.action == 'check':
            data = read_configuration(config_path(args.config))
            print('Configuration valid: ' + ', '.join(sorted(data)))
        elif args.action == 'compose':
            command = args.command[1:] if args.command[:1] == ['--'] else args.command
            try:
                command, env = compose_command(args.profile, command, project=args.project, path=args.config)
            except ConfigurationError as error:
                parser.error(str(error))
            os.execvpe(command[0], command, env)
        else:
            command = args.command[1:] if args.command[:1] == ['--'] else args.command
            if not command:
                parser.error('exec requires a command after the service name.')
            env = execution_environment(args.service, args.config)
            try:
                command = [ARGUMENT_KEY.sub(lambda match: env[match[1]], argument) for argument in command]
            except KeyError:
                raise ConfigurationError('Command references an unset environment key.') from None
            os.execvpe(command[0], command, env)
    except (ConfigurationError, OSError):
        # TOML, OS and child argv diagnostics must not echo secrets or source text.
        print('Configuration operation failed; check file, section and command settings.', file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == '__main__':
    main()
