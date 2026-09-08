"""年鑑DB（csv_companies）への手動企業登録。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from csv_import import CREATE_TABLE_SQL, INDEX_SQLS


class ManualCompanyError(Exception):
    """手動登録の入力・重複エラー。"""


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(CREATE_TABLE_SQL)
    for sql in INDEX_SQLS:
        conn.execute(sql)


def company_exists(conn: sqlite3.Connection, name: str) -> bool:
    n = name.strip()
    if not n:
        return False
    row = conn.execute(
        "SELECT 1 FROM csv_companies WHERE name = ? LIMIT 1", (n,)
    ).fetchone()
    return row is not None


def _is_ai_error_message(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    markers = (
        "Gemini 抽出エラー", "Gemini APIキーエラー", "AI抽出エラー",
        "APIキーエラー", "JSONパースエラー", "pip install",
        "NOT_FOUND", "models/gemini",
    )
    return any(m in s for m in markers)


def apply_manual_scrape_fields(
    db_path: str | Path,
    csv_id: str,
    scrape: dict,
) -> list[str]:
    """スクレイピング結果で空欄の csv_companies 列を補完。更新した項目名リストを返す。"""
    if not csv_id or not scrape:
        return []

    def ok(val) -> bool:
        s = str(val or "").strip()
        return bool(s) and s not in ("不明", "情報なし", "null", "None")

    updates: list[tuple[str, str, str]] = []
    for col, key in (
        ("president", "代表者名"),
        ("capital", "資本金"),
        ("employees", "従業員数"),
        ("established", "設立年"),
        ("business", "事業内容"),
        ("philosophy", "企業理念"),
        ("ai_analysis", "ai_summary"),
        ("corporate_number", "法人番号"),
    ):
        val = scrape.get(key)
        if key == "ai_summary" and _is_ai_error_message(str(val or "")):
            continue
        if ok(val):
            updates.append((col, str(val).strip(), key))

    if not updates:
        return []

    conn = sqlite3.connect(str(db_path), timeout=15)
    try:
        applied: list[str] = []
        for col, val, label in updates:
            cur = conn.execute(
                f"UPDATE csv_companies SET {col}=? "
                f"WHERE csv_id=? AND ({col} IS NULL OR TRIM({col})='')",
                (val, csv_id),
            )
            if cur.rowcount:
                applied.append(label)

        cms = str(scrape.get("cms") or "").strip()
        server = str(scrape.get("server") or "").strip()
        techs = scrape.get("technologies") or []
        score = scrape.get("site_age_score")
        if server and server not in ("不明", "情報なし"):
            cur = conn.execute(
                "UPDATE csv_companies SET server_info=? "
                "WHERE csv_id=? AND (server_info IS NULL OR TRIM(server_info)='')",
                (server, csv_id),
            )
            if cur.rowcount:
                applied.append("サーバー情報")
        if cms and cms not in ("不明", "情報なし") or techs:
            detected = []
            if cms and cms not in ("不明", "情報なし"):
                detected.append(cms)
            for t in techs:
                if t not in detected:
                    detected.append(t)
            tech_blob = json.dumps(
                {
                    "cms": cms,
                    "technologies": techs,
                    "detected": detected or techs,
                    "signals": {"hasViewport": bool(scrape.get("responsive"))},
                },
                ensure_ascii=False,
            )
            cur = conn.execute(
                "UPDATE csv_companies SET tech_stack=? "
                "WHERE csv_id=? AND (tech_stack IS NULL OR TRIM(tech_stack)='')",
                (tech_blob, csv_id),
            )
            if cur.rowcount:
                applied.append("技術スタック")
        if score is not None:
            try:
                cur = conn.execute(
                    "UPDATE csv_companies SET renewal_score=? "
                    "WHERE csv_id=? AND renewal_score IS NULL",
                    (float(score), csv_id),
                )
                if cur.rowcount:
                    applied.append("サイト更新スコア")
            except (TypeError, ValueError):
                pass

        conn.commit()
        return applied
    finally:
        conn.close()


def apply_houjin_fields(
    db_path: str | Path,
    csv_id: str,
    houjin: dict,
) -> list[str]:
    if not csv_id or not houjin:
        return []
    no = str(houjin.get("法人番号") or "").strip()
    if not no or no == "不明":
        return []
    conn = sqlite3.connect(str(db_path), timeout=15)
    try:
        cur = conn.execute(
            "UPDATE csv_companies SET corporate_number=? "
            "WHERE csv_id=? AND (corporate_number IS NULL OR TRIM(corporate_number)='')",
            (no, csv_id),
        )
        conn.commit()
        return ["法人番号"] if cur.rowcount else []
    finally:
        conn.close()


def insert_manual_company(
    db_path: str | Path,
    name: str,
    address: str,
    tel: str,
    website: str = "",
) -> dict:
    """手動登録。戻り値は csv_companies の1行（dict）。"""
    name = name.strip()
    address = address.strip()
    tel = tel.strip()
    website = (website or "").strip()

    if not name:
        raise ManualCompanyError("企業名を入力してください。")
    if not address:
        raise ManualCompanyError("住所を入力してください。")
    if not tel:
        raise ManualCompanyError("電話番号を入力してください。")

    db_path = Path(db_path)
    if not db_path.exists():
        raise ManualCompanyError(
            f"企業DBが見つかりません: {db_path}\n"
            "nenkan.db の場所を確認してください。"
        )

    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_table(conn)
        if company_exists(conn, name):
            raise ManualCompanyError(f"「{name}」は既に登録されています。")

        csv_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """
            INSERT INTO csv_companies (
                csv_id, name, address, tel, website,
                data_source, source_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                csv_id,
                name,
                address,
                tel,
                website or None,
                "app",
                "manual",
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM csv_companies WHERE csv_id = ?", (csv_id,)
        ).fetchone()
        if not row:
            raise ManualCompanyError("登録に失敗しました。")
        return dict(row)
    except ManualCompanyError:
        raise
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "readonly" in msg or "read-only" in msg:
            raise ManualCompanyError(
                "企業DBが読み取り専用のため書き込めません。"
                "NAS上のDBではなく、PCローカルの nenkan.db を指定してください。"
            ) from e
        raise ManualCompanyError(f"DB書き込みエラー: {e}") from e
    finally:
        conn.close()
