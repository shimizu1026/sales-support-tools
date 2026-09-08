"""
検索結果を印刷・スマホ閲覧向けのスタンドアロン HTML に出力する。
"""

from __future__ import annotations

import base64
import html
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd


# ── app.py から独立（循環インポート防止）────────────────────────────

def parse_sales_history(raw: str) -> list[dict]:
    """sales_history テキスト（タブ区切り）を解析して直近3件を返す。"""
    if not raw:
        return []

    def _to_man(text: str):
        t = re.sub(r"[（(][^)）]*[)）]", "", text).strip()
        if not t or t.lower() in ("null", "－", "-", ""):
            return None
        digits = re.search(r"[\d,]+", t.replace(",", ""))
        if not digits:
            return None
        prefix = t[: digits.start()]
        negative = (
            t.startswith(("▲", "△", "−", "－"))
            or any(m in prefix for m in ("▲", "△", "−", "－"))
        )
        val = int(digits.group().replace(",", "")) / 10
        return -val if negative else val

    results = []
    for line in raw.strip().split("\n")[1:]:
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        period = cols[0].strip()
        if not period:
            continue
        sales_man = _to_man(cols[1]) if len(cols) > 1 else None
        profit_man = _to_man(cols[2]) if len(cols) > 2 else None
        pure_profit = _to_man(cols[3]) if len(cols) > 3 else None
        if sales_man is None:
            continue
        results.append({
            "period": period,
            "sales_man": sales_man,
            "profit_man": profit_man,
            "pure_profit": pure_profit,
        })
    return results[-3:]


def fmt_man(val: float) -> str:
    """万円 → 読みやすい文字列（億円 / 万円）"""
    if val >= 10000:
        return f"{val / 10000:.1f}億円"
    return f"{val:,.0f}万円"


_WP_DEFS = [
    ("hp_none", 5, "HPなし", "#C0392B",
     "公式サイトがないと信頼性・集客で大きく損をしています。作成の第一歩をご提案できます。"),
    ("non_ssl", 3, "ノンSSL", "#E67E22",
     "常時SSL化されていないため、Googleの検索順位で不利になっています。対策はすぐできます。"),
    ("non_resp", 3, "ノンレスポンシブ", "#E67E22",
     "スマホからアクセスすると表示が崩れます。今やスマホ比率は6割超です。"),
    ("no_career", 2, "採用ページなし", "#8E44AD",
     "採用コンテンツがないと求職者が離れます。採用LP作成をご提案できます。"),
    ("old_server", 1, "旧来サーバー環境", "#7F8C8D",
     "CMS・サーバー情報が古く、セキュリティや表示速度に課題がある可能性があります。"),
]
_WP_MAX_STARS = sum(d[1] for d in _WP_DEFS)


def _valid_website_for_screenshot(url: str) -> bool:
    u = (url or "").strip()
    if not u or u in ("情報なし", "不明"):
        return False
    try:
        from scraper import is_blocked_url
        return not is_blocked_url(u)
    except ImportError:
        return True


def calc_weakpoints(row) -> tuple[list[tuple], int]:
    url = str(row.get("WebサイトURL", "") or "").strip()

    _wpc = {}
    try:
        _wpc = json.loads(row.get("_weak_points", "") or "{}")
    except Exception:
        pass

    resp_val = row.get("レスポンシブ", None)
    page_text = str(row.get("page_text", "") or "").lower()
    cms_val = str(row.get("CMS", "") or "").strip()
    hosting = str(row.get("hosting_company", "") or "").strip()
    renewal = row.get("_renewal_score")

    if not hosting or hosting == "不明":
        _si = str(row.get("_server_info", "") or "")
        _om = re.search(r"Org:\s*([^\xb7|]+)", _si)
        if _om:
            hosting = _om.group(1).strip()

    found: dict[str, bool] = {}
    found["hp_none"] = not _valid_website_for_screenshot(url)
    found["non_ssl"] = (
        not found["hp_none"]
        and bool(_wpc.get("is_non_ssl") or url.startswith("http://"))
    )
    found["non_resp"] = (
        not found["hp_none"]
        and bool(_wpc.get("is_non_responsive") or resp_val is False or resp_val == 0)
    )
    if page_text:
        has_career = bool(re.search(
            r"採用|求人|recruit|career|join.?us|働く|スタッフ募集|一緒に働",
            page_text,
        ))
        found["no_career"] = not found["hp_none"] and not has_career
    else:
        found["no_career"] = False

    _OLD_HOSTING = [
        "さくらインターネット", "ロリポップ", "ヘテムル", "お名前.com",
        "バリュードメイン", "スターネット", "カゴヤ", "コアサーバー",
        "ムームードメイン", "inetd", "アブルネット",
    ]
    _is_old_server = (
        (cms_val in ("不明", "") and not found["hp_none"])
        or (renewal is not None and float(renewal) >= 15)
        or any(k in hosting for k in _OLD_HOSTING)
    )
    found["old_server"] = not found["hp_none"] and _is_old_server

    total_weight = sum(d[1] for d in _WP_DEFS if found.get(d[0]))
    star_score = min(5, round(total_weight / _WP_MAX_STARS * 5 + 0.4))
    wp_list = [(d[0], d[2], d[3], d[4]) for d in _WP_DEFS if found.get(d[0])]
    return wp_list, star_score


