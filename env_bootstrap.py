"""Streamlit Cloud / ローカル共通の環境変数初期化。"""
from __future__ import annotations

import os
from pathlib import Path

_DEMO_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _DEMO_TRUTHY


def _apply_streamlit_secrets() -> None:
    try:
        import streamlit as st
    except ImportError:
        return
    try:
        for key in st.secrets:
            value = st.secrets[key]
            if isinstance(value, str):
                os.environ.setdefault(key, value)
            elif hasattr(value, "keys"):
                for sub_key, sub_val in value.items():
                    if isinstance(sub_val, str):
                        os.environ.setdefault(str(sub_key), sub_val)
    except Exception:
        pass


def _resolve_relative_paths(app_dir: Path) -> None:
    for key in ("NENKAN_DB_PATH", "DEMO_CRM_DB_PATH", "SALES_DATA_DB_PATH"):
        val = os.getenv(key, "").strip()
        if not val:
            continue
        path = Path(val)
        if not path.is_absolute():
            os.environ[key] = str((app_dir / path).resolve())


def bootstrap_env(app_dir: Path | None = None) -> None:
    root = app_dir or Path(__file__).resolve().parent
    from dotenv import load_dotenv

    load_dotenv(root / ".env")
    _apply_streamlit_secrets()

    if _is_truthy(os.getenv("SALES_DEMO_MODE")):
        for key, default in {
            "NENKAN_DB_PATH": "demo/demo_companies.db",
            "DEMO_CRM_DB_PATH": "demo/demo_crm.db",
            "SALES_DATA_DB_PATH": "demo/demo_sessions.db",
        }.items():
            os.environ.setdefault(key, default)

    if _is_truthy(os.getenv("STREAMLIT_CLOUD_DEMO")):
        os.environ.setdefault("DISABLE_WEB_SCRAPING", "1")

    _resolve_relative_paths(root)


def is_demo_mode() -> bool:
    return _is_truthy(os.getenv("SALES_DEMO_MODE"))


def is_cloud_deploy() -> bool:
    return _is_truthy(os.getenv("STREAMLIT_CLOUD_DEMO"))


def is_web_scraping_disabled() -> bool:
    return _is_truthy(os.getenv("DISABLE_WEB_SCRAPING"))
