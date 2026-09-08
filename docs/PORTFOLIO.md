# SalesScraper — ポートフォリオ用素材

営業チーム向け **社内Webアプリ** の事例。企業年鑑DBは非公開のため、公開版は架空デモデータのみ。

---

## 1行概要（履歴書・LinkedIn用）

> Python / Streamlit で営業リスト検索・Webサイト分析・チームCRM・印刷出力を一体化した社内ツールを設計・開発（利用者：営業チーム数名）

---

## 課題 → 解決

| 課題 | 解決 |
|------|------|
| 企業情報が Excel・年鑑・Web に分散 | SQLite 検索＋Web自動取得で1画面に集約 |
| フォロー状況が個人メモのまま | Supabase で担当企業リストをチーム共有 |
| 訪問前資料作成に時間 | HTML/PDF を1社単位で出力 |

---

## 技術スタック

| 層 | 技術 |
|----|------|
| UI | Streamlit |
| DB | SQLite（企業マスタ）、Supabase（CRM） |
| 取得 | Playwright、requests |
| AI | Ollama / Gemini（サイトテキストから会社概要抽出） |
| その他 | Folium（地図）、python-whois |

---

## 自分が担当した機能（アピールポイント）

1. **Web取得パイプライン** … スクショ・技術スタック・弱点判定・AI/正規表現の二段構え
2. **担当企業リスト（CRM）** … 検索画面から更新→未登録なら自動追加、担当者・ステータス・メモ
3. **UX改善** … CRM を `st.form` 化し、ステータス変更時の全画面リロードを抑制
4. **マルチPC運用** … NAS 上 SQLite ＋ ローカルサムネの運用設計（ポートフォリオではデモDBに置換）

---

## 公開リポジトリ構成（推奨）

```
SalesScraper-portfolio/          # 本番リポジトリとは別名推奨
├── app.py                       # UI（機密コメント削除済み）
├── scraper.py
├── saved_list.py
├── csv_search.py
├── demo/
│   ├── seed_demo_db.py          # 架空15社生成
│   ├── demo_companies.db        # git に含めてOK（生成後）
│   ├── env.example
│   └── README.md
├── docs/
│   └── PORTFOLIO.md             # 本ファイル
├── requirements.txt
└── README.md                    # デモ起動へのリンクのみ
```

**Git に入れない:** `.env`、`nenkan.db`、`thumbnails/`（実データ）、`salescaper.log`

---

## スクショ・動画チェックリスト（3枚＋1本）

| # | 内容 | 注意 |
|---|------|------|
| 1 | 検索結果（デモ社名のみ） | 実在社名・メモを写さない |
| 2 | Web弱点・設立日が出たカード | 「Web取得ON」の効果 |
| 3 | 担当企業リスト＋担当者フィルタ | チーム共有の説明用 |
| 動画 | 上記を90秒 | 音声なしでも可 |

---

## デモ用 Supabase（任意）

CRM まで見せる場合:

1. 無料枠で **デモ専用** プロジェクトを新規作成
2. `saved_companies` テーブル（本番スキーマと同型）
3. `.env` は `demo/env.example` からコピー

CRM なしで見せる場合: **`SALES_DEMO_MODE=1`**（`demo/env.example`）でローカル SQLite CRM を使用。**実装済み。**

Supabase で CRM まで見せる場合: `demo/supabase_schema.sql` をデモプロジェクトで実行。

---

## 面接・説明用トーク（30秒）

「営業がリストアップから訪問準備まで行き来していた業務を、Streamlit の1アプリにまとめました。企業DBは SQLite、フォロー管理は Supabase でチーム共有です。Web取得では Playwright でサイトを見に行き、SSL やスマホ対応などの弱点を自動判定します。年鑑データは社内秘のため、ポートフォリオでは架空15社のデモDBで再現しています。」

---

## よくある質問（想定）

**Q. なぜ Streamlit？**  
A. 営業チームが Python を触らずブラウザだけで使える。社内ツールの MVP に最適だった。

**Q. 年鑑データは？**  
A. 商用の企業年鑑由来のため公開不可。スキーマと検索ロジックはデモDBで示している。

**Q. 一番大変だった点？**  
A. Ollama の応答遅延で Web 取得がタイムアウトする問題。待ち時間延長と正規表現フォールバックで対処。

---

## 次のアクション

- [x] `python demo/seed_demo_db.py` を実行
- [x] デモモード（`SALES_DEMO_MODE=1`）で Supabase なし起動
- [ ] 公開用 GitHub リポジトリを作成し、`demo/` と `docs/` を push
- [ ] スクショ3枚・動画1本を撮影（手順は `demo/README.md` の90秒デモ）
- [ ] ポートフォリオサイトに「GitHub」「デモ手順」「技術スタック」を掲載