def star_label(n: int) -> str:
    if n >= 4:
        color = "#C0392B"
        priority = "提案余地：大"
    elif n >= 2:
        color = "#E67E22"
        priority = "提案余地：中"
    elif n >= 1:
        color = "#F1C40F"
        priority = "提案余地：小"
    else:
        color = "#95A5A6"
        priority = "提案余地：なし"
    stars = "★" * n + "☆" * (5 - n)
    return (
        f'<span style="color:{color};font-size:15px;font-weight:700;">{stars}</span>'
        f'　<span style="font-size:12px;color:{color};">{priority}</span>'
    )


def _calc_elapsed(founded_raw: str):
    from datetime import datetime as _dt

    if not founded_raw or founded_raw in ("不明", ""):
        return None
    _now = _dt.now()
    m_full = re.search(r"(\d{4})[年./\-](\d{1,2})[月./\-](\d{1,2})", founded_raw)
    m_year = re.search(r"(\d{4})", founded_raw)
    if m_full:
        fy, fm, fd = int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3))
        try:
            anniversary = _dt(_now.year, fm, fd)
            return _now.year - fy - (1 if _now < anniversary else 0)
        except ValueError:
            return _now.year - fy
    elif m_year:
        return _now.year - int(m_year.group(1))
    return None


def _esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=True)


def _text(text: Any) -> str:
    s = str(text or "").strip()
    if not s or s in ("不明", "情報なし", "nan"):
        return ""
    return s


def _thumbnail_data_uri(csv_id: str, thumbnails_dir: Path) -> str:
    if not csv_id:
        return ""
    path = thumbnails_dir / f"{csv_id}.jpg"
    if not path.is_file():
        return ""
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{data}"


def _sales_block(row) -> str:
    sales_rows = parse_sales_history(str(row.get("csv_売上推移", "") or ""))
    if sales_rows:
        has_profit = any(r["profit_man"] is not None for r in sales_rows)
        has_pure = any(r.get("pure_profit") is not None for r in sales_rows)
        max_s = max(r["sales_man"] for r in sales_rows) or 1
        lines = [
            '<div class="sales-scroll">',
            '<table class="sales-table"><thead><tr>',
            '<th class="col-period">期</th>',
            '<th class="col-sales">売上</th>',
        ]
        if has_profit:
            lines.append('<th class="col-num">経常利益</th>')
        if has_pure:
            lines.append('<th class="col-num">純利益</th>')
        lines.append("</tr></thead><tbody>")
        for sr in sales_rows:
            pct = int(sr["sales_man"] / max_s * 100)
            p_val = sr.get("profit_man")
            pp_val = sr.get("pure_profit")
            p_lbl = fmt_man(p_val) if p_val is not None else "－"
            pp_lbl = fmt_man(pp_val) if pp_val is not None else "－"
            p_cls = "neg" if p_val is not None and p_val < 0 else ""
            pp_cls = "neg" if pp_val is not None and pp_val < 0 else ""
            lines.append("<tr>")
            lines.append(f'<td class="col-period">{_esc(sr["period"])}</td>')
            lines.append(
                f'<td class="col-sales"><div class="bar-wrap">'
                f'<div class="bar" style="width:{pct}%"></div>'
                f'<span class="bar-amt">{_esc(fmt_man(sr["sales_man"]))}</span>'
                f"</div></td>"
            )
            if has_profit:
                lines.append(f'<td class="col-num {p_cls}">{_esc(p_lbl)}</td>')
            if has_pure:
                lines.append(f'<td class="col-num {pp_cls}">{_esc(pp_lbl)}</td>')
            lines.append("</tr>")
        lines.append("</tbody></table></div>")
        return '<section class="block"><h4>売上推移</h4>' + "".join(lines) + "</section>"

    sales_val = _text(row.get("csv_売上高", "") or row.get("nenkan_売上高", ""))
    if not sales_val or sales_val in ("0", "0.0"):
        return ""
    try:
        rv = float(sales_val)
        if rv >= 1e8:
            label = fmt_man(rv / 10000)
        elif rv >= 1e4:
            label = f"{rv / 1e4:.0f}万円"
        else:
            label = f"{rv:,.0f}円"
    except (ValueError, TypeError):
        label = sales_val
    return f'<section class="block"><h4>直近売上</h4><p>{_esc(label)}</p></section>'


