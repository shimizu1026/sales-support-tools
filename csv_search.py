"""
csv_search.py  ―  csv_companies テーブルを検索するモジュール

NenkanDB（nenkan_search.py）と同じインターフェースを提供するため、
app.py 側の変更が最小限で済む。

【公開API】
    CsvDB(db_path)
        .available          : bool  DBが存在し csv_companies テーブルがあれば True
        .stats()            : dict  {"companies": N, "with_website": N, ...}
        .lookup_company(name)       -> dict | None   社名で1件検索
        .lookup_by_corporate(no)    -> dict | None   法人番号で検索
        .search_companies(keyword, limit) -> list[dict]   キーワード検索（複数件）

【検索の優先順位（lookup_company）】
    1. 完全一致（name）
    2. 完全一致（name_pdf）
    3. name に法人格プレフィックスを付けたパターン（株式会社〇〇 ↔ 〇〇株式会社）
    4. LIKE あいまい検索（name LIKE '%keyword%'）
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger("csv_search")


# ─────────────────────────────────────────────────────
# 正規化ユーティリティ
# ─────────────────────────────────────────────────────

_LEGAL_PREFIXES = [
    "株式会社", "有限会社", "合同会社", "合名会社", "合資会社",
    "一般社団法人", "公益社団法人", "特定非営利活動法人", "医療法人",
    "社会福祉法人", "学校法人", "宗教法人", "財団法人",
]

def _normalize(name: str) -> str:
    """
    検索用正規化：
      - 全角英数字 → 半角
      - 法人格を除去
      - 括弧表記の法人格 (株) → 除去
      - スペース除去
    """
    # 全角英数 → 半角
    name = name.translate(str.maketrans(
        "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
        "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
        "０１２３４５６７８９",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789",
    ))
    # 括弧付き法人格: (株) (有) など
    name = re.sub(r"[（(]株[）)]", "", name)
    name = re.sub(r"[（(]有[）)]", "", name)
    name = re.sub(r"[（(]合[）)]", "", name)
    # 法人格プレフィックス/サフィックスを除去
    for prefix in _LEGAL_PREFIXES:
        name = name.replace(prefix, "")
    # スペース全除去
    name = re.sub(r"[\s　]+", "", name)
    return name.strip()


def _legal_variants(name: str) -> list[str]:
    """
    '株式会社〇〇' → ['〇〇株式会社', '株式会社〇〇'] のようにバリアントを生成。
    検索ヒット率を上げるために使う。
    """
    variants = [name]
    for prefix in _LEGAL_PREFIXES:
        if name.startswith(prefix):
            core = name[len(prefix):]
            variants.append(core + prefix)   # サフィックスに移動
        elif name.endswith(prefix):
            core = name[: -len(prefix)]
            variants.append(prefix + core)   # プレフィックスに移動
    return list(dict.fromkeys(variants))     # 重複除去（順序保持）


# ─────────────────────────────────────────────────────
# 返り値の整形
# ─────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict:
    """
    sqlite3.Row → dict に変換し、app.py / nenkan_search.py の
    既存フィールド名に合わせてエイリアスを付与する。
    """
    d = dict(row)

    # --- nenkan_search.py の lookup_company() と同じキー名でエイリアス ---
    # app.py は nenkan_result["capital"] / ["employees"] 等を参照する
    d.setdefault("capital",       d.get("capital"))
    d.setdefault("employees",     d.get("employees"))
    d.setdefault("established",   d.get("established"))
    d.setdefault("business",      d.get("business"))
    d.setdefault("president",     d.get("president"))
    d.setdefault("address",       d.get("address"))
    d.setdefault("tel",           d.get("tel"))
    d.setdefault("website",       d.get("website"))
    d.setdefault("banks",         d.get("banks"))
    d.setdefault("officers",      d.get("officers"))
    d.setdefault("industry",      d.get("industry"))
    d.setdefault("listing",       "上場" if d.get("is_listed") else "非上場")
    d.setdefault("sales_latest",  d.get("revenue"))

    # スクリーンショット: thumbnail_url があれば screenshot_path として提供
    d["screenshot_path"] = d.get("thumbnail_url") or None

    return d


def _get_president_from_executives(conn, csv_id: str) -> str:
    """
    executives テーブルから代表者名を取得する。
    優先順位: 代表取締役社長 > 代表取締役 > 社長 > 代表社員 > 代表理事
    """
    if not csv_id:
        return ""
    try:
        # 優先順位付きで1件取得
        row = conn.execute(
            """SELECT name FROM executives
               WHERE company_id = ?
               ORDER BY
                 CASE role
                   WHEN '代表取締役社長'           THEN 1
                   WHEN '代表取締役社長執行役員'    THEN 2
                   WHEN '代表取締役'               THEN 3
                   WHEN '取締役社長'               THEN 4
                   WHEN '社長'                     THEN 5
                   WHEN '代表社員'                 THEN 6
                   WHEN '代表理事'                 THEN 7
                   WHEN '代表取締役会長'           THEN 8
                   ELSE 99
                 END
               LIMIT 1""",
            (csv_id,)
        ).fetchone()
        return row["name"] if row else ""
    except Exception:
        return ""


# ─────────────────────────────────────────────────────
# CsvDB クラス
# ─────────────────────────────────────────────────────

class CsvDB:
    def __init__(self, db_path: str = "nenkan.db"):
        self._db_path = db_path
        self._available: bool | None = None

    # ── 接続ヘルパー ────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        from nenkan_sqlite import connect_nenkan

        conn = connect_nenkan(self._db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    # ── available プロパティ ────────────────────────────
    @property
    def available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            from nenkan_sqlite import connect_nenkan

            conn = connect_nenkan(self._db_path, timeout=8)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='csv_companies'"
            ).fetchone()
            conn.close()
            self._available = tables is not None
        except Exception as e:
            logger.warning("CsvDB.available チェック失敗: %s", e)
            self._available = False
        return self._available

    # ── stats ────────────────────────────────────────────
    def stats(self) -> dict:
        if not self.available:
            return {"companies": 0, "with_website": 0, "with_thumbnail": 0, "executives": 0}
        try:
            conn = self._connect()
            cur = conn.cursor()
            total     = cur.execute("SELECT COUNT(*) FROM csv_companies").fetchone()[0]
            with_web  = cur.execute("SELECT COUNT(*) FROM csv_companies WHERE website IS NOT NULL").fetchone()[0]
            with_thumb= cur.execute("SELECT COUNT(*) FROM csv_companies WHERE thumbnail_url IS NOT NULL").fetchone()[0]
            with_cap  = cur.execute("SELECT COUNT(*) FROM csv_companies WHERE capital IS NOT NULL").fetchone()[0]
            try:
                exec_cnt = cur.execute("SELECT COUNT(*) FROM executives").fetchone()[0]
            except Exception:
                exec_cnt = 0
            conn.close()
            return {
                "companies":      total,
                "with_website":   with_web,
                "with_thumbnail": with_thumb,
                "with_capital":   with_cap,
                "executives":     exec_cnt,
            }
        except Exception as e:
            logger.error("CsvDB.stats 失敗: %s", e)
            return {"companies": 0, "with_website": 0, "with_thumbnail": 0, "executives": 0}

    # ── lookup_company（メイン検索）────────────────────────
    def lookup_company(self, name: str) -> dict | None:
        """
        社名で1件検索。ヒットした場合は dict を返す。

        検索順:
          1. name 完全一致
          2. name_pdf 完全一致
          3. 法人格バリアント（'株式会社〇〇' ↔ '〇〇株式会社'）で完全一致
          4. 正規化後コアワードで LIKE 検索（最もスコアが高い1件）
        """
        if not self.available or not name:
            return None

        name = name.strip()
        try:
            conn = self._connect()
            cur = conn.cursor()

            # ① 完全一致（name）
            row = cur.execute(
                "SELECT * FROM csv_companies WHERE name = ? LIMIT 1", (name,)
            ).fetchone()
            if row:
                conn.close()
                logger.debug("CsvDB hit(name exact): %s", name)
                return self._enrich_president(_row_to_dict(row))

            # ② 完全一致（name_pdf）
            row = cur.execute(
                "SELECT * FROM csv_companies WHERE name_pdf = ? LIMIT 1", (name,)
            ).fetchone()
            if row:
                conn.close()
                logger.debug("CsvDB hit(name_pdf exact): %s", name)
                return self._enrich_president(_row_to_dict(row))

            # ③ 法人格バリアントで完全一致
            for variant in _legal_variants(name):
                if variant == name:
                    continue
                row = cur.execute(
                    "SELECT * FROM csv_companies WHERE name = ? OR name_pdf = ? LIMIT 1",
                    (variant, variant),
                ).fetchone()
                if row:
                    conn.close()
                    logger.debug("CsvDB hit(variant): %s -> %s", name, variant)
                    return self._enrich_president(_row_to_dict(row))

            # ④ コアワードで LIKE 検索
            core = _normalize(name)
            if len(core) >= 2:
                row = cur.execute(
                    "SELECT * FROM csv_companies WHERE name LIKE ? OR name_pdf LIKE ? LIMIT 1",
                    (f"%{core}%", f"%{core}%"),
                ).fetchone()
                if row:
                    conn.close()
                    logger.debug("CsvDB hit(LIKE): %s -> core=%s", name, core)
                    return self._enrich_president(_row_to_dict(row))

            conn.close()
            return None

        except Exception as e:
            logger.error("CsvDB.lookup_company 失敗 name=%s: %s", name, e)
            return None

    def lookup_company_by_id(self, csv_id: str) -> dict | None:
        """csv_id で1件取得（社名あいまい検索の取り違えを防ぐ）。"""
        if not self.available or not csv_id:
            return None
        csv_id = csv_id.strip()
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM csv_companies WHERE csv_id = ? LIMIT 1",
                (csv_id,),
            ).fetchone()
            conn.close()
            if row:
                logger.debug("CsvDB hit(csv_id): %s", csv_id)
                return self._enrich_president(_row_to_dict(row))
            return None
        except Exception as e:
            logger.error("CsvDB.lookup_company_by_id 失敗 csv_id=%s: %s", csv_id, e)
            return None

    def _enrich_president(self, d: dict) -> dict:
        """
        president が空の場合、executives テーブルから代表者名を補完する。
        lookup_company / search_companies の返り値に適用する。
        """
        if d.get("president"):
            return d
        csv_id = d.get("csv_id", "")
        if not csv_id:
            return d
        try:
            conn = self._connect()
            name = _get_president_from_executives(conn, csv_id)
            conn.close()
            if name:
                d["president"] = name
                logger.debug("executives補完: csv_id=%s president=%s", csv_id, name)
        except Exception as e:
            logger.debug("_enrich_president失敗: %s", e)
        return d

    # ── lookup_by_corporate（法人番号検索）────────────────
    def lookup_by_corporate(self, corporate_number: str) -> dict | None:
        """法人番号（13桁文字列）でピンポイント検索"""
        if not self.available or not corporate_number:
            return None
        # 数値として渡された場合も13桁ゼロ埋めに統一
        try:
            no = str(int(float(str(corporate_number)))).zfill(13)
        except (ValueError, TypeError):
            no = str(corporate_number).zfill(13)

        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM csv_companies WHERE corporate_number = ? LIMIT 1", (no,)
            ).fetchone()
            conn.close()
            if row:
                logger.debug("CsvDB hit(corporate_number): %s", no)
                return _row_to_dict(row)
            return None
        except Exception as e:
            logger.error("CsvDB.lookup_by_corporate 失敗 no=%s: %s", no, e)
            return None

    # ── search_companies（複数件キーワード検索）──────────
    def search_companies(self, keyword: str, limit: int = 20) -> list[dict]:
        """
        キーワードを含む企業を複数件返す。
        業種・住所・事業内容も対象にする。
        """
        if not self.available or not keyword:
            return []
        kw = f"%{keyword.strip()}%"
        try:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT * FROM csv_companies
                WHERE  name     LIKE ?
                    OR industry LIKE ?
                    OR address  LIKE ?
                    OR business LIKE ?
                LIMIT ?
                """,
                (kw, kw, kw, kw, limit),
            ).fetchall()
            conn.close()
            return [_row_to_dict(r) for r in rows]
        except Exception as e:
            logger.error("CsvDB.search_companies 失敗 keyword=%s: %s", keyword, e)
            return []
    # ── executives: 企業IDから役員一覧を取得 ──────────────
    def get_executives(self, csv_id: str) -> list[dict]:
        """
        csv_id（csv_companies.csv_id）に紐づく役員一覧を返す。

        Returns
        -------
        list[dict]  各要素: {name, role, birth_date, alma_mater, hobby, birthplace}
        """
        if not self.available or not csv_id:
            return []
        try:
            conn = self._connect()
            rows = conn.execute(
                """SELECT name, role, birth_date, alma_mater, hobby, birthplace
                   FROM executives
                   WHERE company_id = ?
                   ORDER BY
                     CASE role
                       WHEN '代表取締役社長' THEN 1
                       WHEN '代表取締役会長' THEN 2
                       WHEN '代表取締役'    THEN 3
                       WHEN '社長'          THEN 4
                       WHEN '会長'          THEN 5
                       WHEN '専務取締役'    THEN 6
                       WHEN '常務取締役'    THEN 7
                       WHEN '取締役'        THEN 8
                       ELSE 99
                     END, name""",
                (csv_id,)
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error("CsvDB.get_executives 失敗 csv_id=%s: %s", csv_id, e)
            return []

    # ── executives: 人名キーワードで会社を検索 ────────────
    def search_by_person(self, name_keyword: str, limit: int = 50) -> list[dict]:
        """
        役員名に name_keyword を含む企業のリストを返す。

        Returns
        -------
        list[dict]  各要素: csv_companies の行 + matched_executives リスト
        """
        if not self.available or not name_keyword:
            return []
        kw = f"%{name_keyword.strip()}%"
        try:
            conn = self._connect()
            rows = conn.execute(
                """SELECT DISTINCT e.company_id, e.name as exec_name, e.role,
                          c.name, c.address, c.tel, c.website,
                          c.capital, c.employees, c.established,
                          c.president, c.business, c.industry,
                          c.description, c.thumbnail_url,
                          c.latitude, c.longitude, c.csv_id
                   FROM executives e
                   LEFT JOIN csv_companies c ON e.company_id = c.csv_id
                   WHERE e.name LIKE ?
                   LIMIT ?""",
                (kw, limit)
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error("CsvDB.search_by_person 失敗 keyword=%s: %s", name_keyword, e)
            return []

    # ── executives: 役員テーブルが存在するか ─────────────
    @property
    def executives_available(self) -> bool:
        """executives テーブルが存在し1件以上あれば True"""
        try:
            conn = self._connect()
            cnt = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='executives'"
            ).fetchone()[0]
            conn.close()
            return cnt > 0
        except Exception:
            return False
