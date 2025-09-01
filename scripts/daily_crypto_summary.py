# -*- coding: utf-8 -*-
"""
Daily Crypto Summary
- Fetches crypto news (RSS) and market data (CoinGecko)
- Summarizes in Hebrew via OpenAI (if quota available), with robust retry
- Falls back to a raw digest email if OpenAI is unavailable (429/other)
- Sends the email via SendGrid (if SENDGRID_API_KEY present) or SMTP

Environment variables (GitHub Secrets recommended):

Required (common):
  OPENAI_API_KEY            -> OpenAI API key (for full summary; optional for fallback-only runs)
  EMAIL_TO                  -> Recipient email address

SendGrid path (preferred if available):
  SENDGRID_API_KEY
  EMAIL_FROM                -> Verified sender (Single Sender or domain-auth)
  # EMAIL_TO used from common

SMTP path (if no SENDGRID_API_KEY):
  EMAIL_HOST                -> e.g. smtp.gmail.com
  EMAIL_PORT                -> e.g. 587
  EMAIL_USER                -> e.g. yourname@gmail.com
  EMAIL_PASS                -> App Password (Gmail) or SMTP password
  # EMAIL_TO used from common

Notes:
- CoinGecko endpoints are public and free (no key).
- RSS feeds are public.

Author: ChatGPT
"""

import os
import sys
import json
import smtplib
import ssl
import requests
import feedparser
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from datetime import datetime, timedelta, timezone
from dateutil import parser as dateparser
import pytz
from bs4 import BeautifulSoup

# ==== Timezone / Now ====
TZ = pytz.timezone("Asia/Jerusalem")
NOW = datetime.now(TZ)
YEST = NOW - timedelta(days=1)

# ==== Env ====
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# SendGrid
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")
EMAIL_FROM = os.environ.get("EMAIL_FROM")

# SMTP
EMAIL_HOST = os.environ.get("EMAIL_HOST")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587")) if os.environ.get("EMAIL_PORT") else None
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")

# Common
EMAIL_TO   = os.environ.get("EMAIL_TO")

# ==== RSS sources ====
RSS_SOURCES = [
    # General crypto
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://cointelegraph.com/rss",
    "https://cryptopotato.com/feed/",
    "https://cryptoslate.com/feed/",
    "https://cryptonews.com/news/feed/",
    # Regulation / SEC
    "https://www.sec.gov/news/pressreleases.rss",
]

# ==== Helpers ====
def clean(text: str) -> str:
    if not text:
        return ""
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())

def pretty_money(x):
    try:
        return f"{float(x):,.0f}"
    except Exception:
        return str(x)

