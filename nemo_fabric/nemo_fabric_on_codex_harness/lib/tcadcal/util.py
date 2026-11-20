"""Small shared helpers: JSON I/O, hashing, file locks, timestamps."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

import numpy as np


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def md5_file(path: Path) -> str:
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def _default(obj: Any) -> Any:
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not JSON serializable: {type(obj)}")


def dumps(obj: Any, indent: int | None = 2) -> str:
    return json.dumps(obj, indent=indent, default=_default)


def write_json(path: Path, obj: Any) -> None:
    """Atomic JSON write (tmp file + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(dumps(obj) + "\n")
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text())


def append_jsonl(path: Path, obj: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, default=_default) + "\n"
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(line)
        fcntl.flock(fh, fcntl.LOCK_UN)


def read_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(s) for s in path.read_text().splitlines() if s.strip()]


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextlib.contextmanager
def cpu_slot(slot_dir: Path, n_slots: int, poll_s: float = 2.0) -> Iterator[int]:
    """Hold one of ``n_slots`` CPU slots (fcntl locks shared by all jobs of a workspace)."""
    slot_dir = Path(slot_dir)
    slot_dir.mkdir(parents=True, exist_ok=True)
    while True:
        for k in range(n_slots):
            fh = open(slot_dir / f"slot{k}.lock", "a+")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fh.close()
                continue
            try:
                yield k
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
                fh.close()
            return
        time.sleep(poll_s)


def fmt_value(value: float, scale: str) -> str:
    """Deck text for a parameter value (6 significant digits; densities in e-notation)."""
    if scale == "log":
        return f"{value:.6e}"
    return f"{value:.6g}"


def round_value(value: float, scale: str) -> float:
    return float(fmt_value(value, scale))
