import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import re
import yfinance as yf
import time
import os
from deep_translator import GoogleTranslator

# --- 环境配置 ---
TG_TOKEN = os.getenv('TG_TOKEN')
TG_CHAT_ID = os.getenv('TG_CHAT_ID')
DB_FILE = "seen_warning_letters.txt"
LAST_SUCCESS_FILE = "last_success_date.txt"

PAGE_URL = (
    "https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/"
    "compliance-actions-and-activities/warning-letters"
)
AJAX_URL = "https://www.fda.gov/datatables/views/ajax"
# GitHub Actions 被 FDA WAF 拦时的备用抓取（另一组出口 IP）
JINA_URL = "https://r.jina.ai/" + PAGE_URL

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

ALIAS_US = {
    "hf foods": "HFFG",
    "hf foods group": "HFFG",
    "genzyme": "SNY",
    "novo nordisk": "NVO",
    "eli lilly": "LLY",
    "hims & hers": "HIMS",
    "hims and hers": "HIMS",
}

LEGAL_STOP = {
    "INC", "CORP", "CORPORATION", "LTD", "LLC", "CO", "COMPANY", "PLC",
    "LP", "GMBH", "LIMITED", "HOLDINGS", "GROUP", "USA", "US", "THE",
    "AND", "DBA", "SA", "AG", "NV", "PVT", "PRIVATE",
}

US_EXCH = {
    "", "NMS", "NYQ", "NGM", "NCM", "ASE", "NYE",
    "NASDAQ", "NYSE", "AMEX", "PCX", "BATS", "CBOE",
}


def send_tg_message(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ 未配置 TG 参数，仅本地打印。")
        print(text)
        return False

    target_ids = [chat_id.strip() for chat_id in TG_CHAT_ID.split(",") if chat_id.strip()]
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    ok_any = False
    for chat_id in target_ids:
        try:
            res = requests.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }, timeout=20)
            print(f"📡 TG 发送状态 [{chat_id}]: {res.status_code}")
            if res.status_code != 200:
                print(f" ⚠️ 详情: {res.text}")
            else:
                ok_any = True
        except Exception as e:
            print(f"❌ TG 发送异常 [{chat_id}]: {e}")
    return ok_any


def convert_date_to_chinese(date_str):
    try:
        dt = datetime.strptime(date_str.strip(), "%m/%d/%Y")
        return f"{dt.year}年{dt.month}月{dt.day}日"
    except:
        return date_str


def clean_company_text(name):
    if not name:
        return []
    s = name.replace("\u00a0", " ")
    s = re.sub(r"(?i)\s+d\.?b\.?a\.?\s+", " / ", s)
    parts = re.split(r"\s*/\s*", s)
    kept = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if re.search(r"\.(com|net|org|io|co\.[a-z]{2})\b", p, re.I):
            continue
        kept.append(p)
    return kept or [name]


def normalize_name(name):
    if not name:
        return []
    clean_str = re.sub(r"[.,!|/&+'()]", " ", name)
    clean_str = re.sub(
        r"(?i)\b(inc|corp|corporation|ltd|llc|co|company|plc|lp|gmbh|"
        r"limited|holdings|group|usa|us|pvt|private)\b",
        " ",
        clean_str,
    )
    return [w for w in clean_str.upper().split() if len(w) >= 2 and w not in LEGAL_STOP]


def first_token_ok(a, b):
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 4 and long_.startswith(short):
        return True
    return False


def is_company_match(app_name, yf_name):
    app_words = normalize_name(app_name)
    yf_words = normalize_name(yf_name)
    if not app_words or not yf_words:
        return False
    if not first_token_ok(app_words[0], yf_words[0]):
        return False
    app_str = " ".join(app_words)
    yf_str = " ".join(yf_words)
    if app_str == yf_str:
        return True
    if len(app_str) >= 8 and (app_str in yf_str or yf_str in app_str):
        return True
    overlap = set(app_words).intersection(set(yf_words))
    if len(overlap) >= 2:
        return True
    if len(app_words) == 1 and len(yf_words) == 1 and app_words[0] == yf_words[0] and len(app_words[0]) >= 4:
        return True
    return False


