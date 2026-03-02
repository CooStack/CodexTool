from __future__ import annotations

import subprocess
import sys
import threading
from typing import Any

_AUTO_INSTALL_LOCK = threading.RLock()
_AUTO_INSTALL_ATTEMPTS: set[str] = set()


def require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{key}` must be a non-empty string")
    return value


def as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    return bool(value)


def as_int(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    if value is None:
        result = default
    elif isinstance(value, bool):
        raise ValueError("bool is not valid for integer field")
    else:
        result = int(value)

    if minimum is not None and result < minimum:
        raise ValueError(f"value must be >= {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"value must be <= {maximum}")
    return result


def install_package_with_pip(package_name: str, feature_name: str) -> None:
    package = package_name.strip()
    feature = feature_name.strip() or package
    if not package:
        raise ValueError("`package_name` must be a non-empty string")

    with _AUTO_INSTALL_LOCK:
        if package in _AUTO_INSTALL_ATTEMPTS:
            return

        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", package],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "pip install failed").strip()
            raise RuntimeError(
                f"Automatic dependency install failed for {feature}. "
                f"Run `{sys.executable} -m pip install {package}` manually. Details: {detail}"
            )

        _AUTO_INSTALL_ATTEMPTS.add(package)
