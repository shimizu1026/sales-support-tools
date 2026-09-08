"""
saved_list.py  ― 保存済みリスト・CRM・検索セッション・スクレイピングキャッシュ管理
【Supabase対応版】

【設定方法】
  .env ファイルに以下を記述してください:
    SUPABASE_URL=https://xxxxxx.supabase.co
    SUPABASE_KEY=your_anon_key_here

【nenkan.dbの設定】
  .env ファイルに以下を記述してください:
    NENKAN_DB_PATH=\\NAS名\SalesScraper\nenkan.db

【Mac で NAS を起動時に自動接続（任意）】
    NAS_SMB_URL=smb://NAS464A3C/share
  初回のみ Finder で同じ NAS に接続し、パスワードをキーチェーンに保存してください。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _is_demo_mode() -> bool:
    return os.getenv("SALES_DEMO_MODE", "").strip().lower() in ("1", "true", "yes", "on")

_ROOT = Path(__file__).resolve().parent
_SESSIONS_DB = Path(
    os.getenv("SALES_DATA_DB_PATH") or (_ROOT / "sales_data.db")
).expanduser().resolve()

# ─────────────────────────────────────────────
# 定数
# ─────────────────────────────────────────────

PAGE_SIZE = 10

CRM_STATUSES = [
    "未着手", "飛込訪問中", "架電済み", "アポ取得", "商談中",
    "見積提出済み", "成約", "失注", "保留",
]

# ─────────────────────────────────────────────
# Supabase クライアント
# ─────────────────────────────────────────────

def _get_client() -> Client:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            ".env に SUPABASE_URL と SUPABASE_KEY を設定してください。"
        )
    return create_client(url, key)


def _supabase_err_msg(exc: Exception) -> str:
    """Supabase / Cloudflare エラーをユーザー向け文言に変換。"""
    s = str(exc)
    if "521" in s or "Web server is down" in s or "JSON could not be generated" in s:
        return (
            "Supabase が復旧中です（Resume 直後は数分かかります）。"
            "ダッシュボードの状態が Active になってから、ブラウザを再読み込みしてください。"
        )
    if "11001" in s or "getaddrinfo failed" in s:
        return (
            "Supabase の URL に接続できません。"
            ".env の SUPABASE_URL を確認するか、プロジェクトが Pause していないか確認してください。"
        )
    return f"Supabase 接続エラー: {exc}"


# ─────────────────────────────────────────────
# テーブル初期化（Supabaseでは不要・互換のため残す）
# ─────────────────────────────────────────────

def init_db() -> None:
    """Supabase接続確認と検索セッション用SQLiteの初期化"""
    if _is_demo_mode():
        from demo.crm_local import init_db as _demo_init
        _demo_init()
        _init_search_sessions_db()
        logger.info("デモモード: ローカルCRMを使用中")
        return

    import concurrent.futures

    def _check_supabase() -> None:
        client = _get_client()
        client.table("saved_companies").select("id").limit(1).execute()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_check_supabase)
            fut.result(timeout=15)
        logger.info("Supabase 接続確認完了")
    except concurrent.futures.TimeoutError as e:
        logger.error("Supabase 接続タイムアウト")
        raise RuntimeError(
            "Supabase（クラウド）への接続が15秒以内に完了しません。"
            "ネット接続を確認してください。"
        ) from e
    except Exception as e:
        logger.error("Supabase 接続エラー: %s", e)
        raise RuntimeError(_supabase_err_msg(e)) from e
    _init_search_sessions_db()


# ─────────────────────────────────────────────
# ユーザー管理
# ─────────────────────────────────────────────

def get_current_user() -> Optional[str]:
    import streamlit as st
    return st.session_state.get("current_user")


def login_user(username: str) -> None:
    import streamlit as st
    username = username.strip()
    if not username:
        return
    st.session_state["current_user"] = username
    if _is_demo_mode():
        from demo.crm_local import upsert_user
        upsert_user(username)
        return
    try:
        client = _get_client()
        client.table("users").upsert(
            {"username": username},
            on_conflict="username"
        ).execute()
    except Exception as e:
        logger.warning("ユーザー登録エラー: %s", e)


def logout_user() -> None:
    import streamlit as st
    st.session_state.pop("current_user", None)


def get_all_users_with_status() -> tuple[list[str], Optional[str]]:
    if _is_demo_mode():
        from demo.crm_local import get_all_users as _demo_users
        return _demo_users()
    try:
        client = _get_client()
        res = client.table("users").select("username").order("username").execute()
        return [r["username"] for r in res.data], None
    except Exception as e:
        logger.warning("get_all_users エラー: %s", e)
        return [], _supabase_err_msg(e)


def get_all_users() -> list[str]:
    users, _ = get_all_users_with_status()
    return users


# ─────────────────────────────────────────────
# カラムマッピング
# ─────────────────────────────────────────────

_COL_MAP = {
    "社名":           "company_name",
    "住所":           "address",
    "TEL":            "tel",
    "WebサイトURL":   "website_url",
    "代表者名":       "representative",
    "資本金":         "capital",
    "従業員数":       "employees",
    "設立年":         "established",
    "事業内容":       "business",
    "業種":           "industry",
    "法人番号":       "corporate_number",
    "csv_売上推移":   "sales_history",
    "csv_概要":       "csv_summary",
    "csv_銀行":       "csv_banks",
    "csv_顧客":       "csv_customers",
    "csv_仕入先":     "csv_suppliers",
    "csv_役員":       "csv_officers",
    "csv_上場":       "csv_listing",
    "_csv_id":        "csv_id",
    "_thumbnail_url": "thumbnail_url",
    "スクリーンショット": "screenshot_path",
    "企業理念":       "philosophy",
    "csv_企業理念":   "csv_philosophy",
    "AI営業ポイント": "ai_point",
    "_weak_points":   "weak_points",
    "_tech_stack":    "tech_stack",
    "_server_info":   "server_info",
    "_renewal_score": "renewal_score",
    "_latitude":      "latitude",
    "_longitude":     "longitude",
}

_COL_MAP_INV = {v: k for k, v in _COL_MAP.items()}

_EMPTY_PROFILE = frozenset({"", "不明", "情報なし", "null", "None", "nan"})


def _is_empty_profile(val) -> bool:
    s = str(val or "").strip()
    return not s or s in _EMPTY_PROFILE


def _company_profile_patch_from_row(row) -> dict:
    """検索結果行から Supabase の会社プロフィール列だけ抽出（空・不明は除く）。"""
    cols = _cols_from_search_row(row, "")
    skip = {
        "username", "crm_status", "crm_assignee", "crm_memo", "crm_updated_at",
    }
    patch: dict = {}
    for db_col, val in cols.items():
        if db_col in skip or val is None:
            continue
        if isinstance(val, float) and str(val) == "nan":
            continue
        s = str(val).strip()
        if s and s not in _EMPTY_PROFILE:
            patch[db_col] = s
    return patch


def _row_to_dict(row: dict) -> dict:
    result = {}
    for db_col, df_col in _COL_MAP_INV.items():
        result[df_col] = row.get(db_col)
    result["id"]         = row.get("id")
    result["ユーザー"]   = row.get("username")
    result["保存日時"]   = (row.get("saved_at", "") or "")[:16]
    result["ステータス"] = row.get("crm_status", "未着手") or "未着手"
    result["担当者"]     = row.get("crm_assignee", "") or ""
    result["メモ"]       = row.get("crm_memo", "") or ""
    result["CRM更新日時"]= row.get("crm_updated_at", "") or ""
    return result


def _cols_from_search_row(row, username: str) -> dict:
    cols = {"username": username}
    for df_col, db_col in _COL_MAP.items():
        val = row.get(df_col)
        if val is None or (isinstance(val, float) and str(val) == "nan"):
            val = None
        elif not isinstance(val, (int, float, type(None))):
            val = str(val)
        cols[db_col] = val
    return cols


def _apply_crm_to_cols(cols: dict, status: str, assignee: str, memo: str) -> None:
    cols["crm_status"] = str(status or "未着手").strip() or "未着手"
    memo_s = str(memo or "").strip()
    assignee_s = str(assignee or "").strip()
    if memo_s:
        cols["crm_memo"] = memo_s
    if assignee_s:
        cols["crm_assignee"] = assignee_s


def publish_search_crm(row, status: str, assignee: str, memo: str) -> tuple[bool, str]:
    """検索結果から CRM をチーム共有。未登録の会社は保存リストへ自動追加。"""
    if _is_demo_mode():
        from demo.crm_local import publish_search_crm as _demo_pub
        return _demo_pub(row, status, assignee, memo)
    name = str(row.get("社名", "") or "").strip()
    if not name:
        return False, "社名がありません。"
    try:
        client = _get_client()
        exists = client.table("saved_companies").select("id").eq(
            "company_name", name
        ).limit(1).execute()
        if exists.data:
            record_id = int(exists.data[0]["id"])
            from datetime import datetime, timezone
            patch = {
                "crm_status":     status or "未着手",
                "crm_assignee":   assignee or "",
                "crm_memo":       memo or "",
                "crm_updated_at": datetime.now(timezone.utc).isoformat(),
            }
            patch.update(_company_profile_patch_from_row(row))
            client.table("saved_companies").update(patch).eq(
                "id", record_id
            ).execute()
            return True, "更新しました"
        username = get_current_user() or "不明"
        cols = _cols_from_search_row(row, username)
        _apply_crm_to_cols(cols, status, assignee, memo)
        from datetime import datetime, timezone
        cols["crm_updated_at"] = datetime.now(timezone.utc).isoformat()
        client.table("saved_companies").insert(cols).execute()
        return True, "共有しました"
    except Exception as e:
        logger.error("publish_search_crm エラー (%s): %s", name, e)
        return False, _supabase_err_msg(e)


# ─────────────────────────────────────────────
# 企業保存・読み込み
# ─────────────────────────────────────────────

def save_companies(
    df: pd.DataFrame,
    selected_indices: list[int],
    draft_crm: dict[str, dict] | None = None,
    draft_memos: dict[str, str] | None = None,
) -> tuple[int, int]:
    """選択された企業を保存済みリストに追加。戻り値: (保存件数, スキップ件数)"""
    if _is_demo_mode():
        from demo.crm_local import save_companies as _demo_save
        return _demo_save(df, selected_indices, draft_crm, draft_memos)
    username = get_current_user() or "不明"
    saved = skipped = 0
    client = _get_client()
    draft_crm = draft_crm or {}
    draft_memos = draft_memos or {}

    for idx in selected_indices:
        row = df.iloc[idx]
        name = str(row.get("社名", "") or "").strip()
        if not name:
            skipped += 1
            continue

        # 重複チェック
        exists = client.table("saved_companies").select("id").eq(
            "company_name", name
        ).execute()
        if exists.data:
            record_id = int(exists.data[0]["id"])
            patch = _company_profile_patch_from_row(row)
            if patch:
                try:
                    client.table("saved_companies").update(patch).eq(
                        "id", record_id
                    ).execute()
                    saved += 1
                except Exception as e:
                    logger.warning("企業プロフィール更新エラー %s: %s", name, e)
                    skipped += 1
            else:
                skipped += 1
            continue

        # カラムマッピング
        cols = _cols_from_search_row(row, username)

        draft = draft_crm.get(name, {})
        memo = str(draft.get("memo") or draft_memos.get(name, "") or "").strip()
        status = str(draft.get("status") or "未着手").strip() or "未着手"
        assignee = str(draft.get("assignee") or "").strip()
        _apply_crm_to_cols(cols, status, assignee, memo)

        try:
            client.table("saved_companies").insert(cols).execute()
            saved += 1
        except Exception as e:
            logger.warning("企業保存エラー %s: %s", name, e)
            skipped += 1

    return saved, skipped


def load_saved_companies() -> pd.DataFrame:
    if _is_demo_mode():
        from demo.crm_local import load_saved_companies as _demo_load
        return _demo_load()
    try:
        client = _get_client()
        res = client.table("saved_companies").select("*").order(
            "saved_at", desc=True
        ).execute()
        if not res.data:
            return pd.DataFrame()
        return pd.DataFrame([_row_to_dict(r) for r in res.data])
    except Exception as e:
        logger.error("load_saved_companies エラー: %s", e)
        return pd.DataFrame()


def load_saved_companies_paged(
    page: int = 0,
    page_size: int = PAGE_SIZE,
    status: str = "",
    assignee: str = "",
) -> pd.DataFrame:
    if _is_demo_mode():
        from demo.crm_local import load_saved_companies_paged as _demo_paged
        return _demo_paged(page, page_size, status, assignee)
    try:
        client = _get_client()
        query = client.table("saved_companies").select("*")
        if status and status != "すべて":
            query = query.eq("crm_status", status)
        if assignee and assignee != "すべて":
            query = query.eq("crm_assignee", assignee)
        offset = page * page_size
        res = query.order("saved_at", desc=True).range(
            offset, offset + page_size - 1
        ).execute()
        if not res.data:
            return pd.DataFrame()
        return pd.DataFrame([_row_to_dict(r) for r in res.data])
    except Exception as e:
        logger.error("load_saved_companies_paged エラー: %s", e)
        return pd.DataFrame()


def count_saved() -> int:
    if _is_demo_mode():
        from demo.crm_local import count_saved as _demo_count
        return _demo_count()
    try:
        client = _get_client()
        res = client.table("saved_companies").select(
            "id", count="exact"
        ).execute()
        return res.count or 0
    except Exception as e:
        logger.warning("count_saved エラー: %s", e)
        return 0


def count_saved_filtered(status: str = "", assignee: str = "") -> int:
    if _is_demo_mode():
        from demo.crm_local import count_saved_filtered as _demo_cf
        return _demo_cf(status, assignee)
    try:
        client = _get_client()
        query = client.table("saved_companies").select("id", count="exact")
        if status and status != "すべて":
            query = query.eq("crm_status", status)
        if assignee and assignee != "すべて":
            query = query.eq("crm_assignee", assignee)
        res = query.execute()
        return res.count or 0
    except Exception as e:
        logger.warning("count_saved_filtered エラー: %s", e)
        return 0


def load_all_ids_filtered(status: str = "", assignee: str = "") -> list[int]:
    if _is_demo_mode():
        from demo.crm_local import load_all_ids_filtered as _demo_ids
        return _demo_ids(status, assignee)
    try:
        client = _get_client()
        query = client.table("saved_companies").select("id")
        if status and status != "すべて":
            query = query.eq("crm_status", status)
        if assignee and assignee != "すべて":
            query = query.eq("crm_assignee", assignee)
        res = query.execute()
        return [r["id"] for r in res.data]
    except Exception as e:
        logger.warning("load_all_ids_filtered エラー: %s", e)
        return []


def delete_companies(ids: list[int]) -> None:
    if not ids:
        return
    if _is_demo_mode():
        from demo.crm_local import delete_companies as _demo_del
        _demo_del(ids)
        return
    try:
        client = _get_client()
        client.table("saved_companies").delete().in_("id", ids).execute()
    except Exception as e:
        logger.error("delete_companies エラー: %s", e)


# ─────────────────────────────────────────────
# CRM 更新
# ─────────────────────────────────────────────

def update_crm(
    record_id: int,
    status: str,
    assignee: str,
    memo: str,
) -> tuple[bool, str]:
    if _is_demo_mode():
        from demo.crm_local import update_crm as _demo_up
        return _demo_up(record_id, status, assignee, memo)
    try:
        client = _get_client()
        from datetime import datetime, timezone
        res = client.table("saved_companies").update({
            "crm_status":     status or "未着手",
            "crm_assignee":   assignee or "",
            "crm_memo":       memo or "",
            "crm_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", record_id).select("id").execute()
        if not res.data:
            logger.warning("update_crm: 0件更新 id=%s", record_id)
            return False, "更新対象が見つかりませんでした（ID不一致の可能性）"
        return True, "更新しました"
    except Exception as e:
        logger.error("update_crm エラー id=%s: %s", record_id, e)
        return False, _supabase_err_msg(e)


def clear_crm(record_id: int) -> tuple[bool, str]:
    """CRM（メモ・ステータス・担当者）を初期状態に戻す。"""
    if _is_demo_mode():
        from demo.crm_local import clear_crm as _demo_clr
        return _demo_clr(record_id)
    try:
        client = _get_client()
        from datetime import datetime, timezone
        client.table("saved_companies").update({
            "crm_status":     "未着手",
            "crm_assignee":   "",
            "crm_memo":       "",
            "crm_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", record_id).execute()
        return True, "CRM情報をクリアしました"
    except Exception as e:
        logger.error("clear_crm エラー: %s", e)
        return False, _supabase_err_msg(e)


def get_crm_map_by_names(names: list[str]) -> dict[str, dict]:
    """会社名リストに対して CRM 情報を一括取得"""
    if not names:
        return {}
    if _is_demo_mode():
        from demo.crm_local import get_crm_map_by_names as _demo_map
        return _demo_map(names)
    try:
        client = _get_client()
        res = client.table("saved_companies").select(
            "id, company_name, username, crm_status, crm_assignee, crm_memo, crm_updated_at"
        ).in_("company_name", names).execute()
        result = {}
        for r in res.data:
            _status   = r.get("crm_status") or "未着手"
            _assignee = r.get("crm_assignee") or ""
            _memo     = r.get("crm_memo") or ""
            result[r["company_name"]] = {
                "id":             r["id"],
                "username":       r.get("username"),
                "status":         _status,
                "assignee":       _assignee,
                "memo":           _memo,
                "crm_status":     _status,
                "crm_assignee":   _assignee,
                "crm_memo":       _memo,
                "crm_updated_at": r.get("crm_updated_at") or "",
            }
        return result
    except Exception as e:
        logger.warning("get_crm_map_by_names エラー: %s", e)
        return {}


# ─────────────────────────────────────────────
# 検索セッション保存・読み込み（ローカル SQLite）
# Supabase の search_sessions はスキーマ不一致のため使用しない
# ─────────────────────────────────────────────

def _search_sessions_json_column(conn: sqlite3.Connection) -> str:
    """既存DBは result_json、新規は同名列を使用。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(search_sessions)")}
    if "result_json" in cols:
        return "result_json"
    if "df_json" in cols:
        return "df_json"
    return "result_json"


