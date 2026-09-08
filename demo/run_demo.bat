@echo off

cd /d %~dp0..

echo [1/3] デモDB生成...

python demo\seed_demo_db.py

if errorlevel 1 exit /b 1

echo.

echo [2/3] .env 準備...

if not exist .env (

    copy demo\env.example .env

    echo .env を作成しました

) else (

    echo .env は既にあります（上書きしません）

)

echo.

echo [3/3] 起動...

echo   streamlit run app.py

echo.

echo ログイン: ユーザー demo または reviewer を選択

pause

