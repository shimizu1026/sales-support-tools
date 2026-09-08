"""nenkan.db を開く（Mac の SMB /Volumes 向け）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote


def is_mac_smb_db(path: Path | str) -> bool:
    if sys.platform != "darwin":
        return False
    s = str(path).replace("\\", "/")
    try:
        s = Path(path).expanduser().resolve().as_posix()
    except OSError:
        pass
    return s.startswith("/Volumes/") or s.startswith("/private/Volumes/")


def _path_variants(path: Path) -> list[Path]:
    p = path.expanduser()
    out: list[Path] = []
    for cand in (p, Path("/private" + p.as_posix()) if p.as_posix().startswith("/Volumes/") else p):
        if cand not in out:
            out.append(cand)
    try:
        resolved = p.resolve()
        if resolved not in out:
            out.append(resolved)
    except OSError:
        pass
    return out


def _try_connect_once(db_path: Path, timeout: float) -> sqlite3.Connection:
    ap = db_path.as_posix()
    attempts: list[tuple[str, str]] = [
        ("uri-ro-1", f"file:{ap}?mode=ro"),
        ("uri-ro-2", f"file://{quote(ap, safe='/')}?mode=ro"),
    ]
    last_err: Exception | None = None
    for _name, uri in attempts:
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=timeout)
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error as e:
            last_err = e
    try:
        conn = sqlite3.connect(str(db_path), timeout=timeout)
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA query_only=ON")
        conn.execute("SELECT 1")
        return conn
    except sqlite3.Error as e:
        last_err = e
    raise last_err or sqlite3.OperationalError("unable to open database file")


def connect_nenkan_writable(path: Path | str, timeout: float = 15) -> sqlite3.Connection:
    """nenkan.db への UPDATE / INSERT 用（読み取り専用 URI は使わない）。"""
    p = Path(path).expanduser()
    if not p.is_file():
        raise sqlite3.OperationalError(f"ファイルなし: {p}")
    conn = sqlite3.connect(str(p), timeout=timeout)
    ms = min(int(timeout * 1000), 15000)
    conn.execute(f"PRAGMA busy_timeout = {ms}")
    return conn


def connect_nenkan(path: Path | str, timeout: float = 15) -> sqlite3.Connection:
    p = Path(path).expanduser()
    errors: list[str] = []
    for variant in _path_variants(p):
        if not variant.is_file():
            errors.append(f"{variant}: ファイルなし")
            continue
        try:
            conn = _try_connect_once(variant, timeout)
            ms = min(int(timeout * 1000), 15000)
            conn.execute(f"PRAGMA busy_timeout = {ms}")
            return conn
        except sqlite3.Error as e:
            errors.append(f"{variant}: {e}")
    raise sqlite3.OperationalError("; ".join(errors) if errors else "unable to open database file")


def discover_mac_nenkan_paths() -> list[Path]:
    """/Volumes/*/SalesScraper/nenkan.db を列挙。"""
    found: list[Path] = []
    for base in (Path("/Volumes"), Path("/private/Volumes")):
        if not base.is_dir():
            continue
        for mount in sorted(base.iterdir()):
            if not mount.is_dir() or mount.name.startswith("."):
                continue
            p = mount / "SalesScraper" / "nenkan.db"
            if p.is_file():
                found.append(p)
    return found


def first_readable_mac_nenkan() -> Path | None:
    for p in discover_mac_nenkan_paths():
        try:
            c = connect_nenkan(p, timeout=5)
            c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='csv_companies'"
            ).fetchone()
            c.close()
            return p
        except sqlite3.Error:
            continue
    return None
