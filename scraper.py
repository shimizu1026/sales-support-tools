from __future__ import annotations

"""
Playwrightスクレイパー + 技術スタック判定 + AI情報抽出 + 国税庁DB検索（単体実行用）
app.pyからsubprocessで呼び出される
引数: url company_name screenshot_dir

AI抽出: OPENAI_API_KEY / GEMINI_API_KEY / Ollama（LOCAL_LLM_*）。
  SCRAPER_AI_PROVIDER=auto|openai|gemini|ollama（既定 auto）
  ollama 時: LOCAL_LLM_URL, LOCAL_LLM_API_KEY, LOCAL_LLM_MODEL
  GEMINI_SCRAPER_MODEL（既定 gemini-2.5-flash）
"""
import os
import sys
import json
import time
import socket
import ssl
import sqlite3
import logging
import requests
import re
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path

from env_bootstrap import bootstrap_env

bootstrap_env(Path(__file__).resolve().parent)


DB_FILE = "houjin.db"

# ─────────────────────────────────────────────
# 自社サイトではない「地図・検索ポータル」のドメイン
# これらに該当するURLはスクレイプ対象から除外する
# ─────────────────────────────────────────────
BLOCKED_DOMAINS = {
    "yelp.com",
    "yelp.co.jp",
    "navitime.co.jp",
    "navitime.com",
    "google.com",
    "google.co.jp",
    "maps.google.com",
    "mapfan.com",
    "mapion.co.jp",
    "tabelog.com",
    "ekiten.jp",
    "itp.ne.jp",
    "townpage.jp",
    "hotpepper.jp",
    "r.gnavi.co.jp",
    "gnavi.co.jp",
    "goo.ne.jp",
    "loco.yahoo.co.jp",
    "map.yahoo.co.jp",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    # 会社案内・業種ポータル系
    "kensetumap.com",
    "houjin-bangou.nta.go.jp",
    "alarmbox.jp",
    "mapfanapi.com",
    "baseconnect.in",
    "musubu.in",
    "salesnow.jp",
    "jobcan.ne.jp",
    "indeed.com",
    "rikunabi.com",
    "mynavi.jp",
    "doda.jp",
    # 百科・PR・法人データサイト・無料STORE（公式HP扱いにしない）
    "wikipedia.org",
    "prtimes.jp",
    "companydata.tsujigawa.com",
    "thebase.in",
    "sessions.thebase.in",
}

# Google Sites 等、google.com 配下だが企業の公式ページとして使われるホスト
ALLOWED_OFFICIAL_HOSTS = frozenset({
    "sites.google.com",
})


def is_blocked_url(url: str) -> bool:
    """地図/検索ポータル系URLはホームページ扱いしない。"""
    if not url:
        return True
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in ALLOWED_OFFICIAL_HOSTS:
        return False
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    for i in range(len(parts) - 1):
        if ".".join(parts[i:]) in BLOCKED_DOMAINS:
            return True
    return False

# ─────────────────────────────────────────────
# ロギング設定
# ─────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    """
    ファイル（scraper.log）と stderr の両方に出力するロガーを返す。
    subprocess から呼ばれるため、stdout には一切書かない（JSON出力と混在させない）。
    ファイルは 2MB を超えたら世代交代し、最大3世代（計6MB）で自動削除。
    """
    from logging.handlers import RotatingFileHandler

    logger = logging.getLogger("scraper")
    if logger.handlers:
        return logger  # 多重登録防止（app.py が先に設定済みの場合もここで終了）

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(funcName)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_path = Path(__file__).parent / "scraper.log"
    try:
        fh = RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass  # ファイルロック時も stderr のみで続行

    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def _log():
    return _setup_logger()


class _LazyLogger:
    def __getattr__(self, name):
        return getattr(_setup_logger(), name)


logger = _LazyLogger()


# ─────────────────────────────────────────────
# 国税庁DB検索
# ─────────────────────────────────────────────

def search_houjin_db(company_name: str) -> dict:
    result = {
        "法人番号": "不明",
        "郵便番号": "不明",
        "法人種別": "不明",
    }

    if not Path(DB_FILE).exists():
        return result

    def normalize(name: str) -> str:
        for prefix in ["株式会社", "有限会社", "合同会社", "合名会社", "合資会社",
                       "一般社団法人", "公益社団法人", "特定非営利活動法人", "医療法人"]:
            name = name.replace(prefix, "").strip()
        return name

    try:
        conn = sqlite3.connect(DB_FILE)
        cur  = conn.cursor()

        cur.execute(
            "SELECT houjin_no, zip_code, houjin_type FROM houjin WHERE houjin_name = ? LIMIT 1",
            (company_name,)
        )
        row = cur.fetchone()

        if not row:
            cur.execute(
                "SELECT houjin_no, zip_code, houjin_type FROM houjin WHERE houjin_name LIKE ? LIMIT 1",
                (f"{company_name}%",)
            )
            row = cur.fetchone()

        if not row:
            normalized = normalize(company_name)
            if normalized and normalized != company_name:
                cur.execute(
                    "SELECT houjin_no, zip_code, houjin_type FROM houjin WHERE houjin_name LIKE ? LIMIT 1",
                    (f"%{normalized}%",)
                )
                row = cur.fetchone()

        conn.close()

        if row:
            houjin_no, zip_code, houjin_type = row
            if zip_code and len(zip_code) == 7:
                zip_code = f"{zip_code[:3]}-{zip_code[3:]}"
            type_map = {
                "101": "国の機関", "201": "地方公共団体",
                "301": "株式会社", "302": "有限会社", "303": "合名会社",
                "304": "合資会社", "305": "合同会社",
                "399": "その他の設立登記法人",
                "401": "外国会社等", "499": "その他",
                "501": "公益社団法人", "502": "公益財団法人",
                "503": "一般社団法人", "504": "一般財団法人",
                "601": "各種農業組合等", "701": "医療法人",
                "801": "学校法人", "899": "その他の特別法人",
                "900": "特定非営利活動法人",
            }
            result["法人番号"] = houjin_no
            result["郵便番号"] = zip_code or "不明"
            result["法人種別"] = type_map.get(houjin_type, houjin_type)
        else:
            logger.debug("法人DB: 該当なし company=%s", company_name)

    except sqlite3.Error as e:
        logger.warning("法人DB SQLiteエラー company=%s: %s", company_name, e)
    except Exception as e:
        logger.warning("法人DB 予期せぬエラー company=%s: %s", company_name, e)

    return result


# ─────────────────────────────────────────────
# ドメイン情報取得（ipinfo.io + socket + SSL）
# ─────────────────────────────────────────────

# ネームサーバーからホスティング会社を推定するマッピング
NS_TO_HOSTING = {
    "dnsv.jp":       "お名前.com",
    "xserver":       "エックスサーバー",
    "sakura":        "さくらインターネット",
    "lolipop":       "ロリポップ",
    "onamae":        "お名前.com",
    "value-domain":  "バリュードメイン",
    "cloudflare":    "Cloudflare",
    "aws":           "AWS (Amazon)",
    "azure":         "Microsoft Azure",
    "googledomains": "Google Domains",
    "gandi":         "Gandi",
    "heteml":        "ヘテムル",
    "coreserver":    "コアサーバー",
    "wpx":           "WPX",
    "zenlogic":      "ゼンロジック",
    "inetd":         "inetd",
    "kagoya":        "カゴヤ",
    "akamai":        "Akamai",
    "fastly":        "Fastly",
    "mudns":         "ムームードメイン",
    "muumuu":        "ムームードメイン",
    "gmogmo":        "GMOインターネット",
    "conoha":        "ConoHa",
    "ablenet":       "アブルネット",
    "star.ne.jp":    "スターネット",
    "nifty":         "ニフクラ",
}


def _get_ip_address(hostname: str) -> str:
    """ホスト名からIPアドレスを取得する。"""
    try:
        ip = socket.gethostbyname(hostname)
        print(f"[DEBUG] IP取得成功: {hostname} -> {ip}", file=sys.stderr)
        return ip
    except Exception as e:
        print(f"[DEBUG] IP取得失敗: {hostname} -> {e}", file=sys.stderr)
        return "不明"


def _get_hosting_from_ipinfo(ip: str) -> dict:
    """
    ipinfo.io の無料APIでIPアドレスからホスティング情報を取得する。
    月50,000リクエストまで無料・1分あたりの制限なし。
    """
    result = {"hosting_company": "不明", "hosting_org": "不明"}
    if not ip or ip == "不明":
        return result
    try:
        resp = requests.get(
            f"https://ipinfo.io/{ip}/json",
            timeout=5,
            headers={"Accept": "application/json"},
        )
        if resp.status_code == 200:
            data = resp.json()
            org = data.get("org", "")        # 例: "AS12345 Sakura Internet Inc."
            company = data.get("company", {}).get("name", "") if isinstance(data.get("company"), dict) else ""
            if org:
                # AS番号部分を除いた会社名を取り出す
                parts = org.split(" ", 1)
                org_name = parts[1] if len(parts) > 1 else org
                result["hosting_org"]     = org_name
                result["hosting_company"] = company or org_name
        elif resp.status_code == 429:
            logger.warning("ipinfo.io レート制限超過 (429) ip=%s — 月間上限に達した可能性あり", ip)
        else:
            logger.warning("ipinfo.io 予期せぬステータス %s ip=%s", resp.status_code, ip)
    except requests.Timeout:
        logger.warning("ipinfo.io タイムアウト ip=%s", ip)
    except requests.RequestException as e:
        logger.warning("ipinfo.io 通信エラー ip=%s: %s", ip, e)
    except Exception as e:
        logger.warning("ipinfo.io 予期せぬエラー ip=%s: %s", ip, e)
    return result


def _whois_query(server: str, query: str) -> str:
    """WhoisサーバーにTCP接続して生テキストを返す（ライブラリ不要）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(8)
        s.connect((server, 43))
        s.send((query + "\r\n").encode("utf-8"))
        chunks = []
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        s.close()
        return b"".join(chunks).decode("utf-8", errors="ignore")
    except socket.timeout:
        logger.warning("Whoisタイムアウト server=%s query=%s", server, query)
        return "__TIMEOUT__"
    except socket.gaierror as e:
        logger.warning("Whois DNS解決失敗 server=%s: %s", server, e)
        return "__BLOCKED__"
    except OSError as e:
        logger.warning("Whois 接続エラー server=%s: %s", server, e)
        return "__BLOCKED__"
    except Exception as e:
        logger.warning("Whois 予期せぬエラー server=%s query=%s: %s", server, query, e)
        return "__BLOCKED__"


