# -*- coding: utf-8 -*-
"""
每日全球科技与格局摘要脚本（适合零基础本地运行）

这个程序会依次做 3 件事：
1. 从几个科技 / 宏观新闻网站的 RSS 源抓取最新新闻
2. 把前 5 条发给 DeepSeek，生成一份中文摘要
3. 把摘要和原始新闻链接发到一个或多个 QQ 邮箱
"""

import html
import os
import re
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import feedparser
import requests
from dotenv import load_dotenv


# ============================================================
# 第一部分：加载配置（敏感信息全部从 .env 读取，绝不写死）
# ============================================================

# load_dotenv() 会读取同目录下的 .env 文件，把里面的键值变成环境变量
load_dotenv()

# os.getenv 用来安全地取出配置；取不到时返回空字符串，后面会再检查
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip() or "deepseek-chat"
QQ_EMAIL = os.getenv("QQ_EMAIL", "").strip()
QQ_AUTH_CODE = os.getenv("QQ_AUTH_CODE", "").strip()


def parse_recipients(raw_value):
    """
    把收件人字符串拆成邮箱列表。
    支持用英文逗号、中文逗号、分号或换行分隔，例如：
    a@qq.com,b@qq.com；c@qq.com
    """
    emails = []
    seen = set()
    for part in re.split(r"[,，;；\n]+", raw_value or ""):
        email = part.strip()
        if not email:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        emails.append(email)
    return emails


# 没填收件人时，默认发给发件人自己
TO_EMAILS = parse_recipients(os.getenv("TO_EMAIL", "").strip() or QQ_EMAIL)

# DeepSeek 的接口和 OpenAI 格式兼容，完整地址是：
# https://api.deepseek.com/chat/completions
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

# 访问新闻网站时带上浏览器身份，减少被网站拦截的概率
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# 目前可用的真实 RSS 源（2026 年 10 月核对过）：
# - TechCrunch 全站科技新闻
# - TechCrunch 人工智能分类
# - Wired 科技评论
# - Ars Technica 科技深度报道
# - 经济学人「科学与技术」栏目（部分网络可能打不开，打不开会自动跳过）
RSS_FEEDS = [
    "https://techcrunch.com/feed/",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://www.wired.com/feed/rss",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://www.economist.com/science-and-technology/rss.xml",
]


def check_config():
    """启动前先检查必填配置是否齐全，避免跑到一半才发现没填密钥。"""
    missing = []
    if not DEEPSEEK_API_KEY or "请在这里填写" in DEEPSEEK_API_KEY:
        missing.append("DEEPSEEK_API_KEY")
    if not QQ_EMAIL or "@qq.com" not in QQ_EMAIL.lower():
        missing.append("QQ_EMAIL（请填写完整的 QQ 邮箱，例如 123456789@qq.com）")
    if not QQ_AUTH_CODE or "请在这里填写" in QQ_AUTH_CODE:
        missing.append("QQ_AUTH_CODE")
    if not TO_EMAILS:
        missing.append("TO_EMAIL（可填多个，用英文逗号分隔）")

    if missing:
        print("配置不完整，请先打开 .env 文件补全下面这些内容：")
        for item in missing:
            print(f"  - {item}")
        print("\n如果你还没有 .env 文件，可以先复制 .env.example 再改名填写。")
        raise SystemExit(1)


# ============================================================
# 第二部分：新闻抓取
# ============================================================

