"""Build a single-file sf-exit executable with PyInstaller (no Python needed to run it).

    pip install pyinstaller '.[duckdb,jwt]'
    python scripts/build_binary.py --name sf-exit-linux-x86_64

The binary lands in dist/. Postgres loading is left out (pip install 'sf-exit[postgres]').
"""

from __future__ import annotations

import argparse
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="sf-exit")
    args = ap.parse_args()
    PyInstaller.__main__.run([
        str(ROOT / "packaging" / "entry.py"),
        "--onefile",
        "--name", args.name,
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--specpath", str(ROOT / "build"),
        "--collect-submodules", "sf_exit",
        "--collect-submodules", "rich",  # rich loads its unicode tables dynamically
        "--collect-all", "duckdb",
        "--copy-metadata", "sf-exit",
        "--exclude-module", "psycopg",
        "--exclude-module", "tkinter",
        "--noconfirm",
        "--clean",
    ])


if __name__ == "__main__":
    main()
