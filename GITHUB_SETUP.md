# GitHub への移行手順

`portfolio/` フォルダ **だけ** をあなたの GitHub に置きます。

## 1. ローカルで Git を初期化（初回のみ）

PowerShell:

```powershell
cd C:\Users\nichibiWin03\PycharmProjects\SalesScraper\portfolio
git init
git add .
git commit -m "Initial commit: SalesScraper portfolio demo"
```

## 2. GitHub で空のリポジトリを作る

1. [github.com/new](https://github.com/new) を開く
2. Repository name: 例 `SalesScraper-portfolio`
3. **Public** を選ぶ（ポートフォリオ用）
4. 「Add a README」等は **付けない**（空で作成）
5. Create repository

## 3.  push する

GitHub に表示される URL を使う（例）:

```powershell
cd C:\Users\nichibiWin03\PycharmProjects\SalesScraper\portfolio
git branch -M main
git remote add origin https://github.com/あなたのユーザー名/SalesScraper-portfolio.git
git push -u origin main
```

## 4. 以降の編集

**GitHub 上で編集**  
→ ファイルを開いて鉛筆アイコンで編集 → Commit

**PC で編集**  
→ `portfolio/` 内のファイルを直す →:

```powershell
cd portfolio
git add .
git commit -m "説明メッセージ"
git push
```

## 5. 営業版 app.py を反映するとき

親フォルダで:

```powershell
cd C:\Users\nichibiWin03\PycharmProjects\SalesScraper
sync_portfolio.bat
cd portfolio
git add .
git commit -m "Sync app from sales version"
git push
```

## 載せないもの

- `.env`（GitHub に上げない）
- 本番 `nenkan.db`

`.gitignore` 済みです。
