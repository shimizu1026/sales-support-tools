"""
nenkan_search.py
=================
企業年鑑DB（nenkan.db）から情報を検索するモジュール。
app.py の scrape_website() の前段として呼び出し、
DBに情報があればAPIリクエストをスキップする。

使い方（app.py内）:
    from nenkan_search import NenkanDB
    ndb = NenkanDB("nenkan.db")          # 起動時に一度だけ
    info = ndb.lookup_company("広島電鉄㈱")  # 検索
    if info:
        # DBの情報を使う（Webスクレイピングスキップ）
        ...
"""

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Optional


class NenkanDB:
    def __init__(self, db_path: str = "nenkan.db"):
        self.db_path = db_path
        self._available = Path(db_path).exists()
        self._local = threading.local()

    def _get_conn(self) -> sqlite3.Connection | None:
        """スレッドごとに接続を作る（ThreadPoolExecutor からの参照でエラーにならない）。"""
        if not self._available:
            return None
        conn = getattr(self._local, "conn", None)
        if conn is None:
            from nenkan_sqlite import connect_nenkan

            conn = connect_nenkan(self.db_path, timeout=15)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    @property
    def available(self) -> bool:
        return self._available

    # ── 社名正規化（検索用） ──────────────────────────
    @staticmethod
    def _normalize(name: str) -> str:
        """㈱→株式会社 等を正規化して検索精度を上げる"""
        name = name.strip()
        name = name.replace("㈱", "株式会社").replace("（株）", "株式会社")
        name = name.replace("㈲", "有限会社").replace("（有）", "有限会社")
        name = name.replace("㈳", "社団法人").replace("㈶", "財団法人")
        name = re.sub(r"[\s　]+", "", name)  # 空白除去
        return name

    def _has_table(self, table: str) -> bool:
        conn = self._get_conn()
        if conn is None:
            return False
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        )
        return cur.fetchone() is not None

    @staticmethod
    def _format_thousand_yen(value) -> str:
        """千円単位の整数を表示用テキストに変換（例: 100000 → 1億円）"""
        if value is None or value == "":
            return ""
        try:
            v = int(value)
        except (TypeError, ValueError):
            return str(value).strip()
        if v >= 100_000:
            oku = v / 100_000
            return f"{int(oku)}億円" if oku == int(oku) else f"{oku:.1f}億円"
        if v >= 10:
            man = v / 10
            return f"{int(man)}万円" if man == int(man) else f"{man:.1f}万円"
        return f"{v}千円"

    def _book_row_to_company(self, row: sqlite3.Row) -> dict:
        """book_companies の行を app.py が期待する companies 形式に変換"""
        d = dict(row)
        est = d.get("established_year") or d.get("founded_year")
        established = str(est) if est else ""
        return {
            "id": d.get("id"),
            "name": d.get("name", ""),
            "name_kana": d.get("name_kana", ""),
            "tel": d.get("tel", ""),
            "fax": d.get("fax", ""),
            "address": d.get("address", ""),
            "postal_code": "",
            "established": established,
            "capital": self._format_thousand_yen(d.get("capital_stock")),
            "employees": d.get("employees", "") or "",
            "sales_latest": self._format_thousand_yen(d.get("revenue")),
            "listing": d.get("stock_exchange", "") or "",
            "president": d.get("president", "") or "",
            "business": d.get("business", "") or "",
            "banks": d.get("banks", "") or "",
            "officers": d.get("officers"),
            "sales_history": d.get("sales_history"),
            "industry": d.get("industry", "") or "",
            "description": d.get("description", "") or "",
            "customers": d.get("customers", "") or "",
            "suppliers": d.get("suppliers", "") or "",
            "factories": d.get("factories", "") or "",
            "source_page": d.get("source_page"),
            "_source_table": "book_companies",
        }

    def _lookup_book_company(self, cur, norm: str, company_name: str) -> Optional[dict]:
        name_norm_sql = (
            "replace(replace(replace(name,'㈱','株式会社'),'㈲','有限会社'),'　','')"
        )
        cur.execute(
            f"SELECT * FROM book_companies WHERE {name_norm_sql} = ? LIMIT 1",
            (norm,),
        )
        row = cur.fetchone()
        if not row and len(company_name) >= 2:
            like = f"%{company_name[:6]}%"
            cur.execute(
                "SELECT * FROM book_companies WHERE name LIKE ? LIMIT 1",
                (like,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return self._book_row_to_company(row)

    def _lookup_legacy_company(self, cur, norm: str, company_name: str) -> Optional[dict]:
        cur.execute(
            "SELECT * FROM companies WHERE replace(replace(name,'㈱','株式会社'),'㈲','有限会社') = ? LIMIT 1",
            (norm,),
        )
        row = cur.fetchone()
        if not row and len(company_name) >= 2:
            like = f"%{company_name[:6]}%"
            cur.execute("SELECT * FROM companies WHERE name LIKE ? LIMIT 1", (like,))
            row = cur.fetchone()
        if not row:
            return None
        result = dict(row)
        result["_source_table"] = "companies"
        for key in ("officers", "sales_history"):
            if result.get(key):
                try:
                    result[key] = json.loads(result[key])
                except Exception:
                    pass
        return result

    # ── 企業編から検索 ────────────────────────────────
    def lookup_company(self, company_name: str) -> Optional[dict]:
        """
        社名で企業編DBを検索。
        book_companies（PDF取込）→ companies（旧）の順で試す。
        完全一致 → 部分一致の順で試みる。
        """
        if not self._available or not company_name:
            return None

        norm = self._normalize(company_name)
        conn = self._get_conn()
        if conn is None:
            return None
        cur = conn.cursor()

        if self._has_table("book_companies"):
            hit = self._lookup_book_company(cur, norm, company_name)
            if hit:
                return hit

        if self._has_table("companies"):
            return self._lookup_legacy_company(cur, norm, company_name)
        return None

    # ── 人名編から代表者を検索 ────────────────────────
    def lookup_persons_by_company(self, company_name: str, limit: int = 10) -> list[dict]:
        """
        社名に関連する役員・経営者を人名編から検索。
        完全一致 → 部分一致の順。
        """
        if not self._available:
            return []

        short = company_name[:6]  # 略称でも引っかかるよう短くする
        conn = self._get_conn()
        if conn is None:
            return []
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM persons
            WHERE company1 LIKE ? OR company2 LIKE ? OR company3 LIKE ?
            LIMIT ?
        """, (f"%{short}%", f"%{short}%", f"%{short}%", limit))
        return [dict(r) for r in cur.fetchall()]

    def lookup_person_by_name(self, name: str) -> list[dict]:
        """氏名で人名編を検索"""
        if not self._available:
            return []
        conn = self._get_conn()
        if conn is None:
            return []
        cur = conn.cursor()
        cur.execute("SELECT * FROM persons WHERE name = ? OR name LIKE ? LIMIT 5",
                    (name, f"%{name}%"))
        return [dict(r) for r in cur.fetchall()]

    # ── app.py用: scrape結果にDB情報をマージ ─────────
    def enrich_scrape_result(self, company_name: str, scrape_result: dict) -> dict:
        """
        スクレイピング結果にDBの情報を補完する。
        DBにある項目は「不明」のものだけ上書きする（Webの情報を優先）。
        """
        company_info = self.lookup_company(company_name)
        persons = self.lookup_persons_by_company(company_name)

        if company_info:
            # DBにある情報でスクレイピング結果の「不明」を補完
            mapping = {
                "代表者名":  company_info.get("president", ""),
                "資本金":    company_info.get("capital", ""),
                "従業員数":  company_info.get("employees", ""),
                "設立年":    company_info.get("established", ""),
                "事業内容":  company_info.get("business", ""),
                "郵便番号":  company_info.get("postal_code", ""),
            }
            for key, val in mapping.items():
                if val and scrape_result.get(key, "不明") in ("不明", "", None):
                    scrape_result[key] = val

            # 追加情報
            scrape_result["nenkan_売上高"] = company_info.get("sales_latest", "")
            scrape_result["nenkan_上場"]   = company_info.get("listing", "")
            scrape_result["nenkan_銀行"]   = company_info.get("banks", "")
            scrape_result["nenkan_page"]   = company_info.get("source_page", "")
            sh_txt = self._sales_history_to_text(company_info.get("sales_history"))
            if sh_txt:
                scrape_result["nenkan_売上推移"] = sh_txt

        raw_off = company_info.get("officers") if company_info else None
        db_officer_txt = self._officers_cell_to_text(raw_off)
        if persons:
            officer_list = []
            for p in persons:
                t = p.get("title1", "")
                n = p.get("name", "")
                if t and n:
                    officer_list.append(f"{t}:{n}")
                elif n:
                    officer_list.append(n)
            ptxt = "、".join(officer_list)
            parts = [x for x in (db_officer_txt, ptxt) if x]
            if parts:
                scrape_result["nenkan_役員"] = " ／ ".join(parts)
        elif db_officer_txt:
            scrape_result["nenkan_役員"] = db_officer_txt

        return scrape_result

    @staticmethod
    def _officers_cell_to_text(raw) -> str:
        if raw is None or raw == "":
            return ""
        if isinstance(raw, str):
            t = raw.strip()
            if not t:
                return ""
            try:
                j = json.loads(t)
            except (json.JSONDecodeError, TypeError):
                return t
        else:
            j = raw
        if isinstance(j, list):
            out = []
            for x in j:
                if isinstance(x, dict):
                    out.append(
                        ",".join(f"{k}:{v}" for k, v in x.items() if v)
                    )
                else:
                    out.append(str(x))
            return "、".join(out)
        if isinstance(j, dict):
            return "、".join(f"{k}:{v}" for k, v in j.items() if v)
        return str(j)

    @staticmethod
    def _sales_history_to_text(raw) -> str:
        if raw is None or raw == "":
            return ""
        if isinstance(raw, str):
            t = raw.strip()
            if not t:
                return ""
            try:
                j = json.loads(t)
            except (json.JSONDecodeError, TypeError):
                return t
        else:
            j = raw
        if isinstance(j, list):
            parts: list[str] = []
            for item in j[:5]:
                if isinstance(item, dict):
                    y = item.get("year", "")
                    a = item.get("amount", "")
                    parts.append(f"{y}:{a}".strip(":"))
                else:
                    parts.append(str(item))
            return "、".join(parts)
        if isinstance(j, dict):
            return "、".join(
                f"{k}:{v}" for k, v in list(j.items())[:5]
            )
        return str(raw)

    # ── 統計 ─────────────────────────────────────────
    def stats(self) -> dict:
        if not self._available:
            return {"available": False}
        conn = self._get_conn()
        if conn is None:
            return {"available": False}
        cur = conn.cursor()
        persons = cur.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        book = 0
        legacy = 0
        if self._has_table("book_companies"):
            book = cur.execute("SELECT COUNT(*) FROM book_companies").fetchone()[0]
        if self._has_table("companies"):
            legacy = cur.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        return {
            "available": True,
            "persons": persons,
            "companies": book or legacy,
            "book_companies": book,
            "legacy_companies": legacy,
            "db_path": self.db_path,
        }

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None