def _get_whois_rdap(domain: str) -> dict:
    """
    RDAP（Whois後継・HTTPS）でWhois情報を取得する。
    ipwhois ライブラリを使用。ポート43不要・443番のみで動作。

    取得できる情報:
      .jp / .co.jp: 有効期限のみ（JPRSは登録者を非公開）
      .com / .net等: 登録者（registrar）+ 有効期限
    """
    result = {"whois_registrar": "不明", "whois_expiry": "不明"}
    try:
        import socket as _sock
        from ipwhois import IPWhois
        import urllib.request as _req
        import json as _json

        # ── .jp / .co.jp: JPRSのRDAPエンドポイントを直接使う ──
        if domain.endswith(".jp"):
            rdap_url = f"https://rdap.jprs.jp/domain/{domain}"
            try:
                req = _req.Request(
                    rdap_url,
                    headers={
                        "Accept": "application/rdap+json",
                        "User-Agent": "salescaper/1.0",
                    }
                )
                with _req.urlopen(req, timeout=8) as resp:
                    data = _json.loads(resp.read())

                # 有効期限
                for event in data.get("events", []):
                    if event.get("eventAction") == "expiration":
                        expiry_raw = event.get("eventDate", "")
                        if expiry_raw:
                            result["whois_expiry"] = expiry_raw[:10]

                # 登録者（JPRSは通常非公開だが念のため確認）
                for entity in data.get("entities", []):
                    if "registrar" in entity.get("roles", []):
                        vcard = entity.get("vcardArray", [])
                        if vcard and len(vcard) > 1:
                            for item in vcard[1]:
                                if item[0] == "fn" and item[3]:
                                    result["whois_registrar"] = item[3]
                if result["whois_registrar"] == "不明":
                    result["whois_registrar"] = "JPRS管理（非公開）"

                logger.debug("RDAP(.jp)成功 domain=%s expiry=%s",
                             domain, result["whois_expiry"])
                return result

            except Exception as e:
                logger.debug("RDAP(.jp)失敗 domain=%s: %s", domain, e)
                # フォールバックへ

        # ── .com / .net等: ipwhois の RDAP lookup ──
        else:
            try:
                ip = _sock.gethostbyname(domain)
                obj = IPWhois(ip)
                rdap_result = obj.lookup_rdap(depth=1, retry_count=2)

                # registrar を entities から取得
                for ent in rdap_result.get("entities", []):
                    if "registrar" in (ent.get("roles") or []):
                        name = ent.get("contact", {}).get("name", "")
                        if name:
                            result["whois_registrar"] = name
                            break

                # フォールバック: asn_description を使用
                if result["whois_registrar"] == "不明":
                    asn_desc = rdap_result.get("asn_description", "")
                    if asn_desc:
                        result["whois_registrar"] = asn_desc

                # 有効期限は RDAP では取りにくいため raw を確認
                raw_data = rdap_result.get("raw", {})
                if isinstance(raw_data, dict):
                    for event in raw_data.get("events", []):
                        if event.get("eventAction") == "expiration":
                            result["whois_expiry"] = event.get("eventDate", "")[:10]

                logger.debug("RDAP(.com等)成功 domain=%s registrar=%s",
                             domain, result["whois_registrar"])
                return result

            except Exception as e:
                logger.debug("RDAP(.com等)失敗 domain=%s: %s", domain, e)
                # フォールバックへ

    except ImportError:
        logger.debug("ipwhois未インストール domain=%s", domain)
    except Exception as e:
        logger.warning("RDAP予期せぬエラー domain=%s: %s", domain, e)

    return result  # 取得できなかった場合は「不明」を返す


