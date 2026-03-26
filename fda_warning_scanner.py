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
LAST_SUCCESS_FILE = "last_success_date.txt"  # <--- 新增状态文件

def send_tg_message(text):
    # 💡 核心修改：支持 TG_CHAT_ID 中填写多个 ID
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ 未配置 TG 参数，仅本地打印。")
        return
   
    target_ids = [chat_id.strip() for chat_id in TG_CHAT_ID.split(',') if chat_id.strip()]
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
   
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
        except Exception as e:
            print(f"❌ TG 发送异常 [{chat_id}]: {e}")

# --- 1. 日期与名称处理模块 (保留原逻辑) ---
def convert_date_to_chinese(date_str):
    try:
        dt = datetime.strptime(date_str.strip(), "%m/%d/%Y")
        return f"{dt.year}年{dt.month}月{dt.day}日"
    except:
        return date_str

def normalize_name(name):
    if not name: return []
    clean_str = re.sub(r'(?i)\b(inc|corp|corporation|ltd|llc|co|company|plc|lp|gmbh)\b|\.|,|-|!', ' ', name)
    return [w for w in clean_str.upper().split() if len(w) > 1]

def is_company_match(app_name, yf_name):
    app_words = normalize_name(app_name)
    yf_words = normalize_name(yf_name)
    if not app_words or not yf_words: return False
    if app_words[0] not in yf_words[0] and yf_words[0] not in app_words[0]: return False
    app_str = ' '.join(app_words)
    yf_str = ' '.join(yf_words)
    if app_str in yf_str or yf_str in app_str: return True
    overlap = set(app_words).intersection(set(yf_words))
    if len(overlap) >= 2: return True
    if len(app_words) == 1 and len(overlap) == 1: return True
    return False

def get_stock_info_smart(name):
    try:
        search_q = ' '.join(name.split()[:2])
        search = yf.Search(search_q, max_results=3)
        if not search.quotes: return None
        for q in search.quotes:
            ticker = q.get('symbol', '')
            if "." not in ticker:
                short_name = q.get('shortname', '')
                long_name = q.get('longname', '')
                if is_company_match(name, short_name) or is_company_match(name, long_name):
                    s = yf.Ticker(ticker)
                    info = s.fast_info
                    return {
                        "ticker": ticker,
                        "price": round(info.last_price, 2),
                        "cap": round(info.market_cap / 1e9, 2)
                    }
        return None
    except: return None

# --- 2. 核心抓取逻辑 ---
def main():
    # --- A. 今日熔断检查 ---
    today_str = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(LAST_SUCCESS_FILE):
        with open(LAST_SUCCESS_FILE, "r") as f:
            if f.read().strip() == today_str:
                print(f"📌 今日 ({today_str}) 已成功推送过数据，熔断机制启动：跳过本次执行。")
                return

    # --- B. 加载历史记录 ---
    if not os.path.exists(DB_FILE):
        open(DB_FILE, 'w').close()
    with open(DB_FILE, "r", encoding="utf-8") as f:
        seen_data = set(line.strip() for line in f if line.strip())

    # --- C. 执行抓取 ---
    days = 14
    url = "https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/compliance-actions-and-activities/warning-letters"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    session = requests.Session()
   
    try:
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
    except:
        return 
   
    dom_id_match = re.search(r'"view_dom_id":"([^"]+)"', response.text)
    if not dom_id_match: return
   
    view_dom_id = dom_id_match.group(1)
    ajax_url = "https://www.fda.gov/datatables/views/ajax"
    cutoff_date = datetime.now().date() - timedelta(days=days)
   
    start = 0
    length = 100
    keep_fetching = True
    translator = GoogleTranslator(source='en', target='zh-CN')
    records_to_send = []
   
    while keep_fetching:
        # 💡 这里还原了你原始代码中完整的所有参数
        params = {
            '_drupal_ajax': '1', 
            '_wrapper_format': 'drupal_ajax', 
            'pager_element': '0',
            'view_args': '', 
            'view_base_path': 'inspections-compliance-enforcement-and-criminal-investigations/compliance-actions-and-activities/warning-letters/datatables-data',
            'view_display_id': 'warning_letter_solr_block', 
            'view_dom_id': view_dom_id,
            'view_name': 'warning_letter_solr_index', 
            'view_path': '/inspections-compliance-enforcement-and-criminal-investigations/compliance-actions-and-activities/warning-letters',
            'draw': '1', 
            'start': str(start), 
            'length': str(length)
        }
       
        try:
            ajax_resp = session.get(ajax_url, params=params, headers=headers, timeout=30)
            data = ajax_resp.json()
        except:
            break
           
        rows = data.get('data', [])
        if not rows: break
           
        oldest_date_in_batch = datetime.now().date()
       
        for row in rows:
            if len(row) < 5: continue
           
            posted_date_str = BeautifulSoup(str(row[0]), "html.parser").get_text(strip=True)
            issue_date_str = BeautifulSoup(str(row[1]), "html.parser").get_text(strip=True)
            subject_en = BeautifulSoup(str(row[4]), "html.parser").get_text(strip=True)
           
            company_cell = BeautifulSoup(str(row[2]), "html.parser")
            company_name = company_cell.get_text(strip=True)
           
            a_tag = company_cell.find('a')
            letter_url = "无链接"
            if a_tag and 'href' in a_tag.attrs:
                href = a_tag['href']
                letter_url = f"https://www.fda.gov{href}" if href.startswith('/') else href
           
            unique_key = letter_url if letter_url != "无链接" else f"{company_name}_{posted_date_str}"
           
            try:
                posted_date = datetime.strptime(posted_date_str, "%m/%d/%Y").date()
                if posted_date < oldest_date_in_batch:
                    oldest_date_in_batch = posted_date
                   
                if posted_date >= cutoff_date and unique_key not in seen_data:
                    stock_data = get_stock_info_smart(company_name)
                    time.sleep(0.4)
                   
                    if stock_data:
                        try:
                            subject_cn = translator.translate(subject_en)
                        except:
                            subject_cn = subject_en
                           
                        records_to_send.append({
                            "posted": convert_date_to_chinese(posted_date_str),
                            "issued": convert_date_to_chinese(issue_date_str),
                            "ticker": stock_data['ticker'],
                            "company": company_name,
                            "subject": subject_cn,
                            "cap": stock_data['cap'],
                            "price": stock_data['price'],
                            "link": letter_url
                        })
                    # 只要扫描过就记录
                    seen_data.add(unique_key)
            except ValueError:
                continue
       
        if oldest_date_in_batch < cutoff_date:
            keep_fetching = False
        else:
            start += length

    # --- D. 组装消息与推送 (只有在有新上市企业记录时才运行) ---
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
        
        # 发送 TG 消息
        send_tg_message(final_msg)
       
        # 💡 只有发送成功后，才记录今日已完成
        with open(LAST_SUCCESS_FILE, "w") as f:
            f.write(today_str)
            
        # 只要有更新就重写数据库
        with open(DB_FILE, "w", encoding="utf-8") as f:
            for item in sorted(seen_data):
                f.write(f"{item}\n")
    else:
        print("💡 本次运行未发现匹配的上市企业新预警。")

if __name__ == "__main__":
    main()
