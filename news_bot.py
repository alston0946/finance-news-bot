import os
import re
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup
from dateutil import tz


# =========================
# 1. 基础配置
# =========================

# 只保留最近 24 小时
MAX_HOURS_OLD = 24

# 最多发送多少条
MAX_ITEMS = 20

# 请求头，尽量像正常浏览器
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    )
}

# 中文财经关键词：只保留更偏中国国内财经/资本市场的内容
KEYWORDS_INCLUDE = [
    "a股", "沪深", "上证", "深证", "北交所", "科创板", "创业板",
    "人民币", "央行", "中国人民银行", "国债", "lpr", "mlf", "逆回购",
    "财政部", "发改委", "工信部", "国家统计局", "商务部", "国务院",
    "证监会", "深交所", "上交所", "北证", "etf", "公募", "私募",
    "半导体", "算力", "机器人", "新能源", "光伏", "储能", "汽车",
    "地产", "消费", "白酒", "银行", "保险", "券商", "黄金", "铜",
    "原油", "煤炭", "钢铁", "航运", "电力", "基建", "制造业",
    "宏观", "经济", "社融", "cpi", "ppi", "出口", "进口"
]

# 可选排除词：尽量减少纯国际突发、战争、娱乐类快讯
KEYWORDS_EXCLUDE = [
    "以色列", "伊朗", "乌克兰", "俄罗斯", "足球", "篮球", "娱乐", "明星"
]

# 邮件配置，从 GitHub Secrets 读取
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587").strip())
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)

# 时区：统一按北京时间判断“最近一天”
TZ_SHANGHAI = tz.gettz("Asia/Shanghai")


# =========================
# 2. 通用工具函数
# =========================

def now_bj() -> datetime:
    return datetime.now(TZ_SHANGHAI)


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def contains_include_keywords(text: str) -> bool:
    text_lower = text.lower()
    return any(k.lower() in text_lower for k in KEYWORDS_INCLUDE)


def contains_exclude_keywords(text: str) -> bool:
    text_lower = text.lower()
    return any(k.lower() in text_lower for k in KEYWORDS_EXCLUDE)


def within_last_24h(dt_obj: Optional[datetime]) -> bool:
    if dt_obj is None:
        return False
    cutoff = now_bj() - timedelta(hours=MAX_HOURS_OLD)
    return dt_obj >= cutoff


def parse_datetime_flexible(text: str) -> Optional[datetime]:
    """
    尝试解析多种常见时间格式，并统一转成北京时间。
    """
    if not text:
        return None

    text = normalize_text(text)

    # 1) RFC 风格，例如: Mon, 27 Jan 2025 14:26:00 -0500
    try:
        dt_obj = parsedate_to_datetime(text)
        if dt_obj is not None:
            return dt_obj.astimezone(TZ_SHANGHAI)
    except Exception:
        pass

    # 2) 2026-03-22 09:35:12
    m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", text)
    if m:
        try:
            dt_obj = datetime.strptime(
                f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S"
            )
            return dt_obj.replace(tzinfo=TZ_SHANGHAI)
        except Exception:
            pass

    # 3) 2026-03-22 09:35
    m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", text)
    if m:
        try:
            dt_obj = datetime.strptime(
                f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M"
            )
            return dt_obj.replace(tzinfo=TZ_SHANGHAI)
        except Exception:
            pass

    # 4) 03-22 09:35 或 3-22 09:35，默认补当前年份
    m = re.search(r"(\d{1,2}-\d{1,2})[ T](\d{2}:\d{2})", text)
    if m:
        try:
            year = now_bj().year
            dt_obj = datetime.strptime(
                f"{year}-{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M"
            )
            return dt_obj.replace(tzinfo=TZ_SHANGHAI)
        except Exception:
            pass

    # 5) 仅有 HH:MM，默认当天
    m = re.search(r"\b(\d{2}:\d{2})\b", text)
    if m:
        try:
            today = now_bj().strftime("%Y-%m-%d")
            dt_obj = datetime.strptime(
                f"{today} {m.group(1)}", "%Y-%m-%d %H:%M"
            )
            return dt_obj.replace(tzinfo=TZ_SHANGHAI)
        except Exception:
            pass

    return None