def _get_whois_info(domain: str) -> dict:
    """
    Whois情報を取得する。
    優先順位:
      1. RDAP（HTTPS・ポート43不要）→ ipwhois + JPRSエンドポイント
      2. Whoisプロトコル（TCP・ポート43）→ フォールバック

    どちらも失敗した場合は「取得不可」を返す。
    """
    # ── ① RDAP（HTTPS）で試みる ──
    rdap_result = _get_whois_rdap(domain)
    if rdap_result.get("whois_expiry") not in ("不明", "", None) or        rdap_result.get("whois_registrar") not in ("不明", "", None):
        logger.debug("RDAP取得成功 domain=%s", domain)
        return rdap_result

    logger.debug("RDAP不取得、Whoisプロトコルにフォールバック domain=%s", domain)

    # ── ② Whoisプロトコル（ポート43）でフォールバック ──
    result = {"whois_registrar": "不明", "whois_expiry": "不明"}
    try:
        if domain.endswith(".jp"):
            raw = _whois_query("whois.jprs.jp", domain)
        else:
            tld = domain.rsplit(".", 1)[-1]
            iana_raw = _whois_query("whois.iana.org", tld)
            if iana_raw in ("__TIMEOUT__", "__BLOCKED__"):
                return {"whois_registrar": "取得不可", "whois_expiry": "取得不可"}
            whois_server = "不明"
            for line in iana_raw.splitlines():
                if line.lower().startswith("whois:"):
                    whois_server = line.split(":", 1)[1].strip()
                    break
            if whois_server == "不明":
                return {"whois_registrar": "取得不可", "whois_expiry": "取得不可"}
            raw = _whois_query(whois_server, domain)

        if raw in ("__TIMEOUT__", "__BLOCKED__"):
            return {"whois_registrar": "取得不可", "whois_expiry": "取得不可"}
        if not raw:
            return {"whois_registrar": "取得不可", "whois_expiry": "取得不可"}

        import re as _re2
        if domain.endswith(".jp"):
            for line in raw.splitlines():
                line = line.strip()
                if "[状態]" in line and "Connected" in line:
                    m = _re2.search(r"\((\d{4}/\d{2}/\d{2})\)", line)
                    if m:
                        result["whois_expiry"] = m.group(1).replace("/", "-")
                if "f. [組織名]" in line or "f.[組織名]" in line:
                    org = line.split("]", 1)[-1].strip()
                    if org:
                        result["whois_registrar"] = org
            if result["whois_registrar"] == "不明":
                result["whois_registrar"] = "JPRS管理（非公開）"
        else:
            for line in raw.splitlines():
                line_lower = line.lower()
                if "registrar:" in line_lower and result["whois_registrar"] == "不明":
                    val = line.split(":", 1)[-1].strip()
                    if val:
                        result["whois_registrar"] = val
                if "expir" in line_lower and ("date" in line_lower or "on" in line_lower):
                    val = line.split(":", 1)[-1].strip()
                    if val and result["whois_expiry"] == "不明":
                        result["whois_expiry"] = val[:10]

    except Exception as e:
        logger.warning("Whoisプロトコルエラー domain=%s: %s", domain, e)
        return {"whois_registrar": "取得不可", "whois_expiry": "取得不可"}

    return result


def _get_name_servers(domain: str) -> dict:
    """
    dnspython でネームサーバー(NS)レコードを取得し、
    ホスティング会社をNSから推定する。
    """
    result = {"name_servers": "不明", "ns_hosting": "不明"}
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "NS")
        ns_list = [str(r.target).rstrip(".").lower() for r in answers]
        result["name_servers"] = ", ".join(ns_list)
        # NSからホスティング会社を推定
        for ns in ns_list:
            for keyword, company in NS_TO_HOSTING.items():
                if keyword in ns:
                    result["ns_hosting"] = company
                    break
            if result["ns_hosting"] != "不明":
                break
    except ImportError:
        logger.warning("dnspython未インストール。NSレコード取得をスキップ。pip install dnspython で解決できます")
    except Exception as e:
        logger.warning("NSレコード取得エラー domain=%s: %s", domain, e)
    return result


def _cert_dn_to_dict(field_tuples) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in field_tuples or []:
        if not isinstance(item, (list, tuple)) or not item:
            continue
        pair = item[0] if len(item) == 1 and isinstance(item[0], (list, tuple)) else item
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            out[str(pair[0])] = str(pair[1])
    return out


def _format_ssl_issuer(issuer: dict[str, str]) -> str:
    org = (issuer.get("organizationName") or "").strip()
    cn = (issuer.get("commonName") or "").strip()
    if org:
        return org
    if not cn:
        return "不明"
    low = cn.lower()
    if "letsencrypt" in low or "let's encrypt" in low:
        return "Let's Encrypt"
    if "digicert" in low:
        return "DigiCert"
    if "globalsign" in low:
        return "GlobalSign"
    if "sectigo" in low:
        return "Sectigo"
    if "google trust" in low:
        return "Google Trust Services"
    if "amazon" in low:
        return "Amazon"
    return cn


