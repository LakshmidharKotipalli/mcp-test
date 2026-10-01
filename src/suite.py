"""YAML suite loading. ``${VAR}`` in start_url is expanded from the environment."""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import Suite

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class SuiteError(Exception):
    pass


def expand_env(text: str, strict: bool = True) -> str:
    def sub(m: re.Match[str]) -> str:
        val = os.environ.get(m.group(1))
        if val is None:
            if not strict:
                return m.group(0)
            raise SuiteError(f"environment variable {m.group(1)} (used in start_url) is not set")
        return val
    return _VAR.sub(sub, text)


def load_suite(path: str | Path, strict: bool = True) -> Suite:
    try:
        data = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise SuiteError(f"cannot read suite {path}: {exc}") from exc
    if not isinstance(data, dict) or "tests" not in data:
        raise SuiteError("suite must be a mapping with a 'tests' list")
    try:
        suite = Suite.model_validate(data)
    except ValidationError as exc:
        raise SuiteError(f"invalid suite: {exc}") from exc
    for t in suite.tests:
        t.start_url = expand_env(t.start_url, strict)
    return suite
