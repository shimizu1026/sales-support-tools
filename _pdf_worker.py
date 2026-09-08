"""HTML ファイル → PDF ファイル（Streamlit とは別プロセスで Playwright を起動）"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: _pdf_worker.py input.html output.pdf", file=sys.stderr)
        return 2

    html_path = Path(sys.argv[1])
    pdf_path = Path(sys.argv[2])
    html_str = html_path.read_text(encoding="utf-8")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html_str, wait_until="load")
            page.emulate_media(media="print")
            pdf_bytes = page.pdf(
                format="A4",
                landscape=True,
                print_background=True,
                margin={
                    "top": "6mm",
                    "bottom": "6mm",
                    "left": "6mm",
                    "right": "6mm",
                },
            )
        finally:
            browser.close()

    pdf_path.write_bytes(pdf_bytes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