# ==== Step 1: Fetch news (last 24h) ====
def fetch_news():
    items = []
    since_utc = YEST.astimezone(timezone.utc)
    for url in RSS_SOURCES:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                pub = None
                for key in ("published", "updated", "created"):
                    if getattr(e, key, None):
                        try:
                            pub = dateparser.parse(getattr(e, key))
                            break
                        except Exception:
                            pass
                if pub is None:
                    continue
                pub_utc = pub.astimezone(timezone.utc) if pub.tzinfo else pub.replace(tzinfo=timezone.utc)
                if pub_utc >= since_utc:
                    items.append({
                        "source": getattr(feed.feed, "title", url) if getattr(feed, "feed", None) else url,
                        "title": clean(getattr(e, "title", "")),
                        "summary": clean(getattr(e, "summary", "")),
                        "link": getattr(e, "link", ""),
                        "published": pub_utc.isoformat()
                    })
        except Exception as ex:
            print(f"[WARN] RSS failed for {url}: {ex}", file=sys.stderr)

    # Deduplicate by (title, link) and sort desc by published
    seen = set()
    deduped = []
    for it in sorted(items, key=lambda x: x["published"], reverse=True):
        key = (it["title"], it["link"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
    # limit to safe number for prompt size
    return deduped[:120]

# ==== Step 2: Fetch market data (CoinGecko) ====
def fetch_market():
    base = "https://api.coingecko.com/api/v3"
    headers = {"Accept": "application/json"}
    out = {}

    # Global
    try:
        g = requests.get(f"{base}/global", headers=headers, timeout=30).json()
        out["global"] = g.get("data", {})
    except Exception as ex:
        print(f"[WARN] global failed: {ex}", file=sys.stderr)

    # Top markets
    try:
        m = requests.get(
            f"{base}/coins/markets",
            params=dict(
                vs_currency="usd",
                order="market_cap_desc",
                per_page=50,
                page=1,
                price_change_percentage="1h,24h,7d"
            ),
            headers=headers, timeout=45
        ).json()
        trimmed = []
        for c in m[:50]:
            trimmed.append({
                "id": c.get("id"),
                "symbol": c.get("symbol"),
                "name": c.get("name"),
                "current_price": c.get("current_price"),
                "market_cap": c.get("market_cap"),
                "price_change_percentage_24h": c.get("price_change_percentage_24h"),
                "price_change_percentage_7d_in_currency": c.get("price_change_percentage_7d_in_currency"),
                "high_24h": c.get("high_24h"),
                "low_24h": c.get("low_24h"),
                "total_volume": c.get("total_volume"),
            })
        out["markets"] = trimmed
    except Exception as ex:
        print(f"[WARN] markets failed: {ex}", file=sys.stderr)

    return out

# ==== Step 3: OpenAI summary (Hebrew), with prompt control ====
def generate_summary(news_items, market_data):
    # Lazily import to avoid dependency if running fallback-only
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    # reduce payload size
    news_for_model = [
        {
            "source": n["source"],
            "title": n["title"],
            "summary": n["summary"][:500],
            "link": n["link"],
            "published": n["published"]
        } for n in news_items[:60]
    ]

    payload = {
        "today_iso": NOW.strftime("%Y-%m-%d"),
        "window": "24h (מאז אתמול בשעה 08:00 ועד היום 08:00 לפי Asia/Jerusalem)",
        "news": news_for_model,
        "market": market_data,
        "audience": "משקיע חכם עסוק, דובר עברית, רוצה עדכון ענייני ומהיר + הרחבות לימודיות קצרות",
    }

    system_prompt = (
        "את/ה עורך/כת ומורה לקריפטו. כתוב/כתבי סיכום יומי בעברית פשוטה, מחולק לקטגוריות, "
        "שמשלב עדכון נוח לקריאה עם הסבר קצר למי שרוצה להבין יותר לעומק. "
        "שמור/שמרי על עובדות מדויקות, ללא היפותזות לא מבוססות או המלצות השקעה."
    )

    user_prompt = f"""
מפרט הסיכום:
1) כותרת: "עדכון יומי – קריפטו | {NOW.strftime('%d.%m.%Y')}"
2) פתיח חד-שורה (TL;DR).
3) שוק בזמן אמת:
   • שווי שוק כולל, נפח 24ש', בולטי 24ש' (Top Movers).
   • BTC/ETH: מחיר, שינוי 24ש', טווח 24ש'.
4) חדשות מרכזיות (5–10 נקודות): כותרת קצרה → 2–3 שורות מהות + למה זה חשוב. ציין מקור בסוגריים.
5) רגולציה ואכיפה: פעולות רגולטורים/SEC וכו'.
6) נקודות לימודיות קצרות: 3–5 נק'.
7) ראדרים להמשך (אירועים/דאטה לשים אליהם לב מחר).
8) בסוף: קישורים למקורות (רשימה ממוספרת, רק מהחדשות ששימשו).

הנתונים (JSON):
{json.dumps(payload, ensure_ascii=False)}

כללי סגנון:
- עברית ברורה, לא סלנג, בלי המלצות קנייה/מכירה.
- ללא טבלאות כבדות; בולטים קצרים וברורים.
- אם נתון חסר, לדלג בשקט (לא להמציא).
"""

    # Model: ניתן להחליף לדגם חסכוני יותר במקרה הצורך
    resp = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        timeout=120
    )
    content = resp.output_text
    return content.strip()

# ==== Fallback (raw digest without OpenAI) ====
def build_raw_digest(news_items, market):
    lines = []
    lines.append(f"עדכון יומי – קריפטו | {NOW.strftime('%d.%m.%Y')} (מצב חירום: בלי מודל, תקציב/גישה ל-OpenAI לא זמינים)\n")

    # Global market
    g = (market or {}).get("global", {})
    mktcap = (g.get("total_market_cap") or {}).get("usd")
    vol24  = (g.get("total_volume") or {}).get("usd")
    if mktcap or vol24:
        lines.append("שוק בקצרה:")
        if mktcap: lines.append(f"• שווי שוק כולל (USD): {pretty_money(mktcap)}")
        if vol24:  lines.append(f"• נפח מסחר 24ש' (USD): {pretty_money(vol24)}")

    # Top movers 24h
    movers = sorted((market or {}).get("markets", []),
                    key=lambda c: (c.get("price_change_percentage_24h") or 0),
                    reverse=True)[:10]
    if movers:
        lines.append("\nTop Movers 24ש':")
        for c in movers:
            chg = c.get("price_change_percentage_24h")
            price = c.get("current_price")
            lines.append(f"• {c.get('name')} ({(c.get('symbol') or '').upper()}): {chg:+.2f}% | ${price:,}")

    # News (8 latest)
    lines.append("\nחדשות אחרונות (8):")
    for n in news_items[:8]:
        ts = n.get("published","")[:19].replace("T"," ")
        title = n.get("title") or ""
        source = n.get("source") or ""
        lines.append(f"• {title} — {source} ({ts})")
        if n.get("summary"):
            lines.append(f"  {n['summary'][:180]}...")
        if n.get("link"):
            lines.append(f"  {n['link']}")

    lines.append("\nהערה: מייל זה נשלח במתכונת בסיסית עקב שגיאת מכסה/חיבור ב־OpenAI API.")
    return "\n".join(lines)

# ==== Send email (auto: SendGrid if available, else SMTP) ====
def send_via_smtp(subject, body_md):
    host = EMAIL_HOST
    port = EMAIL_PORT
    user = EMAIL_USER
    pwd  = EMAIL_PASS
    to   = EMAIL_TO

    if not all([host, port, user, pwd, to]):
        raise RuntimeError("SMTP path selected but one or more SMTP env vars are missing.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Daily Crypto Bot", user))  # From must match EMAIL_USER for Gmail
    msg["To"] = to
    msg.attach(MIMEText(body_md or "—", "plain", _charset="utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.set_debuglevel(1)  # Print SMTP dialogue to Action log
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
        s.login(user, pwd)
        resp = s.sendmail(user, [to], msg.as_string())
        if resp:
            raise RuntimeError(f"SMTP sendmail returned errors: {resp}")

def send_via_sendgrid(subject, body_md):
    if not SENDGRID_API_KEY:
        raise RuntimeError("SENDGRID_API_KEY missing.")
    if not EMAIL_FROM or not EMAIL_TO:
        raise RuntimeError("EMAIL_FROM/EMAIL_TO missing for SendGrid path.")
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
    except ImportError:
        raise RuntimeError("sendgrid package not installed (pip install sendgrid).")

    message = Mail(
        from_email=EMAIL_FROM,
        to_emails=EMAIL_TO,
        subject=subject,
        plain_text_content=body_md or "—"
    )
    sg = SendGridAPIClient(SENDGRID_API_KEY)
    resp = sg.send(message)
    print("SendGrid status:", resp.status_code)
    if resp.status_code >= 300:
        raise RuntimeError(f"SendGrid error: {resp.status_code} headers={dict(resp.headers)}")

def send_email(subject, body_md):
    if SENDGRID_API_KEY:
        print("[INFO] Using SendGrid to send email.")
        send_via_sendgrid(subject, body_md)
    else:
        print("[INFO] Using SMTP to send email.")
        send_via_smtp(subject, body_md)

# ==== Main ====
def main():
    if not EMAIL_TO:
        print("Missing EMAIL_TO.", file=sys.stderr)
        sys.exit(1)

    news = fetch_news()
    market = fetch_market()

    # Try OpenAI summary (if key provided), with simple retry
    summary = None
    if OPENAI_API_KEY:
        last_err = None
        for attempt in range(3):
            try:
                summary = generate_summary(news, market)
                break
            except Exception as e:
                last_err = e
                print(f"[WARN] OpenAI summary failed (attempt {attempt+1}/3): {e}", file=sys.stderr)
                import time, random
                time.sleep(2 * (attempt + 1) + random.random())
        if not summary:
            print("[INFO] Falling back to raw digest email (no OpenAI).", file=sys.stderr)
            summary = build_raw_digest(news, market)
    else:
        print("[INFO] OPENAI_API_KEY not provided; sending raw digest.", file=sys.stderr)
        summary = build_raw_digest(news, market)

    subject = f"עדכון יומי – קריפטו | {NOW.strftime('%d.%m.%Y')}"
    send_email(subject, summary)
    print("Email sent.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        sys.exit(1)
