import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
import feedparser

# ========= 1. 配置 =========
RSS_FEEDS = [
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
    "https://www.ft.com/rss/home",
]

KEYWORDS = [
    "Fed", "federal reserve", "interest rate", "inflation",
    "stocks", "market", "bond", "treasury", "oil", "gold",
    "USD", "yuan", "China", "A-share", "earnings"
]

MAX_ITEMS = 15

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)

# ========= 2. 抓取 RSS =========
def contains_keyword(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    return any(k.lower() in text_lower for k in keywords)

def fetch_news():
    items = []
    seen_links = set()

    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            published = getattr(entry, "published", "")

            if not title or not link:
                continue
            if link in seen_links:
                continue

            text_for_filter = f"{title} {getattr(entry, 'summary', '')}"
            if KEYWORDS and not contains_keyword(text_for_filter, KEYWORDS):
                continue

            seen_links.add(link)
            items.append({
                "title": title,
                "link": link,
                "published": published
            })

    # 简单截断
    return items[:MAX_ITEMS]

# ========= 3. 生成邮件内容 =========
def build_html(news_items):
    today = datetime.now().strftime("%Y-%m-%d")
    if not news_items:
        body = f"""
        <html>
          <body>
            <h2>今日财经新闻摘要（{today}）</h2>
            <p>今天没有筛选到符合关键词的新闻。</p>
          </body>
        </html>
        """
        return body

    li_html = ""
    for i, item in enumerate(news_items, start=1):
        title = item["title"]
        link = item["link"]
        published = item["published"]
        li_html += f"""
        <li style="margin-bottom: 12px;">
          <a href="{link}" target="_blank">{i}. {title}</a><br>
          <span style="color: gray; font-size: 12px;">{published}</span>
        </li>
        """

    body = f"""
    <html>
      <body>
        <h2>今日财经新闻摘要（{today}）</h2>
        <ol>
          {li_html}
        </ol>
      </body>
    </html>
    """
    return body

# ========= 4. 发送邮件 =========
def send_email(subject: str, html_body: str):
    if not all([SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_TO]):
        raise ValueError("缺少 SMTP 配置，请检查 GitHub Secrets。")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())

# ========= 5. 主流程 =========
def main():
    news_items = fetch_news()
    html_body = build_html(news_items)
    subject = f"每日财经新闻摘要 - {datetime.now().strftime('%Y-%m-%d')}"
    send_email(subject, html_body)
    print(f"已发送 {len(news_items)} 条新闻到邮箱。")

if __name__ == "__main__":
    main()