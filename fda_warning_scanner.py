#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FDA Warning Letters 抓取诊断脚本
用途：放到 GitHub Actions 里先跑这一份，确认是熔断 / 解析失败 / IP 拦截，还是接口正常。
不依赖 9/1 还是 9/2：只检查页面、AJAX、JSON 结构是否可用。
"""

import os
import re
import traceback
from datetime import datetime, timezone

import requests

PAGE_URL = (
    "https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations"
    "/compliance-actions-and-activities/warning-letters"
)
AJAX_URL = "https://www.fda.gov/datatables/views/ajax"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def preview(text, n=400):
    return (text or "").replace("\n", " ")[:n]


def main():
    now_utc = datetime.now(timezone.utc)
    print(f"utc_now          : {now_utc.isoformat()}")
    print(f"local_now        : {datetime.now().isoformat()}")
    print(f"cwd              : {os.getcwd()}")
    print(f"files_in_cwd     : {sorted(os.listdir('.'))[:50]}")

    section("1) 本地状态文件")
    for fname in ("last_success_date.txt", "seen_warning_letters.txt"):
        exists = os.path.exists(fname)
        print(f"{fname}: exists={exists}")
        if exists:
            with open(fname, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
            lines = [ln for ln in raw.splitlines() if ln.strip()]
            print(f"  bytes={len(raw.encode('utf-8', errors='replace'))} lines={len(lines)}")
            print(f"  head={preview(raw, 200)}")
            today = datetime.now().strftime("%Y-%m-%d")
            if fname == "last_success_date.txt" and raw.strip() == today:
                print("  !! 熔断会命中：原脚本今天会直接 return，看起来就像 0 秒成功。")

    section("2) 出口 IP（对照是不是 GitHub/Azure 机房）")
    egress_ip = None
    for ip_url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            r = requests.get(ip_url, timeout=15)
            print(f"{ip_url} -> {r.status_code} {r.text.strip()}")
            if r.ok and r.text.strip():
                egress_ip = r.text.strip()
                break
        except Exception as e:
            print(f"{ip_url} FAIL: {type(e).__name__}: {e}")

    if egress_ip:
        try:
            geo = requests.get(f"https://ipinfo.io/{egress_ip}/json", timeout=15)
            print("ipinfo:", preview(geo.text, 500))
        except Exception as e:
            print("ipinfo FAIL:", e)

    session = requests.Session()
    page_headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }

    section("3) 抓 Warning Letters 首页")
    try:
        page = session.get(PAGE_URL, headers=page_headers, timeout=30)
    except Exception:
        print("PAGE REQUEST EXCEPTION:")
        traceback.print_exc()
        return 2

    print(f"status           : {page.status_code}")
    print(f"final_url        : {page.url}")
    print(f"content-type     : {page.headers.get('content-type')}")
    print(f"server           : {page.headers.get('server')}")
    print(f"x-cache-status   : {page.headers.get('x-cache-status')}")
    print(f"cf-ray           : {page.headers.get('cf-ray')}")
    print(f"body_len         : {len(page.text)}")
    print(f"body_head        : {preview(page.text, 350)}")

    blocked_hints = [
        "access denied",
        "just a moment",
        "captcha",
        "reference #",
        "request unsuccessful",
        "akamai",
        "bot detected",
        "forbidden",
    ]
    lowered = page.text.lower()
    hits = [h for h in blocked_hints if h in lowered]
    print(f"block_hints      : {hits or 'none'}")

    dom = re.search(r'"view_dom_id":"([^"]+)"', page.text)
    print(f"view_dom_id      : {dom.group(1) if dom else 'MISSING'}")

    if page.status_code != 200:
        print("VERDICT: 首页 HTTP 非 200。很大概率是机房 IP / WAF。")
        return 3
    if hits and not dom:
        print("VERDICT: 首页像拦截页，且没有 view_dom_id。")
        return 3
    if not dom:
        print("VERDICT: 200 但解析不到 view_dom_id，页面结构变了或返回的不是列表页。")
        return 4

    section("4) 打 datatables AJAX")
    params = {
        "_drupal_ajax": "1",
        "_wrapper_format": "drupal_ajax",
        "pager_element": "0",
        "view_args": "",
        "view_base_path": (
            "inspections-compliance-enforcement-and-criminal-investigations"
            "/compliance-actions-and-activities/warning-letters/datatables-data"
        ),
        "view_display_id": "warning_letter_solr_block",
        "view_dom_id": dom.group(1),
        "view_name": "warning_letter_solr_index",
        "view_path": (
            "/inspections-compliance-enforcement-and-criminal-investigations"
            "/compliance-actions-and-activities/warning-letters"
        ),
        "draw": "1",
        "start": "0",
        "length": "10",
    }
    ajax_headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": PAGE_URL,
    }
    try:
        ajax = session.get(AJAX_URL, params=params, headers=ajax_headers, timeout=30)
    except Exception:
        print("AJAX REQUEST EXCEPTION:")
        traceback.print_exc()
        return 5

    print(f"status           : {ajax.status_code}")
    print(f"content-type     : {ajax.headers.get('content-type')}")
    print(f"body_len         : {len(ajax.text)}")
    print(f"body_head        : {preview(ajax.text, 400)}")

    try:
        data = ajax.json()
    except Exception as e:
        print(f"JSON PARSE FAIL  : {type(e).__name__}: {e}")
        print("VERDICT: AJAX 不是 JSON。典型情况是 WAF 返回了 HTML 挑战页。")
        return 6

    rows = data.get("data") or []
    print(f"json_keys        : {list(data.keys())}")
    print(f"recordsTotal     : {data.get('recordsTotal')}")
    print(f"recordsFiltered  : {data.get('recordsFiltered')}")
    print(f"rows_this_page   : {len(rows)}")

    if not rows:
        print("VERDICT: JSON 通了但 data 为空，接口参数可能过期。")
        return 7

    section("5) 前几条样本（只验证解析，不查股票、不发 TG）")
    from bs4 import BeautifulSoup

    for i, row in enumerate(rows[:8], 1):
        cells = [BeautifulSoup(str(c), "html.parser").get_text(" ", strip=True) for c in row]
        posted = cells[0] if len(cells) > 0 else ""
        issued = cells[1] if len(cells) > 1 else ""
        company = cells[2] if len(cells) > 2 else ""
        subject = cells[4] if len(cells) > 4 else ""
        href = ""
        soup_co = BeautifulSoup(str(row[2]), "html.parser")
        a = soup_co.find("a")
        if a and a.get("href"):
            href = a["href"]
            if href.startswith("/"):
                href = "https://www.fda.gov" + href
        print(f"{i:02d}. posted={posted} issued={issued} company={company}")
        print(f"    subject={subject}")
        print(f"    link={href or '-'}")

    section("6) 结论")
    print("接口可用。日期是 9/1 还是 9/2 不影响这次验证。")
    print("只要 recordsTotal > 0 且能解析出公司名，就说明抓取链路没断。")
    print("若这份脚本在 Actions 里失败、你电脑上成功，基本就是 GitHub 出口 IP 被拦。")
    print("若这份脚本也是秒结束且停在第 1 步熔断提示，先删仓库里的 last_success_date.txt。")
    return 0


if __name__ == "__main__":
    try:
        from bs4 import BeautifulSoup  # noqa: F401
    except ImportError:
        print("需要: pip install requests beautifulsoup4")
        raise
    raise SystemExit(main())
