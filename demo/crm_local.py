"""
ポートフォリオ用ローカル CRM（SQLite）。
SALES_DEMO_MODE=1 のとき saved_list.py から呼ばれる。
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = Path(__file__).resolve().parent / "demo_crm.db"

_SAVED_COLS = [
    "username", "saved_at", "company_name", "address", "tel", "website_url",
    "representative", "capital", "employees", "established", "business", "industry",
    "corporate_number", "sales_history", "csv_summary", "csv_banks", "csv_customers",
    "csv_suppliers", "csv_officers", "csv_listing", "csv_id", "thumbnail_url",
    "screenshot_path", "philosophy", "csv_philosophy", "ai_point", "weak_points",
    "tech_stack", "server_info", "renewal_score", "latitude", "longitude",
    "crm_status", "crm_assignee", "crm_memo", "crm_updated_at",
]

_CREATE_SAVED = """
CREATE TABLE IF NOT EXISTS saved_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    saved_at TEXT,
    company_name TEXT NOT NULL,
    address TEXT, tel TEXT, website_url TEXT, representative TEXT,
    capital TEXT, employees TEXT, established TEXT, business TEXT, industry TEXT,
    corporate_number TEXT, sales_history TEXT, csv_summary TEXT, csv_banks TEXT,
    csv_customers TEXT, csv_suppliers TEXT, csv_officers TEXT, csv_listing TEXT,
    csv_id TEXT, thumbnail_url TEXT, screenshot_path TEXT, philosophy TEXT,
    csv_philosophy TEXT, ai_point TEXT, weak_points TEXT, tech_stack TEXT,
    server_info TEXT, renewal_score TEXT, latitude TEXT, longitude TEXT,
    crm_status TEXT DEFAULT '未着手', crm_assignee TEXT, crm_memo TEXT,
    crm_updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_saved_company_name ON saved_companies(company_name);
CREATE INDEX IF NOT EXISTS idx_saved_status ON saved_companies(crm_status);
CREATE INDEX IF NOT EXISTS idx_saved_assignee ON saved_companies(crm_assignee);
"""

_CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY
);
"""


def db_path() -> Path:
    raw = (os.getenv("DEMO_CRM_DB_PATH") or "").strip()
    return Path(raw).expanduser().resolve() if raw else _DEFAULT_DB


def _conn() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = _conn()
    try:
        conn.executescript(_CREATE_SAVED + _CREATE_USERS)
        conn.commit()
        logger.info("デモCRM SQLite 初期化完了: %s", db_path())
    finally:
        conn.close()


def upsert_user(username: str) -> None:
    username = username.strip()
    if not username:
        return
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO users(username) VALUES (?)",
            (username,),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_users() -> tuple[list[str], None]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT username FROM users ORDER BY username"
        ).fetchall()
        return [r["username"] for r in rows], None
    finally:
        conn.close()


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _apply_filters(
    status: str,
    assignee: str,
) -> tuple[str, list[Any]]:
    where = "WHERE 1=1"
    params: list[Any] = []
    if status and status != "すべて":
        where += " AND crm_status = ?"
        params.append(status)
    if assignee and assignee != "すべて":
        where += " AND crm_assignee = ?"
        params.append(assignee)
    return where, params


def _insert_cols(cols: dict) -> None:
    cols = dict(cols)
    cols.setdefault("saved_at", datetime.now(timezone.utc).isoformat())
    cols.setdefault("crm_status", "未着手")
    keys = [k for k in _SAVED_COLS if k in cols and cols[k] is not None]
    placeholders = ", ".join("?" * len(keys))
    sql = f"INSERT INTO saved_companies ({', '.join(keys)}) VALUES ({placeholders})"
    conn = _conn()
    try:
        conn.execute(sql, [cols[k] for k in keys])
        conn.commit()
    finally:
        conn.close()


def _update_by_id(record_id: int, patch: dict) -> bool:
    patch = {k: v for k, v in patch.items() if k in _SAVED_COLS}
    if not patch:
        return False
    sets = ", ".join(f"{k} = ?" for k in patch)
    conn = _conn()
    try:
        cur = conn.execute(
            f"UPDATE saved_companies SET {sets} WHERE id = ?",
            [*patch.values(), record_id],
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _find_id_by_name(name: str) -> int | None:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id FROM saved_companies WHERE company_name = ? LIMIT 1",
            (name,),
        ).fetchone()
        return int(row["id"]) if row else None
    finally:
        conn.close()


def publish_search_crm(row, status: str, assignee: str, memo: str) -> tuple[bool, str]:
    from saved_list import _cols_from_search_row, _apply_crm_to_cols, _company_profile_patch_from_row, get_current_user

    name = str(row.get("社名", "") or "").strip()
    if not name:
        return False, "社名がありません。"
    try:
        record_id = _find_id_by_name(name)
        now = datetime.now(timezone.utc).isoformat()
        if record_id is not None:
            patch = {
                "crm_status": status or "未着手",
                "crm_assignee": assignee or "",
                "crm_memo": memo or "",
                "crm_updated_at": now,
            }
            patch.update(_company_profile_patch_from_row(row))
            _update_by_id(record_id, patch)
            return True, "更新しました"
        username = get_current_user() or "demo"
        cols = _cols_from_search_row(row, username)
        _apply_crm_to_cols(cols, status, assignee, memo)
        cols["crm_updated_at"] = now
        _insert_cols(cols)
        return True, "共有しました"
    except Exception as e:
        logger.error("demo publish_search_crm (%s): %s", name, e)
        return False, f"デモCRMエラー: {e}"