def deduplicate(items: List[Dict]) -> List[Dict]:
    seen = set()
    result = []
    for item in items:
        key = (item.get("title", ""), item.get("link", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def sort_items(items: List[Dict]) -> List[Dict]:
    return sorted(
        items,
        key=lambda x: x.get("dt") or datetime(1970, 1, 1, tzinfo=TZ_SHANGHAI),
        reverse=True
    )


# =========================
# 3. 抓取：新浪财经 7x24
# =========================

def fetch_sina_7x24() -> List[Dict]:
    """
    尝试从新浪财经 7x24 页提取快讯。
    页面当前存在 7x24 财经直播页。页面结构如改版，解析规则可能需要微调。
    """
    url = "https://finance.sina.com.cn/7x24/"
    items = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        print(f"[Sina] 请求失败: {e}")
        return items

    soup = BeautifulSoup(html, "lxml")

    # 方案 A：找所有可能带时间的文本块
    candidates = soup.find_all(["li", "div", "p"])

    for node in candidates:
        text = normalize_text(node.get_text(" ", strip=True))
        if not text or len(text) < 8:
            continue

        # 常见样式：10:56:47. 文本 或 10:56:47 文本
        m = re.match(r"^(\d{2}:\d{2}:\d{2}|\d{2}:\d{2})[\.。]?\s*(.+)$", text)
        if not m:
            continue

        time_part = m.group(1)
        title = normalize_text(m.group(2))
        if not title or len(title) < 4:
            continue

        dt_obj = parse_datetime_flexible(time_part)
        if not dt_obj:
            continue

        link = ""
        a = node.find("a", href=True)
        if a:
            link = a["href"].strip()

        items.append({
            "source": "新浪财经7x24",
            "title": title,
            "link": link,
            "published": dt_obj.strftime("%Y-%m-%d %H:%M:%S"),
            "dt": dt_obj,
        })

    # 方案 B：从全文正则兜底
    if not items:
        text = soup.get_text("\n", strip=True)
        pattern = re.compile(r"(\d{2}:\d{2}:\d{2}|\d{2}:\d{2})[\.。]?\s*([^\n]{6,120})")
        for m in pattern.finditer(text):
            dt_obj = parse_datetime_flexible(m.group(1))
            title = normalize_text(m.group(2))
            if not dt_obj or not title:
                continue
            items.append({
                "source": "新浪财经7x24",
                "title": title,
                "link": "",
                "published": dt_obj.strftime("%Y-%m-%d %H:%M:%S"),
                "dt": dt_obj,
            })

    return deduplicate(items)


# =========================
# 4. 抓取：东方财富快讯（移动页）
# =========================

def fetch_eastmoney_quicknews() -> List[Dict]:
    """
    尝试抓取东方财富移动快讯页。
    该站当前存在移动端快讯入口。
    """
    url = "https://wap.eastmoney.com/kuaixun/index.html"
    items = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        print(f"[Eastmoney] 请求失败: {e}")
        return items

    soup = BeautifulSoup(html, "lxml")

    candidates = soup.find_all(["li", "div", "p", "a"])

    for node in candidates:
        text = normalize_text(node.get_text(" ", strip=True))
        if not text or len(text) < 8:
            continue

        # 例如：21:04 标题
        m = re.match(r"^(\d{2}:\d{2})(?:[:：]\d{2})?\s+(.+)$", text)
        if not m:
            continue

        time_part = m.group(1)
        title = normalize_text(m.group(2))
        if not title or len(title) < 4:
            continue

        dt_obj = parse_datetime_flexible(time_part)
        if not dt_obj:
            continue

        link = ""
        if node.name == "a" and node.has_attr("href"):
            link = node["href"].strip()
        else:
            a = node.find("a", href=True)
            if a:
                link = a["href"].strip()

        if link and link.startswith("//"):
            link = "https:" + link
        elif link and link.startswith("/"):
            link = "https://wap.eastmoney.com" + link

        items.append({
            "source": "东方财富快讯",
            "title": title,
            "link": link,
            "published": dt_obj.strftime("%Y-%m-%d %H:%M:%S"),
            "dt": dt_obj,
        })

    return deduplicate(items)


# =========================
# 5. 过滤：只保留国内财经且最近一天
# =========================

def filter_domestic_recent_news(items: List[Dict]) -> List[Dict]:
    result = []

    for item in items:
        title = normalize_text(item.get("title", ""))
        link = item.get("link", "")
        dt_obj = item.get("dt")

        if not title:
            continue

        # 最近 24 小时
        if not within_last_24h(dt_obj):
            continue

        # 包含国内财经关键词
        if not contains_include_keywords(title):
            continue

        # 排除明显非目标内容
        if contains_exclude_keywords(title):
            continue

        result.append({
            "source": item.get("source", ""),
            "title": title,
            "link": link,
            "published": item.get("published", ""),
            "dt": dt_obj,
        })

    result = deduplicate(result)
    result = sort_items(result)
    return result[:MAX_ITEMS]


# =========================
# 6. 组装邮件
# =========================

def build_html(news_items: List[Dict]) -> str:
    today = now_bj().strftime("%Y-%m-%d")

    if not news_items:
        return f"""
        <html>
          <body style="font-family: Arial, Helvetica, sans-serif;">
            <h2>今日国内财经新闻摘要（{today}）</h2>
            <p>最近 24 小时内，没有筛选到符合条件的国内财经新闻。</p>
          </body>
        </html>
        """

    li_html = ""
    for i, item in enumerate(news_items, start=1):
        title = item["title"]
        link = item["link"]
        published = item["published"]
        source = item["source"]

        if link:
            title_html = f'<a href="{link}" target="_blank">{i}. {title}</a>'
        else:
            title_html = f"{i}. {title}"

        li_html += f"""
        <li style="margin-bottom: 14px;">
          <div>{title_html}</div>
          <div style="color: #666; font-size: 12px; margin-top: 4px;">
            来源：{source} ｜ 时间：{published}
          </div>
        </li>
        """

    return f"""
    <html>
      <body style="font-family: Arial, Helvetica, sans-serif;">
        <h2>今日国内财经新闻摘要（{today}）</h2>
        <p>以下为最近 24 小时内筛选出的国内财经新闻：</p>
        <ol>
          {li_html}
        </ol>
      </body>
    </html>
    """


# =========================
# 7. 发邮件
# =========================

def send_email(subject: str, html_body: str):
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS, EMAIL_TO]):
        raise ValueError("缺少 SMTP 配置，请检查 GitHub Secrets。")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())


# =========================
# 8. 主流程
# =========================

def main():
    all_items = []

    sina_items = fetch_sina_7x24()
    print(f"[INFO] 新浪抓到 {len(sina_items)} 条原始快讯")
    all_items.extend(sina_items)

    eastmoney_items = fetch_eastmoney_quicknews()
    print(f"[INFO] 东方财富抓到 {len(eastmoney_items)} 条原始快讯")
    all_items.extend(eastmoney_items)

    all_items = deduplicate(all_items)
    final_items = filter_domestic_recent_news(all_items)

    print(f"[INFO] 过滤后保留 {len(final_items)} 条国内财经新闻")

    html_body = build_html(final_items)
    subject = f"今日国内财经新闻摘要 - {now_bj().strftime('%Y-%m-%d')}"
    send_email(subject, html_body)

    print("[INFO] 邮件发送完成")


if __name__ == "__main__":
    main()