def _web_info_rows(row, enable_scraping: bool) -> list[tuple[str, str]]:
    url = _text(row.get("WebサイトURL", ""))
    csv_tech_raw = str(row.get("_tech_stack", "") or "")
    csv_server_raw = str(row.get("_server_info", "") or "")

    csv_cms, csv_tech, csv_resp, csv_host = "不明", "不明", None, "不明"
    if csv_tech_raw:
        try:
            ts = json.loads(csv_tech_raw)
            detected = ts.get("detected", [])
            csv_cms = "、".join(detected) if detected else "不明"
            sigs = ts.get("signals", {})
            tech_list = []
            if sigs.get("wordpress"):
                tech_list.append("WordPress")
            if sigs.get("wix"):
                tech_list.append("Wix")
            if sigs.get("react"):
                tech_list.append("React")
            if sigs.get("nextjs"):
                tech_list.append("Next.js")
            hdr = ts.get("headers", {})
            if hdr.get("server"):
                tech_list.append(hdr["server"])
            csv_tech = "、".join(tech_list) if tech_list else "不明"
            csv_resp = sigs.get("hasViewport")
        except Exception:
            pass

    if csv_server_raw:
        org = re.search(r"Org:\s*([^\xb7|]+)", csv_server_raw)
        if org:
            csv_host = org.group(1).strip()

    has_scraping = enable_scraping and "CMS" in row.index

    cms = (_text(row.get("CMS", "")) if has_scraping else "") or csv_cms
    tech = (_text(row.get("技術スタック", "")) if has_scraping else "") or csv_tech
    resp_scrape = row.get("レスポンシブ") if has_scraping else None
    resp = resp_scrape if resp_scrape is not None else csv_resp
    host = (_text(row.get("hosting_company", "")) if has_scraping else "") or csv_host

    if not url:
        return []

    if not (has_scraping or csv_tech_raw or csv_server_raw):
        ssl_hint = "非SSL（http）" if url.startswith("http://") else "SSL（https）"
        return [("URL", url), ("SSL", ssl_hint)]

    resp_label = "対応" if resp else "非対応"
    return [
        ("CMS", cms or "－"),
        ("技術", tech or "－"),
        ("レスポンシブ", resp_label),
        ("ホスティング", host or "－"),
        ("SSL発行者", _text(row.get("ssl_issuer", "")) or "－"),
        ("SSL有効期限", _text(row.get("ssl_expiry", "")) or "－"),
        ("ドメイン登録", _text(row.get("whois_registrar", "")) or "－"),
        ("ドメイン期限", _text(row.get("whois_expiry", "")) or "－"),
    ]


def _weakpoints_block(row) -> str:
    wp_list, star = calc_weakpoints(row)
    hosting = _text(row.get("hosting_company", ""))
    if not hosting:
        si = str(row.get("_server_info", "") or "")
        om = re.search(r"Org:\s*([^\xb7|]+)", si)
        if om:
            hosting = om.group(1).strip()

    parts = [
        '<section class="block weakpoints">',
        f"<h4>ウィークポイント {star_label(star)}</h4>",
    ]
    if wp_list:
        parts.append("<ul>")
        for wkey, wlabel, _color, talk in wp_list:
            label = wlabel
            if wkey == "old_server" and hosting:
                label = f"{wlabel}（{hosting}）"
            parts.append(
                f"<li><strong>{_esc(label)}</strong>"
                f'<span class="talk">{_esc(talk)}</span></li>'
            )
        parts.append("</ul>")
    else:
        parts.append('<p class="muted">該当なし</p>')
    parts.append("</section>")
    return "".join(parts)


