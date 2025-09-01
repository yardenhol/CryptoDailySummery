# -*- coding: utf-8 -*-
"""
Daily Crypto Summary (HTML + JSON output)
- Fetch crypto news (RSS) & market data (CoinGecko)
- Ask OpenAI to return a structured JSON summary (Hebrew)
- Render a clean RTL HTML email (with plain-text alternative)
- Fallback to basic HTML if OpenAI quota/connection fails

Environment (GitHub Secrets):
  # Common
  OPENAI_API_KEY      -> Optional (if missing or failing, fallback HTML is sent)
  EMAIL_TO            -> Recipient email

  # Gmail / SMTP
  EMAIL_HOST          -> smtp.gmail.com
  EMAIL_PORT          -> 587
  EMAIL_USER          -> yourname@gmail.com
  EMAIL_PASS          -> Gmail App Password (16 chars)

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

EMAIL_HOST = os.environ.get("EMAIL_HOST")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587")) if os.environ.get("EMAIL_PORT") else None
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_TO   = os.environ.get("EMAIL_TO")

# ==== RSS sources ====
RSS_SOURCES = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://cointelegraph.com/rss",
    "https://cryptopotato.com/feed/",
    "https://cryptoslate.com/feed/",
    "https://cryptonews.com/news/feed/",
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

# ==== OpenAI: ask for JSON summary ====
def generate_summary_json(news_items, market_data):
    """
    Returns a dict with keys:
    {
      "date": "DD.MM.YYYY",
      "tldr": "…",
      "market": {"cap": "...", "volume": "...", "movers": "...", "btc": "...", "eth": "..."},
      "news": [{"title":"...", "summary":"...", "source":"...", "link":"..."}],
      "regulation": ["...", "..."],
      "points": ["...", "..."],
      "future": ["...", "..."],
      "links": ["http://...", ...]
    }
    """
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    # Reduce payload to keep cost small
    news_for_model = [
        {
            "source": n["source"],
            "title": n["title"],
            "summary": n["summary"][:500],
            "link": n["link"],
            "published": n["published"]
        } for n in news_items[:50]
    ]

    payload = {
        "today_iso": NOW.strftime("%Y-%m-%d"),
        "window": "24h (מאז אתמול בשעה 08:00 ועד היום 08:00 לפי Asia/Jerusalem)",
        "news": news_for_model,
        "market": market_data,
        "audience": "משקיע חכם עסוק, דובר עברית",
    }

    system_prompt = (
        "את/ה עורך/כת ומורה לקריפטו. החזר/י אך ורק JSON חוקי בעברית, לפי הסכמה שניתנת. "
        "אל תוסיף/י טקסט חופשי מחוץ ל-JSON."
    )

    schema_hint = """
החזר אובייקט JSON עם השדות הבאים בלבד:
{
  "date": "DD.MM.YYYY",
  "tldr": "שורה אחת מסכמת",
  "market": {
    "cap": "שווי שוק כולל",
    "volume": "נפח מסחר 24ש׳",
    "movers": "Top movers (שורה קצרה)",
    "btc": "BTC: מחיר, שינוי 24ש׳, טווח 24ש׳",
    "eth": "ETH: מחיר, שינוי 24ש׳, טווח 24ש׳"
  },
  "news": [
    { "title": "כותרת", "summary": "2–3 שורות מהות ולמה חשוב", "source": "מקור", "link": "URL" }
  ],
  "regulation": ["נקודה", "נקודה"],
  "points": ["נקודה לימודית", "נקודה לימודית"],
  "future": ["ראדרים להמשך", "ראדרים"],
  "links": ["URL1", "URL2", "URL3"]
}
"""

    user_prompt = f"""
יצר/י תקציר יומי בעברית לפי הסכמה (JSON) שלמעלה.
דרישות:
- קצר, ברור, ללא סלנג וללא המלצות קנייה/מכירה.
- בחר/י 5–10 חדשות משמעותיות בלבד ל-news.
- מילא/י "links" עם קישורים שהשתמשת בהם (1–8).
- אם נתון חסר — דלג/י עליו, אל תמציא/י.
- "date" בפורמט DD.MM.YYYY עבור התאריך היום בישראל.

