# SalesScraper デモ版 — 起動手順（5分）



ポートフォリオ閲覧者・採用担当向け。企業年鑑の実データは含みません。



## 最短手順



```powershell

python demo/seed_demo_db.py

copy demo\env.example .env

streamlit run app.py

```



`demo\run_demo.bat` でも DB 生成まで自動化できます。



## 生成されるファイル



| ファイル | 内容 |

|---------|------|

| `demo/demo_companies.db` | 架空企業 45 社（全22地域カバー） |

| `demo/demo_crm.db` | 担当企業リスト サンプル 3 件 |



## ログイン



ユーザー **demo** または **reviewer** を選ぶ（seed で登録済み）。



## 90秒デモの流れ



1. フリーワード「**デモ**」で検索

2. 1社を開き Web取得 ON（任意）

3. サイドバー **担当企業リスト** → サンプル3社を確認

4. メモを編集 → **💾 更新**

5. カード右「印刷・共有（この会社）」→ HTML



## 2つの運用モード



| モード | 設定 | CRM |

|--------|------|-----|

| **ローカルデモ（推奨）** | `SALES_DEMO_MODE=1` | `demo/demo_crm.db` |

| Supabase デモ | `SALES_DEMO_MODE=0` + URL/key | `demo/supabase_schema.sql` を実行 |



## 含めないもの



- `nenkan.db`（企業年鑑実データ）

- NAS パス・本番 Supabase キー

- 実在企業のスクショ・メモ