def clean_text(raw_text):
    """把简介里的 HTML 标签清掉，只留下普通人能读的纯文字。"""
    if not raw_text:
        return ""
    # 把 HTML 实体（例如 &amp;）还原成普通字符
    text = html.unescape(raw_text)
    # 去掉 <p>、<a> 这类标签
    text = re.sub(r"<[^>]+>", " ", text)
    # 把连续空格、换行压成一个空格
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_news(limit=5):
    """
    依次读取 RSS 源，收集新闻标题、简介和链接。
    默认只要最新的 5 条（按各源出现顺序，去重后截取）。
    """
    collected = []
    seen_links = set()

    for url in RSS_FEEDS:
        print(f"正在抓取：{url}")
        try:
            # 先用 requests 把 RSS 原文下载下来，再交给 feedparser 解析
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
            response.raise_for_status()
            feed = feedparser.parse(response.content)

            if not feed.entries:
                print("  这个源暂时没有条目，已跳过。")
                continue

            source_name = feed.feed.get("title", url)
            added_from_this_feed = 0

            for entry in feed.entries:
                link = (entry.get("link") or "").strip()
                title = clean_text(entry.get("title", ""))
                summary = clean_text(entry.get("summary", "") or entry.get("description", ""))

                # 没有标题或链接的条目没有意义，直接跳过
                if not title or not link:
                    continue
                # 同一篇文章可能出现在多个源里，用链接去重
                if link in seen_links:
                    continue

                seen_links.add(link)
                collected.append(
                    {
                        "source": source_name,
                        "title": title,
                        "summary": summary[:400],  # 简介太长时只保留前面一段
                        "link": link,
                    }
                )
                added_from_this_feed += 1

                # 每个源先拿几条，避免 5 条全来自同一个网站
                if added_from_this_feed >= 3:
                    break

            print(f"  已从该源加入 {added_from_this_feed} 条。")

        except requests.RequestException as error:
            # 某个网站打不开时，打印原因后继续抓下一个，不让整个程序中断
            print(f"  抓取失败，已跳过。原因：{error}")

        if len(collected) >= limit:
            break

    news_list = collected[:limit]
    print(f"\n一共准备了 {len(news_list)} 条新闻，将送给 AI 总结。")
    return news_list


# ============================================================
# 第三部分：调用 DeepSeek，生成中文摘要
# ============================================================

def build_prompt(news_list):
    """把 5 条新闻整理成一段发给大模型的中文说明。"""
    lines = []
    for index, item in enumerate(news_list, start=1):
        lines.append(
            f"{index}. 来源：{item['source']}\n"
            f"   标题：{item['title']}\n"
            f"   简介：{item['summary'] or '（该条没有简介）'}\n"
            f"   链接：{item['link']}"
        )
    news_text = "\n\n".join(lines)

    return (
        "下面是今天抓取到的若干条全球科技 / 宏观新闻标题和简介。"
        "请基于这些材料，写一份大约 300 字的「今日全球科技与格局摘要」。\n"
        "要求：\n"
        "1. 用简体中文撰写；\n"
        "2. 不要逐条翻译，而要提炼趋势、格局和可能影响；\n"
        "3. 如果材料有限，请明确说明依据有限，不要编造没有出现的事实；\n"
        "4. 控制在 280～350 字左右。\n\n"
        f"新闻材料：\n{news_text}"
    )


def summarize_with_deepseek(news_list):
    """用 requests 调用 DeepSeek（OpenAI 兼容格式），生成宏观科技趋势摘要。"""
    print("正在调用 DeepSeek 生成摘要...")

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一名专业的宏观科技趋势分析师。"
                    "你擅长把零散新闻整理成面向决策者的中文简报，"
                    "语言冷静、具体、有层次。"
                ),
            },
            {
                "role": "user",
                "content": build_prompt(news_list),
            },
        ],
        "temperature": 0.4,
        "max_tokens": 800,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
    }

    response = requests.post(
        DEEPSEEK_API_URL,
        headers=headers,
        json=payload,
        timeout=60,
    )

    # 常见失败：密钥错误、余额不足、模型名过期。把服务器返回的原文打印出来方便排查
    if response.status_code != 200:
        print("DeepSeek 接口调用失败。")
        print(f"状态码：{response.status_code}")
        print(f"返回内容：{response.text}")
        raise SystemExit(1)

    data = response.json()
    try:
        summary = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        print("DeepSeek 返回的数据格式异常：")
        print(data)
        raise SystemExit(1)

    print("摘要生成成功。")
    return summary


# ============================================================
# 第四部分：把摘要和原始新闻发到 QQ 邮箱
# ============================================================

