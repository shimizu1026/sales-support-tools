"""csv_companies への SSL / ドメイン情報の保存ヘルパー。"""
from __future__ import annotations

import sqlite3

DOMAIN_INFO_COLUMNS: dict[str, str] = {
    "ssl_issuer": "TEXT",
    "ssl_expiry": "TEXT",
    "whois_expiry": "TEXT",
    "hosting_company": "TEXT",
}


def ensure_domain_info_columns(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(csv_companies)")}
    for col, typ in DOMAIN_INFO_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE csv_companies ADD COLUMN {col} {typ}")
    conn.commit()


def extract_domain_info(result: dict) -> dict[str, str]:
    """scrape / get_domain_info の dict から DB 保存用フィールドを抽出。"""

    def norm(key: str) -> str:
        v = (result.get(key) or "").strip()
        return v if v and v not in ("不明", "取得不可") else ""

    out: dict[str, str] = {}
    for db_col, src in (
        ("ssl_issuer", "ssl_issuer"),
        ("ssl_expiry", "ssl_expiry"),
        ("whois_expiry", "whois_expiry"),
        ("hosting_company", "hosting_company"),
    ):
        v = norm(src)
        if v:
            out[db_col] = v

    registrar = norm("whois_registrar")
    if registrar:
        out["domain_registrar"] = registrar

    return out


def apply_domain_info_update(
    conn: sqlite3.Connection, csv_id: str, result: dict
) -> dict[str, str]:
    fields = extract_domain_info(result)
    if not fields:
        return {}
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [csv_id]
    conn.execute(f"UPDATE csv_companies SET {sets} WHERE csv_id=?", vals)
    return fields
