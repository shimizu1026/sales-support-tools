"""
ポートフォリオ用デモDBを生成する。

使い方（プロジェクトルートで）:
    python demo/seed_demo_db.py
    python demo/seed_demo_db.py --out demo/demo_companies.db

生成物:
    - csv_companies テーブル（架空企業 15 社）
    - executives テーブル（空。検索SQL互換用）
    - 企業年鑑の実データは一切含まない
"""
from __future__ import annotations

import argparse
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys_path_insert = ROOT  # noqa: F841 — 実行時に csv_import を import するため

import sys

sys.path.insert(0, str(ROOT))

from csv_import import CREATE_TABLE_SQL, INDEX_SQLS, EXEC_CREATE_SQL, EXEC_INDEX_SQLS  # noqa: E402

# すべて架空。実在企業・年鑑データとの対応は意図的にない。
DEMO_COMPANIES: list[dict] = [
    {
        "name": "株式会社デモ商事",
        "address": "広島県広島市中区デモ町1-2-3",
        "tel": "082-000-1001",
        "website": "https://example.com",
        "president": "デモ 太郎",
        "capital": "3,000万円",
        "employees": "45名",
        "established": "1985年4月",
        "industry": "商社・卸売",
        "business": "日用品・資材の卸売",
        "philosophy": "地域とともに歩む商社を目指します。",
        "banks": "広島デモ銀行",
        "customers": "小売店、公共機関",
        "suppliers": "国内メーカー各社",
        "sales_history": "23.10\t120000\t8500\t6200\n22.10\t115000\t7800\t5900",
        "source_type": "yearbook",
        "latitude": 34.3853,
        "longitude": 132.4553,
        "tech_stack": '{"cms":"WordPress","technologies":["WordPress"],"detected":["WordPress"]}',
        "server_info": "SAKURA Internet Inc.",
        "weak_points": '{"is_non_ssl":false,"is_no_website":false,"is_non_responsive":true}',
    },
    {
        "name": "有限会社サンプル工業",
        "address": "広島県福山市サンプル南2-8-1",
        "tel": "084-000-2002",
        "website": "https://www.w3.org",
        "president": "サンプル 花子",
        "capital": "500万円",
        "employees": "12名",
        "established": "2001年6月",
        "industry": "金属加工",
        "business": "精密部品の切削加工",
        "philosophy": "ものづくりで地域産業を支える。",
        "source_type": "yearbook",
        "latitude": 34.4859,
        "longitude": 133.3625,
    },
    {
        "name": "株式会社テックフロンティア",
        "address": "広島県東広島市研究学園1-1-1",
        "tel": "082-000-3003",
        "website": "https://streamlit.io",
        "president": "技術 一郎",
        "capital": "1億円",
        "employees": "78名",
        "established": "2010年3月",
        "industry": "情報通信",
        "business": "業務システム開発・Web制作",
        "philosophy": "技術で営業現場を効率化する。",
        "banks": "中国デモ銀行",
        "source_type": "yearbook",
        "tech_stack": '{"cms":"不明","technologies":["React"],"detected":["React"]}',
        "renewal_score": 8.0,
    },
    {
        "name": "株式会社グリーンライフ",
        "address": "広島県広島市西区緑が丘3-4-5",
        "tel": "082-000-4004",
        "website": "情報なし",
        "president": "環境 次郎",
        "capital": "2,000万円",
        "employees": "30名",
        "established": "1998年11月",
        "industry": "建設・リフォーム",
        "business": "住宅リフォーム・外構工事",
        "source_type": "yearbook",
        "weak_points": '{"is_non_ssl":false,"is_no_website":true,"is_non_responsive":false}',
    },
    {
        "name": "株式会社北洋物流",
        "address": "広島県呉市港町5-6-7",
        "tel": "0823-00-5005",
        "website": "https://example.org",
        "president": "物流 三郎",
        "capital": "8,000万円",
        "employees": "120名",
        "established": "1975年2月",
        "industry": "運輸・物流",
        "business": "一般貨物自動車運送",
        "banks": "呉デモ信用金庫",
        "customers": "製造業、商社",
        "source_type": "yearbook",
        "sales_history": "23.10\t980000\t42000\t31000\n22.10\t920000\t38000\t28000",
    },
    {
        "name": "株式会社フードプラス",
        "address": "広島県広島市南区食都1-1",
        "tel": "082-000-6006",
        "website": "https://example.net",
        "president": "食品 美咲",
        "capital": "5,000万円",
        "employees": "200名",
        "established": "1967年10月",
        "industry": "食品製造",
        "business": "惣菜・弁当の製造販売",
        "philosophy": "安全でおいしい食を届ける。",
        "source_type": "yearbook",
    },
    {
        "name": "有限会社アート印刷",
        "address": "広島県広島市中区紙町2-2",
        "tel": "082-000-7007",
        "website": "https://www.python.org",
        "president": "印刷 健",
        "capital": "800万円",
        "employees": "18名",
        "established": "1992年8月",
        "industry": "印刷",
        "business": "チラシ・パンフレット印刷",
        "source_type": "yearbook",
        "server_info": "Xserver Inc.",
    },
    {
        "name": "株式会社メディカルサポート",
        "address": "広島県広島市安佐南区医療台4-1",
        "tel": "082-000-8008",
        "website": "https://example.com",
        "president": "医療 理恵",
        "capital": "4,500万円",
        "employees": "65名",
        "established": "2005年1月",
        "industry": "医療・福祉",
        "business": "介護施設運営支援",
        "source_type": "yearbook",
    },
    {
        "name": "株式会社ワカバデモ",
        "address": "広島市西区商工センター2丁目13-14",
        "tel": "082-000-9009",
        "website": "https://example.com",
        "president": "若葉 正記",
        "capital": "1,600万円",
        "employees": "150名",
        "established": "1967年10月",
        "industry": "設備工事",
        "business": "空調・電気設備工事（デモ用架空データ）",
        "source_type": "manual",
        "philosophy": "より豊かで充実した暮らしのために。",
    },
    {
        "name": "株式会社新規登録デモ",
        "address": "広島県広島市中区本通10-1",
        "tel": "082-000-1010",
        "website": "https://streamlit.io",
        "president": "新規 登録",
        "source_type": "manual",
    },
    {
        "name": "株式会社セキュアシステム",
        "address": "広島県広島市安佐北区ITパーク6-6",
        "tel": "082-000-1111",
        "website": "http://example.com",
        "president": "安全 守",
        "capital": "3,200万円",
        "employees": "42名",
        "established": "2015年5月",
        "industry": "情報通信",
        "business": "セキュリティ監視・ネットワーク構築",
        "source_type": "yearbook",
        "weak_points": '{"is_non_ssl":true,"is_no_website":false,"is_non_responsive":false}',
    },
    {
        "name": "有限会社山田建設",
        "address": "広島県三次市山田町1-1",
        "tel": "0824-00-1212",
        "website": "https://example.com",
        "president": "山田 工",
        "capital": "1,000万円",
        "employees": "25名",
        "established": "1988年3月",
        "industry": "建設",
        "business": "土木・舗装工事",
        "officers": "（代）山田 工",
        "source_type": "yearbook",
    },
    {
        "name": "株式会社オーシャントレード",
        "address": "広島県尾道市海辺3-3",
        "tel": "0848-00-1313",
        "website": "https://www.w3.org",
        "president": "海野 航",
        "capital": "6,000万円",
        "employees": "88名",
        "established": "1970年7月",
        "industry": "商社",
        "business": "海産物・食品の輸出入",
        "is_listed": 0,
        "source_type": "yearbook",
    },
    {
        "name": "株式会社クリエイトラボ",
        "address": "広島県広島市中区創造通2-5",
        "tel": "082-000-1414",
        "website": "https://example.com",
        "president": "創造 翔",
        "capital": "500万円",
        "employees": "8名",
        "established": "2019年9月",
        "industry": "デザイン",
        "business": "Webデザイン・ブランディング",
        "source_type": "manual",
    },
    {
        "name": "株式会社北洋ホテルズ",
        "address": "広島県広島市中区堀川町1-1",
        "tel": "082-000-1515",
        "website": "https://example.com",
        "president": "宿泊 佳子",
        "capital": "2億円",
        "employees": "310名",
        "established": "1955年12月",
        "industry": "サービス",
        "business": "ホテル・宴会運営",
        "banks": "広島デモ銀行、中国デモ銀行",
        "customers": "法人旅行、婚礼",
        "source_type": "yearbook",
        "sales_history": "23.10\t450000\t28000\t18000",
    },
]

