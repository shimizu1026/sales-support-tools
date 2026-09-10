# Streamlit Community Cloud への公開手順

ポートフォリオ閲覧者がブラウザで操作できるデモを無料公開する手順です。

## 前提

- GitHub にこのリポジトリを **Public** で push 済み
- `demo/demo_companies.db` と `demo/demo_crm.db` がコミット済み（`seed_demo_db.py` 実行後）

## 1. Streamlit Cloud にデプロイ

1. [share.streamlit.io](https://share.streamlit.io) に GitHub でログイン
2. **New app** → リポジトリ・ブランチ `main` を選択
3. **Main file path:** `app.py`
4. **Advanced settings → Python version:** `3.12`（`.python-version` と同じ）
5. **Secrets** に `.streamlit/secrets.toml.example` の内容を貼り付け
6. **Deploy**

数分後 `https://<あなたのアプリ名>.streamlit.app` で公開されます。

## 2. microCMS ポートフォリオからリンク

事例ページに次を追加します。

- **デモURL:** 上記 Streamlit の URL
- **操作手順（3行）:**
  1. サイドバーでユーザー `demo` を選んでログイン
  2. フリーワード「デモ」で検索
  3. 担当企業リストでメモを編集

## 3. デモで触れる機能

| 機能 | クラウド |
|------|----------|
| 企業検索（架空45社・全22地域） | ✅ |
| 弱点・技術スタック表示（DB内データ） | ✅ |
| 担当企業リスト・CRMメモ | ✅ |
| 印刷・HTML出力 | ✅ |
| PDF出力 | ✅（WeasyPrint・A4横1社1枚） |
| Web取得ON（Playwright） | ❌ 無効（メモリ制限） |

## 4. ローカルとクラウドの違い

| | ローカル | Streamlit Cloud |
|--|----------|-----------------|
| 設定 | `.env`（`demo/env.example` をコピー） | Secrets（`secrets.toml.example`） |
| Web取得 | 可能（Playwright 要） | 無効 |
| セッションDB | 任意 | `demo/demo_sessions.db`（再デプロイで消える場合あり） |

## 5. 再デプロイ

`main` に push すると自動で再ビルドされます。

```powershell
git add .
git commit -m "Update demo"
git push
```

## 6. トラブルシュート

- **企業DBに接続できません** → Secrets の `NENKAN_DB_PATH` と `SALES_DEMO_MODE=1` を確認
- **ビルド失敗** → `requirements.txt` の依存を確認。ローカル専用は `requirements-local.txt`
- **初回表示が遅い** → 無料枠はスリープ後に起動に数十秒かかることがあります
