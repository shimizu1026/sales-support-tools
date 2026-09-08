"""setup.bat 用: .env の NENKAN_DB_PATH またはローカル nenkan.db を検証。"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

env_raw = (os.getenv("NENKAN_DB_PATH") or "").strip()
candidates: list[Path] = []
if env_raw:
    candidates.append(Path(env_raw))
local = (ROOT / "nenkan.db").resolve()
if local not in candidates:
    candidates.append(local)

for path in candidates:
    p = path.expanduser().resolve()
    if not p.is_file():
        print(f"[Skip] {p} not found")
        continue
    try:
        conn = sqlite3.connect(str(p), timeout=15)
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='csv_companies'"
        ).fetchone()
        count = (
            conn.execute("SELECT COUNT(*) FROM csv_companies").fetchone()[0]
            if has_table
            else 0
        )
        conn.close()
        if has_table and count > 0:
            print(f"[OK] nenkan.db: {p} csv_companies rows: {count}")
            sys.exit(0)
        print(f"[Skip] {p} no csv_companies or empty")
    except Exception as exc:
        print(f"[Skip] {p} {exc}")

sys.exit(1)
