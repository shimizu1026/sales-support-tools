# SalesScraper — ポートフォリオ / デモ版（GitHub 用）

このフォルダ **だけ** を GitHub リポジトリにしてください。  
架空データのみ。本番の年鑑・Supabase キーは含みません。

## 初回セットアップ（このフォルダで）

```powershell
copy demo\env.example .env
pip install -r requirements-local.txt
python -m playwright install chromium
python demo/seed_demo_db.py
streamlit run app.py
```

## Streamlit Cloud で公開（ポートフォリオ用）

GitHub に push 後、[share.streamlit.io](https://share.streamlit.io) でデプロイ。  
詳細は [docs/STREAMLIT_CLOUD.md](docs/STREAMLIT_CLOUD.md)

ログイン: ユーザー **demo** または **reviewer**  
検索: フリーワード「**デモ**」

## GitHub に置く

```powershell
cd portfolio
git init
git add .
git commit -m "Initial portfolio demo"
git remote add origin https://github.com/あなたのID/SalesScraper-portfolio.git
git branch -M main
git push -u origin main
```

以降は GitHub 上で編集するか、ローカルで `portfolio/` を直して push。

## 営業版（親フォルダ）から app を反映するとき

親フォルダ（SalesScraper ルート）で:

```powershell
sync_portfolio.bat
```

→ `app.py` などがこのフォルダにコピーされます → ここで commit & push。

## ドキュメント

- [docs/PORTFOLIO.md](docs/PORTFOLIO.md) … 履歴書用文案・スクショ手順
