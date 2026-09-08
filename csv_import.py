"""
csv_import.py  ―  companies_rows.csv を SQLite に一括取り込むスクリプト

【使い方】
    python csv_import.py                          # デフォルト: nenkan.db に取り込む
    python csv_import.py --db mydata.db           # 別DBファイルを指定
    python csv_import.py --csv companies_rows.csv # CSVファイルパスを指定
    python csv_import.py --replace                # 既存テーブルを丸ごと置き換え（差分ではなく全件再取込）

【テーブル名】
    csv_companies  （nenkan.db の companies / persons テーブルとは別テーブル）

【動作】
    - デフォルトは差分取込（既存レコードは UPSERT でスキップ or 更新）
    - --replace を指定した場合はテーブルを DROP して再作成
    - company_name が空の行はスキップ
    - 実行後に件数サマリーを表示
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd


# ─────────────────────────────────────────────────────
# CSVカラム → DBカラム のマッピング
# （CSVにある列のうち、DBに格納する列だけを定義）
# ─────────────────────────────────────────────────────
COLUMN_MAP = {
    # 識別・基本情報
    "id":                     "csv_id",            # CSV側のUUID（主キー候補）
    "company_name":           "name",              # 企業名（検索キー）
    "name_from_pdf":          "name_pdf",          # PDF読み取り時の表記揺れ名
    "corporate_number":       "corporate_number",  # 法人番号
    "address":                "address",           # 住所
    "tel":                    "tel",               # 電話番号
    "website_url":            "website",           # WebサイトURL
    "website_thumbnail_url":  "thumbnail_url",     # サムネイルURL（Supabase）

    # 企業規模・財務
    "capital":                "capital",           # 資本金（テキスト: "61,425千円"）
    "capital_stock":          "capital_stock",     # 資本金（数値: 61425000）
    "employees":              "employees",         # 従業員数（テキスト: "27人"）
    "employee_count":         "employee_count",    # 従業員数（数値）
    "revenue":                "revenue",           # 売上高（数値）
    "sales_data":             "sales_history",     # 売上推移（タブ区切りテキスト）
    "ordinary_profit":        "ordinary_profit",   # 経常利益
    "net_profit":             "net_profit",        # 純利益

    # 設立・業種
    "established":            "established",       # 設立年月（テキスト: "1946年7月"）
    "established_year":       "established_year",  # 設立年（数値）
    "founded_year":           "founded_year",      # 創業年（数値）
    "industry":               "industry",          # 業種

    # 代表・役員・関連先
    "representative":         "president",         # 代表者名
    "officers":               "officers",          # 役員（テキスト）
    "bank":                   "banks",             # 取引銀行
    "shareholders":           "shareholders",      # 株主（JSON文字列）
    "customers":              "customers",         # 主要顧客
    "suppliers":              "suppliers",         # 仕入先
    "branches":               "branches",          # 支店・営業所
    "factories":              "factories",         # 工場

    # 事業内容・分析
    "business_content":       "business",          # 事業内容
    "description":            "description",       # 企業概要（AI/年鑑）
    "corporate_philosophy":   "philosophy",        # 経営理念
    "ai_analysis":            "ai_analysis",       # AI分析テキスト
    "weak_points":            "weak_points",       # 弱点JSON

    # Web・技術情報
    "tech_stack":             "tech_stack",        # 技術スタック
    "server_info":            "server_info",       # サーバー情報
    "renewal_score":          "renewal_score",     # Web更新スコア
    "domain_registrar":       "domain_registrar",  # ドメイン登録者

    # 株式・上場
    "is_listed":              "is_listed",         # 上場フラグ
    "stock_exchange":         "stock_exchange",    # 上場市場

    # 位置情報
    "latitude":               "latitude",          # 緯度
    "longitude":              "longitude",         # 経度

    # メタ情報
    "data_source":            "data_source",       # データソース名
    "source_type":            "source_type",       # yearbook 等
    "target_status":          "target_status",     # 対応状況
    "created_at":             "created_at",        # レコード作成日時
}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS csv_companies (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    csv_id            TEXT UNIQUE,          -- CSVのUUID（UPSERT用）
    name              TEXT NOT NULL,        -- 企業名
    name_pdf          TEXT,                 -- PDF表記名
    corporate_number  TEXT,                 -- 法人番号
    address           TEXT,
    tel               TEXT,
    website           TEXT,
    thumbnail_url     TEXT,                 -- Supabase上のサムネイル画像URL

    capital           TEXT,                 -- 資本金（テキスト）
    capital_stock     REAL,                 -- 資本金（数値・円）
    employees         TEXT,                 -- 従業員数（テキスト）
    employee_count    REAL,                 -- 従業員数（数値）
    revenue           REAL,                 -- 売上高
    sales_history     TEXT,                 -- 売上推移
    ordinary_profit   REAL,
    net_profit        REAL,

    established       TEXT,                 -- 設立年月（テキスト）
    established_year  REAL,
    founded_year      REAL,
    industry          TEXT,

    president         TEXT,                 -- 代表者
    officers          TEXT,
    banks             TEXT,
    shareholders      TEXT,
    customers         TEXT,
    suppliers         TEXT,
    branches          TEXT,
    factories         TEXT,

    business          TEXT,                 -- 事業内容
    description       TEXT,                 -- 企業概要
    philosophy        TEXT,
    ai_analysis       TEXT,
    weak_points       TEXT,                 -- JSON: {is_non_ssl, is_no_website, is_non_responsive}

    tech_stack        TEXT,
    server_info       TEXT,
    renewal_score     REAL,
    domain_registrar  TEXT,

    ssl_issuer        TEXT,
    ssl_expiry        TEXT,
    whois_expiry      TEXT,
    hosting_company   TEXT,

    is_listed         INTEGER DEFAULT 0,
    stock_exchange    TEXT,

    latitude          REAL,
    longitude         REAL,

    data_source       TEXT,
    source_type       TEXT,
    target_status     TEXT,
    created_at        TEXT,

    imported_at       DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

INDEX_SQLS = [
    "CREATE INDEX IF NOT EXISTS idx_csv_name           ON csv_companies(name)",
    "CREATE INDEX IF NOT EXISTS idx_csv_corporate      ON csv_companies(corporate_number)",
    "CREATE INDEX IF NOT EXISTS idx_csv_name_pdf       ON csv_companies(name_pdf)",
    "CREATE INDEX IF NOT EXISTS idx_csv_industry       ON csv_companies(industry)",
]


def normalize_corporate_number(val) -> str | None:
    """法人番号を13桁ゼロ埋め文字列に統一"""
    if pd.isna(val):
        return None
    try:
        return str(int(float(val))).zfill(13)
    except (ValueError, TypeError):
        return str(val)


def load_csv(csv_path: str) -> pd.DataFrame:
    print(f"[1/4] CSVを読み込み中: {csv_path}")
    df = pd.read_csv(csv_path, low_memory=False)
    print(f"      → {len(df):,} 行 / {len(df.columns)} 列 読み込み完了")

    # company_name が空の行を除外
    before = len(df)
    df = df[df["company_name"].notna() & (df["company_name"].str.strip() != "")]
    dropped = before - len(df)
    if dropped:
        print(f"      → 企業名が空の行を {dropped:,} 件スキップ")

    return df


def build_records(df: pd.DataFrame) -> list[dict]:
    """DataFrameをDBレコードのリストに変換"""
    print("[2/4] レコードを変換中...")
    records = []
    for _, row in df.iterrows():
        rec = {}
        for csv_col, db_col in COLUMN_MAP.items():
            val = row.get(csv_col)
            if pd.isna(val):
                rec[db_col] = None
            else:
                rec[db_col] = val

        # 法人番号の正規化（浮動小数点 → 13桁ゼロ埋め文字列）
        rec["corporate_number"] = normalize_corporate_number(row.get("corporate_number"))

        # is_listed を 0/1 に変換
        raw_listed = row.get("is_listed")
        if pd.isna(raw_listed):
            rec["is_listed"] = 0
        else:
            rec["is_listed"] = 1 if str(raw_listed).lower() in ("true", "1", "yes") else 0

        records.append(rec)

    print(f"      → {len(records):,} 件変換完了")
    return records


def import_to_db(records: list[dict], db_path: str, replace: bool) -> None:
    print(f"[3/4] DBに書き込み中: {db_path}")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    if replace:
        print("      → --replace 指定: 既存テーブルをDROPして再作成")
        cur.execute("DROP TABLE IF EXISTS csv_companies")

    cur.execute(CREATE_TABLE_SQL)
    for sql in INDEX_SQLS:
        cur.execute(sql)

    cols = list(records[0].keys())
    placeholders = ", ".join(["?" for _ in cols])
    col_names = ", ".join(cols)
    upsert_sql = (
        f"INSERT INTO csv_companies ({col_names}) VALUES ({placeholders}) "
        f"ON CONFLICT(csv_id) DO UPDATE SET "
        + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "csv_id")
    )

    inserted = 0
    skipped = 0
    batch = []
    BATCH_SIZE = 2000

    for rec in records:
        vals = [rec[c] for c in cols]
        batch.append(vals)
        if len(batch) >= BATCH_SIZE:
            cur.executemany(upsert_sql, batch)
            inserted += cur.rowcount
            batch = []

    if batch:
        cur.executemany(upsert_sql, batch)
        inserted += cur.rowcount

    conn.commit()

    total = cur.execute("SELECT COUNT(*) FROM csv_companies").fetchone()[0]
    conn.close()
    print(f"      → UPSERT完了（処理行数: {inserted:,} / DB総件数: {total:,}）")


def print_summary(db_path: str) -> None:
    print("[4/4] 取込結果サマリー")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    total     = cur.execute("SELECT COUNT(*) FROM csv_companies").fetchone()[0]
    with_web  = cur.execute("SELECT COUNT(*) FROM csv_companies WHERE website IS NOT NULL").fetchone()[0]
    with_cap  = cur.execute("SELECT COUNT(*) FROM csv_companies WHERE capital IS NOT NULL").fetchone()[0]
    with_emp  = cur.execute("SELECT COUNT(*) FROM csv_companies WHERE employees IS NOT NULL").fetchone()[0]
    with_thumb= cur.execute("SELECT COUNT(*) FROM csv_companies WHERE thumbnail_url IS NOT NULL").fetchone()[0]
    with_desc = cur.execute("SELECT COUNT(*) FROM csv_companies WHERE description IS NOT NULL").fetchone()[0]
    listed    = cur.execute("SELECT COUNT(*) FROM csv_companies WHERE is_listed=1").fetchone()[0]

    conn.close()

    print(f"""
    ┌─────────────────────────────────────────┐
    │  csv_companies テーブル 取込完了         │
    ├─────────────────────────────────────────┤
    │  総件数          : {total:>10,} 件       │
    │  Webサイトあり   : {with_web:>10,} 件   │
    │  資本金データあり: {with_cap:>10,} 件   │
    │  従業員数あり    : {with_emp:>10,} 件   │
    │  サムネあり      : {with_thumb:>10,} 件 │
    │  企業概要あり    : {with_desc:>10,} 件  │
    │  上場企業        : {listed:>10,} 件     │
    └─────────────────────────────────────────┘
    """)


# ─────────────────────────────────────────────────────
# executives テーブル（役員・人名）
# ─────────────────────────────────────────────────────

EXEC_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS executives (
    id               TEXT PRIMARY KEY,   -- executives_rows.csv の id
    company_id       TEXT,               -- csv_companies.csv_id と紐づく
    company_name_raw TEXT,               -- PDF読み取り時の表記
    name             TEXT NOT NULL,      -- 氏名
    role             TEXT,               -- 役職
    birth_date       TEXT,               -- 生年月日（例: 1969.9.12）
    alma_mater       TEXT,               -- 出身校
    hobby            TEXT,               -- 趣味
    birthplace       TEXT,               -- 出身地
    imported_at      DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

EXEC_INDEX_SQLS = [
    "CREATE INDEX IF NOT EXISTS idx_exec_company_id ON executives(company_id)",
    "CREATE INDEX IF NOT EXISTS idx_exec_name       ON executives(name)",
    "CREATE INDEX IF NOT EXISTS idx_exec_role       ON executives(role)",
]


def load_exec_csv(csv_path: str) -> pd.DataFrame:
    print(f"[1/3] 役員CSVを読み込み中: {csv_path}")
    df = pd.read_csv(csv_path, low_memory=False)
    before = len(df)
    df = df[df["name"].notna() & (df["name"].str.strip() != "")]
    print(f"      → {len(df):,} 行（氏名なし {before - len(df)} 件スキップ）")
    return df


def import_executives(csv_path: str, db_path: str, replace: bool) -> None:
    df = load_exec_csv(csv_path)

    print(f"[2/3] 役員DBに書き込み中: {db_path}")
    conn = sqlite3.connect(db_path)
    cur  = conn.cursor()

    if replace:
        print("      → --replace: executives テーブルをDROPして再作成")
        cur.execute("DROP TABLE IF EXISTS executives")

    cur.execute(EXEC_CREATE_SQL)
    for sql in EXEC_INDEX_SQLS:
        cur.execute(sql)

    upsert_sql = """
        INSERT INTO executives
            (id, company_id, company_name_raw, name, role,
             birth_date, alma_mater, hobby, birthplace)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            company_id       = excluded.company_id,
            company_name_raw = excluded.company_name_raw,
            name             = excluded.name,
            role             = excluded.role,
            birth_date       = excluded.birth_date,
            alma_mater       = excluded.alma_mater,
            hobby            = excluded.hobby,
            birthplace       = excluded.birthplace
    """

    batch = []
    for _, row in df.iterrows():
        batch.append((
            row.get("id")               if pd.notna(row.get("id"))               else None,
            row.get("company_id")       if pd.notna(row.get("company_id"))       else None,
            row.get("company_name_raw") if pd.notna(row.get("company_name_raw")) else None,
            str(row.get("name", "")).strip(),
            row.get("role")             if pd.notna(row.get("role"))             else None,
            row.get("birth_date")       if pd.notna(row.get("birth_date"))       else None,
            row.get("alma_mater")       if pd.notna(row.get("alma_mater"))       else None,
            row.get("hobby")            if pd.notna(row.get("hobby"))            else None,
            row.get("birthplace")       if pd.notna(row.get("birthplace"))       else None,
        ))
        if len(batch) >= 500:
            cur.executemany(upsert_sql, batch)
            batch = []
    if batch:
        cur.executemany(upsert_sql, batch)

    conn.commit()
    total = cur.execute("SELECT COUNT(*) FROM executives").fetchone()[0]

    # ── company_id が NULL の行を company_name_raw で名寄せ補完 ──
    print("      → company_id NULL 行を名寄せ中…")
    null_rows = cur.execute(
        "SELECT id, company_name_raw FROM executives "
        "WHERE company_id IS NULL AND company_name_raw IS NOT NULL"
    ).fetchall()

    def _normalize_company(name: str) -> str:
        import re as _re
        # 法人格の括弧表記を展開
        name = _re.sub(r"[（(]株[）)]", "株式会社", name)
        name = _re.sub(r"[（(]有[）)]", "有限会社", name)
        name = name.replace("㈱", "株式会社").replace("㈲", "有限会社")
        # 役職が末尾や途中に混入している場合の除去
        # 例: 「創建工業(株)代表取締役社長」→「創建工業株式会社」
        for suffix in [
            "代表取締役社長", "代表取締役会長", "代表取締役", "取締役会長",
            "取締役社長", "取締役", "代表社員", "監査役", "代表理事",
            "社長", "会長", "専務", "常務", "執行役員",
        ]:
            name = name.replace(suffix, "")
        return name.strip()

    linked = 0
    for exec_id, raw in null_rows:
        norm = _normalize_company(raw or "")
        if not norm:
            continue
        match = cur.execute(
            "SELECT csv_id FROM csv_companies WHERE name=? LIMIT 1", (norm,)
        ).fetchone()
        if not match:
            match = cur.execute(
                "SELECT csv_id FROM csv_companies WHERE name LIKE ? LIMIT 1",
                (f"%{norm}%",)
            ).fetchone()
        if match:
            cur.execute(
                "UPDATE executives SET company_id=? WHERE id=?", (match[0], exec_id)
            )
            linked += 1
    conn.commit()
    print(f"      → 名寄せ補完: {linked:,}件 / {len(null_rows):,}件")

    conn.close()
    print(f"      → UPSERT完了（DB総件数: {total:,}）")

    print("[3/3] 役員DB サマリー")
    conn = sqlite3.connect(db_path)
    cur  = conn.cursor()
    with_co  = cur.execute("SELECT COUNT(*) FROM executives WHERE company_id IS NOT NULL").fetchone()[0]
    top_roles = cur.execute(
        "SELECT role, COUNT(*) as cnt FROM executives GROUP BY role ORDER BY cnt DESC LIMIT 5"
    ).fetchall()
    conn.close()
    print(f"""
    ┌─────────────────────────────────────────┐
    │  executives テーブル 取込完了            │
    ├─────────────────────────────────────────┤
    │  総件数          : {total:>10,} 件       │
    │  企業紐づけあり  : {with_co:>10,} 件    │
    └─────────────────────────────────────────┘
    """)
    print("  役職TOP5:")
    for role, cnt in top_roles:
        print(f"    {role:<20s}: {cnt:,}件")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="companies_rows.csv / executives_rows.csv を SQLite に取り込む"
    )
    parser.add_argument("--csv",      default="companies_rows.csv",  help="企業CSVファイルパス")
    parser.add_argument("--exec-csv", default="executives_rows.csv", help="役員CSVファイルパス")
    parser.add_argument("--db",       default="nenkan.db",           help="SQLiteファイルパス")
    parser.add_argument("--replace",  action="store_true",           help="既存テーブルを丸ごと置き換え")
    parser.add_argument("--skip-companies", action="store_true",     help="企業テーブルの取込をスキップ")
    parser.add_argument("--skip-executives", action="store_true",    help="役員テーブルの取込をスキップ")
    args = parser.parse_args()

    # ── 企業テーブル ──
    if not args.skip_companies:
        if not Path(args.csv).exists():
            print(f"エラー: 企業CSVが見つかりません: {args.csv}", file=sys.stderr)
            sys.exit(1)
        df      = load_csv(args.csv)
        records = build_records(df)
        import_to_db(records, args.db, args.replace)
        print_summary(args.db)

    # ── 役員テーブル ──
    if not args.skip_executives:
        if not Path(args.exec_csv).exists():
            print(f"エラー: 役員CSVが見つかりません: {args.exec_csv}", file=sys.stderr)
            sys.exit(1)
        import_executives(args.exec_csv, args.db, args.replace)

    print("✅ 全取込完了")


if __name__ == "__main__":
    main()