def _parse_ssl_not_after(not_after: str) -> str:
    if not not_after:
        return "不明"
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y GMT"):
        try:
            return datetime.strptime(not_after, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return "不明"


def _get_ssl_info(hostname: str) -> dict:
    """SSL証明書の発行者と有効期限を取得する（Python標準ライブラリのみ）。"""
    result = {"ssl_issuer": "不明", "ssl_expiry": "不明"}
    if not hostname:
        return result
    timeout = float(os.environ.get("SCRAPER_SSL_TIMEOUT_S", "8"))
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(
            socket.create_connection((hostname, 443), timeout=timeout),
            server_hostname=hostname,
        ) as ssock:
            cert = ssock.getpeercert()
            issuer_dict = _cert_dn_to_dict(cert.get("issuer", []))
            issuer = _format_ssl_issuer(issuer_dict)
            if issuer != "不明":
                result["ssl_issuer"] = issuer

            not_after = cert.get("notAfter", "")
            expiry = _parse_ssl_not_after(not_after)
            if expiry != "不明":
                result["ssl_expiry"] = expiry
    except ssl.SSLError as e:
        logger.warning("SSL証明書エラー hostname=%s: %s", hostname, e)
    except socket.timeout:
        logger.warning("SSL接続タイムアウト hostname=%s", hostname)
    except OSError as e:
        logger.warning("SSL接続エラー hostname=%s: %s", hostname, e)
    except Exception as e:
        logger.warning("SSL情報取得 予期せぬエラー hostname=%s: %s", hostname, e)
    return result


def get_domain_info(url: str) -> dict:
    """
    URLを受け取り、ドメイン情報を取得してdictで返す。
    キャッシュ: Supabase domain_cache（DOMAIN_CACHE_ENABLED=1 時のみ。既定は nenkan.db 利用）

    返却キー:
        ip_address, hosting_company, hosting_org,
        whois_registrar, whois_expiry,
        name_servers, ssl_issuer, ssl_expiry
    """
    empty = {
        "ip_address":      "不明",
        "hosting_company": "不明",
        "hosting_org":     "不明",
        "whois_registrar": "不明",
        "whois_expiry":    "不明",
        "name_servers":    "不明",
        "ssl_issuer":      "不明",
        "ssl_expiry":      "不明",
    }

    try:
        hostname = urlparse(url).hostname
        logger.debug("get_domain_info開始: url=%s hostname=%s", url, hostname)
        if not hostname:
            return empty

        # サブドメインを除いたドメイン（キャッシュキー用）
        parts = hostname.split(".")
        domain = ".".join(parts[-3:]) if parts[-1] in ("jp",) and len(parts) >= 3 else ".".join(parts[-2:])

    except Exception as e:
        logger.warning("URLパース失敗 url=%s: %s", url, e)
        return empty

    # ── キャッシュ確認 ────────────────────────
    try:
        from saved_list import get_domain_cache, set_domain_cache
        cached = get_domain_cache(domain)
        if cached:
            return cached
    except Exception as e:
        logger.warning("ドメインキャッシュ読み込みエラー domain=%s: %s", domain, e)

    # ── 各情報を取得 ──────────────────────────
    info = dict(empty)

    # ① IPアドレス
    ip = _get_ip_address(hostname)
    info["ip_address"] = ip

    # ② ホスティング会社（ipinfo.io）
    hosting = _get_hosting_from_ipinfo(ip)
    info["hosting_company"] = hosting["hosting_company"]
    info["hosting_org"]     = hosting["hosting_org"]

    # ③ Whois情報
    whois_info = _get_whois_info(domain)
    info["whois_registrar"] = whois_info["whois_registrar"]
    info["whois_expiry"]    = whois_info["whois_expiry"]

    # ④ ネームサーバー
    ns_info = _get_name_servers(domain)
    info["name_servers"] = ns_info["name_servers"]
    # NSからホスティング推定できた場合、ipinfo結果が「不明」なら補完
    if info["hosting_company"] == "不明" and ns_info["ns_hosting"] != "不明":
        info["hosting_company"] = ns_info["ns_hosting"]

    # ⑤ SSL証明書
    ssl_info = _get_ssl_info(hostname)
    info["ssl_issuer"] = ssl_info["ssl_issuer"]
    info["ssl_expiry"] = ssl_info["ssl_expiry"]

    # ── キャッシュに保存 ──────────────────────
    try:
        from saved_list import set_domain_cache
        set_domain_cache(domain, info)
    except Exception as e:
        logger.warning("ドメインキャッシュ保存エラー domain=%s: %s", domain, e)

    return info


# ─────────────────────────────────────────────
# 技術スタック判定
# ─────────────────────────────────────────────

def detect_tech_stack(url: str, html: str, headers: dict) -> dict:
    tech = {
        "cms":          "不明",
        "server":       "不明",
        "responsive":   False,
        "technologies": [],
    }
    html_lower = html.lower()
    techs = []

    for cms_name, sigs in {
        "WordPress":    ["/wp-content/", "/wp-includes/", "wp-json"],
        "Wix":          ["wix.com", "wixstatic.com"],
        "Squarespace":  ["squarespace.com", "squarespace-cdn"],
        "STUDIO":       ["studio.design", "studio-export"],
        "Jimdo":        ["jimdo.com", "jimdosite.com"],
        "Shopify":      ["shopify.com", "cdn.shopify"],
        "EC-CUBE":      ["eccube", "ec-cube"],
        "Drupal":       ["drupal.js", "drupal.min.js"],
        "Joomla":       ["/media/jui/", "joomla"],
        "Movable Type": ["mt.js", "movabletype"],
    }.items():
        if any(s in html_lower for s in sigs):
            tech["cms"] = cms_name
            techs.append(cms_name)
            break

    server_header = headers.get("server", "").lower()
    x_powered_by  = headers.get("x-powered-by", "").lower()
    for server_name, sigs in {
        "Apache": ["apache"], "Nginx": ["nginx"], "IIS": ["microsoft-iis"],
        "LiteSpeed": ["litespeed"], "Cloudflare": ["cloudflare"],
    }.items():
        if any(s in server_header or s in x_powered_by for s in sigs):
            tech["server"] = server_name
            techs.append(server_name)
            break

    for tech_name, sigs in {
        "jQuery":             ["jquery.min.js", "jquery.js"],
        "Bootstrap":          ["bootstrap.min.css", "bootstrap.css"],
        "Google Analytics":   ["google-analytics.com", "gtag("],
        "Google Tag Manager": ["googletagmanager.com"],
        "PHP":                [".php"],
        "React":              ["react.min.js", "react-dom"],
        "Vue.js":             ["vue.min.js", "vue.js"],
    }.items():
        if any(s in html_lower for s in sigs):
            techs.append(tech_name)

    # レスポンシブ判定：<meta name="viewport"> タグの有無で正確に判定
    # 単純な "viewport" 文字列ではなくmetaタグとして記述されているか確認
    import re as _re_resp
    _viewport_pattern = _re_resp.compile(r"(?i)<meta[^>]+name=.viewport.")
    _has_viewport_meta = bool(_viewport_pattern.search(html))
    _has_device_width  = "width=device-width" in html_lower
    tech["responsive"] = _has_viewport_meta or _has_device_width

    tech["technologies"] = list(dict.fromkeys(techs))
    return tech


# ─────────────────────────────────────────────
# AI情報抽出（サイト古さスコア追加版）
# OpenAI または Gemini（GEMINI_API_KEY）。SCRAPER_AI_PROVIDER で選択。
# ─────────────────────────────────────────────


def _company_info_extraction_prompt(company_name: str, page_text: str) -> str:
    snippet = page_text[:5000] if page_text else ""
    return f"""
以下は「{company_name}」のWebサイトから取得したテキストです。
このテキストから以下の情報をJSON形式で抽出・評価してください。
情報が見つからない場合は「不明」としてください。

抽出する項目:
- 代表者名（代表取締役、社長、代表者など）
- 資本金（数字と単位を含む。例：1,000万円）
- 従業員数（数字と単位を含む。例：50名）
- 設立年（西暦。例：1985年）
- 事業内容（50文字以内で要約）
- 企業理念（以下の優先順位で抽出し、200〜300文字程度でまとめる。原文が短い場合はそのまま、長い場合は要約する。
    優先順位1: 「企業理念」「経営理念」「社是」「綱領」と明記されている箇所の本文
    優先順位2: 「代表挨拶」「社長挨拶」「トップメッセージ」「代表メッセージ」から、会社が大切にしている価値観・ミッション・想いを要約
    優先順位3: 「ミッション」「ビジョン」「バリュー」「パーパス」「フィロソフィー」「コンセプト」と明記されている箇所
    ※ テキスト内の「--- 企業理念・挨拶ページ ---」セクションを最優先で参照すること
    ※ 見つからない場合は空文字列を返す（「不明」は使わない）
- ai_summary（このWebサイトの営業アプローチのポイントを50文字以内で）
- site_age_score（Webサイトの「古さ・時代遅れ度」を 0〜20 の整数で評価。
    評価基準：
      20点 → 著しく古い。テキストに「平成」表記・3桁電話番号・Flash等の旧技術への言及がある。
              文体が極めて古く、更新が長期間されていない様子が明確。
      15点 → かなり古い。数年以上更新されていない印象。コンテンツや著作権年が古い（〜2018年以前）。
      10点 → やや古い。2019〜2021年頃の雰囲気。モバイル対応が不十分な表現がある。
       5点 → 比較的新しいが古さの兆候が若干ある（著作権年が2022〜2023年等）。
       0点 → 最新。2024年以降の更新が確認できる、または新しい技術・表現を使っている。
    テキストに情報が少ない場合は 5 を返してください。）
- site_age_reason（site_age_scoreの判定理由を30文字以内で。例：「著作権表記が2015年で長期未更新」）

必ずJSON形式のみで返答してください。説明文や```json等のコードブロックは不要です。
{{
  "代表者名": "...",
  "資本金": "...",
  "従業員数": "...",
  "設立年": "...",
  "事業内容": "...",
  "企業理念": "...",
  "ai_summary": "...",
  "site_age_score": 0,
  "site_age_reason": "..."
}}

テキスト:
{snippet}
"""


def _parse_company_json_response(text: str) -> dict:
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else parts[0]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:].lstrip()
    parsed = json.loads(text.strip())
    raw_score = parsed.get("site_age_score", 0)
    try:
        parsed["site_age_score"] = max(0, min(20, int(raw_score)))
    except (ValueError, TypeError):
        parsed["site_age_score"] = 0
    return parsed


