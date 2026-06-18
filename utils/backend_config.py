"""Runtime FIVELANES_BACKEND selection shared by dashboard and pipeline."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

BackendName = Literal["claude", "llama"]

_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _ROOT / ".env"

_runtime_backend: BackendName | None = None


def _normalize_backend(value: str | None) -> BackendName:
    b = (value or "llama").strip().lower()
    if b not in ("claude", "llama"):
        raise ValueError(f"Invalid FIVELANES_BACKEND: {b!r}")
    return b  # type: ignore[return-value]


def get_backend() -> BackendName:
    global _runtime_backend
    if _runtime_backend is not None:
        return _runtime_backend
    return _normalize_backend(os.getenv("FIVELANES_BACKEND"))


def apply_backend(backend: str) -> BackendName:
    """Set active backend in process env and dependent modules."""
    global _runtime_backend
    b = _normalize_backend(backend)
    _runtime_backend = b
    os.environ["FIVELANES_BACKEND"] = b

    import fivelanes as fl

    fl.FIVELANES_BACKEND = b

    try:
        import services.image_description as image_description

        image_description.FIVELANES_BACKEND = b
    except ImportError:
        pass

    return b


def persist_backend_to_env(backend: str, *, env_path: Path | None = None) -> BackendName:
    """Write FIVELANES_BACKEND to the project .env file."""
    b = _normalize_backend(backend)
    path = env_path or _ENV_PATH
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    found = False
    for line in lines:
        if line.startswith("FIVELANES_BACKEND="):
            updated.append(f"FIVELANES_BACKEND={b}")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"FIVELANES_BACKEND={b}")
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    return b


def set_backend(backend: str, *, persist: bool = True) -> BackendName:
    b = apply_backend(backend)
    if persist:
        persist_backend_to_env(b)
    return b