def save_companies(
    df: pd.DataFrame,
    selected_indices: list[int],
    draft_crm: dict[str, dict] | None,
    draft_memos: dict[str, str] | None,
) -> tuple[int, int]:
    from saved_list import _cols_from_search_row, _apply_crm_to_cols, _company_profile_patch_from_row, get_current_user

    username = get_current_user() or "demo"
    saved = skipped = 0
    draft_crm = draft_crm or {}
    draft_memos = draft_memos or {}

    for idx in selected_indices:
        row = df.iloc[idx]
        name = str(row.get("社名", "") or "").strip()
        if not name:
            skipped += 1
            continue
        record_id = _find_id_by_name(name)
        if record_id is not None:
            patch = _company_profile_patch_from_row(row)
            if patch and _update_by_id(record_id, patch):
                saved += 1
            else:
                skipped += 1
            continue
        cols = _cols_from_search_row(row, username)
        draft = draft_crm.get(name, {})
        memo = str(draft.get("memo") or draft_memos.get(name, "") or "").strip()
        status = str(draft.get("status") or "未着手").strip() or "未着手"
        assignee = str(draft.get("assignee") or "").strip()
        _apply_crm_to_cols(cols, status, assignee, memo)
        try:
            _insert_cols(cols)
            saved += 1
        except Exception as e:
            logger.warning("demo save_companies %s: %s", name, e)
            skipped += 1
    return saved, skipped


def load_saved_companies() -> pd.DataFrame:
    from saved_list import _row_to_dict

    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM saved_companies ORDER BY saved_at DESC"
        ).fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([_row_to_dict(_row_dict(r)) for r in rows])
    except Exception as e:
        logger.error("demo load_saved_companies: %s", e)
        return pd.DataFrame()
    finally:
        conn.close()


def load_saved_companies_paged(
    page: int,
    page_size: int,
    status: str,
    assignee: str,
) -> pd.DataFrame:
    from saved_list import _row_to_dict

    where, params = _apply_filters(status, assignee)
    offset = page * page_size
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM saved_companies {where} ORDER BY saved_at DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([_row_to_dict(_row_dict(r)) for r in rows])
    except Exception as e:
        logger.error("demo load_saved_companies_paged: %s", e)
        return pd.DataFrame()
    finally:
        conn.close()


def count_saved() -> int:
    conn = _conn()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM saved_companies").fetchone()
        return int(row["c"]) if row else 0
    except Exception as e:
        logger.warning("demo count_saved: %s", e)
        return 0
    finally:
        conn.close()


def count_saved_filtered(status: str, assignee: str) -> int:
    where, params = _apply_filters(status, assignee)
    conn = _conn()
    try:
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM saved_companies {where}",
            params,
        ).fetchone()
        return int(row["c"]) if row else 0
    except Exception as e:
        logger.warning("demo count_saved_filtered: %s", e)
        return 0
    finally:
        conn.close()


def load_all_ids_filtered(status: str, assignee: str) -> list[int]:
    where, params = _apply_filters(status, assignee)
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT id FROM saved_companies {where}",
            params,
        ).fetchall()
        return [int(r["id"]) for r in rows]
    except Exception as e:
        logger.warning("demo load_all_ids_filtered: %s", e)
        return []
    finally:
        conn.close()


def delete_companies(ids: list[int]) -> None:
    if not ids:
        return
    placeholders = ", ".join("?" * len(ids))
    conn = _conn()
    try:
        conn.execute(
            f"DELETE FROM saved_companies WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()
    except Exception as e:
        logger.error("demo delete_companies: %s", e)
    finally:
        conn.close()


def update_crm(record_id: int, status: str, assignee: str, memo: str) -> tuple[bool, str]:
    try:
        ok = _update_by_id(record_id, {
            "crm_status": status or "未着手",
            "crm_assignee": assignee or "",
            "crm_memo": memo or "",
            "crm_updated_at": datetime.now(timezone.utc).isoformat(),
        })
        if not ok:
            return False, "更新対象が見つかりませんでした（ID不一致の可能性）"
        return True, "更新しました"
    except Exception as e:
        logger.error("demo update_crm id=%s: %s", record_id, e)
        return False, f"デモCRMエラー: {e}"


def clear_crm(record_id: int) -> tuple[bool, str]:
    try:
        ok = _update_by_id(record_id, {
            "crm_status": "未着手",
            "crm_assignee": "",
            "crm_memo": "",
            "crm_updated_at": datetime.now(timezone.utc).isoformat(),
        })
        if not ok:
            return False, "更新対象が見つかりませんでした"
        return True, "CRM情報をクリアしました"
    except Exception as e:
        logger.error("demo clear_crm: %s", e)
        return False, f"デモCRMエラー: {e}"


def get_crm_map_by_names(names: list[str]) -> dict[str, dict]:
    if not names:
        return {}
    placeholders = ", ".join("?" * len(names))
    conn = _conn()
    try:
        rows = conn.execute(
            f"""SELECT id, company_name, username, crm_status, crm_assignee, crm_memo, crm_updated_at
                FROM saved_companies WHERE company_name IN ({placeholders})""",
            names,
        ).fetchall()
        result = {}
        for r in rows:
            _status = r["crm_status"] or "未着手"
            _assignee = r["crm_assignee"] or ""
            _memo = r["crm_memo"] or ""
            result[r["company_name"]] = {
                "id": r["id"],
                "username": r["username"],
                "status": _status,
                "assignee": _assignee,
                "memo": _memo,
                "crm_status": _status,
                "crm_assignee": _assignee,
                "crm_memo": _memo,
                "crm_updated_at": r["crm_updated_at"] or "",
            }
        return result
    except Exception as e:
        logger.warning("demo get_crm_map_by_names: %s", e)
        return {}
    finally:
        conn.close()