_EMPTY_INFO = frozenset({"不明", "情報なし", ""})


def _info_value_ok(val) -> bool:
    s = str(val or "").strip()
    return bool(s) and s not in _EMPTY_INFO


def _fill_company_fields_from_text(result: dict) -> None:
    """AIが不明のとき、ページテキストから会社概要の定型項目を補完する。"""
    text = str(result.get("page_text") or "")
    if not text:
        return

    if not _info_value_ok(result.get("設立年")):
        for pat in (
            r"設立[：:\s]*(\d{4}年\d{1,2}月?)",
            r"設立[：:\s]*(\d{4}年)",
            r"(\d{4}年\d{1,2}月?)設立",
        ):
            m = re.search(pat, text)
            if m:
                result["設立年"] = m.group(1)
                break

    if not _info_value_ok(result.get("従業員数")):
        for pat in (
            r"従業員(?:数)?[：:\s]*(\d{1,3}(?:,\d{3})*名[^\n]{0,24})",
            r"社員(?:数)?[：:\s]*(\d{1,3}(?:,\d{3})*名[^\n]{0,24})",
            r"従業員(?:数)?[：:\s]*約?(\d+)名",
        ):
            m = re.search(pat, text)
            if m:
                result["従業員数"] = m.group(1).strip()
                break

    if not _info_value_ok(result.get("資本金")):
        m = re.search(r"資本金[：:\s]*([\d,]+(?:\.\d+)?万円)", text)
        if m:
            result["資本金"] = m.group(1)

    if not _info_value_ok(result.get("代表者名")):
        for pat in (
            r"代表(?:者|取締役)[：:\s]*(?:代表取締役[：:\s]*)?"
            r"([一-龥々\u3400-\u9fff]{1,4}\s*[一-龥々\u3400-\u9fff]{1,4})",
            r"代表取締役(?:社長)?[：:\s]*"
            r"([一-龥々\u3400-\u9fff]{1,4}\s*[一-龥々\u3400-\u9fff]{1,4})",
        ):
            m = re.search(pat, text)
            if m:
                name = re.sub(r"\s+", " ", m.group(1).strip())
                if name and name not in ("代表取締役", "代表者"):
                    result["代表者名"] = name
                    break

    if not _info_value_ok(result.get("事業内容")):
        m = re.search(r"事業内容[：:\s]*([^\n]{10,80})", text)
        if m:
            result["事業内容"] = m.group(1).strip()[:50]


def _resolve_scraper_ai(openai_key_from_caller: str) -> tuple[str, str]:
    """
    使用する AI バックエンドと API キーを返す。
    戻り値: ("openai", key) | ("gemini", key) | ("ollama", key) | ("", "")

    環境変数 SCRAPER_AI_PROVIDER:
      - auto（既定）: OPENAI → GEMINI → LOCAL_LLM_URL（Ollama）の順
      - openai / gemini / ollama: 明示指定
    """
    mode = (os.environ.get("SCRAPER_AI_PROVIDER") or "auto").strip().lower()
    oa = (openai_key_from_caller or os.environ.get("OPENAI_API_KEY") or "").strip()
    gm = (os.environ.get("GEMINI_API_KEY") or "").strip()
    local_url = (os.environ.get("LOCAL_LLM_URL") or "").strip().rstrip("/")
    local_key = (os.environ.get("LOCAL_LLM_API_KEY") or "ollama").strip()

    if mode in ("ollama", "local", "local_llm"):
        return ("ollama", local_key) if local_url else ("", "")
    if mode == "openai":
        return ("openai", oa) if oa else ("", "")
    if mode == "gemini":
        return ("gemini", gm) if gm else ("", "")
    if oa:
        return ("openai", oa)
    if gm:
        return ("gemini", gm)
    if local_url:
        return ("ollama", local_key)
    return ("", "")


def extract_company_info_with_openai(page_text: str, company_name: str, openai_api_key: str) -> dict:
    info = {
        "代表者名":       "不明",
        "資本金":         "不明",
        "従業員数":       "不明",
        "設立年":         "不明",
        "事業内容":       "不明",
        "企業理念":       "",
        "ai_summary":     "",
        # ── 新規追加 ──────────────────────────────
        "site_age_score": 0,   # 0〜20 の整数。古いほど高い。
        "site_age_reason": "", # AIによる判定理由（UI表示用）
    }

    key = openai_api_key.strip() if openai_api_key else ""
    if not page_text or not key:
        return info

    prompt = _company_info_extraction_prompt(company_name, page_text)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        text = response.choices[0].message.content.strip()
        parsed = _parse_company_json_response(text)
        info.update(parsed)
        logger.debug(
            "extract_company_info_with_openai: AI抽出結果 company=%s 代表者名=%s 資本金=%s",
            company_name, parsed.get("代表者名"), parsed.get("資本金"),
        )

    except ImportError:
        info["ai_summary"] = "エラー: pip install openai でインストールしてください"
    except json.JSONDecodeError as e:
        info["ai_summary"] = f"AI JSONパースエラー: {str(e)[:100]}"
    except Exception as e:
        err_msg = str(e)
        if "401" in err_msg or "invalid_api_key" in err_msg:
            info["ai_summary"] = f"APIキーエラー: {err_msg[:150]}"
        else:
            info["ai_summary"] = f"AI抽出エラー: {err_msg[:150]}"

    return info


