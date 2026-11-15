"""Read one validated TOML snapshot and project only the requested service."""
from __future__ import annotations

import json
import math
import os
import re
import tomllib
from pathlib import Path

ENVIRONMENT_KEY = re.compile(r'[A-Z][A-Z0-9_]*')
DEFAULT_NAME = 'config.local.toml'


class ConfigurationError(ValueError):
    """Configuration failure whose message contains no setting values."""


def config_path(path=None):
    configured = path if path is not None else os.getenv('AI_GATEWAY_CONFIG', DEFAULT_NAME)
    return Path(configured).expanduser().resolve()


def read_configuration(path):
    """Read and validate an explicit TOML file without exporting settings."""
    try:
        with Path(path).open('rb') as source:
            data = tomllib.load(source)
    except tomllib.TOMLDecodeError:
        raise ConfigurationError('Invalid TOML configuration.') from None
    for section, values in data.items():
        if not isinstance(values, dict):
            raise ConfigurationError('Each service must have its own configuration section.')
        for key, value in values.items():
            if not ENVIRONMENT_KEY.fullmatch(key):
                raise ConfigurationError('Environment keys must use uppercase names.')
            if not isinstance(value, (str, bool, int, float)) or isinstance(value, float) and not math.isfinite(value):
                raise ConfigurationError(f'{section}.{key} must be a finite scalar; keep CSV/JSON values as strings.')
            if isinstance(value, str) and '\0' in value:
                raise ConfigurationError(f'{section}.{key} contains a null character.')
    return data


def _read_configuration(path=None):
    if path is None and os.getenv('PYTHON_DOTENV_DISABLED', '').lower() in {'1', 'true', 'yes'}:
        return {}
    source = config_path(path)
    if not source.is_file():
        if path is not None or os.getenv('AI_GATEWAY_CONFIG'):
            raise ConfigurationError('Configured TOML file does not exist.')
        return {}
    return read_configuration(source)


def _environment_values(values):
    return {key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in values.items()}


def read_services(*services, path=None):
    """Project requested sections from one validated file snapshot."""
    document = _read_configuration(path)
    return {service: _environment_values(document.get(service, {})) for service in services}


def read_service(service, path=None):
    """Read one section; explicit paths opt into file access."""
    return read_services(service, path=path)[service]


def read_service_snapshot(service, path=None):
    """Read TOML settings or an explicitly selected generated JSON snapshot."""
    if path is not None and Path(path).suffix == '.json':
        return json.loads(Path(path).read_text())
    return read_service(service, config_path(path))


def load_service(service):
    """Load before constructing settings. Explicit process environment wins."""
    values = read_service(service)
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def execution_environment(service, path=None):
    """A selected section plus explicit caller overrides; no other section is exported."""
    selected = config_path(path)
    exists = selected.is_file()
    document = _read_configuration(selected if exists else path)
    if exists:
        if service not in document:
            raise ConfigurationError('Requested service section does not exist.')
    values = _environment_values(document.get(service, {}))
    env = {**values, **os.environ, 'PYTHON_DOTENV_DISABLED': '1'}
    if exists:
        env['AI_GATEWAY_CONFIG'] = str(selected)
    return env