def build_email_bodies(ai_summary, news_list):
    """同时准备纯文本和 HTML 两封内容，方便手机邮箱阅读。"""
    today = datetime.now().strftime("%Y-%m-%d")

    text_lines = [
        f"今日全球科技与格局摘要（{today}）",
        "",
        "【AI 摘要】",
        ai_summary,
        "",
        "【原始新闻标题和链接】",
    ]
    for index, item in enumerate(news_list, start=1):
        text_lines.append(f"{index}. {item['title']}")
        text_lines.append(f"   来源：{item['source']}")
        text_lines.append(f"   链接：{item['link']}")
        text_lines.append("")

    html_items = []
    for item in news_list:
        html_items.append(
            "<li>"
            f"<a href=\"{html.escape(item['link'])}\">{html.escape(item['title'])}</a>"
            f"<br><small>来源：{html.escape(item['source'])}</small>"
            "</li>"
        )

    html_body = f"""
    <html>
      <body style="font-family: Arial, 'Microsoft YaHei', sans-serif; line-height: 1.6;">
        <h2>今日全球科技与格局摘要（{today}）</h2>
        <h3>AI 摘要</h3>
        <p>{html.escape(ai_summary).replace(chr(10), "<br>")}</p>
        <h3>原始新闻标题和链接</h3>
        <ol>
          {''.join(html_items)}
        </ol>
        <p style="color:#888;font-size:12px;">本邮件由本地 Python 脚本自动生成，仅供个人阅读。</p>
      </body>
    </html>
    """
    return "\n".join(text_lines), html_body


def send_email(ai_summary, news_list):
    """使用 QQ 邮箱 SMTP 发送邮件。授权码不是 QQ 密码。一次可发给多个收件人。"""
    recipients_display = "、".join(TO_EMAILS)
    print(f"正在发送邮件到：{recipients_display}")
    today = datetime.now().strftime("%Y-%m-%d")
    text_body, html_body = build_email_bodies(ai_summary, news_list)

    message = MIMEMultipart("alternative")
    message["From"] = formataddr(("每日科技摘要", QQ_EMAIL))
    # To 头里列出全部收件人，大家都能看到这封邮件发给了谁
    message["To"] = ", ".join(TO_EMAILS)
    message["Subject"] = f"今日全球科技与格局摘要 | {today}"
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    # QQ 邮箱推荐使用 465 端口 + SSL
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.qq.com", 465, context=context, timeout=30) as server:
            server.login(QQ_EMAIL, QQ_AUTH_CODE)
            server.sendmail(QQ_EMAIL, TO_EMAILS, message.as_string())
    except smtplib.SMTPAuthenticationError:
        print("邮箱登录失败。请确认：")
        print("1. QQ_EMAIL 填的是完整邮箱，例如 123456789@qq.com")
        print("2. QQ_AUTH_CODE 填的是 16 位授权码，而不是 QQ 登录密码")
        print("3. 已在 QQ 邮箱网页版开启 SMTP 服务")
        raise SystemExit(1)
    except smtplib.SMTPException as error:
        print(f"邮件发送失败：{error}")
        raise SystemExit(1)

    print("邮件发送成功，请各位收件人到 QQ 邮箱（含垃圾箱）查看。")


# ============================================================
# 第五部分：主流程
# ============================================================

def main():
    print("====== 开始运行每日科技摘要脚本 ======\n")
    check_config()

    news_list = fetch_news(limit=5)
    if not news_list:
        print("一个新闻都没有抓到。请检查网络，或稍后再试。")
        raise SystemExit(1)

    print("\n本次将总结的新闻：")
    for index, item in enumerate(news_list, start=1):
        print(f"{index}. {item['title']}")

    ai_summary = summarize_with_deepseek(news_list)
    print("\n------ AI 摘要预览 ------")
    print(ai_summary)
    print("------------------------\n")

    send_email(ai_summary, news_list)
    print("\n====== 全部完成 ======")


if __name__ == "__main__":
    # 只有直接运行本文件时才会执行；被别人导入时不会自动发邮件
    main()