def extract_company_info_with_gemini(page_text: str, company_name: str, gemini_api_key: str) -> dict:
    """Google Gemini（google-genai）でサイトテキストから企業情報を抽出"""
    info = {
        "代表者名":       "不明",
        "資本金":         "不明",
        "従業員数":       "不明",
        "設立年":         "不明",
        "事業内容":       "不明",
        "企業理念":       "",
        "ai_summary":     "",
        "site_age_score": 0,
        "site_age_reason": "",
    }
    key = gemini_api_key.strip() if gemini_api_key else ""
    if not page_text or not key:
        return info

    prompt = _company_info_extraction_prompt(company_name, page_text)
    model = (os.environ.get("GEMINI_SCRAPER_MODEL") or "gemini-2.5-flash").strip()

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.0),
        )
        text = (response.text or "").strip()
        parsed = _parse_company_json_response(text)
        info.update(parsed)
        logger.debug(
            "extract_company_info_with_gemini: AI抽出結果 company=%s 代表者名=%s 資本金=%s",
            company_name, parsed.get("代表者名"), parsed.get("資本金"),
        )

    except ImportError:
        info["ai_summary"] = "エラー: pip install google-genai でインストールしてください"
    except json.JSONDecodeError as e:
        info["ai_summary"] = f"AI JSONパースエラー: {str(e)[:100]}"
    except Exception as e:
        err_msg = str(e)
        if "401" in err_msg or "api key" in err_msg.lower() or "PERMISSION_DENIED" in err_msg:
            info["ai_summary"] = f"Gemini APIキーエラー: {err_msg[:150]}"
        else:
            info["ai_summary"] = f"Gemini 抽出エラー: {err_msg[:150]}"

    return info


