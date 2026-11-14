from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
from pathlib import Path

_UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_filename(name: str, max_len: int = 120) -> str:
    """Make a string safe to use as a file name on Windows, macOS and Linux."""
    name = unicodedata.normalize("NFC", str(name))
    name = _UNSAFE.sub("_", name).strip(" .")
    if not name:
        name = "file"
    if len(name.encode("utf-8")) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            keep = max_len - len(ext.encode("utf-8")) - 1
            name = _truncate_utf8(stem, keep) + "." + ext
        else:
            name = _truncate_utf8(name, max_len)
    if name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                      *(f"LPT{i}" for i in range(1, 10))}:
        name = "_" + name
    return name


def _truncate_utf8(s: str, max_bytes: int) -> str:
    return s.encode("utf-8")[:max(1, max_bytes)].decode("utf-8", "ignore")


def file_sha256(path: Path, algo: str = "sha256") -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"  # pragma: no cover