def _init_search_sessions_db() -> None:
    _SESSIONS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_SESSIONS_DB), timeout=15)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(search_sessions)")}
        if not cols:
            conn.execute(
                """
                CREATE TABLE search_sessions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    username        TEXT NOT NULL DEFAULT '',
                    session_name    TEXT NOT NULL,
                    region          TEXT NOT NULL DEFAULT '',
                    industry        TEXT NOT NULL DEFAULT '',
                    enable_scraping INTEGER NOT NULL DEFAULT 0,
                    result_json     TEXT NOT NULL,
                    created_at      TEXT NOT NULL,
                    UNIQUE(username, session_name)
                )
                """
            )
        else:
            if "result_json" not in cols and "df_json" not in cols:
                conn.execute(
                    "ALTER TABLE search_sessions "
                    "ADD COLUMN result_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "region" not in cols:
                conn.execute(
                    "ALTER TABLE search_sessions ADD COLUMN region TEXT NOT NULL DEFAULT ''"
                )
            if "industry" not in cols:
                conn.execute(
                    "ALTER TABLE search_sessions ADD COLUMN industry TEXT NOT NULL DEFAULT ''"
                )
            if "enable_scraping" not in cols:
                conn.execute(
                    "ALTER TABLE search_sessions "
                    "ADD COLUMN enable_scraping INTEGER NOT NULL DEFAULT 0"
                )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_search_sessions_user_name
            ON search_sessions(username, session_name)
            """
        )
        conn.commit()
    finally:
        conn.close()


def _sessions_conn() -> sqlite3.Connection:
    _init_search_sessions_db()
    conn = sqlite3.connect(str(_SESSIONS_DB), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _session_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["enable_scraping"] = bool(d.get("enable_scraping"))
    raw_json = d.get("result_json") or d.get("df_json") or ""
    d["df"] = pd.DataFrame()
    if raw_json and str(raw_json).strip() not in ("", "[]", "null"):
        try:
            d["df"] = pd.DataFrame(json.loads(raw_json))
        except Exception as e:
            logger.warning("search_sessions JSON解析失敗 id=%s: %s", d.get("id"), e)
    return d


def save_search_session(
    session_name: str,
    region: str,
    industry: str,
    enable_scraping: bool,
    df: pd.DataFrame,
) -> int:
    session_name = (session_name or "").strip()
    if not session_name:
        return -1
    df_json = df.to_json(force_ascii=False, orient="records")
    username = get_current_user() or ""
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _sessions_conn()
        try:
            json_col = _search_sessions_json_column(conn)
            conn.execute(
                f"""
                INSERT INTO search_sessions (
                    username, session_name, region, industry,
                    enable_scraping, {json_col}, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, session_name) DO UPDATE SET
                    region=excluded.region,
                    industry=excluded.industry,
                    enable_scraping=excluded.enable_scraping,
                    {json_col}=excluded.{json_col},
                    created_at=excluded.created_at
                """,
                (
                    username,
                    session_name,
                    region or "",
                    industry or "",
                    int(bool(enable_scraping)),
                    df_json,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM search_sessions "
                "WHERE username=? AND session_name=?",
                (username, session_name),
            ).fetchone()
            return int(row["id"]) if row else -1
        finally:
            conn.close()
    except Exception as e:
        logger.error("save_search_session エラー: %s", e)
        return -1


def load_search_session(session_id: int) -> Optional[dict]:
    try:
        conn = _sessions_conn()
        try:
            row = conn.execute(
                "SELECT * FROM search_sessions WHERE id=?",
                (int(session_id),),
            ).fetchone()
            if not row:
                return None
            return _session_row_to_dict(row)
        finally:
            conn.close()
    except Exception as e:
        logger.error("load_search_session エラー: %s", e)
        return None


def list_search_sessions(username: Optional[str] = None) -> list[dict]:
    try:
        conn = _sessions_conn()
        try:
            sql = (
                """
                SELECT id, username, session_name, region, industry,
                       enable_scraping, created_at
                FROM search_sessions
                """
            )
            params: tuple = ()
            if username:
                sql += " WHERE username = ?"
                params = (username,)
            sql += " ORDER BY created_at DESC"
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.warning("list_search_sessions エラー: %s", e)
        return []


def delete_search_session(session_id: int) -> None:
    try:
        conn = _sessions_conn()
        try:
            conn.execute(
                "DELETE FROM search_sessions WHERE id=?",
                (int(session_id),),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("delete_search_session エラー: %s", e)


# ─────────────────────────────────────────────
# スクレイピングキャッシュ
# ─────────────────────────────────────────────

_CACHE_EXPIRE_DAYS = 30


def get_scrape_cache(url: str) -> Optional[dict]:
    try:
        client = _get_client()
        from datetime import datetime, timezone, timedelta
        expire = (datetime.now(timezone.utc) - timedelta(days=_CACHE_EXPIRE_DAYS)).isoformat()
        res = client.table("scrape_cache").select("result_json").eq(
            "url", url
        ).gte("cached_at", expire).execute()
        if not res.data:
            return None
        return json.loads(res.data[0]["result_json"])
    except Exception:
        return None


def set_scrape_cache(url: str, company_name: str, result: dict) -> None:
    try:
        result_json = json.dumps(result, ensure_ascii=False)
        client = _get_client()
        client.table("scrape_cache").upsert({
            "url":          url,
            "company_name": company_name,
            "result_json":  result_json,
        }, on_conflict="url").execute()
    except Exception as e:
        logger.warning("set_scrape_cache エラー: %s", e)


def delete_scrape_cache(url: str) -> None:
    try:
        client = _get_client()
        client.table("scrape_cache").delete().eq("url", url).execute()
    except Exception as e:
        logger.warning("delete_scrape_cache エラー: %s", e)


def get_scrape_cache_info() -> dict:
    try:
        client = _get_client()
        from datetime import datetime, timezone, timedelta
        expire = (datetime.now(timezone.utc) - timedelta(days=_CACHE_EXPIRE_DAYS)).isoformat()
        total_res = client.table("scrape_cache").select("id", count="exact").execute()
        valid_res = client.table("scrape_cache").select("id", count="exact").gte(
            "cached_at", expire
        ).execute()
        total = total_res.count or 0
        valid = valid_res.count or 0
        return {"total": total, "valid": valid, "expired": total - valid}
    except Exception as e:
        logger.warning("get_scrape_cache_info エラー: %s", e)
        return {"total": 0, "valid": 0, "expired": 0}


# ─────────────────────────────────────────────
# ドメインキャッシュ（Supabase domain_cache）
# 既定オフ。nenkan.db に SSL/Whois を保存するためクラウドキャッシュは不要。
# 有効化: .env に DOMAIN_CACHE_ENABLED=1
# ─────────────────────────────────────────────

def _domain_cache_enabled() -> bool:
    return os.environ.get("DOMAIN_CACHE_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def delete_domain_cache_all() -> int:
    if not _domain_cache_enabled():
        return 0
    try:
        client = _get_client()
        res = client.table("domain_cache").select("id", count="exact").execute()
        count = res.count or 0
        client.table("domain_cache").delete().neq("id", 0).execute()
        return count
    except Exception as e:
        logger.warning("delete_domain_cache_all エラー: %s", e)
        return 0


def delete_domain_cache_unavailable() -> int:
    if not _domain_cache_enabled():
        return 0
    try:
        client = _get_client()
        res = client.table("domain_cache").select("url, info_json").execute()
        deleted = 0
        for r in res.data:
            try:
                info = json.loads(r.get("info_json") or "{}")
                if info.get("hosting_company") in ("不明", "取得不可", "", None):
                    client.table("domain_cache").delete().eq("url", r["url"]).execute()
                    deleted += 1
            except Exception:
                pass
        return deleted
    except Exception as e:
        logger.warning("delete_domain_cache_unavailable エラー: %s", e)
        return 0


def get_domain_cache(domain: str) -> dict | None:
    if not _domain_cache_enabled():
        return None
    try:
        client = _get_client()
        res = client.table("domain_cache").select("info_json").eq(
            "url", domain
        ).execute()
        if res.data:
            return json.loads(res.data[0].get("info_json") or "{}")
    except Exception as e:
        logger.warning("get_domain_cache エラー domain=%s: %s", domain, e)
    return None


def set_domain_cache(domain: str, info: dict) -> None:
    if not _domain_cache_enabled():
        return
    try:
        client = _get_client()
        client.table("domain_cache").upsert({
            "url":       domain,
            "info_json": json.dumps(info, ensure_ascii=False),
        }, on_conflict="url").execute()
    except Exception as e:
        logger.warning("set_domain_cache エラー domain=%s: %s", domain, e)


def get_domain_cache_stats() -> dict:
    if not _domain_cache_enabled():
        return {"total": 0, "ok": 0, "unavailable": 0}
    try:
        client = _get_client()
        res = client.table("domain_cache").select("info_json").execute()
        total = len(res.data)
        unavailable = 0
        for r in res.data:
            try:
                info = json.loads(r.get("info_json") or "{}")
                if info.get("hosting_company") in ("不明", "取得不可", "", None):
                    unavailable += 1
            except Exception:
                unavailable += 1
        return {"total": total, "ok": total - unavailable, "unavailable": unavailable}
    except Exception as e:
        logger.warning("get_domain_cache_stats エラー: %s", e)
        return {"total": 0, "ok": 0, "unavailable": 0}
