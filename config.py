import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).parent
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("CYCLING_TRAINING_CONFIG", BASE_DIR / "config.json")
)


class ConfigError(RuntimeError):
    pass


def _resolve_path(value: str) -> Path:
    expanded = Path(os.path.expanduser(value))
    if expanded.is_absolute():
        return expanded
    return (BASE_DIR / expanded).resolve()


@lru_cache(maxsize=1)
def load_config() -> Dict[str, Any]:
    if not DEFAULT_CONFIG_PATH.exists():
        raise ConfigError(
            f"Config not found: {DEFAULT_CONFIG_PATH}. "
            "Copy config.example.json to config.json or set CYCLING_TRAINING_CONFIG."
        )
    try:
        return json.loads(DEFAULT_CONFIG_PATH.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Config is not valid JSON: {DEFAULT_CONFIG_PATH} (line {exc.lineno}, column {exc.colno}). "
            "Fix the JSON or copy config.example.json and reapply changes."
        ) from exc


def get_config() -> Dict[str, Any]:
    return load_config()


def get_path(value: str) -> Path:
    return _resolve_path(value)