def extract_company_info_with_ollama(page_text: str, company_name: str) -> dict:
    """Ollama（OpenAI互換API）でサイトテキストから企業情報を抽出"""
    info = {
        "代表者名":       "不明",
        "資本金":         "不明",
        "従業員数":       "不明",
        "設立年":         "不明",
        "事業内容":       "不明",
        "企業理念":       "",
        "ai_summary":     "",
        "site_age_score": 0,
        "site_age_reason": "",
    }
    base_url = (os.environ.get("LOCAL_LLM_URL") or "").strip().rstrip("/")
    api_key = (os.environ.get("LOCAL_LLM_API_KEY") or "ollama").strip()
    model = (os.environ.get("LOCAL_LLM_MODEL") or "llama3.1:8b").strip()
    if not page_text or not base_url:
        return info

    prompt = _company_info_extraction_prompt(company_name, page_text)

    try:
        from openai import OpenAI
        client = OpenAI(base_url=base_url, api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        text = response.choices[0].message.content.strip()
        parsed = _parse_company_json_response(text)
        info.update(parsed)
        logger.debug(
            "extract_company_info_with_ollama: AI抽出結果 company=%s 代表者名=%s 資本金=%s model=%s",
            company_name, parsed.get("代表者名"), parsed.get("資本金"), model,
        )

    except ImportError:
        info["ai_summary"] = "エラー: pip install openai でインストールしてください"
    except json.JSONDecodeError as e:
        info["ai_summary"] = f"AI JSONパースエラー: {str(e)[:100]}"
    except Exception as e:
        info["ai_summary"] = f"Ollama 抽出エラー: {str(e)[:150]}"

    return info


# ─────────────────────────────────────────────
# 会社概要ページURL検出
# ─────────────────────────────────────────────

# 会社概要ページを示すキーワード（日本語・英語）
COMPANY_PROFILE_KEYWORDS = [
    "会社概要", "企業情報", "会社案内", "会社紹介", "企業概要",
    "会社情報", "概要", "会社について", "私たちについて",
    "company", "about", "profile", "corporate", "outline", "overview",
]

# 企業理念・挨拶ページ探索用キーワード（find_philosophy_url で使用）
PHILOSOPHY_KEYWORDS = [
    "企業理念", "経営理念", "理念", "ミッション", "ビジョン", "バリュー",
    "社是", "フィロソフィー", "philosophy", "mission", "vision", "value",
    "代表挨拶", "社長挨拶", "代表メッセージ", "社長メッセージ", "トップメッセージ",
    "greeting", "message", "president", "ceo",
    "会社の想い", "私たちの想い", "コンセプト", "concept",
]

def find_profile_url(base_url: str, html: str) -> str | None:
    """
    トップページのHTMLから会社概要ページのURLを1件だけ返す。
    URLエンコードされた日本語パス（例: /%e4%bc%9a%e7%a4%be%e6%a6%82%e8%a6%81/）も検出する。
    見つからない場合は None を返す。
    """
    from urllib.parse import urljoin, urlparse, unquote
    from html.parser import HTMLParser

    class LinkParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []  # (href, anchor_text)
        def handle_starttag(self, tag, attrs):
            if tag == "a":
                href = dict(attrs).get("href", "")
                if href:
                    self.links.append((href, ""))
        def handle_data(self, data):
            if self.links:
                href, _ = self.links[-1]
                self.links[-1] = (href, data.strip())

    parser = LinkParser()
    try:
        parser.feed(html)
    except Exception as e:
        logger.warning("会社概要URL検索中にHTMLパースエラー base_url=%s: %s", base_url, e)
        return None

    base_parsed = urlparse(base_url)
    base_domain = base_parsed.netloc

    for href, anchor in parser.links:
        # 外部リンクは除外
        if href.startswith("http") and urlparse(href).netloc != base_domain:
            continue
        # フラグメントのみ・空・mailto等は除外
        if not href or href.startswith("#") or href.startswith("mailto") or href.startswith("tel"):
            continue

        # URLエンコードをデコードして日本語キーワードも検出できるようにする
        href_decoded = unquote(href).lower()
        anchor_lower = anchor.lower()

        for kw in COMPANY_PROFILE_KEYWORDS:
            if kw in href_decoded or kw in anchor_lower:
                full_url = urljoin(base_url, href)
                logger.debug("会社概要URL検出 kw=%s href=%s", kw, full_url)
                return full_url

    return None


# ─────────────────────────────────────────────
# メインスクレイプ関数
# ─────────────────────────────────────────────

def find_philosophy_url(base_url: str, html: str) -> str | None:
    """
    トップページのHTMLから企業理念・挨拶ページのURLを1件だけ返す。
    find_profile_url と同じ仕組みで PHILOSOPHY_KEYWORDS で探索する。
    """
    from urllib.parse import urljoin, urlparse, unquote
    from html.parser import HTMLParser

    class LinkParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []
        def handle_starttag(self, tag, attrs):
            if tag == "a":
                href = dict(attrs).get("href", "")
                if href:
                    self.links.append((href, ""))
        def handle_data(self, data):
            if self.links:
                href, _ = self.links[-1]
                self.links[-1] = (href, data.strip())

    parser = LinkParser()
    try:
        parser.feed(html)
    except Exception as e:
        logger.warning("理念URL検索中にHTMLパースエラー base_url=%s: %s", base_url, e)
        return None

    base_parsed = urlparse(base_url)
    base_domain = base_parsed.netloc

    for href, anchor in parser.links:
        if href.startswith("http") and urlparse(href).netloc != base_domain:
            continue
        if not href or href.startswith("#") or href.startswith("mailto") or href.startswith("tel"):
            continue
        href_decoded = unquote(href).lower()
        anchor_lower = anchor.lower()
        for kw in PHILOSOPHY_KEYWORDS:
            if kw in href_decoded or kw in anchor_lower:
                full_url = urljoin(base_url, href)
                logger.debug("理念・挨拶URL検出 kw=%s href=%s", kw, full_url)
                return full_url
    return None


def scrape(
    url: str,
    company_name: str,
    screenshot_dir: str,
    openai_api_key: str = "",
    csv_id: str = "",
    tel: str = "",
    address: str = "",
) -> dict:
    result = {
        "screenshot_path":  None,
        "page_text":        "",
        "error":            None,
        "cms":              "不明",
        "server":           "不明",
        "responsive":       False,
        "technologies":     [],
        "代表者名":         "不明",
        "資本金":           "不明",
        "従業員数":         "不明",
        "設立年":           "不明",
        "事業内容":         "不明",
        "企業理念":         "",
        "ai_summary":       "",
        "法人番号":         "不明",
        "郵便番号":         "不明",
        "法人種別":         "不明",
        # ── サイトの古さスコア ─────────────────────
        "site_age_score":   0,
        "site_age_reason":  "",
        # ── ドメイン情報 ───────────────────────────
        "ip_address":       "不明",
        "hosting_company":  "不明",
        "hosting_org":      "不明",
        "whois_registrar":  "不明",
        "whois_expiry":     "不明",
        "name_servers":     "不明",
        "ssl_issuer":       "不明",
        "ssl_expiry":       "不明",
    }

    # csv_id 指定時は thumbnails/{csv_id}.jpg に保存（一括再スクショ用）
    thumb_dir = Path(os.environ.get("SCRAPER_THUMB_DIR", "thumbnails"))
    if csv_id:
        thumb_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = (thumb_dir / f"{csv_id}.jpg").resolve()
        _shot_type = "jpeg"
    else:
        Path(screenshot_dir).mkdir(exist_ok=True)
        safe_name = "".join(
            c for c in company_name if c.isalnum() or c in (" ", "_", "-")
        ).strip()
        screenshot_path = Path(screenshot_dir).resolve() / f"{safe_name}.png"
        _shot_type = "png"

    # ── 地図・検索ポータル系URLはホームページ扱いしない ──
    # Yelp / ナビタイム / Google Maps / 食べログ などは自社サイトではないため、
    # スクリーンショットも情報取得もスキップする
    if is_blocked_url(url):
        logger.info("ブロック対象URLのためスキップ: %s (%s)", url, company_name)
        result["error"] = "homepage_not_found"
        return result

    # ── 国税庁DB検索 ──────────────────────────
    # nenkan.db（企業年鑑）に存在する企業のみ houjin.db を参照する。
    # csv_id が渡されている = 年鑑DBにある企業 → 法人番号・郵便番号・法人種別を補完
    # csv_id が空 = 年鑑DBにない企業 → houjin.db を参照しない（別会社情報の混入防止）
    if csv_id:
        houjin_info = search_houjin_db(company_name)
        result.update(houjin_info)
    else:
        logger.debug("houjin.db検索スキップ（年鑑DB未登録企業）: %s", company_name)

    # ── Playwright スクレイピング ──────────────
    html = ""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                # HTTP・証明書エラーのサイトでもスクリーンショットを取得できるようにする
                ignore_https_errors=True,
            )
            page = context.new_page()

            # ── ① ページを開く ──────────────
            # 既定では入力URLをそのままスクショ＆照合する。
            # 旧仕様（ルートに正規化）を使いたい場合は環境変数で切替。
            from urllib.parse import urlparse as _urlparse
            _parsed = _urlparse(url)
            if os.environ.get("SCRAPER_FORCE_TOP_ROOT") == "1":
                top_url = f"{_parsed.scheme}://{_parsed.netloc}/"
            else:
                top_url = url

            # networkidle は常時通信のサイトでタイムアウトしやすい → DOM表示で進める。
            goto_timeout_ms = int(os.environ.get("SCRAPER_GOTO_TIMEOUT_MS", "55000"))
            wait_after_s = float(os.environ.get("SCRAPER_TOP_WAIT_AFTER_S", "3"))
            last_exc: BaseException | None = None
            for wait_until in ("domcontentloaded", "load"):
                try:
                    page.goto(top_url, timeout=goto_timeout_ms, wait_until=wait_until)
                    last_exc = None
                    break
                except BaseException as e:
                    last_exc = e
            if last_exc is not None:
                raise last_exc
            time.sleep(wait_after_s)

            # ── ② トップページのスクリーンショットを撮る ──
            ss_timeout = int(os.environ.get("SCRAPER_SS_TIMEOUT_MS", "45000"))
            page.screenshot(
                path=str(screenshot_path),
                full_page=False,
                type=_shot_type,
                timeout=ss_timeout,
                animations="disabled",
            )
            result["screenshot_path"] = str(screenshot_path)
            logger.debug("スクリーンショット取得 url=%s -> %s", top_url, screenshot_path)

            # ── ③ トップページのHTMLとテキストを取得 ──
            html = page.content()
            text = page.inner_text("body")
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            top_text = "\n".join(lines)

            # ── ④ 会社概要ページを追加取得 ──────────
            # find_profile_url は top_url 基準で検索する
            profile_text = ""
            profile_url = find_profile_url(top_url, html)
            if profile_url:
                try:
                    page.goto(profile_url, timeout=15000, wait_until="domcontentloaded")
                    time.sleep(1)
                    profile_raw = page.inner_text("body")
                    profile_lines = [l.strip() for l in profile_raw.splitlines() if l.strip()]
                    profile_text = "\n".join(profile_lines)
                    result["profile_url"] = profile_url
                    logger.debug("会社概要ページ取得 %s", profile_url)
                except Exception as e:
                    logger.warning("会社概要ページ取得失敗 url=%s profile_url=%s: %s", url, profile_url, e)

            # ── ⑤ 企業理念・挨拶ページを追加取得 ──────────
            philosophy_text = ""
            philosophy_url  = find_philosophy_url(top_url, html)
            # profile_url と同じURLの場合はスキップ
            if philosophy_url and philosophy_url != profile_url:
                try:
                    page.goto(philosophy_url, timeout=15000, wait_until="domcontentloaded")
                    time.sleep(1)
                    phil_raw   = page.inner_text("body")
                    phil_lines = [l.strip() for l in phil_raw.splitlines() if l.strip()]
                    philosophy_text = "\n".join(phil_lines)
                    result["philosophy_url"] = philosophy_url
                    logger.debug("理念・挨拶ページ取得 %s", philosophy_url)
                except Exception as e:
                    logger.warning("理念・挨拶ページ取得失敗 url=%s phil_url=%s: %s",
                                   url, philosophy_url, e)

            # トップ + 会社概要 + 理念ページを結合（合計8000文字上限）
            combined = top_text
            if profile_text:
                combined += "\n\n--- 会社概要ページ ---\n\n" + profile_text
            if philosophy_text:
                combined += "\n\n--- 企業理念・挨拶ページ ---\n\n" + philosophy_text
            result["page_text"] = combined[:8000]

            # ── 社名・TEL・住所照合 ─────────────────
            # 照合は 8000 字制限ではなく取得全文（さらにHTMLからも）使う。
            verify_source = combined
            page_title_for_verify = ""
            try:
                page_title_for_verify = page.title() or ""
            except Exception:
                pass
            try:
                if html:
                    import re as _re
                    html_text = _re.sub(r"<[^>]+>", " ", html)
                    verify_source = combined + "\n" + html_text
            except Exception:
                pass
            try:
                from company_verify import verify_company_page
                from urllib.parse import urlparse as _urlparse2
                _hostname = _urlparse2(top_url).netloc
                vstatus = verify_company_page(
                    verify_source,
                    company_name,
                    tel=tel,
                    address=address,
                    hostname=_hostname,
                    page_title=page_title_for_verify,
                )
                result["url_verify"] = vstatus
                if vstatus == "mismatch":
                    logger.warning(
                        "URL不一致: company=%s url=%s (社名/TEL/住所がページに無い)",
                        company_name, url,
                    )
                    result["error"] = "url_mismatch"
                    keep_mismatch_ss = os.environ.get(
                        "SCRAPER_KEEP_MISMATCH_SCREENSHOT", ""
                    ).strip() in ("1", "true", "yes")
                    if keep_mismatch_ss:
                        logger.info(
                            "照合不一致だがスクショ保持 SCRAPER_KEEP_MISMATCH_SCREENSHOT "
                            "(company=%s)",
                            company_name,
                        )
                    else:
                        result["screenshot_path"] = None
                        if screenshot_path.exists():
                            try:
                                screenshot_path.unlink()
                            except OSError:
                                pass
                elif vstatus == "review":
                    logger.info("URL要確認(社名のみ一致): %s %s", company_name, url)
            except ImportError:
                result["url_verify"] = "unknown"

            browser.close()
    except Exception as e:
        import traceback
        result["error"] = traceback.format_exc()

    # ── 技術スタック判定 ──────────────────────
    try:
        headers_resp = {}
        try:
            r = requests.get(url, timeout=8, verify=False,
                             headers={"User-Agent": "Mozilla/5.0"})
            headers_resp = dict(r.headers)
            if not html:
                html = r.text
        except requests.Timeout:
            logger.warning("技術スタック用HTTPタイムアウト url=%s", url)
        except requests.RequestException as e:
            logger.warning("技術スタック用HTTP通信エラー url=%s: %s", url, e)

        tech = detect_tech_stack(url, html, headers_resp)
        result["cms"]          = tech["cms"]
        result["server"]       = tech["server"]
        result["responsive"]   = tech["responsive"]
        result["technologies"] = tech["technologies"]
    except Exception as e:
        logger.warning("技術スタック判定エラー url=%s: %s", url, e)

    # ── AI情報抽出（古さスコア含む）────────────
    if result.get("page_text"):
        _fill_company_fields_from_text(result)

    # SCRAPER_SKIP_AI=1 のときは API を使わない（一括再スクショ用）
    skip_ai = os.environ.get("SCRAPER_SKIP_AI", "").strip() in ("1", "true", "yes")
    provider, ai_key = _resolve_scraper_ai(openai_api_key)
    if not skip_ai and result["page_text"] and provider and ai_key:
        logger.debug(
            "AI情報抽出開始 provider=%s company=%s page_text長=%d文字 概要ページ=%s",
            provider, company_name, len(result["page_text"]),
            result.get("profile_url", "なし"),
        )
        if provider == "openai":
            ai_info = extract_company_info_with_openai(
                result["page_text"], company_name, ai_key
            )
        elif provider == "ollama":
            ai_info = extract_company_info_with_ollama(
                result["page_text"], company_name
            )
        else:
            ai_info = extract_company_info_with_gemini(
                result["page_text"], company_name, ai_key
            )
        result.update(ai_info)
    else:
        if not result["page_text"]:
            logger.warning("AI情報抽出スキップ: ページテキストが空 company=%s", company_name)
        elif skip_ai:
            logger.debug("AI情報抽出スキップ: SCRAPER_SKIP_AI company=%s", company_name)
        elif not provider or not ai_key:
            logger.warning(
                "AI情報抽出スキップ: APIキー／Ollama URL 未設定、"
                "または SCRAPER_AI_PROVIDER と設定が不一致 company=%s",
                company_name,
            )

    if result.get("page_text"):
        _fill_company_fields_from_text(result)

    # ── ドメイン情報取得（キャッシュ付き）────────
    if url and url.startswith("http"):
        domain_info = get_domain_info(url)
        result.update(domain_info)

    return result