def is_us_ticker(ticker):
    if not ticker:
        return False
    t = ticker.strip().upper()
    if "." in t:
        return False
    if t.endswith("-USD") or t.endswith("=X"):
        return False
    if re.search(r"-(P|WT|W|U|R)$", t):
        return False
    return True


def alias_ticker(company_name):
    low = (company_name or "").lower()
    for k, t in sorted(ALIAS_US.items(), key=lambda kv: -len(kv[0])):
        if k in low:
            return t
    return None


def get_stock_info_smart(name):
    try:
        chunks = clean_company_text(name)
        candidates = []

        alias = alias_ticker(name)
        if alias and is_us_ticker(alias):
            candidates.append(alias)

        queries = []
        for chunk in chunks:
            words = chunk.split()
            if words:
                queries.append(words[0])
            if len(words) >= 2:
                queries.append(" ".join(words[:2]))
            if len(words) >= 3:
                queries.append(" ".join(words[:3]))
        seen_q, uniq_q = set(), []
        for q in queries:
            k = q.lower()
            if k not in seen_q:
                seen_q.add(k)
                uniq_q.append(q)

        quotes = []
        for q in uniq_q:
            try:
                search = yf.Search(q, max_results=8)
                quotes.extend(search.quotes or [])
            except Exception as e:
                print(f"  [yf.Search] {q!r}: {e}")
            time.sleep(0.2)

        for q in quotes:
            ticker = (q.get("symbol") or "").upper()
            if not is_us_ticker(ticker):
                continue
            short_name = q.get("shortname") or ""
            long_name = q.get("longname") or ""
            hit = False
            for chunk in chunks + [name]:
                if is_company_match(chunk, short_name) or is_company_match(chunk, long_name):
                    hit = True
                    break
            if hit:
                candidates.append(ticker)

        ordered, seen_t = [], set()
        for t in candidates:
            if t not in seen_t and is_us_ticker(t):
                seen_t.add(t)
                ordered.append(t)

        for ticker in ordered:
            try:
                info = yf.Ticker(ticker).fast_info
                price = getattr(info, "last_price", None)
                cap = getattr(info, "market_cap", None)
                exch = (getattr(info, "exchange", None) or "").upper()
                if exch and exch not in US_EXCH:
                    continue
                if price is None:
                    continue
                cap_b = round(cap / 1e9, 2) if cap else None
                return {
                    "ticker": ticker,
                    "price": round(float(price), 2),
                    "cap": cap_b if cap_b is not None else 0,
                }
            except Exception as e:
                print(f"  [Ticker] {ticker}: {e}")
                continue
        return None
    except Exception as e:
        print(f"  [get_stock_info_smart] {name}: {e}")
        return None


def is_blocked_page(url, text):
    t = (text or "").lower()
    u = (url or "").lower()
    return (
        "abuse-detection-apology" in u
        or "abuse-detection-apology" in t
        or "apology_objects" in u
    )