def _company_card(
    row,
    *,
    enable_scraping: bool,
    thumbnails_dir: Path,
    index: int,
    doc_banner: str = "",
    include_screenshots: bool = True,
) -> str:
    name = _text(row.get("社名", "")) or "（社名なし）"
    founded = _text(row.get("設立年", ""))
    elapsed = _calc_elapsed(founded) if founded else None
    capital = _text(row.get("資本金", ""))
    emp = re.sub(r"^【[^】]*】", "", _text(row.get("従業員数", ""))).strip()
    web_score = _text(row.get("Web提案スコア", ""))
    web_label = _text(row.get("Web提案優先度", ""))

    badges = []
    if founded:
        yrs = f"（{elapsed}年）" if elapsed is not None and elapsed >= 0 else ""
        badges.append(f"設立 {founded}{yrs}")
    if capital:
        badges.append(f"資本金 {capital}")
    if emp:
        badges.append(f"社員数 {emp}")
    if web_label:
        badges.append(web_label)
    elif web_score:
        badges.append(f"Web提案 {web_score}点")

    left_items = [
        ("代表者", _text(row.get("代表者名", ""))),
        ("住所", _text(row.get("住所", ""))),
        ("TEL", _text(row.get("TEL", ""))),
        ("URL", _text(row.get("WebサイトURL", ""))),
        ("主業務", _text(row.get("事業内容", ""))),
        ("業種", _text(row.get("業種", ""))),
    ]

    left_html = "".join(
        f'<div class="kv"><dt>{_esc(k)}</dt><dd>{_linkify(v) if k == "URL" else _esc(v or "－")}</dd></div>'
        for k, v in left_items
        if v or k in ("代表者", "住所", "TEL")
    )

    ai_comment = (_text(row.get("AI営業ポイント", "")) if enable_scraping else "")
    csv_summary = re.sub(r"^【[^】]*】\s*", "", _text(row.get("csv_概要", "")))
    comment = ai_comment or csv_summary
    comment_html = (
        f'<section class="block comment"><h4>寸評</h4><p>{_esc(comment)}</p></section>'
        if comment
        else ""
    )

    web_rows = _web_info_rows(row, enable_scraping)
    web_html = ""
    if web_rows:
        rows_html = "".join(
            f"<tr><th>{_esc(k)}</th><td>{_linkify(v) if k == 'URL' else _esc(v)}</td></tr>"
            for k, v in web_rows
        )
        web_html = f'<section class="block"><h4>Web情報</h4><table class="info-table">{rows_html}</table></section>'

    extra_lines = []
    for label, key in (
        ("上場", "nenkan_上場"),
        ("主要銀行", "nenkan_銀行"),
        ("役員", "nenkan_役員"),
        ("主要銀行（年鑑）", "csv_銀行"),
        ("主要顧客", "csv_顧客"),
        ("主要仕入先", "csv_仕入先"),
    ):
        val = _text(row.get(key, ""))
        if val:
            extra_lines.append(f"<li><strong>{_esc(label)}：</strong>{_esc(val)}</li>")
    extra_html = (
        f'<section class="block"><h4>補足</h4><ul class="plain">{"".join(extra_lines)}</ul></section>'
        if extra_lines
        else ""
    )

    philosophy = _text(row.get("企業理念", "")) or _text(row.get("csv_企業理念", ""))
    phil_html = (
        f'<section class="block"><h4>企業理念</h4><p>{_esc(philosophy)}</p></section>'
        if philosophy
        else ""
    )

    csv_id = _text(row.get("_csv_id", ""))
    url = _text(row.get("WebサイトURL", ""))
    thumb_col = ""
    if include_screenshots:
        thumb = _thumbnail_data_uri(csv_id, thumbnails_dir)
        if thumb:
            if url:
                img = (
                    f'<a href="{_esc(url)}" target="_blank" rel="noopener">'
                    f'<img src="{thumb}" alt="" class="thumb"></a>'
                )
            else:
                img = f'<img src="{thumb}" alt="" class="thumb">'
        else:
            img = '<p class="muted">スクリーンショットなし</p>'
        thumb_col = f'<div class="col col-thumb">{img}</div>'

    badge_html = "".join(f'<span class="badge">{_esc(b)}</span>' for b in badges)
    reason = _text(row.get("Web提案理由", ""))
    reason_html = f'<p class="reason">{_esc(reason)}</p>' if reason else ""
    banner_html = f'<div class="doc-banner">{doc_banner}</div>' if doc_banner else ""
    _co_class = "company" if include_screenshots else "company no-screenshot"
    _body_class = "company-body" if include_screenshots else "company-body no-screenshot"

    return f"""
<article class="{_co_class}">
  {banner_html}
  <header class="company-head">
    <div class="company-no">{index}</div>
    <div>
      <h2>{_esc(name)}</h2>
      <div class="badges">{badge_html}</div>
      {reason_html}
    </div>
  </header>
  <div class="{_body_class}">
    <div class="col">{left_html}</div>
    <div class="col">
      {_sales_block(row)}
      {comment_html}
      {web_html}
      {_weakpoints_block(row)}
      {extra_html}
      {phil_html}
    </div>
    {thumb_col}
  </div>
</article>
"""