if __name__ == "__main__":
    # 引数: scraper.py <url> <company_name> <screenshot_dir> [csv_id]
    # csv_id: 年鑑DB登録企業かどうかの判定に使用（空文字なら houjin.db 検索をスキップ）
    url            = sys.argv[1]
    company_name   = sys.argv[2]
    screenshot_dir = sys.argv[3]
    csv_id_arg     = sys.argv[4] if len(sys.argv) > 4 else ""
    openai_api_key = os.environ.get("OPENAI_API_KEY", "")

    try:
        output = scrape(url, company_name, screenshot_dir, openai_api_key, csv_id=csv_id_arg)
    except Exception:
        import traceback
        output = {
            "screenshot_path": None, "page_text": "", "error": traceback.format_exc(),
            "cms": "不明", "server": "不明", "responsive": False, "technologies": [],
            "代表者名": "不明", "資本金": "不明", "従業員数": "不明",
            "設立年": "不明", "事業内容": "不明", "ai_summary": "",
            "法人番号": "不明", "郵便番号": "不明", "法人種別": "不明",
            "site_age_score": 0, "site_age_reason": "",
            "ip_address": "不明", "hosting_company": "不明", "hosting_org": "不明",
            "whois_registrar": "不明", "whois_expiry": "不明",
            "name_servers": "不明", "ssl_issuer": "不明", "ssl_expiry": "不明",
        }

    # Windowsバックスラッシュをスラッシュに統一
    if output.get("screenshot_path"):
        output["screenshot_path"] = output["screenshot_path"].replace("\\", "/")

    # stdout はJSONのみ・stderrにはロガーが書くので混在しない
    sys.stdout.buffer.write(json.dumps(output, ensure_ascii=False).encode("utf-8"))
