"""
screenshot_lookup.py  ―  社名からスクリーンショット画像を解決するモジュール

【画像の種類と対応】
  ① ローカルJPG（thumbnails/ フォルダ内）
       ファイル名 = CSV の id 列の UUID
       例: thumbnails/00d82f45-c311-42a5-a733-8e18baaaf36f.jpg
       → st.image(ローカルパス) で表示

  ② Supabase URL（CSV の website_thumbnail_url が Supabase 型）
       例: https://…supabase.co/…/thumbnails/{UUID}.jpg
       → ① のローカルファイルと同じ UUID なのでローカル優先
       → ローカルになければ URL を返す

  ③ thum.io URL（CSV の website_thumbnail_url が thum.io 型）
       例: https://image.thum.io/get/…{サイトURL}
       → ローカルファイルなし、URL をそのまま返す

  ④ Playwright スクレイピング画像（screenshot_path）
       → このモジュールは関与しない（app.py 側で優先利用）

【優先順位】
  Playwright画像 ＞ ①ローカルJPG ＞ ②Supabase URL ＞ ③thum.io URL

【使い方】
    from screenshot_lookup import find_screenshot

    path_or_url = find_screenshot(company_name, csv_db)
    if path_or_url:
        st.image(path_or_url, use_container_width=True)
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("screenshot_lookup")

# デフォルトのサムネイルフォルダ（app.py の SCREENSHOT_DIR と別管理）
DEFAULT_THUMBNAILS_DIR = Path("thumbnails")


def find_screenshot(
    company_name: str,
    csv_db,                          # CsvDB インスタンス（型ヒントは循環 import 回避のため省略）
    thumbnails_dir: Path | None = None,
) -> str | None:
    """
    社名から画像のローカルパスまたは URL を返す。

    Parameters
    ----------
    company_name : str
        検索する企業名（表記揺れは CsvDB 内部で吸収）
    csv_db : CsvDB
        csv_search.CsvDB のインスタンス
    thumbnails_dir : Path | None
        thumbnails フォルダのパス。None なら DEFAULT_THUMBNAILS_DIR を使う

    Returns
    -------
    str | None
        - ローカルパス文字列 : st.image(path) でそのまま表示可
        - "https://..." 文字列 : st.image(url) でそのまま表示可
        - None : 画像なし
    """
    if not company_name or csv_db is None or not csv_db.available:
        return None

    t_dir = thumbnails_dir or DEFAULT_THUMBNAILS_DIR

    try:
        co = csv_db.lookup_company(company_name)
    except Exception as e:
        logger.warning("find_screenshot: CsvDB検索失敗 name=%s: %s", company_name, e)
        return None

    if co is None:
        return None

    # ── ① ローカルJPGを確認（最優先）────────────────────────────
    # CSV の id 列 == thumbnails/{id}.jpg のファイル名
    csv_id = co.get("csv_id", "")
    if csv_id:
        local_path = t_dir / f"{csv_id}.jpg"
        if local_path.exists():
            logger.debug("find_screenshot: ローカルJPGヒット name=%s path=%s", company_name, local_path)
            return str(local_path)

    # ── ② thumbnail_url（Supabase / thum.io）────────────────────
    thumb_url = co.get("thumbnail_url", "")
    if thumb_url and thumb_url.startswith("http"):
        logger.debug("find_screenshot: thumbnail_url使用 name=%s url=%s", company_name, thumb_url[:60])
        return thumb_url

    return None


def find_screenshot_by_id(
    csv_id: str,
    thumbnails_dir: Path | None = None,
) -> str | None:
    """
    CSV の id（UUID）が分かっている場合に直接ローカルJPGを探す。
    CsvDB を使わないので高速。

    Parameters
    ----------
    csv_id : str
        CSV の id 列の UUID 文字列
    thumbnails_dir : Path | None
        thumbnails フォルダのパス

    Returns
    -------
    str | None  ローカルパスまたは None
    """
    if not csv_id:
        return None
    t_dir = thumbnails_dir or DEFAULT_THUMBNAILS_DIR
    local_path = t_dir / f"{csv_id}.jpg"
    if local_path.exists():
        return str(local_path)
    return None


def preload_thumbnail_index(
    thumbnails_dir: Path | None = None,
) -> dict[str, str]:
    """
    thumbnails/ フォルダ内の全JPGを走査して {uuid: パス文字列} の辞書を返す。

    起動時に一度だけ呼んでキャッシュしておくと、
    大量の企業を表示するときのファイル存在確認コストをゼロにできる。

    使い方（app.py 起動時）:
        from screenshot_lookup import preload_thumbnail_index
        _thumbnail_index = preload_thumbnail_index()

    表示時:
        path = _thumbnail_index.get(csv_id)
        if path:
            st.image(path, ...)
    """
    t_dir = thumbnails_dir or DEFAULT_THUMBNAILS_DIR
    if not t_dir.exists():
        logger.info("preload_thumbnail_index: フォルダが存在しません: %s", t_dir)
        return {}

    index = {}
    for pattern in ("*.jpg", "*.jpeg", "*.png"):
        for img in t_dir.glob(pattern):
            index[img.stem] = str(img)

    logger.info("preload_thumbnail_index: %d件読み込み完了 dir=%s", len(index), t_dir)
    return index