def parse_html_table_rows(html):
    """解析首页静态表（约 10 行），WAF 备用路径用。"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="datatable") or soup.find("table")
    if not table:
        return []
    out = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        posted = tds[0].get_text(strip=True)
        issued = tds[1].get_text(strip=True)
        company = tds[2].get_text(strip=True)
        subject = tds[4].get_text(strip=True)
        a = tds[2].find("a")
        href = a.get("href") if a else ""
        if href and href.startswith("/"):
            link = "https://www.fda.gov" + href
        else:
            link = href or "无链接"
        out.append((posted, issued, company, subject, link))
    return out


def parse_jina_markdown(text):
    """jina 返回的是 markdown 表格 + 链接。"""
    rows = []
    # 典型: | 08/25/2026 | 07/27/2026 | [Vargas Produce LLC](https://www.fda.gov/...) | office | subject |
    line_re = re.compile(
        r"\|\s*(\d{2}/\d{2}/\d{4})\s*\|\s*(\d{2}/\d{2}/\d{4})\s*\|\s*(.*?)\s*\|"
    )
    for line in text.splitlines():
        m = line_re.search(line)
        if not m:
            continue
        posted, issued, rest = m.group(1), m.group(2), m.group(3)
        # rest 里第一段是公司（可能带链接），后面还有 office / subject
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 5:
            continue
        posted, issued, company_cell, _office, subject = cols[0], cols[1], cols[2], cols[3], cols[4]
        link_m = re.search(r"\[([^\]]+)\]\(([^)]+)\)", company_cell)
        if link_m:
            company = link_m.group(1).strip()
            href = link_m.group(2).strip()
            link = href if href.startswith("http") else ("https://www.fda.gov" + href)
        else:
            company = re.sub(r"[*\[\]]", "", company_cell).strip()
            link = "无链接"
        if re.match(r"\d{2}/\d{2}/\d{4}", posted) and company and company.lower() != "company name":
            rows.append((posted.strip(), issued.strip(), company, subject.strip(), link))
    return rows


def fetch_letters_ajax(session, html, days):
    m = re.search(r'"view_dom_id":"([^"]+)"', html)
    if not m:
        return None
    view_dom_id = m.group(1)
    cutoff = datetime.now().date() - timedelta(days=days)
    print(f"ajax view_dom_id={view_dom_id[:16]}... cutoff={cutoff}")
    letters = []
    start, length = 0, 100
    while True:
        params = {
            "_drupal_ajax": "1",
            "_wrapper_format": "drupal_ajax",
            "pager_element": "0",
            "view_args": "",
            "view_base_path": (
                "inspections-compliance-enforcement-and-criminal-investigations/"
                "compliance-actions-and-activities/warning-letters/datatables-data"
            ),
            "view_display_id": "warning_letter_solr_block",
            "view_dom_id": view_dom_id,
            "view_name": "warning_letter_solr_index",
            "view_path": (
                "/inspections-compliance-enforcement-and-criminal-investigations/"
                "compliance-actions-and-activities/warning-letters"
            ),
            "draw": "1",
            "start": str(start),
            "length": str(length),
        }
        try:
            ajax_resp = session.get(AJAX_URL, params=params, headers=HEADERS, timeout=30)
            if is_blocked_page(str(ajax_resp.url), ajax_resp.text):
                print("❌ ajax 也被 WAF 拦截")
                return None
            ajax_resp.raise_for_status()
            data = ajax_resp.json()
        except Exception as e:
            print(f"❌ ajax 失败 start={start}: {e}")
            return letters or None

        rows = data.get("data") or []
        print(f"ajax start={start} rows={len(rows)} total={data.get('recordsTotal')}")
        if not rows:
            break
        oldest = datetime.now().date()
        for row in rows:
            if len(row) < 5:
                continue
            posted = BeautifulSoup(str(row[0]), "html.parser").get_text(strip=True)
            issued = BeautifulSoup(str(row[1]), "html.parser").get_text(strip=True)
            cell = BeautifulSoup(str(row[2]), "html.parser")
            company = cell.get_text(strip=True)
            subject = BeautifulSoup(str(row[4]), "html.parser").get_text(strip=True)
            a = cell.find("a")
            href = a["href"] if a and a.has_attr("href") else ""
            link = f"https://www.fda.gov{href}" if href.startswith("/") else (href or "无链接")
            try:
                d = datetime.strptime(posted, "%m/%d/%Y").date()
            except ValueError:
                continue
            if d < oldest:
                oldest = d
            if d >= cutoff:
                letters.append((posted, issued, company, subject, link))
        if oldest < cutoff:
            break
        start += length
    return letters


def fetch_all_letters(days=14):
    session = requests.Session()

    # 1) 直连 FDA
    html = ""
    try:
        r = session.get(PAGE_URL, headers=HEADERS, timeout=30, allow_redirects=True)
        print(f"直连 HTTP {r.status_code} url={r.url} len={len(r.text)}")
        if not is_blocked_page(str(r.url), r.text) and r.status_code == 200:
            html = r.text
    except Exception as e:
        print(f"直连失败: {e}")

    if html:
        ajax_rows = fetch_letters_ajax(session, html, days)
        if ajax_rows:
            print(f"ajax 拿到 {len(ajax_rows)} 封")
            return ajax_rows
        table_rows = parse_html_table_rows(html)
        if table_rows:
            print(f"首页静态表拿到 {len(table_rows)} 封（ajax 不可用）")
            return table_rows

    # 2) GHA 被 WAF 拦：走 jina 代理读首页
    print("⚠️ FDA 直连被拦，改用 jina 备用源（首页表格）")
    try:
        jr = requests.get(JINA_URL, headers=HEADERS, timeout=60)
        print(f"jina HTTP {jr.status_code} len={len(jr.text)}")
        jr.raise_for_status()
        rows = parse_jina_markdown(jr.text)
        if not rows:
            rows = parse_html_table_rows(jr.text)
        print(f"jina 解析到 {len(rows)} 封")
        return rows
    except Exception as e:
        print(f"❌ jina 备用源也失败: {e}")
        return []


def main():
    today_str = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(LAST_SUCCESS_FILE):
        with open(LAST_SUCCESS_FILE, "r") as f:
            if f.read().strip() == today_str:
                print(f"📌 今日 ({today_str}) 已成功推送过数据，熔断机制启动：跳过本次执行。")
                return

    if not os.path.exists(DB_FILE):
        open(DB_FILE, "w").close()
    with open(DB_FILE, "r", encoding="utf-8") as f:
        seen_data = set(line.strip() for line in f if line.strip())

    days = 14
    cutoff_date = datetime.now().date() - timedelta(days=days)
    raw_rows = fetch_all_letters(days=days)
    if not raw_rows:
        print("❌ 未拿到任何警告信，退出且不写熔断。")
        return

    translator = GoogleTranslator(source="en", target="zh-CN")
    records_to_send = []
    scanned_new = 0

    for posted_date_str, issue_date_str, company_name, subject_en, letter_url in raw_rows:
        unique_key = letter_url if letter_url != "无链接" else f"{company_name}_{posted_date_str}"
        try:
            posted_date = datetime.strptime(posted_date_str, "%m/%d/%Y").date()
        except ValueError:
            continue
        if posted_date < cutoff_date:
            continue
        if unique_key in seen_data:
            continue

        scanned_new += 1
        print(f"  扫描: {posted_date_str} | {company_name}")
        stock_data = get_stock_info_smart(company_name)
        time.sleep(0.2)

        if stock_data:
            print(f"    ✅ ${stock_data['ticker']}")
            try:
                subject_cn = translator.translate(subject_en)
            except:
                subject_cn = subject_en
            records_to_send.append({
                "posted": convert_date_to_chinese(posted_date_str),
                "issued": convert_date_to_chinese(issue_date_str),
                "ticker": stock_data["ticker"],
                "company": company_name,
                "subject": subject_cn,
                "cap": stock_data["cap"],
                "price": stock_data["price"],
                "link": letter_url,
            })
        else:
            print("    — 无美股匹配")
        seen_data.add(unique_key)

    print(f"新扫描 {scanned_new} 封，命中美股 {len(records_to_send)} 家")

    if records_to_send:
        final_msg = f"<b>🚨FDA警告信预警 ({len(records_to_send)}家上市企业)</b>\n\n"
        msg_blocks = []
        for idx, item in enumerate(records_to_send, 1):
            block = (f"{idx}. 📅发布日期: {item['posted']}\n"
                     f" 📝签发日期: {item['issued']}\n"
                     f" 🏢公司: ${item['ticker']} ({item['company']})\n"
                     f" ⚠️原因: {item['subject']}\n"
                     f" 💰市值: ${item['cap']}B\n"
                     f" 💵股价: ${item['price']}\n"
                     f' 🔗<a href="{item["link"]}">点击查看公告</a>')
            msg_blocks.append(block)
        final_msg += "\n\n---------------\n\n".join(msg_blocks)
        final_msg += "\n\n#FDA #WarningLetters"

        sent = send_tg_message(final_msg)
        if sent or not (TG_TOKEN and TG_CHAT_ID):
            with open(LAST_SUCCESS_FILE, "w") as f:
                f.write(today_str)
            with open(DB_FILE, "w", encoding="utf-8") as f:
                for item in sorted(seen_data):
                    f.write(f"{item}\n")
        else:
            print("⚠️ TG 全部发送失败，不写熔断/数据库，下次重试。")
    else:
        print("💡 本次运行未发现匹配的上市企业新预警。")
        with open(DB_FILE, "w", encoding="utf-8") as f:
            for item in sorted(seen_data):
                f.write(f"{item}\n")


if __name__ == "__main__":
    main()