הנתונים (JSON לחומרי רקע):
{json.dumps(payload, ensure_ascii=False)}
"""

    resp = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": schema_hint.strip()},
            {"role": "user", "content": user_prompt}
        ],
        response_format={"type": "json_object"},
        timeout=120
    )
    txt = resp.output_text
    return json.loads(txt)

# ==== HTML formatting (RTL) ====
def format_email_html(summary_dict):
    """Build styled RTL HTML email from the JSON summary"""
    # Safe pulls
    tldr = summary_dict.get("tldr", "")
    mk  = summary_dict.get("market", {}) or {}
    news = summary_dict.get("news", []) or []
    regulation = summary_dict.get("regulation", []) or []
    points = summary_dict.get("points", []) or []
    future = summary_dict.get("future", []) or []
    links = summary_dict.get("links", []) or []

    def li_list(items):
        return "".join(f"<li>{clean(str(x))}</li>" for x in items if x)

    news_html = "".join(
        f'<li><b>{clean(n.get("title",""))}</b> — {clean(n.get("summary",""))} '
        f'<i>({clean(n.get("source",""))})</i></li>'
        for n in news
    )
    links_html = "".join(
        f'<li><a href="{link}" target="_blank">{link}</a></li>' for link in links if link
    )

    html = f"""
    <html dir="rtl" lang="he">
      <body style="font-family: Arial, Helvetica, sans-serif; line-height:1.7; color:#1f2937; max-width:760px; margin:auto; padding:24px; background:#ffffff;">
        <h2 style="text-align:center; margin:0 0 8px;">📊 עדכון יומי – קריפטו | {NOW.strftime('%d.%m.%Y')}</h2>
        <p style="font-size:16px; margin:0 0 18px;"><b>TL;DR:</b> {clean(tldr)}</p>

        <div style="background:#f1f5f9; padding:14px 16px; border-radius:10px; margin-top:14px;">
          <h3 style="margin-top:0;">שוק בזמן אמת</h3>
          <ul style="margin:8px 0 0 0; padding-inline-start:22px;">
            <li>שווי שוק כולל: {clean(mk.get("cap",""))}</li>
            <li>נפח מסחר 24ש׳: {clean(mk.get("volume",""))}</li>
            <li>בולטי 24ש׳: {clean(mk.get("movers",""))}</li>
            <li>{clean(mk.get("btc",""))}</li>
            <li>{clean(mk.get("eth",""))}</li>
          </ul>
        </div>

        <div style="margin-top:18px;">
          <h3>חדשות מרכזיות</h3>
          <ul style="margin:8px 0 0 0; padding-inline-start:22px;">{news_html}</ul>
        </div>

        <div style="margin-top:18px;">
          <h3>רגולציה ואכיפה</h3>
          <ul style="margin:8px 0 0 0; padding-inline-start:22px;">{li_list(regulation)}</ul>
        </div>

        <div style="margin-top:18px;">
          <h3>נקודות לימודיות</h3>
          <ul style="margin:8px 0 0 0; padding-inline-start:22px;">{li_list(points)}</ul>
        </div>

        <div style="margin-top:18px;">
          <h3>ראדרים להמשך</h3>
          <ul style="margin:8px 0 0 0; padding-inline-start:22px;">{li_list(future)}</ul>
        </div>

        <div style="margin-top:18px;">
          <h3>🔗 קישורים למקורות</h3>
          <ol style="margin:8px 0 0 0; padding-inline-start:22px;">{links_html}</ol>
        </div>

        <p style="color:#6b7280; font-size:12px; margin-top:24px;">נשלח אוטומטית ע״י הבוט. אין לראות באמור ייעוץ או שיווק השקעות.</p>
      </body>
    </html>
    """
    return html

# ==== Fallback builder (when OpenAI not available) ====
def build_fallback_summary_dict(news_items, market):
    # Compose a minimal but readable dict
    g = (market or {}).get("global", {})
    mktcap = (g.get("total_market_cap") or {}).get("usd")
    vol24  = (g.get("total_volume") or {}).get("usd")

    # Movers
    movers = sorted((market or {}).get("markets", []),
                    key=lambda c: (c.get("price_change_percentage_24h") or 0),
                    reverse=True)[:5]
    movers_str = " / ".join(
        f"{c.get('name')} {c.get('price_change_percentage_24h'):+.2f}%"
        for c in movers if c.get("name") is not None
    )

    # BTC / ETH lines (best-effort)
    def find_coin(symbol):
        for c in (market or {}).get("markets", []):
            if (c.get("symbol") or "").lower() == symbol:
                return c
        return None

    def coin_line(c, label):
        if not c: return ""
        price = c.get("current_price")
        chg   = c.get("price_change_percentage_24h")
        hi    = c.get("high_24h")
        lo    = c.get("low_24h")
        return f"{label}: ${price:,} ({chg:+.2f}%), טווח 24ש׳: ${lo:,}–${hi:,}"

    btc = coin_line(find_coin("btc"), "BTC")
    eth = coin_line(find_coin("eth"), "ETH")

    # News top 7
    news_top = news_items[:7]
    news_struct = []
    links = []
    for n in news_top:
        news_struct.append({
            "title": n.get("title",""),
            "summary": (n.get("summary") or "")[:200] + ("..." if (n.get("summary") and len(n["summary"])>200) else ""),
            "source": n.get("source",""),
            "link": n.get("link","")
        })
        if n.get("link"):
            links.append(n["link"])

    return {
        "date": NOW.strftime("%d.%m.%Y"),
        "tldr": "עדכון יומי במתכונת בסיסית עקב חוסר זמינות מודל.",
        "market": {
            "cap": f"{pretty_money(mktcap)} $ (סה״כ)" if mktcap else "",
            "volume": f"{pretty_money(vol24)} $ (24ש׳)" if vol24 else "",
            "movers": movers_str or "",
            "btc": btc,
            "eth": eth,
        },
        "news": news_struct,
        "regulation": [],
        "points": [],
        "future": [],
        "links": links[:8]
    }

# ==== Email sending (SMTP / Gmail) ====
def send_email_html(subject, html_body, plain_fallback=""):
    host = EMAIL_HOST
    port = EMAIL_PORT
    user = EMAIL_USER
    pwd  = EMAIL_PASS
    to   = EMAIL_TO

    if not all([host, port, user, pwd, to]):
        raise RuntimeError("SMTP env vars missing (EMAIL_HOST/PORT/USER/PASS/TO).")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Daily Crypto Bot", user))  # For Gmail: From must equal EMAIL_USER
    msg["To"] = to

    # Plain fallback + HTML
    if plain_fallback:
        msg.attach(MIMEText(plain_fallback, "plain", _charset="utf-8"))
    msg.attach(MIMEText(html_body or "<html><body>—</body></html>", "html", _charset="utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.set_debuglevel(1)  # SMTP dialogue to logs
        s.ehlo(); s.starttls(context=ctx); s.ehlo()
        s.login(user, pwd)
        resp = s.sendmail(user, [to], msg.as_string())
        if resp:
            raise RuntimeError(f"SMTP sendmail returned errors: {resp}")

# ==== Main ====
def main():
    if not EMAIL_TO:
        print("Missing EMAIL_TO.", file=sys.stderr)
        sys.exit(1)

    news = fetch_news()
    market = fetch_market()

    # Try OpenAI → JSON → HTML, with retry
    summary_dict = None
    if OPENAI_API_KEY:
        last_err = None
        for attempt in range(3):
            try:
                summary_dict = generate_summary_json(news, market)
                # Basic sanity: must have keys
                if not isinstance(summary_dict, dict) or "market" not in summary_dict:
                    raise ValueError("Model returned unexpected structure.")
                break
            except Exception as e:
                last_err = e
                print(f"[WARN] OpenAI JSON summary failed (attempt {attempt+1}/3): {e}", file=sys.stderr)
                import time, random
                time.sleep(2 * (attempt + 1) + random.random())

        if not summary_dict:
            print("[INFO] Falling back to basic structured dict (no OpenAI).", file=sys.stderr)
            summary_dict = build_fallback_summary_dict(news, market)
    else:
        print("[INFO] OPENAI_API_KEY not provided; sending fallback summary.", file=sys.stderr)
        summary_dict = build_fallback_summary_dict(news, market)

    subject = f"עדכון יומי – קריפטו | {NOW.strftime('%d.%m.%Y')}"
    html_body = format_email_html(summary_dict)

    # Optional simple plain text (short)
    plain = f"עדכון יומי – קריפטו | {NOW.strftime('%d.%m.%Y')}\n\nTL;DR: {summary_dict.get('tldr','')}\n\nלקבלת גרסה קריאה, פתח את המייל בתצוגת HTML."

    send_email_html(subject, html_body, plain_fallback=plain)
    print("Email sent (HTML).")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        sys.exit(1)
