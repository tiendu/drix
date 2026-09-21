from __future__ import annotations

from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    PyInstaller.__main__.run(
        [
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            "drix",
            "--paths",
            str(ROOT / "src"),
            "--distpath",
            str(ROOT / "dist"),
            "--workpath",
            str(ROOT / "build" / "pyinstaller"),
            "--specpath",
            str(ROOT / "build"),
            str(ROOT / "scripts" / "drix_entry.py"),
        ]
    )


if __name__ == "__main__":
    main()