COLUMNS = [
    "csv_id", "name", "address", "tel", "website",
    "president", "capital", "employees", "established", "industry", "business",
    "philosophy", "banks", "customers", "suppliers", "officers", "sales_history",
    "tech_stack", "server_info", "weak_points", "renewal_score",
    "latitude", "longitude", "source_type", "data_source", "created_at", "is_listed",
]


def seed(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(CREATE_TABLE_SQL)
        for sql in INDEX_SQLS:
            conn.execute(sql)
        conn.execute(EXEC_CREATE_SQL)
        for sql in EXEC_INDEX_SQLS:
            conn.execute(sql)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for row in DEMO_COMPANIES:
            record = {col: None for col in COLUMNS}
            record["csv_id"] = str(uuid.uuid4())
            record["data_source"] = "portfolio_demo"
            record["created_at"] = now
            record["is_listed"] = row.get("is_listed", 0)
            for key, val in row.items():
                if key in record:
                    record[key] = val

            placeholders = ", ".join("?" * len(COLUMNS))
            cols = ", ".join(COLUMNS)
            conn.execute(
                f"INSERT INTO csv_companies ({cols}) VALUES ({placeholders})",
                [record[c] for c in COLUMNS],
            )

        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM csv_companies").fetchone()[0]
        print(f"OK: {db_path} に {count} 社（架空データ）を作成しました。")
    finally:
        conn.close()


def seed_crm(crm_path: Path) -> None:
    """担当企業リスト用サンプル（スクショ撮影向け）。"""
    from demo.crm_local import _CREATE_SAVED, _CREATE_USERS

    crm_path.parent.mkdir(parents=True, exist_ok=True)
    if crm_path.exists():
        crm_path.unlink()

    now = datetime.now(timezone.utc).isoformat()
    samples = [
        {
            "username": "demo",
            "company_name": "株式会社デモ商事",
            "address": DEMO_COMPANIES[0]["address"],
            "tel": DEMO_COMPANIES[0]["tel"],
            "website_url": DEMO_COMPANIES[0]["website"],
            "industry": DEMO_COMPANIES[0]["industry"],
            "crm_status": "商談中",
            "crm_assignee": "demo",
            "crm_memo": "来週アポ。WebサイトのSSL対応を提案予定。",
            "saved_at": now,
            "crm_updated_at": now,
        },
        {
            "username": "reviewer",
            "company_name": "有限会社サンプル工業",
            "address": DEMO_COMPANIES[1]["address"],
            "industry": DEMO_COMPANIES[1]["industry"],
            "crm_status": "架電済み",
            "crm_assignee": "reviewer",
            "crm_memo": "担当者不在。再架電 8/20。",
            "saved_at": now,
            "crm_updated_at": now,
        },
        {
            "username": "demo",
            "company_name": "株式会社テックフロンティア",
            "address": DEMO_COMPANIES[2]["address"],
            "industry": DEMO_COMPANIES[2]["industry"],
            "crm_status": "アポ取得",
            "crm_assignee": "demo",
            "crm_memo": "",
            "saved_at": now,
            "crm_updated_at": now,
        },
    ]

    conn = sqlite3.connect(str(crm_path))
    try:
        conn.executescript(_CREATE_SAVED + _CREATE_USERS)
        for u in ("demo", "reviewer"):
            conn.execute("INSERT INTO users(username) VALUES (?)", (u,))
        for row in samples:
            keys = list(row.keys())
            placeholders = ", ".join("?" * len(keys))
            conn.execute(
                f"INSERT INTO saved_companies ({', '.join(keys)}) VALUES ({placeholders})",
                [row[k] for k in keys],
            )
        conn.commit()
        print(f"OK: {crm_path} に CRM サンプル {len(samples)} 件を作成しました。")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="ポートフォリオ用デモDB生成")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).parent / "demo_companies.db"),
        help="出力SQLiteパス",
    )
    parser.add_argument(
        "--crm-out",
        default=str(Path(__file__).parent / "demo_crm.db"),
        help="デモCRM SQLiteパス",
    )
    parser.add_argument(
        "--skip-crm",
        action="store_true",
        help="CRMサンプルを生成しない",
    )
    args = parser.parse_args()
    seed(Path(args.out))
    if not args.skip_crm:
        seed_crm(Path(args.crm_out))


if __name__ == "__main__":
    main()
