from __future__ import annotations

import os
import sys
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler as _RFH
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent

from env_bootstrap import bootstrap_env, is_cloud_deploy, is_demo_mode, is_web_scraping_disabled

bootstrap_env(_APP_DIR)

# 起動確認用（ログより先に書く）。SalesScraperSetup フォルダ内の _last_app_load.txt を見る
try:
    (_APP_DIR / "_last_app_load.txt").write_text(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S") + f" pid={os.getpid()}\n",
        encoding="utf-8",
    )
except OSError:
    pass

# ── ロガー設定（最初に確定させる。他のimportより必ず前）──────────────────────
# ファイル名を salescaper.log にすることで古い app.log と競合しない。
# PyCharm を閉じなくても新しいログファイルに書き出される。
_log_path = _APP_DIR / "salescaper.log"
_fmt = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s %(funcName)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
try:
    _fh = _RFH(_log_path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
    _fh.setFormatter(_fmt)
except OSError:
    _fh = logging.NullHandler()
_sh = logging.StreamHandler(sys.stderr)
_sh.setFormatter(_fmt)

for _noisy in ("PIL", "urllib3", "httpx", "httpcore", "asyncio", "streamlit"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

for _name in ("app", "saved_list", "scraper"):
    _lg = logging.getLogger(_name)
    _lg.setLevel(logging.DEBUG)
    if not _lg.handlers:
        _lg.addHandler(_fh)
        _lg.addHandler(_sh)
    _lg.propagate = False

logger = logging.getLogger("app")
logger.info("app.py 起動")  # 起動確認ログ

_NENKAN_DB_PATH = os.getenv("NENKAN_DB_PATH") or str(_APP_DIR / "nenkan.db")
_DB_CONNECT_NOTE = ""


def _mac_nas_path_hint(env_raw: str) -> str:
    """Windows の UNC パスが .env に残っているときの Mac 向け案内。"""
    if sys.platform != "darwin":
        return ""
    s = env_raw.strip().replace("/", "\\")
    if not (s.startswith("\\\\") or s.startswith("//")):
        return ""
    parts = [p for p in s.split("\\") if p]
    if len(parts) < 3:
        return ""
    # \\NAS\share\folder\... → /Volumes/share/folder/...
    tail = "/".join(parts[2:])
    vol = parts[1]
    return (
        f"\n\n【Mac】.env の NENKAN_DB_PATH は Windows 形式のため使えません。\n"
        f"1. Finder → サーバへ接続 → smb://{parts[0]}/{vol}\n"
        f"2. マウント後、次のようなパスに書き換えてください:\n"
        f"   NENKAN_DB_PATH=/Volumes/{vol}/{tail}\n"
        f"（/Volumes 直下のフォルダ名は環境で異なる場合があります。"
        f"Finder で nenkan.db を右クリック→「パス名をコピー」が確実です。）"
    )


def _safe_exists(path: Path, timeout: float = 8.0) -> bool:
    """NAS（UNC）上の exists() が返らないことがあるためタイムアウト付き。"""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FTimeout

    p = path.expanduser()
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(p.is_file)
        try:
            return bool(fut.result(timeout=timeout))
        except _FTimeout:
            logger.warning("パス確認タイムアウト: %s", p)
            return False
        except OSError as e:
            logger.warning("パス確認エラー: %s (%s)", p, e)
            return False


def _test_nenkan_db(db_path: Path) -> bool:
    """nenkan.db が開けて csv_companies を1件読めるか（NAS向け・軽量）。"""
    if not _safe_exists(db_path):
        return False

    try:
        from nenkan_sqlite import connect_nenkan

        conn = connect_nenkan(db_path, timeout=5)
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='csv_companies' LIMIT 1"
        ).fetchone():
            conn.close()
            return False
        conn.execute("SELECT 1 FROM csv_companies LIMIT 1").fetchone()
        conn.close()
        return True
    except Exception:
        return False


def _resolve_nenkan_db_path() -> tuple[Path | None, str]:
    """
    利用可能な nenkan.db を選ぶ。
    .env の NAS パスが壊れている／開けないときはアプリ横のローカルにフォールバック。
    """
    env_raw = (os.getenv("NENKAN_DB_PATH") or "").strip()
    candidates: list[tuple[str, Path]] = []
    if env_raw:
        candidates.append(("NAS（.env）", Path(env_raw)))
    local = (_APP_DIR / "nenkan.db").resolve()
    if not candidates or local != candidates[0][1].resolve():
        candidates.append(("ローカル", local))

    errors: list[str] = []
    for label, path in candidates:
        p = path.expanduser().resolve()
        if not _safe_exists(p):
            errors.append(f"{label}: なし")
            continue
        if _test_nenkan_db(p):
            note = ""
            return p, note
        errors.append(f"{label}: 壊れているか読めません")

    note = " / ".join(errors)
    if env_raw:
        note += _mac_nas_path_hint(env_raw)
    return None, note


def _nenkan_db_file() -> Path:
    """スペース入りパスでも安全に SQLite を開くため Path で統一。"""
    if _RESOLVED_NENKAN_DB is not None:
        return _RESOLVED_NENKAN_DB
    return Path(_NENKAN_DB_PATH).expanduser().resolve()


def _connect_nenkan_writable():
    import sqlite3 as _sqlite3
    from nenkan_sqlite import connect_nenkan_writable

    conn = connect_nenkan_writable(_nenkan_db_file(), timeout=15)
    conn.row_factory = _sqlite3.Row
    return conn


def _connect_nenkan():
    import sqlite3 as _sqlite3
    from nenkan_sqlite import connect_nenkan

    conn = connect_nenkan(_nenkan_db_file(), timeout=15)
    conn.row_factory = _sqlite3.Row
    return conn


import streamlit as st
import pandas as pd
import hashlib
try:
    import folium
    from streamlit_folium import st_folium as _st_folium
    _FOLIUM_OK = True
except ImportError:
    _FOLIUM_OK = False
from saved_list import (
    init_db as _init_db_raw,
    get_current_user, login_user, logout_user,
    save_companies, load_saved_companies, delete_companies,
    update_crm, count_saved, CRM_STATUSES,
    publish_search_crm, clear_crm,
    get_all_users as _get_all_users_raw,
    get_all_users_with_status as _get_all_users_with_status,
    get_crm_map_by_names,
    save_search_session, load_search_session,
    list_search_sessions, delete_search_session,
    get_scrape_cache, set_scrape_cache, delete_scrape_cache, get_scrape_cache_info,
    load_saved_companies_paged, count_saved_filtered,
    load_all_ids_filtered, PAGE_SIZE,
)
from print_export import build_printable_html, html_to_pdf, _safe_filename_part
from manual_company import (
    insert_manual_company,
    ManualCompanyError,
    apply_manual_scrape_fields,
    apply_houjin_fields,
)

# 企業年鑑DB（nenkan.db）があれば読み込む
import sqlite3 as _sqlite3_boot


def _load_industries_for_path(db_path: Path) -> list[str]:
    """csv_companies の industry カラムから件数順にリストを生成する。"""
    try:
        from nenkan_sqlite import connect_nenkan

        conn = connect_nenkan(db_path, timeout=10)
        rows = conn.execute(
            """SELECT industry, COUNT(*) as cnt
               FROM csv_companies
               WHERE industry IS NOT NULL
               GROUP BY industry
               ORDER BY cnt DESC"""
        ).fetchall()
        conn.close()
        return [r[0] for r in rows] + ["その他（直接入力）"]
    except Exception:
        return ["その他（直接入力）"]


def _is_remote_nenkan_path(path: Path) -> bool:
    s = str(path)
    return s.startswith("\\\\") or s.startswith("//") or "/Volumes/" in s.replace("\\", "/")


def _bootstrap_app_impl() -> tuple:
    """NAS接続・業種リスト取得（1セッション1回）。"""
    logger.info("DB bootstrap 開始 folder=%s", _APP_DIR)
    resolved, note = _resolve_nenkan_db_path()
    nenkan_db = None
    csv_db = None
    industries = ["その他（直接入力）"]
    if resolved is None:
        logger.error("企業DBを開けません: %s", note)
        return resolved, note, nenkan_db, csv_db, industries

    db_s = str(resolved)
    try:
        from nenkan_search import NenkanDB

        nenkan_db = NenkanDB(db_s)
        if nenkan_db.available:
            logger.info("年鑑DB読込OK path=%s", resolved)
    except ImportError:
        logger.info("nenkan_search.py が見つかりません（年鑑DB機能無効）")
    except _sqlite3_boot.DatabaseError as e:
        logger.error("年鑑DBが壊れています path=%s: %s", resolved, e)
        nenkan_db = None

    try:
        from csv_search import CsvDB

        csv_db = CsvDB(db_s)
        if csv_db.available:
            logger.info("CSV企業DB読込OK path=%s", resolved)
            if note:
                logger.warning("企業DB: %s", note.replace("\n", " "))
        else:
            logger.info("csv_companies テーブルなし（csv_import.py を先に実行してください）")
    except ImportError:
        logger.info("csv_search.py が見つかりません（CSV企業DB機能無効）")
    except _sqlite3_boot.DatabaseError as e:
        logger.error("CSV企業DBが壊れています path=%s: %s", resolved, e)
        csv_db = None

    if csv_db and csv_db.available and not _is_remote_nenkan_path(resolved):
        try:
            from domain_info_db import ensure_domain_info_columns
            from nenkan_sqlite import connect_nenkan

            _mig = connect_nenkan(resolved, timeout=10)
            ensure_domain_info_columns(_mig)
            _mig.close()
        except Exception as _e:
            logger.warning("domain_info 列マイグレーション: %s", _e)

    try:
        industries = _load_industries_for_path(resolved)
    except Exception as _e:
        logger.warning("業種リスト取得スキップ: %s", _e)
        industries = ["その他（直接入力）"]
    logger.info("DB bootstrap 完了")
    return resolved, note, nenkan_db, csv_db, industries


# main() 内で ensure_app_booted() により設定（import 時に DB 接続しない）
_RESOLVED_NENKAN_DB: Path | None = None
_DB_CONNECT_NOTE = ""
_nenkan_db = None
_csv_db = None
INDUSTRIES: list[str] = ["その他（直接入力）"]


def ensure_app_booted() -> None:
    """Streamlit 実行開始後に1回だけ DB を初期化する。"""
    global _RESOLVED_NENKAN_DB, _DB_CONNECT_NOTE, _nenkan_db, _csv_db, INDUSTRIES
    if st.session_state.get("_app_boot_done"):
        (
            _RESOLVED_NENKAN_DB,
            _DB_CONNECT_NOTE,
            _nenkan_db,
            _csv_db,
            INDUSTRIES,
        ) = st.session_state["_app_boot_data"]
        return
    with st.spinner("企業DBを読み込み中…"):
        boot = _bootstrap_app_impl()
    st.session_state["_app_boot_done"] = True
    st.session_state["_app_boot_data"] = boot
    (
        _RESOLVED_NENKAN_DB,
        _DB_CONNECT_NOTE,
        _nenkan_db,
        _csv_db,
        INDUSTRIES,
    ) = boot

_THUMBNAILS_DIR = Path(__file__).parent / "thumbnails"


@st.cache_resource(show_spinner=False)
def _cached_thumbnail_index() -> dict:
    """初回利用時に thumbnails/ を走査（起動時は走査しない）。"""
    try:
        from screenshot_lookup import preload_thumbnail_index
        idx = preload_thumbnail_index(_THUMBNAILS_DIR)
        logger.info("サムネイルインデックス: %d件", len(idx))
        return idx
    except ImportError:
        logger.info("screenshot_lookup.py が見つかりません（サムネイル機能無効）")
        return {}


def _thumb_index() -> dict:
    return _cached_thumbnail_index()

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────
SCREENSHOT_DIR    = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

PAGE_SIZE = 10

# ─────────────────────────────────────────────
# プルダウン選択肢
# ─────────────────────────────────────────────
REGIONS = [
    "広島市中区", "広島市東区", "広島市南区", "広島市西区",
    "広島市安佐南区", "広島市安佐北区", "広島市安芸区", "広島市佐伯区",
    "呉市", "竹原市", "三原市", "尾道市", "福山市", "府中市",
    "三次市", "庄原市", "大竹市", "東広島市", "廿日市市",
    "安芸高田市", "江田島市", "安芸郡府中町",
    "その他（直接入力）",
]

SORT_OPTIONS: dict[str, tuple[str | None, bool]] = {
    "🔴 Web提案スコア順（高い順）": ("Web提案スコア", False),
    "📋 取得順（デフォルト）": (None, False),
}

def _load_industries_from_db() -> list[str]:
    """後方互換。起動時は _bootstrap_app が INDUSTRIES を設定する。"""
    if _RESOLVED_NENKAN_DB is not None:
        return _load_industries_for_path(_RESOLVED_NENKAN_DB)
    return ["その他（直接入力）"]

# ─────────────────────────────────────────────
# ステータスバッジ（CRM）
# ─────────────────────────────────────────────
STATUS_COLORS = {
    "未着手":        ("#888888", "#F0F0F0"),
    "飛込訪問中":    ("#00695C", "#E0F2F1"),
    "架電済み":      ("#1565C0", "#E3F2FD"),
    "担当者確認中":  ("#00695C", "#E0F2F1"),
    "メール送付済み": ("#6A1B9A", "#F3E5F5"),
    "商談中":        ("#E65100", "#FFF3E0"),
    "見積提出済み":  ("#F57F17", "#FFFDE7"),
    "成約":          ("#2E7D32", "#E8F5E9"),
    "見送り":        ("#B71C1C", "#FFEBEE"),
}

def _draft_crm_store() -> dict[str, dict]:
    if "draft_crm" not in st.session_state:
        st.session_state["draft_crm"] = {}
        for name, memo in st.session_state.get("draft_memos", {}).items():
            if memo:
                st.session_state["draft_crm"][name] = {
                    "memo": memo,
                    "status": "未着手",
                    "assignee": "",
                }
    return st.session_state["draft_crm"]


def _get_draft_crm(company_name: str) -> dict[str, str]:
    store = _draft_crm_store()
    if company_name not in store:
        store[company_name] = {"memo": "", "status": "未着手", "assignee": ""}
    return store[company_name]


def _crm_key_suffix(company_name: str) -> str:
    import hashlib
    return hashlib.sha256(company_name.strip().encode("utf-8")).hexdigest()[:16]


def _srch_crm_widget_keys(company_name: str) -> tuple[str, str, str]:
    s = _crm_key_suffix(company_name)
    return f"srch_memo_{s}", f"srch_status_{s}", f"srch_assign_{s}"


def _init_srch_crm_widgets(
    company_name: str,
    memo: str = "",
    status: str = "未着手",
    assignee: str = "",
    crm_updated_at: str = "",
) -> tuple[str, str, str]:
    """検索結果CRM。crm_updated_at が変わったら DB 値で session_state を上書き。"""
    memo_k, status_k, assign_k = _srch_crm_widget_keys(company_name)
    ver_k = f"srch_crm_ver_{_crm_key_suffix(company_name)}"
    ver = str(crm_updated_at or "")
    if st.session_state.get(ver_k) != ver or memo_k not in st.session_state:
        st.session_state[memo_k] = memo
        st.session_state[status_k] = status if status in CRM_STATUSES else "未着手"
        st.session_state[assign_k] = assignee
        st.session_state[ver_k] = ver
    return memo_k, status_k, assign_k


def _sync_saved_crm_widgets(
    record_id: int,
    memo: str,
    status: str,
    assignee: str,
    crm_updated_at: str = "",
) -> tuple[str, str, str]:
    """保存リストCRM。DB更新後に古い入力が残らないよう同期。"""
    rid = str(record_id)
    memo_k, status_k, assign_k = f"sv_memo_{rid}", f"sv_status_{rid}", f"sv_assign_{rid}"
    ver_k = f"sv_crm_ver_{rid}"
    ver = str(crm_updated_at or "")
    if st.session_state.get(ver_k) != ver or memo_k not in st.session_state:
        st.session_state[memo_k] = memo
        st.session_state[status_k] = status if status in CRM_STATUSES else "未着手"
        st.session_state[assign_k] = assignee
        st.session_state[ver_k] = ver
    return memo_k, status_k, assign_k


def _sync_memo_list_widgets(
    record_id: int,
    memo: str,
    status: str,
    assignee: str,
    crm_updated_at: str = "",
) -> tuple[str, str, str]:
    rid = str(record_id)
    status_k, assign_k, memo_k = f"memo_status_{rid}", f"memo_assignee_{rid}", f"memo_text_{rid}"
    ver_k = f"memo_crm_ver_{rid}"
    ver = str(crm_updated_at or "")
    if st.session_state.get(ver_k) != ver or memo_k not in st.session_state:
        st.session_state[memo_k] = memo
        st.session_state[status_k] = status if status in CRM_STATUSES else "未着手"
        st.session_state[assign_k] = assignee
        st.session_state[ver_k] = ver
    return status_k, assign_k, memo_k


def _invalidate_saved_crm_widgets(record_id: int) -> None:
    rid = str(record_id)
    for k in (
        f"sv_memo_{rid}", f"sv_status_{rid}", f"sv_assign_{rid}", f"sv_crm_ver_{rid}",
        f"memo_text_{rid}", f"memo_status_{rid}", f"memo_assignee_{rid}", f"memo_crm_ver_{rid}",
    ):
        st.session_state.pop(k, None)


def _reset_srch_crm_widgets(company_name: str) -> None:
    memo_k, status_k, assign_k = _srch_crm_widget_keys(company_name)
    st.session_state[memo_k] = ""
    st.session_state[status_k] = "未着手"
    st.session_state[assign_k] = ""


def _reset_draft_crm(company_name: str) -> None:
    _draft_crm_store().pop(company_name, None)
    _reset_srch_crm_widgets(company_name)


def status_badge(status: str) -> str:
    color, bg = STATUS_COLORS.get(status, ("#888888", "#F0F0F0"))
    return (
        f'<span style="background:{bg}; color:{color}; font-weight:bold; '
        f'padding:2px 10px; border-radius:12px; font-size:0.85em;">{status}</span>'
    )


@st.cache_data(show_spinner="PDFを生成中…")
def _cached_print_pdf(cache_key: str, html_str: str) -> bytes:
    """HTML から PDF を生成（同一内容はキャッシュ）。"""
    try:
        return html_to_pdf(html_str)
    except Exception as e:
        raise RuntimeError(f"{type(e).__name__}: {e or '詳細なし'}") from e


def render_print_export_buttons(
    sub_df: pd.DataFrame,
    *,
    key_prefix: str,
    file_tag: str,
    title: str = "SalesScraper 検索結果",
    subtitle: str = "",
    enable_scraping: bool = False,
    include_screenshots: bool = True,
    compact: bool = False,
) -> None:
    """HTML / PDF ダウンロードボタン（1社または複数社）。"""
    if sub_df.empty:
        st.caption("出力対象がありません。")
        return
    _html = build_printable_html(
        sub_df,
        title=title,
        subtitle=subtitle,
        enable_scraping=enable_scraping,
        thumbnails_dir=_THUMBNAILS_DIR,
        include_screenshots=include_screenshots,
    )
    _base = f"search_{_safe_filename_part(file_tag)}"
    _n = len(sub_df)
    _html_key = hashlib.sha256(_html.encode("utf-8")).hexdigest()
    _pdf_state = f"print_pdf_{key_prefix}_{_html_key[:24]}"
    _pdf_err_state = f"{_pdf_state}_err"

    col_h, col_p = st.columns(2)
    with col_h:
        st.download_button(
            label="📄 HTML" if compact else f"📄 HTML（{_n} 件）",
            data=_html.encode("utf-8"),
            file_name=f"{_base}.html",
            mime="text/html",
            use_container_width=True,
            key=f"{key_prefix}_html",
        )
    with col_p:
        if st.button(
            "📕 PDF生成" if compact else f"📕 PDFを生成（{_n} 件）",
            key=f"{key_prefix}_gen_pdf",
            use_container_width=True,
        ):
            with st.spinner("PDFを生成中…"):
                try:
                    st.session_state[_pdf_state] = _cached_print_pdf(
                        _html_key, _html
                    )
                    st.session_state.pop(_pdf_err_state, None)
                except Exception as _pdf_err:
                    st.session_state.pop(_pdf_state, None)
                    st.session_state[_pdf_err_state] = str(_pdf_err)
                    logger.warning(
                        "PDF生成失敗: %s", _pdf_err, exc_info=True
                    )
        if _pdf_state in st.session_state:
            st.download_button(
                label="📥 PDFをダウンロード",
                data=st.session_state[_pdf_state],
                file_name=f"{_base}.pdf",
                mime="application/pdf",
                use_container_width=True,
                key=f"{key_prefix}_pdf_dl",
            )
        elif _pdf_err_state in st.session_state:
            st.caption(
                f"PDF生成不可: {st.session_state[_pdf_err_state]}"
            )


def render_card_print_export(
    row,
    *,
    key_prefix: str,
    doc_title: str,
    enable_scraping: bool = False,
    include_screenshots: bool | None = None,
) -> None:
    """カード1社分の印刷・共有（右カラム：地図の下）。"""
    company = str(row.get("社名", "") or "").strip() or "company"
    d = row.to_dict() if hasattr(row, "to_dict") else dict(row)
    d.pop("index", None)
    sub_df = pd.DataFrame([d])
    if include_screenshots is None:
        include_screenshots = st.session_state.get(
            "print_include_screenshots", True
        )
    st.divider()
    st.markdown("**🖨 印刷・共有（この会社）**")
    st.caption(f"対象：{company}")
    render_print_export_buttons(
        sub_df,
        key_prefix=key_prefix,
        file_tag=_safe_filename_part(company)[:36],
        title=doc_title,
        subtitle=company,
        enable_scraping=enable_scraping,
        include_screenshots=include_screenshots,
        compact=True,
    )

# ─────────────────────────────────────────────
# 売上推移パーサー
# ─────────────────────────────────────────────

def parse_sales_history(raw: str) -> list[dict]:
    """
    sales_history テキスト（タブ区切り）を解析して直近3件を返す。

    列構成: 期 / 売上(千円) / 経常利益(千円) / 純利益(千円)

    返り値:
      [{"period": "23.10", "sales_man": 102265.0,
        "profit_man": 1440.0,   # 経常利益（万円）
        "pure_profit": 930.0,   # 純利益（万円）
       }, ...]
      ※ マイナス（▲）は負値に変換。千円→万円に変換済み。
    """
    import re as _re
    if not raw:
        return []

    def _to_man(text: str):
        """千円テキスト→万円float。▲や△はマイナス扱い。nullや－はNone。"""
        t = _re.sub(r"[（(][^)）]*[)）]", "", text).strip()
        if not t or t.lower() in ("null", "－", "-", ""):
            return None
        digits = _re.search(r"[\d,]+", t.replace(",", ""))
        if not digits:
            return None
        prefix = t[: digits.start()]
        negative = (
            t.startswith(("▲", "△", "−", "－"))
            or any(m in prefix for m in ("▲", "△", "−", "－"))
        )
        val = int(digits.group().replace(",", "")) / 10  # 千円→万円
        return -val if negative else val

    results = []
    for line in raw.strip().split("\n")[1:]:   # 先頭ヘッダー行をスキップ
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        period = cols[0].strip()
        if not period:
            continue
        sales_man  = _to_man(cols[1]) if len(cols) > 1 else None
        profit_man = _to_man(cols[2]) if len(cols) > 2 else None
        pure_profit= _to_man(cols[3]) if len(cols) > 3 else None
        if sales_man is None:
            continue
        results.append({
            "period":      period,
            "sales_man":   sales_man,
            "profit_man":  profit_man,   # 経常利益（万円）
            "pure_profit": pure_profit,  # 純利益（万円）
        })
    return results[-3:]   # 直近3件のみ


def fmt_man(val: float) -> str:
    """万円 → 読みやすい文字列（億円 / 万円）"""
    if val >= 10000:
        return f"{val / 10000:.1f}億円"
    return f"{val:,.0f}万円"


# ─────────────────────────────────────────────
# スコアリング関数
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# ウィークポイント判定・スコアリング
# ─────────────────────────────────────────────

# 各ウィークポイントの定義
# (キー, 重み, ラベル, 色, 営業トーク)
_WP_DEFS = [
    (
        "hp_none", 5,
        "HPなし",
        "#C0392B",
        "公式サイトがないと信頼性・集客で大きく損をしています。作成の第一歩をご提案できます。",
    ),
    (
        "non_ssl", 3,
        "ノンSSL",
        "#E67E22",
        "常時SSL化されていないため、Googleの検索順位で不利になっています。対策はすぐできます。",
    ),
    (
        "non_resp", 3,
        "ノンレスポンシブ",
        "#E67E22",
        "スマホからアクセスすると表示が崩れます。今やスマホ比率は6割超です。",
    ),
    (
        "no_career", 2,
        "採用ページなし",
        "#8E44AD",
        "採用コンテンツがないと求職者が離れます。採用LP作成をご提案できます。",
    ),
    (
        "old_server", 1,
        "旧来サーバー環境",
        "#7F8C8D",
        "CMS・サーバー情報が古く、セキュリティや表示速度に課題がある可能性があります。",
    ),
]

_WP_MAX_STARS = sum(d[1] for d in _WP_DEFS)  # 最大重み合計 = 14


def _tech_stack_has_viewport(row) -> bool | None:
    """tech_stack JSON の hasViewport。無ければ None。"""
    import json as _j
    raw = str(row.get("_tech_stack", "") or "")
    if not raw:
        return None
    try:
        sigs = _j.loads(raw).get("signals", {})
        if "hasViewport" in sigs:
            return bool(sigs["hasViewport"])
    except Exception:
        pass
    return None


def _resolve_responsive(row, *, scrape_val=None, csv_resp=None) -> bool:
    """レスポンシブ表示用。明示的に非対応と分かったときだけ False。"""
    if scrape_val is True or csv_resp is True:
        return True
    if scrape_val is False or csv_resp is False:
        return False
    db_resp = _tech_stack_has_viewport(row)
    if db_resp is True:
        return True
    if db_resp is False:
        return False
    try:
        import json as _j
        wpc = _j.loads(row.get("_weak_points", "") or "{}")
        if wpc.get("is_non_responsive"):
            return False
    except Exception:
        pass
    return True  # 未取得時は対応扱い（暫定）


def calc_weakpoints(row) -> tuple[list[tuple], int]:
    """
    ウィークポイントを判定してリストとスコアを返す。

    CSV の weak_points / renewal_score を優先し、
    スクレイピング結果で補完する。

    Returns
    -------
    wp_list : list of (key, label, color, talk)
    star_score : 0〜5 の★数
    """
    import json as _j, re as _r

    url      = str(row.get("WebサイトURL", "") or "").strip()

    # CSV weak_points を解析
    _wpc = {}
    try:
        _wpc = _j.loads(row.get("_weak_points", "") or "{}")
    except Exception:
        pass

    # スクレイピング結果
    resp_val   = row.get("レスポンシブ", None)
    page_text  = str(row.get("page_text", "") or "").lower()
    cms_val    = str(row.get("CMS", "") or "").strip()
    hosting    = str(row.get("hosting_company", "") or "").strip()
    renewal    = row.get("_renewal_score")

    # server_info から hosting 補完
    if not hosting or hosting == "不明":
        _si = str(row.get("_server_info", "") or "")
        _om = _r.search("Org:\\s*([^\xb7|]+)", _si)
        if _om:
            hosting = _om.group(1).strip()

    found = {}  # {key: True}

    # ① HPなし（実URLの有無で判定。DBの is_no_website が古いまま残る場合を防ぐ）
    found["hp_none"] = not _valid_website_for_screenshot(url)

    # ② ノンSSL（HPありの場合のみ）
    found["non_ssl"] = (
        not found["hp_none"] and
        bool(_wpc.get("is_non_ssl") or (url.startswith("http://")))
    )

    # ③ ノンレスポンシブ（HPあり・明示的非対応のみ）
    found["non_resp"] = (
        not found["hp_none"] and
        not _resolve_responsive(row, scrape_val=resp_val, csv_resp=_tech_stack_has_viewport(row))
    )

    # ④ 採用ページなし（スクレイピングON時 or CSV判定があるとき）
    if page_text:
        has_career = bool(_r.search(
            r"採用|求人|recruit|career|join.?us|働く|スタッフ募集|一緒に働",
            page_text
        ))
        found["no_career"] = not found["hp_none"] and not has_career
    else:
        found["no_career"] = False  # 判定不可は表示しない

    # ⑤ 旧来サーバー環境（CMS不明 or renewalスコア高 or 旧来ホスティング）
    _OLD_HOSTING = [
        "さくらインターネット", "ロリポップ", "ヘテムル", "お名前.com",
        "バリュードメイン", "スターネット", "カゴヤ", "コアサーバー",
        "ムームードメイン", "inetd", "アブルネット",
    ]
    _is_old_server = (
        (cms_val in ("不明", "") and not found["hp_none"]) or
        (renewal is not None and float(renewal) >= 15) or
        any(k in hosting for k in _OLD_HOSTING)
    )
    found["old_server"] = not found["hp_none"] and _is_old_server

    # 重み合計 → ★スコア（0〜5）
    total_weight = sum(
        d[1] for d in _WP_DEFS if found.get(d[0])
    )
    star_score = min(5, round(total_weight / _WP_MAX_STARS * 5 + 0.4))

    # 結果リスト（定義順）
    wp_list = [
        (d[0], d[2], d[3], d[4])
        for d in _WP_DEFS
        if found.get(d[0])
    ]

    return wp_list, star_score


def star_label(n: int) -> str:
    """★スコアを色付きHTMLで返す"""
    if n >= 4:
        color = "#C0392B"
        priority = "提案余地：大"
    elif n >= 2:
        color = "#E67E22"
        priority = "提案余地：中"
    elif n >= 1:
        color = "#F1C40F"
        priority = "提案余地：小"
    else:
        color = "#95A5A6"
        priority = "提案余地：なし"
    stars = "★" * n + "☆" * (5 - n)
    return (
        f'<span style="color:{color};font-size:15px;font-weight:700;">{stars}</span>'
        f'　<span style="font-size:12px;color:{color};">{priority}</span>'
    )


def calc_web_renewal_score(row: pd.Series) -> tuple[int, list[str]]:
    """後方互換性のために残す（add_scores から呼ばれる）"""
    wp_list, star = calc_weakpoints(row)
    score = min(star * 20, 100)
    reasons = [label for _, label, _, _ in wp_list]
    return score, reasons


def score_to_label(score: int) -> str:
    if score >= 80:   return "🔴 最優先"
    elif score >= 60: return "🟠 優先"
    elif score >= 40: return "🟡 検討"
    elif score >= 20: return "🟢 低優先"
    else:             return "⚪ 対象外"


def add_scores(df: pd.DataFrame, industry: str) -> pd.DataFrame:
    web_scores, web_reasons = [], []
    for _, row in df.iterrows():
        ws, wr = calc_web_renewal_score(row)
        web_scores.append(ws)
        web_reasons.append("、".join(wr) if wr else "該当なし")
    df["Web提案スコア"] = web_scores
    df["Web提案優先度"] = [score_to_label(s) for s in web_scores]
    df["Web提案理由"]   = web_reasons
    return df


# ─────────────────────────────────────────────
# CSV企業DB 検索関数
# ─────────────────────────────────────────────


def _csv_region_clause(region: str) -> tuple[str, list]:
    """
    住所条件とパラメータを返す。
    広島市○区は「広島市中区」と「広島市 … 中区」表記の両方を拾う。
    府中市／府中町は紛らわしいため個別に厳密処理する。
    """
    if not region or region == "すべて":
        return "", []
    r = region.strip()

    # 府中市：「広島県府中市」に限定（安芸郡府中町を除外）
    if r == "府中市":
        return (
            "(address LIKE ? AND address NOT LIKE ?)",
            ["%府中市%", "%府中町%"],
        )

    # 府中町：「安芸郡府中町」に限定（府中市を除外）
    if r in ("府中町", "安芸郡府中町"):
        return (
            "(address LIKE ? AND address NOT LIKE ?)",
            ["%府中町%", "%府中市%"],
        )

    # 広島市○区：「広島市南区」と「安佐南区」を混同しないよう区名は厳密に
    if r.startswith("広島市") and r.endswith("区") and len(r) > 3:
        ward = r[3:]  # e.g. 中区, 安佐南区
        return (
            "(address LIKE ? OR address LIKE ?)",
            [f"%広島市{ward}%", f"%広島市 {ward}%"],
        )

    return ("address LIKE ?", [f"%{r}%"])


def _csv_industry_clause(industry: str) -> tuple[str, list]:
    """
    業種条件とパラメータ。
    industry カラムの実値と完全一致で検索する。
    「その他（直接入力）」の場合はフリーワード的に部分一致にフォールバック。
    """
    if not industry or industry == "すべて":
        return "", []

    # DB実値と完全一致（高速・確実）
    return ("industry = ?", [industry])


def _executives_table_exists(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='executives'"
    ).fetchone()
    return row is not None


_EXEC_PRESIDENT_SUBQUERY = """
    (SELECT e.name FROM executives e
     WHERE e.company_id = c.csv_id
     AND (e.role LIKE '%代表取締役%'
       OR e.role LIKE '%代表理事%'
       OR e.role IN ('社長','取締役社長','代表社員','代表','代表者',
                     '理事長','取締役会長','会長','専務取締役',
                     '常務取締役','理事','専務理事','常務理事'))
     ORDER BY
       CASE
         WHEN e.role LIKE '%代表取締役社長%' THEN 1
         WHEN e.role LIKE '%代表取締役%'    THEN 2
         WHEN e.role LIKE '%代表理事%'      THEN 3
         WHEN e.role IN ('社長','取締役社長') THEN 4
         WHEN e.role IN ('理事長','代表','代表者') THEN 5
         WHEN e.role IN ('取締役会長','会長') THEN 6
         WHEN e.role IN ('専務取締役','常務取締役') THEN 7
         ELSE 8
       END
     LIMIT 1)
"""


def _csv_search_select_sql(where: str, *, use_executives: bool) -> str:
    if use_executives:
        pres = f"COALESCE(NULLIF(c.president, ''), {_EXEC_PRESIDENT_SUBQUERY})"
    else:
        pres = "c.president"
    return f"""
        SELECT c.*,
               {pres} AS president_enriched
        FROM csv_companies c
        {where}
        ORDER BY c.name COLLATE NOCASE, c.csv_id
    """


def _dedupe_company_search_rows(rows: list) -> list:
    """同一企業の重複行を1件にまとめる（website あり・法人番号ありを優先）。"""
    import unicodedata

    def _norm_name(s: str) -> str:
        return unicodedata.normalize("NFKC", (s or "")).strip().lower()

    def _has_website(r) -> bool:
        return bool(str(dict(r).get("website") or "").strip())

    def _row_key(r) -> tuple:
        d = dict(r)
        cn = (d.get("corporate_number") or "").strip()
        if cn:
            return ("cn", cn)
        addr = (d.get("address") or "").strip()[:24]
        return ("na", _norm_name(d.get("name")), addr)

    def _prefer(a, b):
        if _has_website(a) and not _has_website(b):
            return a
        if _has_website(b) and not _has_website(a):
            return b
        da, db = dict(a), dict(b)
        if (da.get("corporate_number") or "").strip() and not (
            db.get("corporate_number") or ""
        ).strip():
            return a
        return a

    best: dict[tuple, object] = {}
    order: list[tuple] = []
    for row in rows:
        k = _row_key(row)
        if k not in best:
            best[k] = row
            order.append(k)
        else:
            best[k] = _prefer(row, best[k])
    return [best[k] for k in order]


def _is_manual_company_row(row) -> bool:
    """手動登録企業（年鑑未掲載）かどうか。"""
    src = str(row.get("_source_type") or "").strip().lower()
    if src:
        return src == "manual"
    csv_id = str(row.get("_csv_id") or "").strip()
    if csv_id and _csv_db and _csv_db.available:
        co = _csv_db.lookup_company_by_id(csv_id)
        if co:
            return str(co.get("source_type") or "").strip().lower() == "manual"
    return False


def _philosophy_from_db_row(d: dict) -> tuple[str, str]:
    """DB行から (Web/AI用企業理念, 年鑑DB用企業理念) を分離して返す。"""
    src = str(d.get("source_type") or "").strip().lower()
    phil = str(d.get("philosophy") or "").strip()
    if src == "manual":
        return phil, ""
    return "", phil


def _enrich_saved_row_from_nenkan(row) -> dict:
    """保存リスト行を年鑑DB（csv_companies）の最新値で補完。"""
    def _empty(val) -> bool:
        s = str(val or "").strip()
        return not s or s in ("不明", "情報なし", "null", "None", "nan")

    d = row.to_dict() if hasattr(row, "to_dict") else dict(row)
    if not _csv_db or not _csv_db.available:
        return d

    csv_co = None
    csv_id = str(d.get("_csv_id") or d.get("csv_id") or "").strip()
    if csv_id:
        csv_co = _csv_db.lookup_company_by_id(csv_id)
    if not csv_co:
        name = str(d.get("社名") or "").strip()
        if name:
            csv_co = _csv_db.lookup_company(name)
    if not csv_co:
        return d

    if not csv_id and csv_co.get("csv_id"):
        d["_csv_id"] = csv_co["csv_id"]

    for df_col, db_col in (
        ("代表者名", "president"),
        ("資本金", "capital"),
        ("従業員数", "employees"),
        ("設立年", "established"),
        ("事業内容", "business"),
        ("業種", "industry"),
    ):
        if _empty(d.get(df_col)):
            val = str(csv_co.get(db_col) or "").strip()
            if val and val not in ("不明", "情報なし"):
                d[df_col] = val

    src = str(csv_co.get("source_type") or "").strip().lower()
    if src == "manual":
        phil = str(csv_co.get("philosophy") or "").strip()
        if phil and _empty(d.get("企業理念")):
            d["企業理念"] = phil
    elif _empty(d.get("csv_企業理念")):
        phil = str(csv_co.get("philosophy") or "").strip()
        if phil:
            d["csv_企業理念"] = phil

    if not d.get("_source_type"):
        d["_source_type"] = csv_co.get("source_type") or ""

    return d


def _render_nenkan_detail_expander(row) -> None:
    """年鑑DB詳細情報。手動登録企業は年鑑非掲載の旨を表示。"""
    if _is_manual_company_row(row):
        with st.expander("📋 年鑑DB詳細情報"):
            st.markdown(
                "<span style='font-size:13px;color:var(--color-text-secondary);"
                "line-height:1.6;'>"
                "ℹ️ この企業は<strong>手動登録</strong>のため、"
                "<strong>企業年鑑には掲載されていません</strong>。"
                "上場・主要銀行・取引先など、年鑑由来の詳細情報はありません。"
                "</span>",
                unsafe_allow_html=True,
            )
        return

    nenkan_listing = str(row.get("nenkan_上場", "") or "").strip()
    nenkan_banks   = str(row.get("nenkan_銀行", "") or "").strip()
    nenkan_officers = str(row.get("nenkan_役員", "") or "").strip()
    csv_banks    = str(row.get("csv_銀行", "") or "").strip()
    csv_customer = str(row.get("csv_顧客", "") or "").strip()
    csv_supplier = str(row.get("csv_仕入先", "") or "").strip()
    csv_officers = str(row.get("csv_役員", "") or "").strip()
    csv_listing  = str(row.get("csv_上場", "") or "").strip()

    listing = csv_listing or nenkan_listing
    banks = csv_banks or nenkan_banks
    officers = csv_officers or nenkan_officers

    if not any([listing, banks, csv_customer, csv_supplier, officers]):
        return

    with st.expander("📋 年鑑DB詳細情報"):
        if listing:
            st.markdown(f"**上場：** {listing}")
        if banks:
            st.markdown(f"**主要銀行：** {banks}")
        if csv_customer:
            st.markdown(f"**主要顧客：** {csv_customer}")
        if csv_supplier:
            st.markdown(f"**主要仕入先：** {csv_supplier}")
        if officers:
            label = "役員" if csv_officers else "役員（年鑑）"
            st.markdown(f"**{label}：** {officers}")


def search_csv_companies(
    keyword: str = "",
    region: str = "",
    industry: str = "",
    limit: int | None = None,
) -> tuple[pd.DataFrame, int]:
    """
    csv_companies テーブルを検索して DataFrame を返す。

    limit: 表示件数上限。None または 0 以下で上限なし。
    """
    if not (_csv_db and _csv_db.available):
        return pd.DataFrame(), 0

    conn = _connect_nenkan()

    conditions = []
    params     = []

    if region and region != "すべて":
        rsql, rpar = _csv_region_clause(region)
        if rsql:
            conditions.append(rsql)
            params.extend(rpar)

    if industry and industry != "すべて":
        isql, ipar = _csv_industry_clause(industry)
        if isql:
            conditions.append(isql)
            params.extend(ipar)

    # キーワード（企業名・事業内容・概要 + 役員名）
    exec_company_ids = set()
    if keyword:
        conditions.append(
            "(name LIKE ? OR name_pdf LIKE ? OR business LIKE ? OR description LIKE ?)"
        )
        params.extend([f"%{keyword}%"] * 4)

        # 役員名でもヒットする企業IDを取得
        try:
            exec_rows = conn.execute(
                "SELECT DISTINCT company_id FROM executives "
                "WHERE name LIKE ? AND company_id IS NOT NULL",
                (f"%{keyword}%",)
            ).fetchall()
            exec_company_ids = {r[0] for r in exec_rows}
        except Exception:
            pass

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    use_executives = _executives_table_exists(conn)
    sql = _csv_search_select_sql(where, use_executives=use_executives)
    rows = conn.execute(sql, params).fetchall()

    # 役員名ヒット企業を追加
    if exec_company_ids and use_executives:
        already = {dict(r).get("csv_id") for r in rows}
        extra_ids = list(exec_company_ids - already)
        if extra_ids:
            placeholders = ",".join("?" * len(extra_ids))
            extra_sql = _csv_search_select_sql(
                f"WHERE c.csv_id IN ({placeholders})",
                use_executives=True,
            )
            extra = conn.execute(extra_sql, extra_ids).fetchall()
            rows = list(rows) + list(extra)

    rows = _dedupe_company_search_rows(list(rows))
    total_matched = len(rows)
    if limit is not None and limit > 0:
        rows = rows[:limit]

    conn.close()

    if not rows:
        return pd.DataFrame(), total_matched

    # officers略称から代表者名を抽出するヘルパー
    import re as _re_off
    _REP_ABBR = ['代社', '代会', '代', '代副社', '代専', '社', '理事長']
    def _extract_rep_from_officers(officers_str: str):
        if not officers_str:
            return None
        for abbr in _REP_ABBR:
            m = _re_off.search(r'\(' + _re_off.escape(abbr) + r'\)([^\(]{2,20}?)(?:\(|$)', officers_str)
            if m:
                name = m.group(1).strip()
                if name:
                    return name
        return None

    records = []
    for row in rows:
        d = dict(row)
        # 代表者名: president_enriched → president → officers略称解析 の順で取得
        _pres = (d.get("president_enriched", "") or d.get("president", "") or "").strip()
        if not _pres:
            _pres = _extract_rep_from_officers(d.get("officers", "") or "") or ""
        _phil_web, _phil_yearbook = _philosophy_from_db_row(d)
        records.append({
            "社名":         d.get("name", "情報なし") or "情報なし",
            "住所":         d.get("address", "情報なし") or "情報なし",
            "TEL":          d.get("tel", "情報なし") or "情報なし",
            "WebサイトURL": d.get("website", "情報なし") or "情報なし",
            "代表者名":     _pres or "不明",
            "資本金":       d.get("capital", "不明") or "不明",
            "従業員数":     d.get("employees", "不明") or "不明",
            "設立年":       d.get("established", "不明") or "不明",
            "事業内容":     d.get("business", "不明") or "不明",
            "業種":         d.get("industry", "不明") or "不明",
            "法人番号":     str(d.get("corporate_number", "") or ""),
            "csv_概要":     d.get("description", "") or "",
            "csv_銀行":     d.get("banks", "") or "",
            "csv_顧客":     d.get("customers", "") or "",
            "csv_仕入先":   d.get("suppliers", "") or "",
            "csv_役員":     d.get("officers", "") or "",
            "csv_売上高":      str(d.get("revenue", "") or ""),
            "csv_売上推移":    d.get("sales_history", "") or "",
            "csv_経常利益":    str(d.get("ordinary_profit", "") or ""),
            "csv_上場":        "上場" if d.get("is_listed") else "非上場",
            "csv_業種":        d.get("industry", "") or "",
            "_csv_id":         d.get("csv_id", "") or "",
            "_thumbnail_url":  d.get("thumbnail_url", "") or "",
            "_latitude":       d.get("latitude"),
            "_longitude":      d.get("longitude"),
            "_weak_points":    d.get("weak_points", "") or "",
            "_tech_stack":     d.get("tech_stack", "") or "",
            "_server_info":    d.get("server_info", "") or "",
            "_renewal_score":  d.get("renewal_score"),
            "ssl_issuer":      (d.get("ssl_issuer") or "").strip() or "不明",
            "ssl_expiry":      (d.get("ssl_expiry") or "").strip() or "不明",
            "whois_registrar": (d.get("domain_registrar") or "").strip() or "不明",
            "whois_expiry":    (d.get("whois_expiry") or "").strip() or "不明",
            "hosting_company": (d.get("hosting_company") or "").strip() or "不明",
            "企業理念":        _phil_web,
            "csv_企業理念":    _phil_yearbook,
            "_source_type":    d.get("source_type") or "",
            "AI営業ポイント":  d.get("ai_analysis", "") or "",
        })

    df = pd.DataFrame(records)
    return df, total_matched


# ─────────────────────────────────────────────
# スクリーンショット表示ヘルパー
# ─────────────────────────────────────────────

def _valid_website_for_screenshot(url: str) -> bool:
    """HPなし・地図ポータルURLのときはスクショを表示しない。"""
    u = (url or "").strip()
    if not u or u in ("情報なし", "不明"):
        return False
    try:
        from scraper import is_blocked_url
        return not is_blocked_url(u)
    except ImportError:
        return True


def render_screenshot(ss_path: str, url: str = "") -> None:
    """
    スクリーンショットをクリック可能な画像として表示する。
    - url が指定されていれば、クリックでそのURLを新タブで開く
    - ローカルパスの場合は base64 に変換して HTML に埋め込む
    - URL画像の場合はそのまま <img src> に使う
    """
    if not ss_path:
        return

    import base64 as _b64

    # 画像ソースを決定
    if ss_path.startswith("http"):
        img_src = ss_path
    elif Path(ss_path).exists():
        with open(ss_path, "rb") as _f:
            _data = _b64.b64encode(_f.read()).decode()
        _ext = Path(ss_path).suffix.lower().lstrip(".")
        _mime = "jpeg" if _ext in ("jpg", "jpeg") else _ext
        img_src = f"data:image/{_mime};base64,{_data}"
    else:
        return

    # クリック可能な画像HTML
    if url and url not in ("情報なし", "不明", ""):
        html = (
            f'<a href="{url}" target="_blank" rel="noopener noreferrer" '            f'style="display:block;cursor:pointer;">'            f'<img src="{img_src}" style="width:100%;border-radius:6px;"'            f' title="{url}" />'            f'</a>'
        )
    else:
        html = (
            f'<img src="{img_src}" '            f'style="width:100%;border-radius:6px;" />' 
        )

    st.markdown(html, unsafe_allow_html=True)


# ─────────────────────────────────────────────
# 地図表示ヘルパー（OpenStreetMap / Folium）
# ─────────────────────────────────────────────

def render_map(lat: float, lng: float, company_name: str, address: str,
               map_key: str = "") -> None:
    """
    Folium で OpenStreetMap を表示する。
    map_key: 複数地図を表示する際の重複キー防止用（企業名ベースで呼び出し元から渡す）
    """
    if not _FOLIUM_OK:
        st.caption("地図表示には folium と streamlit-folium が必要です。")
        return

    import hashlib as _hl
    # map_key が未指定の場合は座標と企業名からユニークキーを生成
    _key = map_key or _hl.md5(f"{lat}_{lng}_{company_name}".encode()).hexdigest()

    m = folium.Map(
        location=[lat, lng],
        zoom_start=16,
        tiles="OpenStreetMap",
    )
    folium.Marker(
        location=[lat, lng],
        popup=folium.Popup(f"<b>{company_name}</b><br>{address}", max_width=200),
        tooltip=company_name,
        icon=folium.Icon(color="blue", icon="building", prefix="fa"),
    ).add_to(m)

    _st_folium(m, width="100%", height=200, returned_objects=[], key=_key)


def _geocode_gsi(address: str) -> tuple[float, float] | None:
    """国土地理院の住所検索（日本の住所向け）。"""
    import json as _json
    import urllib.parse
    import urllib.request

    q = urllib.parse.quote(address.strip())
    url = f"https://msearch.gsi.go.jp/address-search/AddressSearch?q={q}"
    req = urllib.request.Request(url, headers={"User-Agent": "salescaper/1.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = _json.loads(resp.read())
    if not data:
        return None
    coords = data[0].get("geometry", {}).get("coordinates")
    if not coords or len(coords) < 2:
        return None
    lng, lat = float(coords[0]), float(coords[1])
    return lat, lng


def _geocode_nominatim(query: str) -> tuple[float, float] | None:
    import json as _json
    import urllib.parse
    import urllib.request

    params = urllib.parse.urlencode({
        "q": query, "format": "json", "limit": "1", "countrycodes": "jp",
    })
    url = f"https://nominatim.openstreetmap.org/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "salescaper/1.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = _json.loads(resp.read())
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


def _geocode_address_candidates(address: str) -> list[str]:
    """Nominatim 用フォールバック候補（詳細→粗い順）。"""
    addr = address.strip()
    cands: list[str] = [addr, f"日本,{addr}"]
    import re as _re
    m = _re.search(r"(.+?市.+?区)", addr)
    if m:
        ward = m.group(1)
        cands.extend([f"日本,{ward}", ward])
    m2 = _re.search(r"(.+?市)", addr)
    if m2:
        city = m2.group(1)
        cands.append(f"日本,{city}")
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def geocode_address(address: str) -> tuple[float, float] | None:
    """
    住所文字列から緯度経度を取得する。
    1. 国土地理院 API（日本住所）
    2. OpenStreetMap Nominatim（フォールバック）
    """
    if not address or address in ("情報なし", "不明", ""):
        return None

    addr = address.strip()
    try:
        gsi = _geocode_gsi(addr)
        if gsi:
            logger.debug("geocode GSI成功 address=%s", addr)
            return gsi
    except Exception as e:
        logger.debug("geocode GSI失敗 address=%s: %s", addr, e)

    for candidate in _geocode_address_candidates(addr):
        try:
            result = _geocode_nominatim(candidate)
            if result:
                logger.debug("geocode Nominatim成功 q=%s", candidate)
                return result
        except Exception as e:
            logger.debug("geocode Nominatim失敗 q=%s: %s", candidate, e)
    return None


def save_geocode_to_db(csv_id: str, lat: float, lng: float) -> None:
    """ジオコード結果を nenkan.db の csv_companies にキャッシュする"""
    try:
        conn = _connect_nenkan_writable()
        conn.execute(
            "UPDATE csv_companies SET latitude=?, longitude=? WHERE csv_id=?",
            (lat, lng, csv_id)
        )
        conn.commit()
        conn.close()
        logger.info("geocode保存: csv_id=%s lat=%s lng=%s", csv_id, lat, lng)
    except Exception as e:
        logger.warning("geocodeキャッシュ保存失敗: %s", e)


def _is_url_matching(db_url: str, scraped_url: str) -> bool:
    """
    DBに登録されているURLとスクレイピングしたURLが同一ドメインか検証する。
    別会社のデータ混入を防ぐための安全チェック。

    例:
      db_url="https://www.example.co.jp/", scraped_url="https://www.example.co.jp/about" → True
      db_url="https://www.oono.co.jp/",    scraped_url="https://www.my-life.jp/"         → False
    """
    if not db_url or not scraped_url:
        return False
    try:
        from urllib.parse import urlparse as _up
        db_host      = _up(db_url).netloc.lower().lstrip("www.")
        scraped_host = _up(scraped_url).netloc.lower().lstrip("www.")
        return bool(db_host) and db_host == scraped_host
    except Exception:
        return False


def save_scrape_result_to_db(csv_id: str, scrape_result: dict) -> None:
    """スクレイピングで取得した代表者名・企業理念・AI営業ポイントをDBに保存する"""
    if not csv_id:
        return
    try:
        conn = _connect_nenkan_writable()

        # ── URL一致チェック：DB登録URLとスクレイピング先URLが異なる場合は
        #    philosophy / ai_analysis の書き込みを行わない（別会社データ混入防止）
        db_row = conn.execute(
            "SELECT website FROM csv_companies WHERE csv_id=?", (csv_id,)
        ).fetchone()
        db_url      = (db_row[0] or "") if db_row else ""
        scraped_url = (scrape_result.get("scraped_url", "") or
                       scrape_result.get("screenshot_path", "") or "")
        # scraped_url がなければ scrape_result 内の url キーも試みる
        if not scraped_url:
            scraped_url = ""
        url_ok = (not db_url) or _is_url_matching(db_url, scraped_url)

        if not url_ok:
            logger.warning(
                "URL不一致のためphilosophy/ai_analysis書き込みをスキップ: "
                "csv_id=%s db_url=%s scraped_url=%s",
                csv_id, db_url, scraped_url,
            )

        # 代表者名：スクレイピング結果が「不明」でない場合のみ上書き
        president = (scrape_result.get("代表者名", "") or "").strip()
        if president and president != "不明":
            conn.execute(
                "UPDATE csv_companies SET president=? WHERE csv_id=? AND (president IS NULL OR president='')",
                (president, csv_id)
            )
            logger.debug("president保存: csv_id=%s president=%s", csv_id, president)

        # 企業理念：URLが一致する場合のみ上書き
        if url_ok:
            philosophy = (scrape_result.get("企業理念", "") or "").strip()
            if philosophy:
                conn.execute(
                    "UPDATE csv_companies SET philosophy=? WHERE csv_id=?",
                    (philosophy, csv_id)
                )

            # AI営業ポイント（ai_analysis列）— エラー文は保存しない
            ai_summary = (scrape_result.get("ai_summary", "") or "").strip()
            if ai_summary and not _is_ai_error_message(ai_summary):
                conn.execute(
                    "UPDATE csv_companies SET ai_analysis=? WHERE csv_id=?",
                    (ai_summary, csv_id)
                )

        from domain_info_db import apply_domain_info_update

        apply_domain_info_update(conn, csv_id, scrape_result)

        conn.commit()
        conn.close()
        logger.info("スクレイピング結果DB保存: csv_id=%s", csv_id)
    except Exception as e:
        logger.warning("スクレイピング結果DB保存失敗: csv_id=%s %s", csv_id, e)


# ─────────────────────────────────────────────
# Playwright スクレイピング
# ─────────────────────────────────────────────

def _scrape_subprocess_timeout() -> int:
    """scraper.py 子プロセスの待ち時間（秒）。Ollama は応答が遅いため長め。"""
    raw = (os.environ.get("SCRAPER_TIMEOUT_SEC") or "").strip()
    if raw.isdigit():
        return max(30, int(raw))
    provider = (os.environ.get("SCRAPER_AI_PROVIDER") or "auto").strip().lower()
    if provider in ("ollama", "local", "local_llm"):
        return 180
    if provider == "auto" and (os.environ.get("LOCAL_LLM_URL") or "").strip():
        return 180
    return 90


def scrape_website(url: str, company_name: str, csv_id: str = "") -> dict:
    import subprocess, json, sys
    result = {
        "screenshot_path": None, "page_text": "", "error": None,
        "cms": "不明", "server": "不明", "responsive": False, "technologies": [],
        "代表者名": "不明", "資本金": "不明", "従業員数": "不明",
        "設立年": "不明", "事業内容": "不明", "ai_summary": "",
        "法人番号": "不明", "郵便番号": "不明", "法人種別": "不明",
        "ip_address": "不明", "hosting_company": "不明", "hosting_org": "不明",
        "whois_registrar": "不明", "whois_expiry": "不明",
        "name_servers": "不明", "ssl_issuer": "不明", "ssl_expiry": "不明",
        # スクレイピングしたURLを記録（DB保存時のURL一致検証に使用）
        "scraped_url": url,
    }
    try:
        env = os.environ.copy()
        timeout_sec = _scrape_subprocess_timeout()
        proc = subprocess.run(
            [sys.executable, "scraper.py", url, company_name,
             str(SCREENSHOT_DIR), csv_id],
            capture_output=True, timeout=timeout_sec, env=env, cwd=str(_APP_DIR),
        )
        # returncode に関わらずまず stdout の JSON パースを試みる
        # （scraper.py は例外時もJSONを返すよう修正済み）
        stdout_raw = proc.stdout
        if stdout_raw:
            try:
                result = json.loads(stdout_raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # 文字コードが壊れている場合は errors='replace' で救済
                try:
                    result = json.loads(stdout_raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError as e:
                    result["error"] = f"JSON解析失敗: {e}"
            ss = result.get("screenshot_path")
            logger.debug("scrape_website完了 name=%s screenshot_path=%s", company_name, ss)
            if result.get("error"):
                logger.warning("scraper.py エラーあり name=%s:\n%s",
                               company_name, result["error"])
        else:
            err = proc.stderr.decode("utf-8", errors="replace")
            logger.error("scraper.py 異常終了 name=%s returncode=%s stderr=%s",
                         company_name, proc.returncode, err[:200])
            result["error"] = err
    except Exception as e:
        logger.error("scrape_website 例外 name=%s: %s", company_name, e)
        result["error"] = str(e)
    return result


# ─────────────────────────────────────────────
# サイドバー：ログインUI
# ─────────────────────────────────────────────

# Supabase呼び出しをキャッシュして体感を改善（毎回ネット往復を避ける）
@st.cache_resource(show_spinner=False)
def init_db():
    _init_db_raw()
    return True


@st.cache_data(ttl=300, show_spinner=False)
def get_all_users() -> list[str]:
    return _get_all_users_raw()


def _apply_saved_search_session(session_id: int) -> None:
    """保存済み検索を session_state に復元（Streamlit on_click コールバック用）。"""
    session_data = load_search_session(session_id)
    if not session_data:
        st.session_state["_open_session_error"] = (
            f"検索結果を読み込めませんでした（id={session_id}）。"
            "再保存するか、管理者に連絡してください。"
        )
        return
    _sd = dict(session_data) if hasattr(session_data, "keys") else session_data
    _df = _sd.get("df")
    if _df is None or getattr(_df, "empty", True):
        st.session_state["_open_session_error"] = (
            "保存されている企業リストが空です。"
            "検索し直してから「名前を付けて保存」し直してください。"
        )
        return
    st.session_state["df"] = _df
    st.session_state["region"] = _sd.get("region", "")
    st.session_state["industry"] = _sd.get("industry", "")
    st.session_state["enable_scraping"] = bool(_sd.get("enable_scraping", False))
    st.session_state["current_page"] = 0
    st.session_state["loaded_session"] = _sd.get("session_name", "")
    st.session_state["page"] = "search"
    for idx in _df.index:
        st.session_state[f"check_{idx}"] = False
    st.session_state.pop("_open_session_error", None)


def _delete_saved_search_session(session_id: int) -> None:
    delete_search_session(session_id)


def render_login_sidebar() -> None:
    with st.sidebar:
        st.markdown("## 👤 ユーザー")
        current_user = get_current_user()

        # ── DB接続エラー時のみ警告表示（正常時は何も出さない）──────
        if not (_csv_db and _csv_db.available):
            _db_warn = "⚠️ 企業DBに接続できません。"
            if _DB_CONNECT_NOTE:
                _db_warn += f"\n\n{_DB_CONNECT_NOTE}"
            else:
                _db_warn += "\n\nnenkan.db の場所を確認するか、管理者に連絡してください。"
            st.warning(_db_warn)
        elif _DB_CONNECT_NOTE:
            st.info(_DB_CONNECT_NOTE)

        if current_user:
            st.success(f"ログイン中：**{current_user}**")
            if st.button("🚪 ログアウト", use_container_width=True):
                logout_user()
                # ログアウト時はクエリパラメータも削除
                st.query_params.clear()
                st.rerun()
        else:
            st.markdown("ユーザーを選ぶか、新規名を入力してください。")
            with st.spinner("ユーザー一覧を取得中…"):
                existing_users, users_err = _get_all_users_with_status()
            if users_err:
                st.warning(users_err)
            elif existing_users:
                selected = st.selectbox(
                    "ユーザーを選択",
                    options=existing_users,
                    label_visibility="collapsed",
                )
                if st.button("ログイン", use_container_width=True, type="primary"):
                    st.session_state["current_user"] = selected
                    st.query_params["u"] = selected
                    st.rerun()
            else:
                st.caption("登録ユーザーがまだありません。下で新規作成してください。")
            with st.expander("新規ユーザー", expanded=not existing_users and not users_err):
                new_name = st.text_input(
                    "新しいユーザー名",
                    placeholder="例：yamada",
                    label_visibility="collapsed",
                )
                if st.button("新規としてログイン", use_container_width=True):
                    if new_name.strip():
                        login_user(new_name.strip())
                        get_all_users.clear()
                        st.query_params["u"] = new_name.strip()
                        st.rerun()
                    else:
                        st.warning("ユーザー名を入力してください。")

        if current_user:
            # ── 検索セッション一覧（ログイン後のみ）────────────────────────
            st.divider()

            if st.button("➕ 企業を新規登録", use_container_width=True):
                st.session_state["page"] = "register"
                st.rerun()
            if st.button("📝 担当者メモ一覧", use_container_width=True):
                st.session_state["page"] = "memo"
                st.rerun()
            if st.session_state.get("page") in ("memo", "register"):
                if st.button("🔍 検索に戻る", use_container_width=True):
                    st.session_state["page"] = "search"
                    st.rerun()

            st.divider()
            st.markdown("## 📂 保存済み検索")

            current_page = st.session_state.get("page", "search")
            if current_page in ("memo", "register"):
                st.caption("※ 検索画面に戻ると保存済み検索を開けます。")
            else:
                _sess_user = get_current_user() or None
                sessions = list_search_sessions(_sess_user)
                if not sessions:
                    st.caption("保存された検索はありません。")
                else:
                    for s in sessions:
                        if hasattr(s, "keys"):
                            s = dict(s)
                        s_id       = s.get("id")
                        if s_id is None:
                            s_id = s.get("session_id")
                        if s_id is None:
                            continue
                        s_id = int(s_id)
                        s_name     = s.get("session_name", "（名称不明）")
                        s_date_raw = s.get("created_at") or s.get("saved_at", "")
                        s_date     = str(s_date_raw)[:10] if s_date_raw else ""
                        s_user     = s.get("username") or s.get("user") or s.get("user_name", "")
                        s_region   = s.get("region", "")
                        s_industry = s.get("industry", "")
                        scraping   = "🌐" if s.get("enable_scraping") else ""
                        _cond_parts = [p for p in (s_region, s_industry) if p]
                        _cond = "・".join(_cond_parts) if _cond_parts else "業種のみ等"

                        label   = f"{scraping} {s_name}"
                        caption = f"{_cond}　{s_date}　{s_user}"

                        col_btn, col_del = st.columns([4, 1])
                        with col_btn:
                            st.markdown(f"**{label}**")
                            st.caption(caption)
                            st.button(
                                "▶ 開く",
                                key=f"open_session_{s_id}",
                                use_container_width=True,
                                on_click=_apply_saved_search_session,
                                args=(s_id,),
                            )
                        with col_del:
                            st.button(
                                "🗑️",
                                key=f"del_session_{s_id}",
                                help="この検索を削除",
                                on_click=_delete_saved_search_session,
                                args=(s_id,),
                            )
                        st.markdown("---")


def _normalize_website_url(url: str) -> str:
    u = (url or "").strip()
    if not u or u in ("情報なし", "不明"):
        return ""
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def _enrich_manual_company(row: dict, website: str) -> tuple[str, list[str]]:
    """新規登録後にAI・外部DBで追加情報を収集してDBを更新。"""
    csv_id = str(row.get("csv_id") or "")
    name = str(row.get("name") or "")
    address = str(row.get("address") or "")
    db_path = _nenkan_db_file()
    collected: list[str] = []
    warnings: list[str] = []

    if address and csv_id:
        geo = geocode_address(address)
        if geo:
            save_geocode_to_db(csv_id, geo[0], geo[1])
            collected.append("地図座標")
        else:
            warnings.append("地図座標を取得できませんでした")

    try:
        from scraper import search_houjin_db
        hj = search_houjin_db(name)
        hj_applied = apply_houjin_fields(db_path, csv_id, hj)
        if hj_applied:
            collected.extend(hj_applied)
    except Exception as e:
        logger.warning("houjin補完失敗 name=%s: %s", name, e)

    url = _normalize_website_url(website or row.get("website") or "")
    if url and csv_id:
        try:
            logger.info("手動登録後スクレイピング開始: %s %s", name, url)
            result = scrape_website(url, name, csv_id=csv_id)
            result["scraped_url"] = url
            if result.get("error"):
                warnings.append(f"Web取得: {result.get('error')}")
            save_scrape_result_to_db(csv_id, result)
            scraped = apply_manual_scrape_fields(db_path, csv_id, result)
            collected.extend(scraped)
            try:
                set_scrape_cache(url, name, result)
            except Exception:
                pass
            if result.get("screenshot_path") or result.get("ai_summary"):
                if "Webサイト分析" not in collected:
                    collected.append("Webサイト分析")
            if not scraped and not result.get("ai_summary"):
                warnings.append("Webサイトから追加項目を取得できませんでした")
        except Exception as e:
            logger.warning("手動登録後スクレイピング失敗 name=%s: %s", name, e)
            warnings.append(f"Web分析失敗: {e}")
    elif not url:
        warnings.append("URL未入力のためWebサイト分析はスキップしました")

    seen: set[str] = set()
    uniq = []
    for item in collected:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    summary = "、".join(uniq) if uniq else ""
    return summary, warnings


def _reg_badge_html(required: bool) -> str:
    if required:
        return (
            '<span style="display:inline-block;background:#E53935;color:#fff;'
            'font-size:11px;font-weight:700;line-height:1.4;padding:2px 8px;'
            'border-radius:3px;margin-right:8px;vertical-align:middle;">必須</span>'
        )
    return (
        '<span style="display:inline-block;background:#9CA3AF;color:#fff;'
        'font-size:11px;font-weight:700;line-height:1.4;padding:2px 8px;'
        'border-radius:3px;margin-right:8px;vertical-align:middle;">任意</span>'
    )


def _reg_field_label(label: str, *, required: bool = True) -> None:
    st.markdown(
        f'{_reg_badge_html(required)}'
        f'<span style="font-weight:700;font-size:15px;color:#111827;">{label}</span>',
        unsafe_allow_html=True,
    )


def _reg_form_styles() -> None:
    st.markdown(
        """
        <style>
        [data-testid="stForm"] input[type="text"] {
            border-radius: 8px !important;
            margin-bottom: 0.25rem !important;
        }
        [data-testid="stForm"] [data-testid="stMarkdownContainer"] p {
            margin-bottom: 0.35rem !important;
        }
        [data-testid="stForm"] button[kind="primaryFormSubmit"] {
            width: auto !important;
            min-width: 120px !important;
            max-width: 200px !important;
            padding: 0.4rem 1.6rem !important;
            border-radius: 999px !important;
            background-color: #E53935 !important;
            border-color: #E53935 !important;
            font-weight: 600 !important;
        }
        [data-testid="stForm"] button[kind="primaryFormSubmit"]:hover {
            background-color: #C62828 !important;
            border-color: #C62828 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────
# 企業新規登録ページ
# ─────────────────────────────────────────────

def render_manual_register() -> None:
    st.subheader("➕ 企業を新規登録")
    st.caption(
        "年鑑DBにない企業を手動で登録します。"
        "登録後は通常の検索結果と同様に利用できます。"
    )

    db_path = _nenkan_db_file()
    if _DB_CONNECT_NOTE:
        st.info(_DB_CONNECT_NOTE)

    _reg_form_styles()

    with st.form("manual_company_form"):
        _reg_field_label("企業名", required=True)
        st.text_input(
            "企業名", placeholder="例) 株式会社サンプル",
            key="reg_name", label_visibility="collapsed",
        )
        _reg_field_label("住所", required=True)
        st.text_input(
            "住所", placeholder="例) 岡山市北区駅元町1-1",
            key="reg_address", label_visibility="collapsed",
        )
        _reg_field_label("電話番号", required=True)
        st.text_input(
            "電話番号", placeholder="例) 086-123-4567",
            key="reg_tel", label_visibility="collapsed",
        )
        _reg_field_label("WebサイトURL", required=False)
        st.text_input(
            "WebサイトURL", placeholder="例) https://example.co.jp",
            key="reg_website", label_visibility="collapsed",
        )
        auto_collect = st.checkbox(
            "登録後にAIで追加情報を自動収集する",
            value=True,
            key="reg_auto_collect",
            help="検索画面の「Webサイト取得・技術スタック判定」と同じ処理です。"
            "URLがある場合、代表者・技術スタック・スクショなどを取得します（数分かかることがあります）。",
        )
        _btn_l, _btn_c, _btn_r = st.columns([3, 2, 3])
        with _btn_c:
            submitted = st.form_submit_button("登録する", type="primary")

    if not submitted:
        return

    name = st.session_state.get("reg_name", "")
    address = st.session_state.get("reg_address", "")
    tel = st.session_state.get("reg_tel", "")
    website = st.session_state.get("reg_website", "")
    auto_collect = st.session_state.get("reg_auto_collect", True)

    try:
        row = insert_manual_company(db_path, name, address, tel, website)
        registered_name = row.get("name") or name.strip()
        if auto_collect:
            with st.spinner("AIが追加情報を収集中…（1〜2分かかることがあります）"):
                summary, enrich_warns = _enrich_manual_company(row, website)
            st.session_state["_reg_enrich_msg"] = summary
            st.session_state["_reg_enrich_warns"] = enrich_warns
        st.session_state["page"] = "search"
        st.session_state["free_word"] = registered_name
        st.session_state["_pending_search"] = {
            "region": "",
            "industry": "",
            "industry_select": "すべて",
            "free_word": registered_name,
            "enable_scraping": auto_collect,
        }
        st.rerun()
    except ManualCompanyError as e:
        st.error(str(e))


# ─────────────────────────────────────────────
# 担当者メモ一覧ページ
# ─────────────────────────────────────────────

def render_memo_list() -> None:
    st.subheader("📝 担当者メモ一覧")

    saved_df = load_saved_companies()
    if saved_df.empty:
        st.info("担当企業が登録されていません。")
        return

    # メモ・担当者・ステータスのいずれかが入っているものに絞る
    memo_df = saved_df[
        saved_df["メモ"].fillna("").str.strip().ne("") |
        saved_df["担当者"].fillna("").str.strip().ne("") |
        saved_df["ステータス"].fillna("").ne("未着手")
    ].copy()

    # ── フィルター ──
    col_f1, col_f2, col_f3 = st.columns([2, 2, 2])
    with col_f1:
        filter_status = st.selectbox(
            "ステータス",
            ["すべて"] + CRM_STATUSES,
            key="memo_filter_status",
        )
    with col_f2:
        filter_assignee = st.text_input(
            "担当者",
            placeholder="名前を入力…",
            key="memo_filter_assignee",
        )
    with col_f3:
        show_all = st.checkbox("メモなしも含めて全件表示", key="memo_show_all")

    if show_all:
        memo_df = saved_df.copy()

    if filter_status != "すべて":
        memo_df = memo_df[memo_df["ステータス"] == filter_status]
    if filter_assignee.strip():
        memo_df = memo_df[
            memo_df["担当者"].str.contains(filter_assignee.strip(), na=False)
        ]

    st.caption(f"表示件数: {len(memo_df)} 件")

    if memo_df.empty:
        st.info("該当する件数がありません。")
        return

    # ── テーブル表示 ──
    for _, row in memo_df.iterrows():
        record_id  = int(row["id"])
        company    = str(row.get("社名", ""))
        status     = str(row.get("ステータス", "未着手") or "未着手")
        assignee   = str(row.get("担当者", "") or "")
        memo       = str(row.get("メモ", "") or "")
        saved_at   = str(row.get("保存日時", ""))
        web_url    = str(row.get("WebサイトURL", "") or "")
        crm_color, crm_bg = STATUS_COLORS.get(status, ("#888888", "#F0F0F0"))

        status_html = (
            f'<span style="background:{crm_bg};color:{crm_color};'
            f'font-weight:600;padding:2px 10px;border-radius:20px;'
            f'font-size:0.8em;">{status}</span>'
        )

        # カード形式で1件表示
        st.markdown(
            f'<div style="border:0.5px solid #E5E7EB;border-radius:8px;'
            f'padding:12px 16px;margin-bottom:8px;background:#FAFAFA;">'
            f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">'
            f'<span style="font-weight:600;font-size:1.0em;">🏢 {company}</span>'
            f'{status_html}'
            f'{"　👤 " + assignee if assignee else ""}'
            f'<span style="margin-left:auto;font-size:0.8em;color:#9CA3AF;">{saved_at[:10]}</span>'
            f'</div>'
            + (f'<div style="font-size:0.9em;color:#374151;white-space:pre-wrap;">{memo}</div>' if memo else
               '<div style="font-size:0.85em;color:#9CA3AF;">メモなし</div>')
            + '</div>',
            unsafe_allow_html=True,
        )

        # インライン編集
        with st.expander("✏️ 編集", expanded=False):
            _crm_upd = str(row.get("CRM更新日時", "") or "")
            _status_k, _assign_k, _memo_k = _sync_memo_list_widgets(
                record_id, memo, status, assignee, _crm_upd,
            )
            with st.form(key=f"memo_crm_form_{record_id}", clear_on_submit=False):
                ec1, ec2, ec3 = st.columns([2, 2, 3])
                with ec1:
                    st.selectbox(
                        "ステータス", CRM_STATUSES,
                        key=_status_k,
                    )
                with ec2:
                    st.text_input(
                        "担当者",
                        placeholder="例：山田",
                        key=_assign_k,
                    )
                with ec3:
                    st.text_area(
                        "メモ",
                        placeholder="例：来週折り返し待ち",
                        key=_memo_k,
                        height=80,
                    )
                _memo_submitted = st.form_submit_button(
                    "💾 更新", type="primary", use_container_width=True,
                )
            if _memo_submitted:
                _ok, _msg = update_crm(
                    record_id,
                    st.session_state[_status_k],
                    st.session_state[_assign_k],
                    st.session_state[_memo_k],
                )
                if _ok:
                    _invalidate_saved_crm_widgets(record_id)
                    st.success(f"✅ {_msg}")
                    st.rerun()
                else:
                    st.error(_msg)


# ─────────────────────────────────────────────
# 担当企業リスト表示（CRM対応）
# ─────────────────────────────────────────────

def _is_ai_error_message(text: str) -> bool:
    """AI寸評としてDBに誤保存されたエラー文か。"""
    s = (text or "").strip()
    if not s:
        return False
    markers = (
        "Gemini 抽出エラー", "Gemini APIキーエラー", "AI抽出エラー",
        "APIキーエラー", "JSONパースエラー", "pip install",
        "NOT_FOUND", "models/gemini",
    )
    return any(m in s for m in markers)


def _calc_elapsed(founded_raw: str):
    """設立日文字列から経過年数を計算（毎年設立記念日を過ぎると自動で増加）"""
    import re as _re
    from datetime import datetime as _dt
    if not founded_raw or founded_raw in ("不明", ""):
        return None
    _now = _dt.now()
    m_full = _re.search(r"(\d{4})[年./\-](\d{1,2})[月./\-](\d{1,2})", founded_raw)
    m_year = _re.search(r"(\d{4})", founded_raw)
    if m_full:
        fy, fm, fd = int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3))
        try:
            anniversary = _dt(_now.year, fm, fd)
            return _now.year - fy - (1 if _now < anniversary else 0)
        except ValueError:
            return _now.year - fy
    elif m_year:
        return _now.year - int(m_year.group(1))
    return None


def render_saved_list() -> None:
    st.divider()
    saved_count = count_saved()
    st.subheader(f"📋 担当企業リスト（{saved_count} 件）")
    st.caption("チームで共有するフォロー対象企業。担当者・メモ・ステータスを管理します。")

    if saved_count == 0:
        st.info("まだ担当企業が登録されていません。")
        return

    # ── フィルター ────────────────────────────
    col_f1, col_f2, _ = st.columns([2, 2, 4])
    with col_f1:
        filter_status = st.selectbox(
            "ステータスで絞り込み",
            options=["すべて"] + CRM_STATUSES,
            key="filter_status",
        )
    with col_f2:
        filter_assignee = st.text_input(
            "担当者で絞り込み",
            placeholder="名前を入力…",
            key="filter_assignee",
        )

    # ── フィルター後の件数をDBから取得（全件ロード不要）──
    filtered_count = count_saved_filtered(filter_status, filter_assignee)
    total_pages    = max(1, (filtered_count + PAGE_SIZE - 1) // PAGE_SIZE)

    # ── ページ番号をセッションで管理 ──────────
    # フィルターが変わったらページを0に戻す
    _filter_sig = f"{filter_status}|{filter_assignee}"
    if st.session_state.get("_saved_filter_sig") != _filter_sig:
        st.session_state["_saved_filter_sig"] = _filter_sig
        st.session_state["saved_page"] = 0

    current_page = st.session_state.get("saved_page", 0)
    current_page = max(0, min(current_page, total_pages - 1))

    # ── このページ分だけDBからロード ──────────
    display_df = load_saved_companies_paged(
        page=current_page,
        page_size=PAGE_SIZE,
        status=filter_status,
        assignee=filter_assignee,
    )

    # ── CSVエクスポート（全件対象）───────────
    export_df_all = load_saved_companies_paged(
        page=0, page_size=10000,
        status=filter_status, assignee=filter_assignee,
    ).drop(columns=["id", "ユーザー", "スクリーンショット"], errors="ignore")
    saved_csv = export_df_all.to_csv(index=False).encode("shift_jis", errors="ignore")
    st.download_button(
        label="📥 担当企業リストをCSVでエクスポート（フィルター適用）",
        data=saved_csv,
        file_name="saved_companies.csv",
        mime="text/csv",
        use_container_width=True,
    )
    st.caption(f"表示件数: {filtered_count} 件　（{current_page + 1} / {total_pages} ページ）")

    # ── まとめて削除エリア ────────────────────
    # チェックボックスのキー "bulk_check_{id}" を直接 session_state で管理する。
    # bulk_checked_ids セットは廃止し、session_state["bulk_check_{id}"] を
    # 唯一の真実のソースとして使う。

    # 現在ページの全 id リスト（全選択・全解除・削除対象の特定に使う）
    _page_ids = display_df["id"].astype(int).tolist() if not display_df.empty else []

    col_sel_all, col_sel_none, col_bulk_del, _ = st.columns([1, 1, 2, 4])
    with col_sel_all:
        if st.button("☑ 全選択", key="bulk_all"):
            # フィルター条件に合う全 id の st.session_state["bulk_check_{id}"] を True に
            for _fid in load_all_ids_filtered(filter_status, filter_assignee):
                st.session_state[f"bulk_check_{_fid}"] = True
            st.rerun()
    with col_sel_none:
        if st.button("☐ 全解除", key="bulk_none"):
            # bulk_check_ プレフィックスのキーをすべて False に
            for _k in list(st.session_state.keys()):
                if _k.startswith("bulk_check_"):
                    st.session_state[_k] = False
            st.rerun()
    with col_bulk_del:
        # bulk_check_ プレフィックスのキーから全チェック済み id を収集
        # （_page_ids に限定せず session_state 全体から取る）
        _checked_ids = [
            int(_k.replace("bulk_check_", ""))
            for _k in st.session_state
            if _k.startswith("bulk_check_") and st.session_state[_k]
        ]
        _checked_count = len(_checked_ids)
        confirm_bulk_key = "confirm_bulk_delete"

        if _checked_count == 0 and not st.session_state.get(confirm_bulk_key):
            st.button("🗑️ まとめて削除（チェックして選択）",
                      disabled=True, use_container_width=True,
                      key="bulk_del_disabled")
        elif st.session_state.get(confirm_bulk_key):
            # 確認ダイアログ表示中は保存済みの id リストを使う
            _confirmed_ids = st.session_state.get("bulk_confirm_ids", _checked_ids)
            st.warning(f"{len(_confirmed_ids)} 件を削除します。よろしいですか？")
            cy, cn = st.columns(2)
            with cy:
                if st.button("✅ はい", key="bulk_yes", use_container_width=True):
                    delete_companies(_confirmed_ids)
                    for _cid in _confirmed_ids:
                        st.session_state.pop(f"bulk_check_{_cid}", None)
                    st.session_state.pop(confirm_bulk_key, None)
                    st.session_state.pop("bulk_confirm_ids", None)
                    st.session_state["saved_page"] = 0
                    st.rerun()
            with cn:
                if st.button("❌ いいえ", key="bulk_no", use_container_width=True):
                    st.session_state.pop(confirm_bulk_key, None)
                    st.session_state.pop("bulk_confirm_ids", None)
                    st.rerun()
        else:
            if st.button(f"🗑️ まとめて削除（{_checked_count} 件選択中）",
                         use_container_width=True, key="bulk_del_active"):
                # ボタンを押した瞬間の id リストをセッションに保存してから rerun
                st.session_state["bulk_confirm_ids"] = _checked_ids
                st.session_state[confirm_bulk_key] = True
                st.rerun()

    # ── ページネーションUI ──────────────────
    if total_pages > 1:
        st.divider()
        pg_cols = st.columns([1, 2, 1])
        with pg_cols[0]:
            if current_page > 0:
                if st.button("◀ 前のページ", use_container_width=True, key="pg_prev"):
                    st.session_state["saved_page"] = current_page - 1
                    st.rerun()
        with pg_cols[1]:
            st.markdown(
                f'<div style="text-align:center;font-size:14px;padding-top:6px;">'
                f'{current_page + 1} / {total_pages} ページ</div>',
                unsafe_allow_html=True,
            )
        with pg_cols[2]:
            if current_page < total_pages - 1:
                if st.button("次のページ ▶", use_container_width=True, key="pg_next"):
                    st.session_state["saved_page"] = current_page + 1
                    st.rerun()

    st.divider()

    # ── 各社カード ────────────────────────────
    for _, row in display_df.iterrows():
        row = _enrich_saved_row_from_nenkan(row)
        record_id = int(row["id"])
        status    = row.get("ステータス", "未着手") or "未着手"

        def safe_score(val) -> str:
            if val is None or val == "":
                return "0"
            try:
                return str(int(val))
            except (ValueError, TypeError):
                return "0"

        web_score = safe_score(row.get("Web提案スコア"))
        web_label = row.get("Web提案優先度", "") or ""

        # チェックボックス
        col_ck, col_exp = st.columns([0.3, 9.7])
        with col_ck:
            st.checkbox(
                "", key=f"bulk_check_{record_id}",
                label_visibility="collapsed",
            )
        with col_exp:
            # ── ヘッダー項目を検索結果と統一 ──
            _sv_founded_h = str(row.get("設立年", "") or "").strip()
            _sv_elapsed_h = _calc_elapsed(_sv_founded_h)
            _sv_years_h   = f"{_sv_elapsed_h}年" if _sv_elapsed_h is not None and _sv_elapsed_h >= 0 else ""
            _sv_capital_h = str(row.get("資本金", "") or "").strip()
            _sv_emp_h     = str(row.get("従業員数", "") or "").strip()
            _sv_assign_h  = str(row.get("担当者", "") or "").strip()
            _sv_wp_list_h, _ = calc_weakpoints(row)
            _WP_SHORT_SV = {
                "hp_none":   "HPなし",
                "non_ssl":   "SSL不備",
                "non_resp":  "スマホ未対応",
                "no_career": "採用ページなし",
                "old_server":"旧サーバー",
            }
            _sv_wp_labels = [_WP_SHORT_SV.get(k, k) for k, *_ in _sv_wp_list_h]
            _sv_wp_header = "　".join(_sv_wp_labels) if _sv_wp_labels else ""

            _sv_label_parts = [f"📋 {row['社名']}"]
            if _sv_founded_h and _sv_founded_h != "不明":
                _sv_f_disp = _sv_founded_h
                if _sv_years_h:
                    _sv_f_disp += f"（{_sv_years_h}）"
                _sv_label_parts.append(_sv_f_disp)
            if _sv_capital_h and _sv_capital_h != "不明":
                _sv_label_parts.append(f"資本金 {_sv_capital_h}")
            if _sv_emp_h and _sv_emp_h not in ("不明", "0"):
                _sv_label_parts.append(_sv_emp_h)
            if _sv_wp_header:
                _sv_label_parts.append(f"⚠ {_sv_wp_header}")
            if _sv_assign_h:
                _sv_label_parts.append(f"担当：{_sv_assign_h}")
            expander_label = "　｜　".join(_sv_label_parts)
            with st.expander(expander_label, expanded=False):

                # ── 3カラム構成（検索結果カードと同一）─────────────
                col_left, col_mid, col_right = st.columns([0.7, 1.1, 1.1], gap="large")

                # ── 左カラム：基本情報 ──────────────────────────
                with col_left:
                    st.caption(f"保存日時: {row['保存日時']}　保存者: {row.get('ユーザー', '')}")

                    st.markdown("**代表者**")
                    st.markdown(str(row.get("代表者名", "不明") or "不明"))

                    st.markdown("**住所**")
                    st.markdown(str(row.get("住所", "情報なし") or "情報なし"))

                    st.markdown("**TEL**")
                    st.markdown(str(row.get("TEL", "情報なし") or "情報なし"))

                    st.markdown("**URL**")
                    _sv_url = str(row.get("WebサイトURL", "") or "").strip()
                    if _sv_url and _sv_url not in ("情報なし", "不明"):
                        st.markdown(f"[{_sv_url}]({_sv_url})")
                    else:
                        st.markdown("情報なし")

                    _sv_biz = str(row.get("事業内容", "不明") or "不明")
                    if _sv_biz and _sv_biz != "不明":
                        st.markdown("**主業務**")
                        st.markdown(_sv_biz)

                    if row.get("業種") and row["業種"] != "不明":
                        st.markdown(f"**業種：** {row['業種']}")

                    _sv_emp = str(row.get("従業員数", "") or "").strip()
                    if _sv_emp and _sv_emp not in ("不明", "0"):
                        import re as _re_sv
                        _sv_emp = _re_sv.sub(r"^【[^】]*】", "", _sv_emp).strip()
                        st.markdown(f"**社員数：** {_sv_emp}")

                    st.markdown("**設立日**")
                    _sv_founded = str(row.get("設立年", "") or "").strip()
                    import re as _re_sv2
                    if _sv_founded and _sv_founded != "不明":
                        _sv_fc = _re_sv2.sub("^【[^】]*】\\s*", "", _sv_founded).strip()
                        _sv_fc = _re_sv2.sub("[（(]創(\\d{4}年\\d{1,2}月)[）)]", "（創業 \\1）", _sv_fc)
                        from datetime import datetime as _dt
                        _sv_year_m = _re_sv2.search(r"(\d{4})", _sv_fc)
                        _sv_years = ""
                        if _sv_year_m:
                            try:
                                _sv_elapsed = _dt.now().year - int(_sv_year_m.group(1))
                                if _sv_elapsed >= 0:
                                    _sv_years = f'<span style="color:#DC2626;font-weight:700;">{_sv_elapsed}年</span>'
                            except Exception:
                                pass
                        st.markdown(f"{_sv_fc}　{_sv_years}", unsafe_allow_html=True)
                    else:
                        st.markdown("不明")

                # ── 中央カラム：財務・Web・ウィークポイント ────────
                with col_mid:
                    _sv_cap = str(row.get("資本金", "") or "").strip()
                    st.markdown("**資本金**")
                    st.markdown(_sv_cap if _sv_cap and _sv_cap != "不明" else "不明")

                    _sv_sales_rows = parse_sales_history(row.get("csv_売上推移", "") or "")
                    if _sv_sales_rows:
                        _sv_has_profit = any(r["profit_man"] is not None for r in _sv_sales_rows)
                        _sv_has_pure   = any(r.get("pure_profit") is not None for r in _sv_sales_rows)
                        _sv_max_s = max(r["sales_man"] for r in _sv_sales_rows) or 1
                        _sv_hdr = (
                            '<div style="display:flex;align-items:center;gap:6px;'
                            'margin-bottom:3px;padding-bottom:3px;'
                            'border-bottom:0.5px solid var(--color-border-tertiary);">'
                            '<span style="font-size:12px;color:var(--color-text-secondary);width:48px;flex-shrink:0;"></span>'
                            '<span style="flex:1;font-size:12px;color:var(--color-text-secondary);">売上</span>'
                        )
                        if _sv_has_profit:
                            _sv_hdr += '<span style="font-size:12px;color:var(--color-text-secondary);width:80px;text-align:right;flex-shrink:0;">経常利益</span>'
                        if _sv_has_pure:
                            _sv_hdr += '<span style="font-size:12px;color:var(--color-text-secondary);width:80px;text-align:right;flex-shrink:0;">純利益</span>'
                        _sv_hdr += '</div>'
                        _sv_body = ""
                        for _sr in _sv_sales_rows:
                            _sv_pct  = int(_sr["sales_man"] / _sv_max_s * 100)
                            _sv_slbl = fmt_man(_sr["sales_man"])
                            _sv_pval = _sr.get("profit_man")
                            _sv_ppv  = _sr.get("pure_profit")
                            _sv_plbl = fmt_man(_sv_pval) if _sv_pval is not None else "－"
                            _sv_pplb = fmt_man(_sv_ppv)  if _sv_ppv  is not None else "－"
                            _sv_pc   = "color:#A32D2D;" if (_sv_pval is not None and _sv_pval < 0) else ""
                            _sv_ppc  = "color:#A32D2D;" if (_sv_ppv  is not None and _sv_ppv  < 0) else ""
                            _sv_body += (
                                f'<div style="display:flex;align-items:center;gap:6px;margin-bottom:5px;">'
                                f'<span style="font-size:13px;color:var(--color-text-secondary);width:48px;flex-shrink:0;">{_sr["period"]}</span>'
                                f'<div style="flex:1;display:flex;align-items:center;gap:4px;">'
                                f'<div style="flex:1;height:10px;background:var(--color-background-secondary);border-radius:4px;overflow:hidden;">'
                                f'<div style="width:{_sv_pct}%;height:100%;background:#378ADD;border-radius:4px;"></div></div>'
                                f'<span style="font-size:13px;color:var(--color-text-primary);width:76px;text-align:right;flex-shrink:0;">{_sv_slbl}</span>'
                                f'</div>'
                            )
                            if _sv_has_profit:
                                _sv_body += f'<span style="font-size:13px;width:80px;text-align:right;flex-shrink:0;{_sv_pc}">{_sv_plbl}</span>'
                            if _sv_has_pure:
                                _sv_body += f'<span style="font-size:13px;width:80px;text-align:right;flex-shrink:0;{_sv_ppc}">{_sv_pplb}</span>'
                            _sv_body += '</div>'
                        st.markdown("**売上推移**")
                        st.markdown(_sv_hdr + _sv_body, unsafe_allow_html=True)
                    else:
                        _sv_sales_val = str(row.get("csv_売上高", "") or "").strip()
                        if _sv_sales_val and _sv_sales_val not in ("0", "0.0", "nan"):
                            st.markdown("**直近売上**")
                            try:
                                _sv_rv = float(_sv_sales_val)
                                st.markdown(fmt_man(_sv_rv / 10000) if _sv_rv >= 1e8 else
                                            (f"{_sv_rv/1e4:.0f}万円" if _sv_rv >= 1e4 else f"{_sv_rv:,.0f}円"))
                            except (ValueError, TypeError):
                                st.markdown(_sv_sales_val)

                    _sv_ai      = str(row.get("AI営業ポイント", "") or "").strip()
                    if _is_ai_error_message(_sv_ai):
                        _sv_ai = ""
                    _sv_raw_sum = str(row.get("csv_概要", "") or "").strip()
                    import re as _re_sv3
                    _sv_summary = _re_sv3.sub(r"^【[^】]*】\s*", "", _sv_raw_sum).strip()
                    _sv_comment = _sv_ai or _sv_summary
                    if _sv_comment:
                        st.markdown("**寸評**")
                        st.info(f"💡 {_sv_comment}")

                    _sv_cms_w   = str(row.get("CMS", "") or "")
                    _sv_tech_w  = str(row.get("技術スタック", "") or "")
                    _sv_resp_w  = _resolve_responsive(row, scrape_val=row.get("レスポンシブ"))
                    _sv_host_w  = str(row.get("hosting_company", "") or "")
                    _sv_ssl_i_w = str(row.get("ssl_issuer", "") or "")
                    _sv_ssl_e_w = str(row.get("ssl_expiry", "") or "")
                    _sv_whois_w = str(row.get("whois_registrar", "") or "")
                    _sv_whe_w   = str(row.get("whois_expiry", "") or "")
                    with st.expander("🌐 Web情報"):
                        def _sv_wval(v):
                            if not v or v in ("不明", "取得不可"):
                                return "<span style='color:var(--color-text-secondary);'>-</span>"
                            return v
                        _sv_wrows = [
                            ("CMS",        _sv_cms_w  or "不明"),
                            ("技術",        _sv_tech_w or "不明"),
                            ("レスポンシブ", "✅ 対応" if _sv_resp_w else "❌ 非対応"),
                            ("ホスティング", _sv_host_w or "不明"),
                            ("SSL発行者",   _sv_ssl_i_w or "不明"),
                            ("SSL有効期限", _sv_ssl_e_w or "不明"),
                            ("ドメイン登録", _sv_whois_w or "不明"),
                            ("ドメイン期限", _sv_whe_w or "不明"),
                        ]
                        _sv_whtml = '<div style="font-size:16px;line-height:1.8;">'
                        for _wk, _wv in _sv_wrows:
                            _wc = "color:#A32D2D;" if (_wk == "レスポンシブ" and not _sv_resp_w) else (
                                  "color:#0F6E56;" if (_wk == "レスポンシブ" and _sv_resp_w) else "")
                            _wvh = _wv if _wk == "レスポンシブ" else _sv_wval(_wv)
                            _sv_whtml += (
                                f'<div style="display:flex;justify-content:space-between;'
                                f'align-items:baseline;padding:5px 0;'
                                f'border-bottom:0.5px solid var(--color-border-tertiary);">'
                                f'<span style="color:var(--color-text-secondary);">{_wk}</span>'
                                f'<span style="{_wc}">{_wvh}</span></div>'
                            )
                        _sv_whtml += '</div>'
                        st.markdown(_sv_whtml, unsafe_allow_html=True)

                    _sv_wp_list, _sv_star = calc_weakpoints(row)
                    _sv_wp_bg = (
                        "#FFF0F0" if _sv_star >= 4 else
                        "#FFF8F0" if _sv_star >= 2 else
                        "#FFFFF0" if _sv_star >= 1 else "#F8F8F8"
                    )
                    _sv_wp_bd = (
                        "#FECACA" if _sv_star >= 4 else
                        "#FED7AA" if _sv_star >= 2 else
                        "#FEF08A" if _sv_star >= 1 else "#E5E7EB"
                    )
                    st.markdown(
                        f'<div style="background:{_sv_wp_bg};border:1px solid {_sv_wp_bd};'
                        f'border-radius:8px;padding:10px 12px;margin-bottom:6px;">'
                        f'<div style="font-weight:700;font-size:14px;margin-bottom:4px;">'
                        f'⚠️ ウィークポイント　{star_label(_sv_star)}'
                        f'</div></div>',
                        unsafe_allow_html=True,
                    )
                    if _sv_wp_list:
                        _sv_wph = '<div style="margin-top:4px;">'
                        for _wkey, _wlabel, _wcolor, _wtalk in _sv_wp_list:
                            _sv_dl = _wlabel
                            if _wkey == "old_server" and _sv_host_w and _sv_host_w not in ("不明", "取得不可", ""):
                                _sv_dl = f"{_wlabel}（{_sv_host_w}）"
                            _sv_wph += (
                                f'<div style="padding:8px 0;border-bottom:0.5px solid var(--color-border-tertiary);">'
                                f'<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">'
                                f'<span style="font-size:13px;font-weight:700;color:{_wcolor};">⚠ {_sv_dl}</span>'
                                f'</div>'
                                f'<div style="font-size:12px;color:#555;line-height:1.6;'
                                f'padding:5px 8px;background:#FAFAFA;border-left:3px solid {_wcolor};'
                                f'border-radius:0 4px 4px 0;">💬 {_wtalk}</div>'
                                f'</div>'
                            )
                        _sv_wph += '</div>'
                        st.markdown(_sv_wph, unsafe_allow_html=True)
                    else:
                        st.markdown(
                            "<span style='font-size:13px;color:var(--color-text-secondary);'>✅ 現時点で検出された課題はありません</span>",
                            unsafe_allow_html=True,
                        )

                    _render_nenkan_detail_expander(row)

                    _saved_phil     = str(row.get("企業理念", "") or "").strip()
                    _saved_csv_phil = str(row.get("csv_企業理念", "") or "").strip()
                    if _is_manual_company_row(row):
                        _saved_csv_phil = ""
                    if _saved_phil or _saved_csv_phil:
                        if _saved_phil:
                            st.markdown(
                                "**💬 企業理念**　<span style='font-size:11px;color:var(--color-text-secondary);'>（AI抽出）</span>",
                                unsafe_allow_html=True,
                            )
                            st.markdown(
                                f'<div style="font-size:13px;line-height:1.7;color:var(--color-text-primary);'
                                f'background:var(--color-background-secondary);border-radius:6px;padding:8px 10px;margin-top:4px;">{_saved_phil}</div>',
                                unsafe_allow_html=True,
                            )
                        if _saved_csv_phil and _saved_csv_phil != _saved_phil:
                            st.markdown(
                                "**💬 企業理念**　<span style='font-size:11px;color:var(--color-text-secondary);'>（年鑑DB）</span>",
                                unsafe_allow_html=True,
                            )
                            st.markdown(
                                f'<div style="font-size:13px;line-height:1.7;color:var(--color-text-primary);'
                                f'background:var(--color-background-secondary);border-radius:6px;padding:8px 10px;margin-top:4px;">{_saved_csv_phil}</div>',
                                unsafe_allow_html=True,
                            )

                # ── 右カラム：スクリーンショット・地図・CRM ──────
                with col_right:
                    _saved_ss = row.get("スクリーンショット", "") or ""
                    if not _saved_ss:
                        _saved_nm = row.get("社名", "")
                        if _csv_db and _csv_db.available and _saved_nm:
                            _saved_co3 = _csv_db.lookup_company(_saved_nm)
                            if _saved_co3:
                                _saved_cid3 = _saved_co3.get("csv_id", "")
                                _ti = _thumb_index()
                                if _saved_cid3 and _saved_cid3 in _ti:
                                    _saved_ss = _ti[_saved_cid3]
                                elif _saved_co3.get("thumbnail_url"):
                                    _saved_web3 = (
                                        (_saved_co3.get("website") or "").strip()
                                        or str(row.get("WebサイトURL", "") or "").strip()
                                    )
                                    if _valid_website_for_screenshot(_saved_web3):
                                        _saved_ss = _saved_co3["thumbnail_url"]
                    if _saved_ss and isinstance(_saved_ss, str):
                        _saved_url2 = str(row.get("WebサイトURL", "") or "").strip()
                        if _saved_url2 in ("情報なし", "不明"):
                            _saved_url2 = ""
                        render_screenshot(_saved_ss, _saved_url2)

                    _sv_addr = str(row.get("住所", "") or "")
                    _sv_lat  = None
                    _sv_lng  = None
                    _sv_cid3 = ""
                    if _csv_db and _csv_db.available:
                        _sv_nm2 = row.get("社名", "")
                        _sv_co4 = _csv_db.lookup_company(_sv_nm2) if _sv_nm2 else None
                        if _sv_co4:
                            _sv_lat = _sv_co4.get("latitude") or _sv_co4.get("_latitude")
                            _sv_lng = _sv_co4.get("longitude") or _sv_co4.get("_longitude")
                            _sv_cid3 = _sv_co4.get("csv_id", "")
                    import math as _sv_math
                    for _vv, _nn in ((_sv_lat, "lat"), (_sv_lng, "lng")):
                        try:
                            _fv = float(_vv) if _vv is not None else None
                            if _fv is None or _sv_math.isnan(_fv): _fv = None
                        except (TypeError, ValueError):
                            _fv = None
                        if _nn == "lat": _sv_lat = _fv
                        else:            _sv_lng = _fv
                    if (not _sv_lat or not _sv_lng) and _sv_addr and _sv_addr not in ("情報なし", "不明"):
                        _sv_geo = geocode_address(_sv_addr)
                        if _sv_geo:
                            _sv_lat, _sv_lng = _sv_geo
                            if _sv_cid3:
                                save_geocode_to_db(_sv_cid3, _sv_lat, _sv_lng)
                    if _sv_lat and _sv_lng:
                        _sv_mk = f"map_saved_{str(row.get('社名', ''))}_{_sv_lat}_{_sv_lng}"
                        render_map(float(_sv_lat), float(_sv_lng), str(row.get("社名", "")),
                                   _sv_addr, map_key=_sv_mk)
                    else:
                        st.caption("📍 地図: 住所情報なし")

                    render_card_print_export(
                        row,
                        key_prefix=f"print_card_sv_{record_id}",
                        doc_title="SalesScraper 担当企業リスト",
                        enable_scraping=False,
                    )

                    st.markdown("**担当者メモ**")
                    _sv_status   = str(row.get("ステータス", "未着手") or "未着手")
                    _sv_assignee = str(row.get("担当者", "") or "")
                    _sv_memo_val = str(row.get("メモ", "") or "")
                    _sv_crm_upd  = str(row.get("CRM更新日時", "") or "").strip()
                    st.markdown(f"保存者：{str(row.get('ユーザー', '') or '')}", unsafe_allow_html=True)
                    _sv_memo_k, _sv_status_k, _sv_assign_k = _sync_saved_crm_widgets(
                        record_id, _sv_memo_val, _sv_status, _sv_assignee, _sv_crm_upd,
                    )
                    with st.form(key=f"sv_crm_form_{record_id}", clear_on_submit=False):
                        st.text_area(
                            "メモ", placeholder="例：来週折り返し待ち",
                            key=_sv_memo_k, label_visibility="collapsed", height=72,
                        )
                        st.markdown("**CRMステータス**")
                        st.selectbox(
                            "ステータス", CRM_STATUSES,
                            key=_sv_status_k, label_visibility="collapsed",
                        )
                        st.text_input(
                            "担当者", placeholder="例：山田",
                            key=_sv_assign_k, label_visibility="collapsed",
                        )
                        if _sv_crm_upd:
                            st.caption(f"🕐 最終更新: {_sv_crm_upd[:16]}")
                        _sv_submitted = st.form_submit_button(
                            "💾 更新", type="primary", use_container_width=True,
                        )
                    if _sv_submitted:
                        _ok, _msg = update_crm(
                            record_id,
                            st.session_state[_sv_status_k],
                            st.session_state[_sv_assign_k],
                            st.session_state[_sv_memo_k],
                        )
                        if _ok:
                            _invalidate_saved_crm_widgets(record_id)
                            st.success(f"✅ {_msg}")
                            st.rerun()
                        else:
                            st.error(_msg)

                    confirm_key = f"confirm_del_{record_id}"
                    if st.session_state.get(confirm_key):
                        st.warning("本当に削除しますか？")
                        col_yes, col_no = st.columns(2)
                        with col_yes:
                            if st.button("✅ はい", key=f"yes_del_{record_id}", use_container_width=True):
                                delete_companies([record_id])
                                st.session_state.pop(confirm_key, None)
                                st.rerun()
                        with col_no:
                            if st.button("❌ いいえ", key=f"no_del_{record_id}", use_container_width=True):
                                st.session_state.pop(confirm_key, None)
                                st.rerun()
                    else:
                        if st.button("🗑️ 削除", key=f"del_{record_id}", use_container_width=True):
                            st.session_state[confirm_key] = True
                            st.rerun()
                st.divider()


# ─────────────────────────────────────────────
# Streamlit UI メイン
# ─────────────────────────────────────────────

def main():
    logger.info("main() 開始")
    try:
        (_APP_DIR / "_last_main_run.txt").write_text(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    st.set_page_config(
        page_title="SalesScraper – 営業リスト作成ツール",
        page_icon="🔍",
        layout="wide",
    )
    ensure_app_booted()
    # 再実行中の半透明フェード演出を抑える（処理時間は変わらないが体感が穏やかに）
    st.markdown(
        """
        <style>
        [data-stale="true"] { opacity: 1 !important; filter: none !important; }
        .stApp { transition: none !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.title("🔍 SalesScraper – 営業リスト作成ツール")

    if is_demo_mode():
        if is_cloud_deploy():
            st.info(
                "🎭 **ポートフォリオデモ** — 架空企業45社。"
                " ①ユーザー `demo` でログイン → ②「デモ」で検索 → ③担当企業リストでメモ編集"
            )
        else:
            st.info(
                "🎭 **デモモード** — 架空企業45社・ローカルCRM（Supabase不要）。"
                " 手順は `demo/README.md` を参照。"
            )

    # URLからログイン復元（Supabase往復なし・リロード対策）
    if not get_current_user():
        u = (st.query_params.get("u") or "").strip()
        if u:
            st.session_state["current_user"] = u

    render_login_sidebar()

    _open_err = st.session_state.pop("_open_session_error", None)
    if _open_err:
        st.error(_open_err)

    if not get_current_user():
        st.info("👈 サイドバーからログインしてください。")
        st.stop()

    try:
        with st.spinner("クラウド接続を確認しています…"):
            init_db()
    except RuntimeError as e:
        st.error(str(e))
        st.stop()

    # ── ページルーティング ────────────────────
    current_page = st.session_state.get("page", "search")
    if current_page == "memo":
        render_memo_list()
        return

    if current_page == "register":
        render_manual_register()
        return

    if not (_csv_db and _csv_db.available):
        st.error("⚠️ CSV企業DBが見つかりません。csv_import.py を実行して nenkan.db を作成してください。")
        st.stop()

    # ── 入力フォーム（st.form：送信時にアプリ全体を再実行して検索を起動） ─────
    with st.form("search_form", clear_on_submit=False):
        col1, col2 = st.columns(2)
        with col1:
            region_select = st.selectbox("🗾 地域", ["すべて"] + REGIONS, key="region_select")
            region = (
                st.text_input("地域を入力してください", placeholder="例：岡山市北区", key="region_free")
                if region_select == "その他（直接入力）"
                else ("" if region_select == "すべて" else region_select)
            )
        with col2:
            industry_select = st.selectbox("🏭 業種", ["すべて"] + INDUSTRIES, key="industry_select")
            industry = (
                st.text_input("業種を入力してください", placeholder="例：印刷業", key="industry_free")
                if industry_select == "その他（直接入力）"
                else ("" if industry_select == "すべて" else industry_select)
            )

        free_word = st.text_input(
            "✏️ フリーワード（任意）",
            placeholder="例：海産物",
            help="企業名・事業内容・概要・役員名を横断検索します。役員名を入力するとその人が在籍する会社が表示されます。",
            key="free_word",
        )

        if "sort_order" not in st.session_state:
            st.session_state["sort_order"] = "📋 取得順（デフォルト）"
        _sort_keys = list(SORT_OPTIONS.keys())
        sort_order = st.selectbox(
            "📊 表示順",
            options=_sort_keys,
            index=_sort_keys.index(st.session_state["sort_order"]),
            key="sort_order_form",
        )

        enable_scraping = False
        if is_web_scraping_disabled():
            st.caption(
                "🌐 Web取得はクラウドデモでは無効です。"
                " 弱点・技術情報は検索結果に表示済みです。"
            )
        else:
            enable_scraping = st.checkbox(
                "🌐 Webサイト取得・技術スタック判定を行う（時間がかかります）",
                value=False,
                key="enable_scraping_chk",
            )

        _btn_l, _btn_c, _btn_r = st.columns([3, 2, 3])
        with _btn_c:
            submitted = st.form_submit_button("🔎 検索する", type="primary")

    if submitted:
        if not region and not industry and not free_word.strip():
            st.warning("地域・業種・フリーワードのいずれかを入力してください。")
        else:
            st.session_state["_pending_search"] = {
                "region": region,
                "industry": industry,
                "industry_select": industry_select,
                "free_word": free_word,
                "enable_scraping": enable_scraping,
                "sort_order": sort_order,
            }
            st.rerun()

    # ── 検索実行（フォームから渡された条件で実行） ──────────────────────────
    pending = st.session_state.pop("_pending_search", None)
    if pending:
        region = pending["region"]
        industry = pending["industry"]
        industry_select = pending["industry_select"]
        free_word = pending["free_word"]
        enable_scraping = pending["enable_scraping"]
        sort_order = pending.get("sort_order", st.session_state.get("sort_order", "📋 取得順（デフォルト）"))
        st.session_state["sort_order"] = sort_order
        logger.info(
            "検索開始 region=%s industry=%s free_word=%s scraping=%s sort=%s",
            region, industry, free_word, enable_scraping, sort_order,
        )

        with st.spinner("企業年鑑DBを検索中…"):
            df, search_total = search_csv_companies(
                keyword=free_word.strip(),
                region=region,
                industry=industry if industry_select != "その他（直接入力）" else industry,
            )
        st.session_state["search_total_matched"] = search_total  # 件数（limit 未使用時は len(df) と同じ）

        if df.empty:
            st.warning("検索結果が0件でした。条件を変えてお試しください。")
            st.stop()

        _reg_msg = st.session_state.pop("_reg_enrich_msg", None)
        _reg_warns = st.session_state.pop("_reg_enrich_warns", None)
        if _reg_msg is not None:
            if _reg_msg:
                st.success(
                    f"✅ 新規登録しました（自動収集: {_reg_msg}）。"
                    f"{len(df)} 件表示しています。"
                )
            else:
                st.warning(
                    f"✅ 新規登録しましたが、追加情報は取得できませんでした。"
                    f"（{len(df)} 件表示）"
                )
        else:
            st.success(f"✅ {len(df)} 件の企業が見つかりました。")
        if enable_scraping and len(df) > 50:
            st.warning(
                f"🌐 Web取得ON: {len(df)} 社すべてを分析します。"
                "件数が多いと完了まで時間がかかります。"
            )
        if _reg_warns:
            for w in _reg_warns:
                st.caption(f"⚠️ {w}")

        # スクレイピングOFF: ローカルサムネ＋年鑑URLのみ（全件 scrape_cache は呼ばない）
        _thumbs = _thumb_index()

        def _resolve_screenshot_fast(row_idx):
            csv_id = str(df.loc[row_idx, "_csv_id"]) if "_csv_id" in df.columns else ""
            _url = str(df.loc[row_idx, "WebサイトURL"]) if "WebサイトURL" in df.columns else ""
            if csv_id and csv_id in _thumbs:
                return _thumbs[csv_id]
            if _valid_website_for_screenshot(_url):
                _thumb = df.loc[row_idx, "_thumbnail_url"] if "_thumbnail_url" in df.columns else ""
                return str(_thumb).strip() if _thumb else None
            return None

        df["スクリーンショット"] = df.index.map(_resolve_screenshot_fast)

        # ── スクレイピング ────────────────────
        scrape_results = {}
        if enable_scraping:
            _thumbs = _thumb_index()
            st.subheader("🌐 Webサイト取得・分析中")
            scrape_bar = st.progress(0, text="開始します…")
            total     = len(df)
            completed = [0]

            from concurrent.futures import ThreadPoolExecutor, as_completed

            def scrape_task(args):
                i, url, name = args
                _csv_id_row = ""
                if "_csv_id" in df.columns and i in df.index:
                    _csv_id_row = str(df.loc[i, "_csv_id"] or "").strip()

                # ── ① キャッシュ確認（90日以内なら即返す） ──
                if url and url != "情報なし":
                    cached = get_scrape_cache(url)
                    if cached is not None:
                        logger.info("キャッシュヒット: %s", name)
                        # 年鑑DBで「不明」を補完
                        if _nenkan_db and _nenkan_db.available:
                            co = _nenkan_db.lookup_company(name)
                            if co:
                                for key, db_val in {
                                    "代表者名": co.get("president", ""),
                                    "資本金":   co.get("capital", ""),
                                    "従業員数": co.get("employees", ""),
                                    "設立年":   co.get("established", ""),
                                    "事業内容": co.get("business", ""),
                                }.items():
                                    if db_val and cached.get(key, "不明") in ("不明", "", None):
                                        cached[key] = db_val
                                cached["nenkan_売上高"] = co.get("sales_latest", "")
                                cached["nenkan_上場"]   = co.get("listing", "")
                                cached["nenkan_銀行"]   = co.get("banks", "")
                                persons = _nenkan_db.lookup_persons_by_company(name, limit=5)
                                if persons:
                                    cached["nenkan_役員"] = "、".join([
                                        f"{p.get('title1','')}:{p.get('name','')}"
                                        for p in persons if p.get("name")
                                    ])
                        # キャッシュヒット時もサムネイル補完
                        if not cached.get("screenshot_path"):
                            _csv_co_c = (_csv_db.lookup_company(name)
                                         if _csv_db and _csv_db.available else None)
                            if _csv_co_c:
                                _csv_id_c = _csv_co_c.get("csv_id", "")
                                _csv_web_c = (_csv_co_c.get("website") or url or "").strip()
                                if _csv_id_c and _csv_id_c in _thumbs:
                                    cached["screenshot_path"] = _thumbs[_csv_id_c]
                                elif _valid_website_for_screenshot(_csv_web_c) and _csv_co_c.get("thumbnail_url"):
                                    cached["screenshot_path"] = _csv_co_c["thumbnail_url"]
                        return i, cached

                # ── ② 年鑑DBから取得 ──
                nenkan_result = {}
                nenkan_full_hit = False
                if _nenkan_db and _nenkan_db.available:
                    co = _nenkan_db.lookup_company(name)
                    if co:
                        nenkan_result = {
                            "代表者名": co.get("president", "不明") or "不明",
                            "資本金":   co.get("capital", "不明") or "不明",
                            "従業員数": co.get("employees", "不明") or "不明",
                            "設立年":   co.get("established", "不明") or "不明",
                            "事業内容": co.get("business", "不明") or "不明",
                            "nenkan_売上高": co.get("sales_latest", ""),
                            "nenkan_上場":   co.get("listing", ""),
                            "nenkan_銀行":   co.get("banks", ""),
                        }
                        persons = _nenkan_db.lookup_persons_by_company(name, limit=5)
                        if persons:
                            nenkan_result["nenkan_役員"] = "、".join([
                                f"{p.get('title1','')}:{p.get('name','')}"
                                for p in persons if p.get("name")
                            ])
                        logger.info("年鑑DBヒット: %s", name)
                        _required = ["代表者名", "資本金", "設立年", "事業内容"]
                        nenkan_full_hit = all(
                            nenkan_result.get(k, "不明") not in ("不明", "", None)
                            for k in _required
                        )

                # ── ②' CSV企業DBから取得（年鑑DBより優先） ──
                csv_result = {}
                csv_full_hit = False
                if _csv_db and _csv_db.available:
                    csv_co = (
                        _csv_db.lookup_company_by_id(_csv_id_row)
                        if _csv_id_row
                        else None
                    )
                    if not csv_co:
                        csv_co = _csv_db.lookup_company(name)
                    if csv_co:
                        _src = str(csv_co.get("source_type") or "").strip().lower()
                        _yearbook_phil = (
                            (csv_co.get("philosophy") or "").strip()
                            if _src != "manual"
                            else ""
                        )
                        csv_result = {
                            "代表者名": (csv_co.get("president", "") or "").strip() or "不明",
                            "資本金":   csv_co.get("capital", "不明") or "不明",
                            "従業員数": csv_co.get("employees", "不明") or "不明",
                            "設立年":   csv_co.get("established", "不明") or "不明",
                            "事業内容": csv_co.get("business", "不明") or "不明",
                            "_csv_philosophy": _yearbook_phil,
                            "_source_type": csv_co.get("source_type") or "",
                            "csv_売上高": csv_co.get("sales_latest", "") or "",
                            "csv_上場":   csv_co.get("listing", "") or "",
                            "csv_銀行":   csv_co.get("banks", "") or "",
                            "csv_業種":   csv_co.get("industry", "") or "",
                            "csv_概要":   csv_co.get("description", "") or "",
                            "csv_顧客":   csv_co.get("customers", "") or "",
                            "csv_仕入先": csv_co.get("suppliers", "") or "",
                            "ssl_issuer": (csv_co.get("ssl_issuer") or "").strip() or "不明",
                            "ssl_expiry": (csv_co.get("ssl_expiry") or "").strip() or "不明",
                            "whois_registrar": (csv_co.get("domain_registrar") or "").strip() or "不明",
                            "whois_expiry": (csv_co.get("whois_expiry") or "").strip() or "不明",
                            "hosting_company": (csv_co.get("hosting_company") or "").strip() or "不明",
                        }
                        # CSV のサムネイル（ローカルJPG優先、なければURL）
                        _csv_id = csv_co.get("csv_id", "")
                        _csv_web = (csv_co.get("website") or "").strip()
                        if _csv_id and _csv_id in _thumbs:
                            csv_result["screenshot_path"] = _thumbs[_csv_id]
                        elif _valid_website_for_screenshot(_csv_web) and csv_co.get("thumbnail_url"):
                            csv_result["screenshot_path"] = csv_co["thumbnail_url"]
                        logger.info("CSV企業DBヒット: %s", name)
                        _required = ["代表者名", "資本金", "設立年", "事業内容"]
                        csv_full_hit = all(
                            csv_result.get(k, "不明") not in ("不明", "", None)
                            for k in _required
                        )

                # ── ③ URLなし → DB情報のみ返す（Web分析不可） ──
                if url == "情報なし":
                    base = {
                        "screenshot_path": None, "page_text": "", "error": None,
                        "cms": "不明", "server": "不明", "responsive": False,
                        "technologies": [], "代表者名": "不明", "資本金": "不明",
                        "従業員数": "不明", "設立年": "不明", "事業内容": "不明",
                        "企業理念": "", "ai_summary": "", "法人番号": "不明",
                        "郵便番号": "不明", "法人種別": "不明",
                        "ip_address": "不明", "hosting_company": "不明",
                        "whois_registrar": "不明", "whois_expiry": "不明",
                        "name_servers": "不明", "ssl_issuer": "不明", "ssl_expiry": "不明",
                        "site_age_score": 0, "site_age_reason": "",
                    }
                    base.update(nenkan_result)
                    base.update(csv_result)
                    if nenkan_full_hit:
                        logger.info("年鑑フルヒット（URLなし）: %s", name)
                    if csv_full_hit:
                        logger.info("CSV企業DBフルヒット（URLなし）: %s", name)
                    return i, base

                if nenkan_full_hit:
                    logger.info("年鑑フルヒット（Web分析は続行）: %s", name)
                if csv_full_hit:
                    logger.info("CSV企業DBフルヒット（Web分析は続行）: %s", name)

                # ── ④ スクレイピング実行 ──
                # DBに登録されているURLとスクレイピング先URLの一致を事前チェック
                _db_url_for_check = ""
                _csv_id_for_scrape = _csv_id_row
                if _csv_db and _csv_db.available:
                    _co_for_check = (
                        _csv_db.lookup_company_by_id(_csv_id_row)
                        if _csv_id_row
                        else _csv_db.lookup_company(name)
                    )
                    if _co_for_check:
                        _db_url_for_check   = (_co_for_check.get("website") or "").strip()
                        if not _csv_id_for_scrape:
                            _csv_id_for_scrape  = (_co_for_check.get("csv_id") or "").strip()
                if _db_url_for_check and not _is_url_matching(_db_url_for_check, url):
                    logger.warning(
                        "⚠️ URL不一致検出（スクレイピング前）: company=%s db_url=%s scrape_url=%s "
                        "→ スクレイピングは続行しますが、philosophy等のDB保存はスキップされます",
                        name, _db_url_for_check, url,
                    )

                # csv_id を渡すことで scraper.py 側が年鑑DB登録企業のみ
                # houjin.db を参照する（未登録企業への法人番号混入防止）
                result = scrape_website(url, name, csv_id=_csv_id_for_scrape)

                # スクレイピングしたURLを結果に付与（save_scrape_result_to_dbのURL検証に使用）
                result["scraped_url"] = url

                # ── ⑤ 結果をキャッシュに保存 ──
                set_scrape_cache(url, name, result)

                # ── ⑥ 年鑑DBで「不明」を補完 ──
                for key, val in nenkan_result.items():
                    if val and result.get(key, "不明") in ("不明", "", None):
                        result[key] = val
                    elif key.startswith("nenkan_"):
                        result[key] = val

                # ── ⑦ CSV企業DBで「不明」を補完（年鑑DBより優先） ──
                for key, val in csv_result.items():
                    if key == "screenshot_path":
                        if val and not result.get("screenshot_path"):
                            result["screenshot_path"] = val
                    elif val and result.get(key, "不明") in ("不明", "", None):
                        result[key] = val
                    elif key.startswith("csv_"):
                        result[key] = val

                # ── ⑧ screenshot_path の最終補完 ──
                # Playwright でも CSV でも画像が取れなかった場合のフォールバック
                if not result.get("screenshot_path") and _csv_db and _csv_db.available:
                    _csv_co_f = _csv_db.lookup_company(name)
                    if _csv_co_f:
                        _csv_id_f = _csv_co_f.get("csv_id", "")
                        _csv_web_f = (_csv_co_f.get("website") or "").strip()
                        if _csv_id_f and _csv_id_f in _thumbs:
                            result["screenshot_path"] = _thumbs[_csv_id_f]
                        elif _valid_website_for_screenshot(_csv_web_f) and _csv_co_f.get("thumbnail_url"):
                            result["screenshot_path"] = _csv_co_f["thumbnail_url"]

                return i, result

            # ローカル環境用の並列数
            max_w = 3
            tasks = [(i, row["WebサイトURL"], row["社名"]) for i, row in df.iterrows()]
            _scrape_wait = _scrape_subprocess_timeout() + 15
            with ThreadPoolExecutor(max_workers=max_w) as executor:
                futures = {executor.submit(scrape_task, task): task for task in tasks}
                for future in as_completed(futures, timeout=3000):
                    task = futures[future]
                    try:
                        i, result = future.result(timeout=_scrape_wait)
                        scrape_results[i] = result
                    except Exception as e:
                        # タスクが例外で終わった場合：ログを残しデフォルト値で継続
                        _i, _url, _name = task
                        logger.error(
                            "スクレイピングスレッド失敗 index=%s name=%s url=%s: %s",
                            _i, _name, _url, e,
                        )
                        scrape_results[_i] = {
                            "screenshot_path": None, "page_text": "", "error": str(e),
                            "cms": "不明", "server": "不明", "responsive": False,
                            "technologies": [], "代表者名": "不明", "資本金": "不明",
                            "従業員数": "不明", "設立年": "不明", "事業内容": "不明",
                            "ai_summary": "", "法人番号": "不明", "郵便番号": "不明",
                            "法人種別": "不明", "ip_address": "不明",
                            "hosting_company": "不明", "whois_registrar": "不明",
                            "whois_expiry": "不明", "name_servers": "不明",
                            "ssl_issuer": "不明", "ssl_expiry": "不明",
                            "site_age_score": 0, "site_age_reason": "",
                        }
                    completed[0] += 1
                    scrape_bar.progress(
                        completed[0] / total,
                        text=f"処理中… {completed[0]} / {total} 件完了"
                    )
            scrape_bar.empty()
            st.success("✅ 取得完了！")

            def _resolve_ss_after_scrape(row_idx):
                ss = scrape_results[row_idx].get("screenshot_path")
                if ss and isinstance(ss, str):
                    if ss.startswith("http") or Path(ss).exists():
                        return ss
                return _resolve_screenshot_fast(row_idx)
            df["スクリーンショット"] = df.index.map(_resolve_ss_after_scrape)

            # スクリーンショット格納件数をログに記録
            ss_count = sum(1 for v in scrape_results.values() if v.get("screenshot_path"))
            logger.info("スクリーンショット格納件数: %d / %d", ss_count, len(scrape_results))

            # ── スクレイピング結果をDBに永続保存 ──────────────
            _saved_count = 0
            _db_path = _nenkan_db_file()
            for _si, _sres in scrape_results.items():
                _csv_id_s = df.loc[_si, "_csv_id"] if "_csv_id" in df.columns and _si in df.index else ""
                if _csv_id_s:
                    save_scrape_result_to_db(str(_csv_id_s), _sres)
                    try:
                        _applied = apply_manual_scrape_fields(_db_path, str(_csv_id_s), _sres)
                        if _applied:
                            logger.info(
                                "CSV項目DB補完: csv_id=%s %s",
                                _csv_id_s, "、".join(_applied),
                            )
                    except Exception as _e:
                        logger.warning("CSV項目DB補完失敗: csv_id=%s %s", _csv_id_s, _e)
                    _saved_count += 1
            if _saved_count:
                logger.info("スクレイピング結果DB保存: %d件", _saved_count)
            df["CMS"]               = df.index.map(lambda i: scrape_results[i].get("cms", "不明"))
            df["サーバー"]          = df.index.map(lambda i: scrape_results[i].get("server", "不明"))
            df["レスポンシブ"]      = df.index.map(lambda i: scrape_results[i].get("responsive", False))
            df["技術スタック"]      = df.index.map(lambda i: "、".join(scrape_results[i].get("technologies", [])))
            df["代表者名"]          = df.index.map(lambda i: scrape_results[i].get("代表者名", "不明"))
            df["資本金"]            = df.index.map(lambda i: scrape_results[i].get("資本金", "不明"))
            df["従業員数"]          = df.index.map(lambda i: scrape_results[i].get("従業員数", "不明"))
            df["設立年"]            = df.index.map(lambda i: scrape_results[i].get("設立年", "不明"))
            df["事業内容"]          = df.index.map(lambda i: scrape_results[i].get("事業内容", "不明"))
            df["AI営業ポイント"]    = df.index.map(lambda i: scrape_results[i].get("ai_summary", ""))
            df["page_text"]         = df.index.map(lambda i: scrape_results[i].get("page_text", ""))
            df["企業理念"]          = df.index.map(lambda i: scrape_results[i].get("企業理念", ""))
            # CSV由来の企業理念を別列として保持（スクレイピング結果と両方表示するため）
            if "企業理念" in df.columns:
                df["csv_企業理念"] = df.get("csv_企業理念", df["企業理念"].apply(lambda x: ""))
            # search_csv_companies の返り値にある「企業理念」列（CSV由来）をcsv_企業理念に移す
            _csv_phil_col = df.index.map(
                lambda i: scrape_results[i].get("_csv_philosophy", "") or ""
            )
            df["csv_企業理念"] = _csv_phil_col
            df["法人番号"]          = df.index.map(lambda i: scrape_results[i].get("法人番号", "不明"))
            df["郵便番号"]          = df.index.map(lambda i: scrape_results[i].get("郵便番号", "不明"))
            df["法人種別"]          = df.index.map(lambda i: scrape_results[i].get("法人種別", "不明"))
            df["エラー"]            = df.index.map(lambda i: scrape_results[i].get("error") or "")
            df["ip_address"]        = df.index.map(lambda i: scrape_results[i].get("ip_address", "不明"))
            df["hosting_company"]   = df.index.map(lambda i: scrape_results[i].get("hosting_company", "不明"))
            df["whois_registrar"]   = df.index.map(lambda i: scrape_results[i].get("whois_registrar", "不明"))
            df["whois_expiry"]      = df.index.map(lambda i: scrape_results[i].get("whois_expiry", "不明"))
            df["name_servers"]      = df.index.map(lambda i: scrape_results[i].get("name_servers", "不明"))
            df["ssl_issuer"]        = df.index.map(lambda i: scrape_results[i].get("ssl_issuer", "不明"))
            df["ssl_expiry"]        = df.index.map(lambda i: scrape_results[i].get("ssl_expiry", "不明"))
            df["site_age_score"]    = df.index.map(lambda i: scrape_results[i].get("site_age_score", 0))
            df["site_age_reason"]   = df.index.map(lambda i: scrape_results[i].get("site_age_reason", ""))

        # ── CSV由来のスクリーンショット列を追加 ──
        def _get_thumb(row):
            csv_id = row.get("_csv_id", "")
            if csv_id and csv_id in _thumbs:
                return _thumbs[csv_id]
            thumb = row.get("_thumbnail_url", "")
            if thumb and thumb.startswith("http"):
                return thumb
            return None

        if "スクリーンショット" not in df.columns:
            df["スクリーンショット"] = df.apply(_get_thumb, axis=1)
        else:
            # スクレイピング画像がない行だけ補完
            mask = df["スクリーンショット"].isna() | (df["スクリーンショット"] == "")
            df.loc[mask, "スクリーンショット"] = df[mask].apply(_get_thumb, axis=1)

        df = add_scores(df, industry)

        st.session_state["df"]              = df
        st.session_state["region"]          = region
        st.session_state["industry"]        = industry
        st.session_state["enable_scraping"] = enable_scraping
        st.session_state["current_page"]    = 0
        for idx in df.index:
            st.session_state[f"check_{idx}"] = False

    # ── 結果表示 ──────────────────────────────
    if "df" not in st.session_state:
        render_saved_list()
        return

    df              = st.session_state["df"]
    region          = st.session_state.get("region", "")
    industry        = st.session_state.get("industry", "")
    enable_scraping = st.session_state.get("enable_scraping", False)

    # スコアサマリー
    if "Web提案スコア" in df.columns:
        st.subheader("🎯 スコアリングサマリー")
        st.markdown("**🖥️ Webリニューアル提案 TOP5**")
        for _, r in df[["社名", "Web提案スコア", "Web提案優先度"]].sort_values(
            "Web提案スコア", ascending=False).head(5).iterrows():
            st.markdown(f"{r['Web提案優先度']} **{r['社名']}** ({r['Web提案スコア']}点)")
        st.divider()

    if "sort_order" not in st.session_state:
        st.session_state["sort_order"] = "📋 取得順（デフォルト）"

    st.subheader(f"📋 検索結果一覧（{len(df)} 件）")
    st.caption(f"表示順: {st.session_state['sort_order']}")

    col_all, col_none = st.columns([1, 1])
    with col_all:
        if st.button("☑ 全選択"):
            for idx in df.index:
                st.session_state[f"check_{idx}"] = True
            st.rerun()
    with col_none:
        if st.button("☐ 全解除"):
            for idx in df.index:
                st.session_state[f"check_{idx}"] = False
            st.rerun()

    # ソートを適用した表示用 DataFrame を作成（元の df は変更しない）
    sort_col, sort_asc = SORT_OPTIONS[st.session_state["sort_order"]]
    if sort_col and sort_col in df.columns:
        display_df = df.sort_values(sort_col, ascending=sort_asc).reset_index(drop=False)
        # 先頭に優先度バッジを表示するためのメッセージ
        priority_col = "Web提案優先度"
        top_label = display_df.iloc[0][priority_col] if len(display_df) > 0 else ""
        if "最優先" in top_label or "優先" in top_label:
            st.success(f"✅ {st.session_state['sort_order']} で並び替えています。最上位：{display_df.iloc[0]['社名']}（{top_label}）")
    else:
        display_df = df.reset_index(drop=False)

    _selected_indices = [
        i for i in df.index if st.session_state.get(f"check_{i}", False)
    ]
    _selected_count = len(_selected_indices)

    # ── 印刷・スマホ共有用HTML ─────────────
    _print_parts = [p for p in (region, industry) if p]
    _print_subtitle = "・".join(_print_parts) if _print_parts else "検索条件"
    _fw = str(st.session_state.get("free_word", "") or "").strip()
    if _fw:
        _print_subtitle += f"　KW:{_fw}"

    def _selected_from_display() -> pd.DataFrame:
        if not _selected_indices:
            return display_df.iloc[0:0].drop(columns=["index"], errors="ignore")
        _sel = set(_selected_indices)
        if "index" in display_df.columns:
            _out = display_df[display_df["index"].isin(_sel)]
        else:
            _out = display_df[display_df.index.isin(_sel)]
        return _out.drop(columns=["index"], errors="ignore")

    _export_df_all = display_df.drop(columns=["index"], errors="ignore")
    _include_ss = st.checkbox(
        "HPスクショを含める（OFFにするとファイルが軽くなります）",
        value=True,
        key="print_include_screenshots",
    )
    st.subheader("🖨 印刷・スマホ共有（一括）")
    st.caption(
        "複数社をまとめて出力する場合はこちら。"
        "1社ずつ出力する場合は、各カード右側の「印刷・共有（この会社）」を使ってください。"
        "HTMLはすぐダウンロードできます。"
        "PDFは「PDF生成」を押したときだけ作成します（数十秒かかることがあります）。"
    )
    st.markdown("**全件**")
    render_print_export_buttons(
        _export_df_all,
        key_prefix="download_print_all",
        file_tag=f"{_safe_filename_part(region or 'all')}_{_safe_filename_part(industry or 'all')}_all",
        title="SalesScraper 検索結果",
        subtitle=_print_subtitle,
        enable_scraping=enable_scraping,
        include_screenshots=_include_ss,
    )
    if _selected_count > 0:
        st.markdown("**選択のみ**")
        render_print_export_buttons(
            _selected_from_display(),
            key_prefix="download_print_selected_top",
            file_tag=f"{_safe_filename_part(region or 'all')}_{_safe_filename_part(industry or 'all')}_selected",
            title="SalesScraper 検索結果",
            subtitle=_print_subtitle + "　選択分",
            enable_scraping=enable_scraping,
            include_screenshots=_include_ss,
        )
    st.caption("⭐ 企業をチェックすると「選択のみ」も出力できます。")

    # ページネーション
    total_pages = max(1, (len(display_df) + PAGE_SIZE - 1) // PAGE_SIZE)
    if "current_page" not in st.session_state:
        st.session_state["current_page"] = 0

    page      = st.session_state["current_page"]
    start_idx = page * PAGE_SIZE
    end_idx   = min(start_idx + PAGE_SIZE, len(display_df))
    page_df   = display_df.iloc[start_idx:end_idx]

    def render_pagination(key_suffix: str):
        col_prev, col_info, col_next = st.columns([1, 3, 1])
        with col_prev:
            if st.button("← 前へ", disabled=(page == 0), key=f"prev_{key_suffix}"):
                st.session_state["current_page"] -= 1
                st.rerun()
        with col_info:
            st.markdown(
                f"<div style='text-align:center; padding-top:8px;'>"
                f"{page + 1} / {total_pages} ページ（{start_idx + 1}〜{end_idx}件目）"
                f"</div>",
                unsafe_allow_html=True,
            )
        with col_next:
            if st.button("次へ →", disabled=(page >= total_pages - 1), key=f"next_{key_suffix}"):
                st.session_state["current_page"] += 1
                st.rerun()

    render_pagination("top")

    # 表示中の社名リストでCRM情報を一括取得（DBアクセスは1回のみ）
    page_names = page_df["社名"].tolist()
    crm_map    = get_crm_map_by_names(page_names)

    # ── 経過年数計算ヘルパー ──────────────────────────────────────────
    import re as _re
    from datetime import datetime as _dt

    # 各会社カード（アコーディオン形式）
    for _, row in page_df.iterrows():
        i = int(row["index"]) if "index" in row.index else _
        if f"check_{i}" not in st.session_state:
            st.session_state[f"check_{i}"] = False

        web_label = row.get("Web提案優先度", "")
        checked   = st.session_state[f"check_{i}"]
        crm_info  = crm_map.get(row["社名"])
        is_saved  = crm_info is not None
        company_name = str(row["社名"])

        # ── 経過年数を計算 ──
        founded_raw = str(row.get("設立年", "") or "").strip()
        elapsed     = _calc_elapsed(founded_raw)
        years_str   = f"{elapsed}年" if elapsed is not None and elapsed >= 0 else ""

        # ── CRMステータス ──
        if is_saved:
            cur_status = (crm_info["status"] or "未着手") if crm_info else "未着手"
            cur_assign = (crm_info["assignee"] or "") if crm_info else ""
            cur_memo   = (crm_info["memo"] or "") if crm_info else ""
        else:
            _draft = _get_draft_crm(company_name)
            cur_status = _draft.get("status") or "未着手"
            cur_assign = _draft.get("assignee") or ""
            cur_memo   = _draft.get("memo") or ""
        crm_color, crm_bg = STATUS_COLORS.get(cur_status, ("#888888", "#F0F0F0"))

        # ── ヘッダーバッジHTML ──
        badge_base = (
            "display:inline-block;font-size:0.75em;font-weight:600;"
            "padding:2px 9px;border-radius:20px;white-space:nowrap;"
        )
        badge_gray  = badge_base + "background:#F3F4F6;color:#374151;border:0.5px solid #D1D5DB;"
        badge_red   = badge_base + "background:#FEE2E2;color:#991B1B;border:0.5px solid #FECACA;"
        badge_crm   = badge_base + f"background:{crm_bg};color:{crm_color};border:0.5px solid {crm_color}40;"

        header_badges = []
        if founded_raw and founded_raw != "不明":
            if years_str:
                # 設立日と周年を1バッジにまとめ、周年部分を赤字に
                header_badges.append(
                    f'<span style="{badge_gray}">設立 {founded_raw}　'
                    f'<span style="color:#991B1B;font-weight:700;">{years_str}</span></span>'
                )
            else:
                header_badges.append(f'<span style="{badge_gray}">設立 {founded_raw}</span>')
        capital_val = str(row.get("資本金", "") or "").strip()
        if capital_val and capital_val != "不明":
            header_badges.append(f'<span style="{badge_gray}">資本金 {capital_val}</span>')
        emp_val = str(row.get("従業員数", "") or "").strip()
        if emp_val and emp_val != "不明":
            import re as _re_emp
            emp_val = _re_emp.sub(r"^【[^】]*】", "", emp_val).strip()
            header_badges.append(f'<span style="{badge_gray}">社員数：{emp_val}</span>')
        header_badges.append(f'<span style="{badge_crm}">{cur_status}</span>')

        badges_html = "".join(header_badges)

        # ── アコーディオンヘッダー ──
        border_color = "#4CAF50" if checked else ("#2196F3" if is_saved else "#E5E7EB")
        bg_header    = "#f0fff0" if checked else ("#EEF6FF" if is_saved else "#F9FAFB")

        # ── ウィークポイント短縮ラベル ──
        _wp_list_h, _star_h = calc_weakpoints(row)
        _WP_SHORT = {
            "hp_none":  "HPなし",
            "non_ssl":  "SSL不備",
            "non_resp": "スマホ未対応",
            "no_career":"採用ページなし",
            "old_server":"旧サーバー",
        }
        _wp_short_labels = [_WP_SHORT.get(k, k) for k, *_ in _wp_list_h]
        _wp_header = "　".join(_wp_short_labels) if _wp_short_labels else ""

        # ── expanaderラベルにすべて詰め込む ──
        label_parts = [f"🏢 {company_name}"]
        if founded_raw and founded_raw != "不明":
            _f_disp = founded_raw
            if years_str:
                _f_disp += f"（{years_str}）"
            label_parts.append(_f_disp)
        if capital_val and capital_val != "不明":
            label_parts.append(f"資本金 {capital_val}")
        if emp_val and emp_val != "不明":
            label_parts.append(f"社員数：{emp_val}")
        if _wp_header:
            label_parts.append(f"⚠ {_wp_header}")
        if cur_assign:
            label_parts.append(f"担当：{cur_assign}")
        expander_label = "　｜　".join(label_parts)

        # ── アコーディオン本体（3カラム） ──
        with st.expander(expander_label, expanded=False):
            col_left, col_mid, col_right = st.columns([0.7, 1.1, 1.1], gap="large")

            # ── 左カラム：基本情報 ──────────────────────────
            with col_left:
                st.markdown("**代表者**")
                rep = str(row.get("代表者名", "不明") or "不明")
                st.markdown(f"{rep}")

                st.markdown("**住所**")
                st.markdown(f"{row['住所']}")

                st.markdown("**TEL**")
                st.markdown(f"{row['TEL']}")

                st.markdown("**URL**")
                url_val = row.get("WebサイトURL", "情報なし") or "情報なし"
                if url_val and url_val != "情報なし":
                    st.markdown(f"[{url_val}]({url_val})")
                else:
                    st.markdown("情報なし")

                jigyou = row.get("事業内容", "不明") or "不明"
                if jigyou and jigyou != "不明":
                    st.markdown("**主業務**")
                    st.markdown(f"{jigyou}")

                if row.get("業種") and row["業種"] != "不明":
                    st.markdown(f"**業種：** {row['業種']}")

            # ── 中央カラム：財務・Web情報 ────────────────────
            with col_mid:
                _sales_rows = parse_sales_history(row.get("csv_売上推移", "") or "")
                if _sales_rows:
                    _has_profit = any(r["profit_man"] is not None for r in _sales_rows)
                    _has_pure   = any(r.get("pure_profit") is not None for r in _sales_rows)
                    _max_s = max(r["sales_man"] for r in _sales_rows) or 1

                    # ヘッダー
                    _hdr = (
                        '<div style="display:flex;align-items:center;gap:6px;'
                        'margin-bottom:3px;padding-bottom:3px;'
                        'border-bottom:0.5px solid var(--color-border-tertiary);">'
                        '<span style="font-size:12px;color:var(--color-text-secondary);'
                        'width:48px;flex-shrink:0;"></span>'
                        '<span style="flex:1;font-size:12px;color:var(--color-text-secondary);">'
                        '売上</span>'
                    )
                    if _has_profit:
                        _hdr += ('<span style="font-size:12px;color:var(--color-text-secondary);'
                                 'width:80px;text-align:right;flex-shrink:0;">経常利益</span>')
                    if _has_pure:
                        _hdr += ('<span style="font-size:12px;color:var(--color-text-secondary);'
                                 'width:80px;text-align:right;flex-shrink:0;">純利益</span>')
                    _hdr += '</div>'

                    _body = ""
                    for _sr in _sales_rows:
                        _pct   = int(_sr["sales_man"] / _max_s * 100)
                        _s_lbl = fmt_man(_sr["sales_man"])
                        _p_val = _sr.get("profit_man")
                        _pp_val= _sr.get("pure_profit")
                        _p_lbl = fmt_man(_p_val)  if _p_val  is not None else "－"
                        _pp_lbl= fmt_man(_pp_val) if _pp_val is not None else "－"
                        _p_col = "color:#A32D2D;" if (_p_val  is not None and _p_val  < 0) else ""
                        _pp_col= "color:#A32D2D;" if (_pp_val is not None and _pp_val < 0) else ""
                        _body += (
                            '<div style="display:flex;align-items:center;gap:6px;margin-bottom:5px;">'
                            f'<span style="font-size:13px;color:var(--color-text-secondary);'
                            f'width:48px;flex-shrink:0;">{_sr["period"]}</span>'
                            f'<div style="flex:1;display:flex;align-items:center;gap:4px;">'
                            f'<div style="flex:1;height:10px;background:var(--color-background-secondary);'
                            f'border-radius:4px;overflow:hidden;">'
                            f'<div style="width:{_pct}%;height:100%;background:#378ADD;border-radius:4px;"></div>'
                            f'</div>'
                            f'<span style="font-size:13px;color:var(--color-text-primary);'
                            f'width:76px;text-align:right;flex-shrink:0;">{_s_lbl}</span>'
                            f'</div>'
                        )
                        if _has_profit:
                            _body += (f'<span style="font-size:13px;width:80px;text-align:right;'
                                      f'flex-shrink:0;{_p_col}">{_p_lbl}</span>')
                        if _has_pure:
                            _body += (f'<span style="font-size:13px;width:80px;text-align:right;'
                                      f'flex-shrink:0;{_pp_col}">{_pp_lbl}</span>')
                        _body += '</div>'

                    st.markdown("**売上推移**")
                    st.markdown(_hdr + _body, unsafe_allow_html=True)
                else:
                    # sales_historyがない場合は直近売上のみ表示
                    _sales_val = str(row.get("csv_売上高", "") or row.get("nenkan_売上高", "") or "").strip()
                    if _sales_val and _sales_val not in ("0", "0.0", "nan"):
                        st.markdown("**直近売上**")
                        try:
                            _rv = float(_sales_val)
                            st.markdown(fmt_man(_rv / 10000) if _rv >= 1e8 else
                                        (f"{_rv/1e4:.0f}万円" if _rv >= 1e4 else f"{_rv:,.0f}円"))
                        except (ValueError, TypeError):
                            st.markdown(_sales_val)

                # 寸評（スクレイピング時のAI分析 → CSV概要の優先順）
                _ai_comment  = str(row.get("AI営業ポイント", "") or "").strip()
                if _is_ai_error_message(_ai_comment):
                    _ai_comment = ""
                _raw_summary = str(row.get("csv_概要", "") or "").strip()
                # 「【寸評】」などの注記プレフィックスを除去
                import re as _re3
                _csv_summary = _re3.sub(r"^【[^】]*】\s*", "", _raw_summary).strip()

                _comment_text = _ai_comment or _csv_summary
                if _comment_text:
                    st.markdown("**寸評**")
                    st.info(f"💡 {_comment_text}")

                # ── Web情報（スクレイピング OR CSV由来データで表示）──
                _url_for_web = str(row.get("WebサイトURL", "") or "").strip()
                _has_scraping = enable_scraping and "CMS" in df.columns

                # CSV由来のtech_stack/server_infoを解析してCMS・ホスティングを取得
                import json as _json2
                _csv_tech_raw    = str(row.get("_tech_stack", "") or "")
                _csv_server_raw  = str(row.get("_server_info", "") or "")
                _csv_renewal     = row.get("_renewal_score")

                # tech_stackのJSONからCMS・技術・レスポンシブを取得
                _csv_cms   = "不明"
                _csv_tech  = "不明"
                _csv_resp  = None   # True/False/None
                if _csv_tech_raw:
                    try:
                        _ts = _json2.loads(_csv_tech_raw)
                        _detected = _ts.get("detected", [])
                        _csv_cms  = "、".join(_detected) if _detected else "不明"
                        _sigs = _ts.get("signals", {})
                        _tech_list = []
                        if _sigs.get("wordpress"):    _tech_list.append("WordPress")
                        if _sigs.get("wix"):          _tech_list.append("Wix")
                        if _sigs.get("react"):        _tech_list.append("React")
                        if _sigs.get("nextjs"):       _tech_list.append("Next.js")
                        _hdr = _ts.get("headers", {})
                        if _hdr.get("server"):        _tech_list.append(_hdr["server"])
                        _csv_tech = "、".join(_tech_list) if _tech_list else "不明"
                        _csv_resp = _sigs.get("hasViewport")   # Viewportタグ有無 ≒ レスポンシブ
                    except Exception:
                        pass

                # server_infoからホスティング会社名を抽出
                _csv_host = "不明"
                if _csv_server_raw:
                    import re as _re5
                    _org = _re5.search("Org:\\s*([^\xb7|]+)", _csv_server_raw)
                    if _org:
                        _csv_host = _org.group(1).strip()

                # 表示するデータを決定（スクレイピング優先、なければCSV由来）
                _has_web_detail = _has_scraping or bool(_csv_tech_raw or _csv_server_raw)

                with st.expander("🌐 Web情報"):
                    if _has_web_detail:
                        # CMS・技術・レスポンシブ・ホスティングの値を確定
                        _cms  = (row.get("CMS", "") or "") if _has_scraping else ""
                        _cms  = _cms if _cms and _cms != "不明" else _csv_cms
                        _tech = (row.get("技術スタック", "") or "") if _has_scraping else ""
                        _tech = _tech if _tech and _tech != "不明" else _csv_tech
                        _resp_scrape = row.get("レスポンシブ") if _has_scraping else None
                        _resp = _resolve_responsive(row, scrape_val=_resp_scrape, csv_resp=_csv_resp)
                        _host = (row.get("hosting_company", "") or "") if _has_scraping else ""
                        _host = _host if _host and _host != "不明" else _csv_host
                        if (not _host or _host == "不明") and row.get("hosting_company") not in ("", "不明", None):
                            _host = row.get("hosting_company", "不明")
                        _ssl_i = row.get("ssl_issuer", "不明") or "不明"
                        _ssl_e = row.get("ssl_expiry", "不明") or "不明"
                        _whois = row.get("whois_registrar", "不明") or "不明"
                        _whois_e = row.get("whois_expiry", "不明") or "不明"

                        _rows_web = [
                            ("CMS",        _cms),
                            ("技術",       _tech if _tech else "不明"),
                            ("レスポンシブ", "✅ 対応" if _resp else "❌ 非対応"),
                            ("ホスティング", _host),
                            ("SSL発行者",   _ssl_i),
                            ("SSL有効期限", _ssl_e),
                            ("ドメイン登録", _whois),
                            ("ドメイン期限", _whois_e),
                        ]
                        # 値の表示変換：取得不可→薄グレー小文字、不明→ダッシュ
                        def _web_val_html(v):
                            if v == "取得不可":
                                return "<span style='color:#B0B0B0;font-size:13px;'>取得不可</span>"
                            if not v or v == "不明":
                                return "<span style='color:var(--color-text-secondary);'>-</span>"
                            return v
                        _web_html = '<div style="font-size:16px;line-height:1.8;">'
                        for _wk, _wv in _rows_web:
                            _color = ""
                            if _wk == "レスポンシブ" and not _resp:
                                _color = "color:#A32D2D;"
                            elif _wk == "レスポンシブ" and _resp:
                                _color = "color:#0F6E56;"
                            _wv_html = _wv if _wk == "レスポンシブ" else _web_val_html(_wv)
                            _web_html += (
                                f'<div style="display:flex;justify-content:space-between;'
                                f'align-items:baseline;padding:5px 0;'
                                f'border-bottom:0.5px solid var(--color-border-tertiary);">'
                                f'<span style="color:var(--color-text-secondary);">{_wk}</span>'
                                f'<span style="text-align:right;{_color}">{_wv_html}</span></div>'
                            )
                        _web_html += '</div>'
                        st.markdown(_web_html, unsafe_allow_html=True)

                        if "site_age_score" in df.columns:
                            _age_s = row.get("site_age_score", 0)
                            _age_r = row.get("site_age_reason", "")
                            _age_l = (
                                "🔴 かなり古い" if _age_s >= 15 else
                                "🟠 やや古い"   if _age_s >= 10 else
                                "🟡 普通"       if _age_s >= 5  else
                                "🟢 新しい"
                            )
                            st.markdown(f"**サイト古さ：** {_age_s}点　{_age_l}"
                                        + (f"（{_age_r}）" if _age_r else ""))
                    else:
                        # スクレイピングなし：URLとSSL判定のみ
                        if _url_for_web and _url_for_web not in ("情報なし", "不明"):
                            st.markdown(f"[{_url_for_web}]({_url_for_web})")
                            _ssl_hint = "❌ 非SSL（http）" if _url_for_web.startswith("http://") else "✅ SSL（https）"
                            _ssl_color = "#A32D2D" if _url_for_web.startswith("http://") else "#0F6E56"
                            st.markdown(
                                f'<span style="font-size:12px;color:{_ssl_color};">{_ssl_hint}</span>',
                                unsafe_allow_html=True,
                            )
                        else:
                            st.caption("WebサイトURLなし")

                # ── ウィークポイント（統合版）────────────────
                _wp_list, _star = calc_weakpoints(row)
                _hosting_disp = str(row.get("hosting_company", "") or "").strip()
                if not _hosting_disp or _hosting_disp == "不明":
                    import re as _re_wp2
                    _si2 = str(row.get("_server_info", "") or "")
                    _om2 = _re_wp2.search("Org:\\s*([^\xb7|]+)", _si2)
                    if _om2:
                        _hosting_disp = _om2.group(1).strip()

                # ヘッダー：見出し＋★スコア
                st.markdown(
                    f"**⚠️ ウィークポイント**　{star_label(_star)}",
                    unsafe_allow_html=True,
                )

                if _wp_list:
                    _wp_html = '<div style="margin-top:6px;">'
                    for _wkey, _wlabel, _wcolor, _wtalk in _wp_list:
                        # 旧来サーバー環境はホスティング名を追記
                        _display_label = _wlabel
                        if _wkey == "old_server" and _hosting_disp and _hosting_disp not in ("不明", "取得不可", ""):
                            _display_label = f"{_wlabel}（{_hosting_disp}）"
                        _wp_html += (
                            f'<div style="padding:7px 0;border-bottom:0.5px solid var(--color-border-tertiary);">'                            f'<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px;">'                            f'<span style="font-size:13px;font-weight:700;color:{_wcolor};">⚠ {_display_label}</span>'                            f'</div>'                            f'<div style="font-size:12px;color:var(--color-text-secondary);line-height:1.5;padding-left:4px;">'                            f'{_wtalk}'                            f'</div>'                            f'</div>'
                        )
                    _wp_html += '</div>'
                    st.markdown(_wp_html, unsafe_allow_html=True)
                else:
                    st.markdown(
                        "<span style='font-size:13px;color:var(--color-text-secondary);'>該当なし</span>",
                        unsafe_allow_html=True,
                    )

                _render_nenkan_detail_expander(row)

            # ── 右カラム：スクリーンショット・地図・CRM ──────
            with col_right:
                # スクリーンショット（クリックでWebサイトに遷移）
                _ss_path = row.get("スクリーンショット") or None
                _ss_url  = str(row.get("WebサイトURL", "") or "").strip()
                if _ss_url in ("情報なし", "不明"):
                    _ss_url = ""
                if _ss_path and isinstance(_ss_path, str):
                    render_screenshot(_ss_path, _ss_url)

                # ── 地図（OpenStreetMap） ──────────────────────
                _lat = row.get("_latitude")
                _lng = row.get("_longitude")
                _addr = str(row.get("住所", "") or "")
                _csv_id_map = str(row.get("_csv_id", "") or "")

                # NaN / 無効値を None に統一
                import math as _math
                for _v, _n in ((_lat, "_lat"), (_lng, "_lng")):
                    try:
                        _fv = float(_v) if _v is not None else None
                        if _fv is None or _math.isnan(_fv):
                            _fv = None
                    except (TypeError, ValueError):
                        _fv = None
                    if _n == "_lat": _lat = _fv
                    else:            _lng = _fv

                # 緯度経度がない場合はジオコードで取得してDBにキャッシュ
                if (not _lat or not _lng) and _addr and _addr not in ("情報なし", "不明"):
                    with st.spinner("地図を取得中…"):
                        _geo = geocode_address(_addr)
                    if _geo:
                        _lat, _lng = _geo
                        if _csv_id_map:
                            save_geocode_to_db(_csv_id_map, _lat, _lng)

                if _lat and _lng:
                    _map_key = f"map_search_{row.get('社名', '')}_{_lat}_{_lng}"
                    render_map(float(_lat), float(_lng), str(row.get("社名", "")),
                               _addr, map_key=_map_key)
                else:
                    st.caption("📍 地図: 住所情報なし")

                render_card_print_export(
                    row,
                    key_prefix=f"print_card_srch_{i}",
                    doc_title="SalesScraper 検索結果",
                    enable_scraping=enable_scraping,
                )

                # ← 修正4: 担当者メモを地図の直下に移動（常に表示）
                st.markdown("**担当者メモ**")
                if is_saved:
                    saved_by = crm_info.get("username") or crm_info.get("user") or crm_info.get("user_name", "")
                    crm_rid = crm_info.get("id")
                    if crm_rid is not None:
                        crm_rid = int(crm_rid)
                    st.markdown(f"<span style='font-size:0.8em;color:#6B7280;'>保存者：{saved_by}</span>",
                                unsafe_allow_html=True)
                    _srch_crm_upd = str(crm_info.get("crm_updated_at", "") or "").strip() if crm_info else ""
                    _memo_wkey, _status_wkey, _assign_wkey = _init_srch_crm_widgets(
                        company_name, cur_memo, cur_status, cur_assign, _srch_crm_upd,
                    )
                    with st.form(key=f"srch_crm_form_{i}", clear_on_submit=False):
                        st.text_area(
                            "メモ", placeholder="例：来週折り返し待ち",
                            key=_memo_wkey, label_visibility="collapsed", height=72,
                        )
                        st.markdown("**CRMステータス**")
                        st.selectbox(
                            "ステータス", CRM_STATUSES,
                            key=_status_wkey, label_visibility="collapsed",
                        )
                        st.text_input(
                            "担当者", placeholder="例：山田",
                            key=_assign_wkey, label_visibility="collapsed",
                        )
                        if _srch_crm_upd:
                            st.caption(f"🕐 最終更新: {_srch_crm_upd[:16]}")
                        _srch_submitted = st.form_submit_button(
                            "💾 更新", type="primary", use_container_width=True,
                        )
                    if _srch_submitted and crm_rid:
                        _ok, _msg = update_crm(
                            crm_rid,
                            st.session_state[_status_wkey],
                            st.session_state[_assign_wkey],
                            st.session_state[_memo_wkey],
                        )
                        if _ok:
                            _invalidate_saved_crm_widgets(crm_rid)
                            st.session_state.pop(
                                f"srch_crm_ver_{_crm_key_suffix(company_name)}", None
                            )
                            st.success(f"✅ {_msg}")
                            st.rerun()
                        else:
                            st.error(_msg)
                    _c_clr, _c_del = st.columns(2)
                    with _c_clr:
                        if crm_rid and st.button(
                            "🗑️ CRMクリア", key=f"srch_clr_{i}", use_container_width=True,
                        ):
                            _ok_clr, _msg_clr = clear_crm(crm_rid)
                            if _ok_clr:
                                _reset_srch_crm_widgets(company_name)
                                st.success(f"✅ {_msg_clr}")
                                st.rerun()
                            else:
                                st.error(_msg_clr)
                    with _c_del:
                        _confirm_del_key = f"confirm_srch_del_{_crm_key_suffix(company_name)}"
                        if st.session_state.get(_confirm_del_key):
                            st.warning("リストから削除しますか？")
                            _dy, _dn = st.columns(2)
                            with _dy:
                                if st.button("はい", key=f"yes_srch_del_{i}", use_container_width=True):
                                    delete_companies([crm_rid])
                                    st.session_state.pop(_confirm_del_key, None)
                                    _reset_srch_crm_widgets(company_name)
                                    st.rerun()
                            with _dn:
                                if st.button("いいえ", key=f"no_srch_del_{i}", use_container_width=True):
                                    st.session_state.pop(_confirm_del_key, None)
                                    st.rerun()
                        elif crm_rid and st.button(
                            "🗑️ リストから削除", key=f"srch_del_{i}", use_container_width=True,
                        ):
                            st.session_state[_confirm_del_key] = True
                            st.rerun()
                else:
                    _draft = _get_draft_crm(company_name)
                    _memo_wkey, _status_wkey, _assign_wkey = _init_srch_crm_widgets(
                        company_name,
                        _draft.get("memo", ""),
                        _draft.get("status", "未着手"),
                        _draft.get("assignee", ""),
                    )
                    with st.form(key=f"srch_draft_crm_form_{i}", clear_on_submit=False):
                        st.text_area(
                            "メモ",
                            placeholder="例：来週折り返し待ち",
                            key=_memo_wkey,
                            label_visibility="collapsed",
                            height=72,
                        )
                        st.markdown("**CRMステータス**")
                        st.selectbox(
                            "ステータス",
                            CRM_STATUSES,
                            key=_status_wkey,
                            label_visibility="collapsed",
                        )
                        st.text_input(
                            "担当者",
                            placeholder="例：山田",
                            key=_assign_wkey,
                            label_visibility="collapsed",
                        )
                        st.caption(
                            "💡 「💾 更新」でチームに共有されます。"
                            "未登録の会社は自動で担当企業リストにも追加されます。"
                        )
                        _pub_submitted = st.form_submit_button(
                            "💾 更新", type="primary", use_container_width=True,
                        )
                    if _pub_submitted:
                        _draft_crm_store()[company_name] = {
                            "memo": st.session_state[_memo_wkey],
                            "status": st.session_state[_status_wkey],
                            "assignee": st.session_state[_assign_wkey],
                        }
                        _ok, _msg = publish_search_crm(
                            row,
                            st.session_state[_status_wkey],
                            st.session_state[_assign_wkey],
                            st.session_state[_memo_wkey],
                        )
                        if _ok:
                            _draft_crm_store().pop(company_name, None)
                            st.success(f"✅ {_msg}")
                            st.rerun()
                        else:
                            st.error(_msg)
                    if st.button("🗑️ 入力をクリア", key=f"srch_draft_clr_{i}", use_container_width=True):
                        _reset_draft_crm(company_name)
                        st.rerun()

                # 保存・出力チェックボックス
                st.checkbox("⭐ 保存・出力対象に選ぶ", key=f"check_{i}")

            # ── 企業理念（カード一番下・全幅）────────────────
            _philosophy     = str(row.get("企業理念", "") or "").strip()
            _csv_philosophy = str(row.get("csv_企業理念", "") or "").strip()
            if _is_manual_company_row(row):
                _csv_philosophy = ""
            if _philosophy or _csv_philosophy:
                if _philosophy:
                    st.markdown(
                        "**企業理念**　<span style='font-size:11px;"
                        "color:var(--color-text-secondary);'>（AI抽出）</span>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f'<div style="font-size:15px;line-height:1.7;'
                        f'color:var(--color-text-primary);'
                        f'background:var(--color-background-secondary);border-radius:6px;'
                        f'padding:10px 12px;margin-top:4px;">{_philosophy}</div>',
                        unsafe_allow_html=True,
                    )
                if _csv_philosophy and _csv_philosophy != _philosophy:
                    st.markdown(
                        "**企業理念**　<span style='font-size:11px;"
                        "color:var(--color-text-secondary);'>（年鑑DB）</span>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f'<div style="font-size:15px;line-height:1.7;'
                        f'color:var(--color-text-primary);'
                        f'background:var(--color-background-secondary);border-radius:6px;'
                        f'padding:10px 12px;margin-top:4px;">{_csv_philosophy}</div>',
                        unsafe_allow_html=True,
                    )

    # ── 検索結果の保存 ────────────────────────
    st.divider()
    # 再現した検索の場合はバナー表示
    if "loaded_session" in st.session_state:
        st.info(f"📂 「{st.session_state['loaded_session']}」を再現中")

    with st.expander("💾 この検索結果を名前を付けて保存", expanded=False):
        # デフォルト名を「地域・業種 MM/DD」形式で提案
        from datetime import date
        _name_parts = [p for p in (region, industry) if p]
        default_name = (
            f"{'・'.join(_name_parts) if _name_parts else '検索'} "
            f"{date.today().strftime('%m/%d')}"
        )
        session_name_input = st.text_input(
            "保存名",
            value=default_name,
            placeholder="例：広島中区・不動産業 4/17",
            key="session_name_input",
        )
        _cond_parts = [p for p in (region, industry) if p]
        _cond_label = "・".join(_cond_parts) if _cond_parts else "（地域なし・業種のみ等）"
        st.caption(
            f"検索条件：{_cond_label}　"
            f"取得件数：{len(df)}件　"
            f"スクレイピング：{'あり' if enable_scraping else 'なし'}"
        )
        if st.button("📥 保存する", type="primary", use_container_width=True,
                     key="save_session_btn"):
            _save_name = session_name_input.strip()
            if _save_name:
                new_id = save_search_session(
                    session_name=_save_name,
                    region=region,
                    industry=industry,
                    enable_scraping=enable_scraping,
                    df=df,
                )
                if new_id >= 0:
                    st.session_state["loaded_session"] = _save_name
                    st.success(
                        f"✅ 「{_save_name}」として保存しました！"
                        "（サイドバーから再開できます）"
                    )
                    st.rerun()
                else:
                    st.error("保存に失敗しました。しばらくしてから再度お試しください。")
            else:
                st.warning("保存名を入力してください。")

    # ── 出力・保存エリア ──────────────────────
    st.divider()
    selected_indices = _selected_indices
    selected_count   = _selected_count

    st.subheader(f"📤 選択した企業の保存・出力（{selected_count} 件選択中）")

    if selected_count == 0:
        st.info(
            "「⭐ この会社を保存・出力対象に選ぶ」にチェックを入れると、"
            "保存・CSV・印刷用HTML（選択分）が使えます。"
        )
    else:
        base_cols  = ["社名", "郵便番号", "住所", "TEL", "WebサイトURL", "法人番号", "法人種別"]
        score_cols = ["Web提案スコア", "Web提案優先度", "Web提案理由"]
        ai_cols    = ["代表者名", "資本金", "従業員数", "設立年", "事業内容", "AI営業ポイント"]
        export_cols = [c for c in base_cols + score_cols + ai_cols if c in df.columns]
        export_df   = df.loc[selected_indices, export_cols].reset_index(drop=True)
        _html_sel_df = _selected_from_display()

        st.markdown(f"**出力プレビュー（{len(export_df)} 件）**")
        st.dataframe(export_df, use_container_width=True)

        col1, col_mid, col4 = st.columns([1, 2, 1])
        with col1:
            csv_bytes = export_df.to_csv(index=False).encode("shift_jis", errors="ignore")
            st.download_button(
                label="📥 CSVでダウンロード",
                data=csv_bytes,
                file_name=f"sales_list_{region}_{industry}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with col_mid:
            render_print_export_buttons(
                _html_sel_df,
                key_prefix="download_print_selected_bottom",
                file_tag=f"{_safe_filename_part(region or 'all')}_{_safe_filename_part(industry or 'all')}_selected",
                title="SalesScraper 検索結果",
                subtitle=_print_subtitle + "　選択分",
                enable_scraping=enable_scraping,
                include_screenshots=st.session_state.get(
                    "print_include_screenshots", True
                ),
            )
        with col4:
            if st.button("📋 担当企業リストに追加", use_container_width=True):
                saved, skipped = save_companies(
                    df, selected_indices, draft_crm=_draft_crm_store()
                )
                if saved > 0:
                    st.success(f"✅ {saved} 件を担当企業リストに追加しました！")
                if skipped > 0:
                    st.warning(f"⚠️ {skipped} 件は重複のためスキップしました。")

    # ── 担当企業リスト（CRM対応） ─────────────
    render_saved_list()


def _run_app() -> None:
    try:
        main()
    except Exception:
        logger.exception("main() 未処理エラー")
        raise


if __name__ == "__main__":
    _run_app()
else:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        if get_script_run_ctx() is not None:
            _run_app()
    except Exception:
        pass