def _linkify(url: str) -> str:
    if not url:
        return "－"
    esc = _esc(url)
    return f'<a href="{esc}" target="_blank" rel="noopener">{esc}</a>'


def _safe_filename_part(text: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", (text or "search").strip())
    return s[:40] or "search"


def build_printable_html(
    df: pd.DataFrame,
    *,
    title: str = "SalesScraper 検索結果",
    subtitle: str = "",
    enable_scraping: bool = False,
    thumbnails_dir: Path | None = None,
    include_screenshots: bool = True,
) -> str:
    """検索結果 DataFrame からスタンドアロン HTML 文字列を生成する。"""
    thumbs = thumbnails_dir or Path(__file__).resolve().parent / "thumbnails"
    today = date.today().strftime("%Y/%m/%d")
    count = len(df)
    sub = _esc(subtitle) if subtitle else ""
    doc_line = f"<strong>{_esc(title)}</strong>　{sub}　{count}件　出力日 {today}"
    cards = []
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        cards.append(
            _company_card(
                row,
                enable_scraping=enable_scraping,
                thumbnails_dir=thumbs,
                index=i,
                doc_banner=doc_line if i == 1 else "",
                include_screenshots=include_screenshots,
            )
        )

    css = """
:root {
  --text: #1f2937;
  --muted: #6b7280;
  --border: #e5e7eb;
  --accent: #2563eb;
  --bg: #ffffff;
  --card: #fafafa;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Hiragino Sans", "Yu Gothic UI", "Meiryo", sans-serif;
  color: var(--text);
  background: var(--bg);
  line-height: 1.55;
  font-size: 14px;
}
.no-print {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 10px 16px;
  background: #111827;
  color: #fff;
}
.no-print button {
  border: 0;
  border-radius: 6px;
  padding: 8px 14px;
  font-size: 14px;
  cursor: pointer;
  background: var(--accent);
  color: #fff;
}
.no-print .hint { font-size: 12px; color: #d1d5db; }
.doc-banner {
  font-size: 12px;
  color: var(--muted);
  border-bottom: 1px solid var(--border);
  margin-bottom: 10px;
  padding-bottom: 6px;
}
.company {
  margin: 16px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--card);
  page-break-inside: avoid;
}
.company-head {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  border-bottom: 1px solid var(--border);
  padding-bottom: 10px;
  margin-bottom: 12px;
}
.company-no {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: #dbeafe;
  color: #1d4ed8;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
}
.company-head h2 { margin: 0 0 6px; font-size: 18px; }
.badges { display: flex; flex-wrap: wrap; gap: 6px; }
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  background: #f3f4f6;
  border: 1px solid var(--border);
  font-size: 12px;
}
.reason { margin: 6px 0 0; font-size: 12px; color: var(--muted); }
.company-body {
  display: grid;
  grid-template-columns: 1fr 1.2fr 0.9fr;
  gap: 14px;
}
.company-body.no-screenshot {
  grid-template-columns: 1fr 1.5fr;
}
.col .block { margin-bottom: 12px; }
.col h4 {
  margin: 0 0 6px;
  font-size: 13px;
  color: var(--muted);
  border-left: 3px solid var(--accent);
  padding-left: 6px;
}
.kv { margin-bottom: 8px; }
.kv dt { font-size: 12px; color: var(--muted); margin-bottom: 2px; }
.kv dd { margin: 0; word-break: break-all; }
.comment p {
  margin: 0;
  padding: 8px 10px;
  background: #eff6ff;
  border-radius: 6px;
}
.info-table, .sales-table {
  border-collapse: collapse;
  font-size: 13px;
}
.sales-scroll {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  max-width: 100%;
}
.sales-table {
  width: max-content;
  min-width: 100%;
}
.sales-table .col-period {
  width: 3.2em;
  white-space: nowrap;
  padding-right: 4px;
}
.sales-table .col-sales {
  min-width: 128px;
}
.sales-table .col-num {
  min-width: 5.5em;
  white-space: nowrap;
  text-align: right;
}
.info-table {
  width: 100%;
}
.info-table th, .info-table td,
.sales-table th, .sales-table td {
  border-bottom: 1px solid var(--border);
  padding: 3px 6px;
  text-align: left;
  vertical-align: middle;
}
.sales-table th.col-num,
.sales-table td.col-num {
  text-align: right;
}
.info-table th {
  width: 38%;
  color: var(--muted);
  font-weight: 600;
}
.sales-table th {
  color: var(--muted);
  font-weight: 600;
  font-size: 12px;
}
.bar-wrap {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  min-width: 120px;
}
.bar-wrap .bar {
  height: 6px;
  background: #378add;
  border-radius: 3px;
  min-width: 2px;
  max-width: 100px;
}
.bar-wrap .bar-amt {
  white-space: nowrap;
  font-size: 12px;
}
.neg { color: #a32d2d; }
.weakpoints ul { margin: 0; padding-left: 1.1em; }
.weakpoints li { margin-bottom: 8px; }
.weakpoints .talk {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin-top: 2px;
}
.plain { margin: 0; padding-left: 1.1em; }
.muted { color: var(--muted); font-size: 13px; }
.thumb {
  width: 100%;
  max-width: 320px;
  border-radius: 6px;
  border: 1px solid var(--border);
}
a { color: var(--accent); word-break: break-all; }
@media (max-width: 900px) {
  .company-body { grid-template-columns: 1fr; }
  .col-thumb { order: -1; }
  .thumb { max-width: 100%; }
}
@media print {
  @page { size: A4 landscape; margin: 6mm; }
  .no-print { display: none !important; }
  body { font-size: 9px; line-height: 1.45; }
  .doc-banner {
    margin-bottom: 6px;
    padding-bottom: 4px;
    font-size: 9px;
  }
  .company {
    margin: 0;
    padding: 8px;
    box-shadow: none;
    page-break-inside: avoid;
    break-inside: avoid;
    page-break-after: always;
    break-after: page;
  }
  .company:last-child {
    page-break-after: auto;
    break-after: auto;
  }
  .company-head { margin-bottom: 8px; padding-bottom: 6px; }
  .company-head h2 { font-size: 14px; }
  .company-body {
    grid-template-columns: 0.85fr 1.35fr 0.75fr;
    gap: 8px;
  }
  .company-body.no-screenshot {
    grid-template-columns: 0.9fr 1.6fr;
  }
  .col .block { margin-bottom: 6px; }
  .col h4 { font-size: 9px; margin-bottom: 4px; }
  .sales-scroll { overflow: visible; }
  .bar-wrap .bar { height: 4px; max-width: 80px; }
  .bar-wrap .bar-amt { font-size: 9px; }
  .kv { margin-bottom: 4px; }
  .comment p { padding: 5px 7px; }
  a { color: inherit; text-decoration: none; }
  .thumb { max-width: 170px; }
}
"""

    body = "\n".join(cards)
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{css}</style>
</head>
<body>
<div class="no-print">
  <button type="button" onclick="window.print()">🖨 印刷</button>
  <span class="hint">PC：印刷ダイアログ（A4横・1社1枚）　スマホ：共有メニューから印刷またはPDF保存</span>
</div>
{body}
</body>
</html>
"""


def html_to_pdf(html_str: str) -> bytes:
    """HTML文字列を A4 PDF に変換（別プロセスで Playwright を起動）。"""
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    worker = Path(__file__).resolve().parent / "_pdf_worker.py"
    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "input.html"
        pdf_path = Path(tmp) / "output.pdf"
        html_path.write_text(html_str, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(worker), str(html_path), str(pdf_path)],
            capture_output=True,
            timeout=180,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            out = proc.stdout.decode("utf-8", errors="replace").strip()
            detail = err or out or f"終了コード {proc.returncode}"
            raise RuntimeError(detail)
        if not pdf_path.is_file():
            raise RuntimeError("PDFファイルが作成されませんでした")
        return pdf_path.read_bytes()
