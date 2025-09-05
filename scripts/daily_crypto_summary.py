# -*- coding: utf-8 -*-
"""
Daily Crypto Summary (Hebrew-only, RTL, JSON→HTML) – Gmail SMTP

Env (GitHub Secrets):
  EMAIL_HOST=smtp.gmail.com
  EMAIL_PORT=587
  EMAIL_USER=yourname@gmail.com
  EMAIL_PASS=<Gmail App Password 16 chars>
  EMAIL_TO=<recipient@gmail.com>
Optional (לסיכום המלא):
  OPENAI_API_KEY=<sk-...>   # אם חסר/נכשל → נשלח Fallback בסיסי בעברית

תלויות: requests, feedparser, python-dateutil, pytz, openai==1.*, beautifulsoup4
"""

import os
import sys
import re
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

# ===== Time / TZ =====
TZ = pytz.timezone("Asia/Jerusalem")
NOW = datetime.now(TZ)
YEST = NOW - timedelta(days=1)

# ===== ENV =====
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

EMAIL_HOST = os.environ.get("EMAIL_HOST")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587")) if os.environ.get("EMAIL_PORT") else None
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_TO   = os.environ.get("EMAIL_TO")
EMAIL_TO_LIST = os.environ.get("EMAIL_TO_LIST")

# ===== Sources =====
RSS_SOURCES = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://cointelegraph.com/rss",
    "https://cryptopotato.com/feed/",
    "https://cryptoslate.com/feed/",
    "https://cryptonews.com/news/feed/",
    "https://www.sec.gov/news/pressreleases.rss",
]

# ===== Helpers =====
def clean(text: str) -> str:
    if not text:
        return ""
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())

def pretty_money(x):
    try:
        v = float(x)
        return f"{v:,.0f}"
    except Exception:
        return str(x)

# ===== Step 1: News (24h) =====
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

    # Dedup & sort
    seen = set()
    deduped = []
    for it in sorted(items, key=lambda x: x["published"], reverse=True):
        key = (it["title"], it["link"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
    return deduped[:120]

# ===== Step 2: Market (CoinGecko) =====
def fetch_market():
    base = "https://api.coingecko.com/api/v3"
    headers = {"Accept": "application/json"}
    out = {}

    try:
        g = requests.get(f"{base}/global", headers=headers, timeout=30).json()
        out["global"] = g.get("data", {})
    except Exception as ex:
        print(f"[WARN] global failed: {ex}", file=sys.stderr)

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

# ===== Step 3: OpenAI JSON (Hebrew-only) =====
def generate_summary_json(news_items, market_data):
    """
    Returns dict:
    {
      "date": "DD.MM.YYYY",
      "tldr": "…",
      "market": {"cap": "...", "volume": "...", "movers": "...", "btc": "...", "eth": "..."},
      "news": [{"title":"...", "summary":"...", "source":"...", "link":"..."}],
      "regulation": ["...", "..."],
      "points": ["...", "..."],
      "future": ["...", "..."],
      "links": [{"title":"...", "url":"..."}]
    }
    """
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    news_for_model = [
        {
            "source": n["source"],
            "title": n["title"],
            "summary": (n["summary"] or "")[:500],
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
        "את/ה עורך/ת ומורה לקריפטו. החזר/י אך ורק JSON חוקי בעברית (RTL), ללא טקסט מחוץ ל-JSON. "
        "תרגם/י לעברית את כל הכותרות, התקצירים והשדות שמקורם באנגלית. "
        "השאר/י באנגלית רק סמלים טכניים (BTC, ETH) וטיקר/שמות מותג קצרים שאינם בני תרגום (למשל OKB, SOL, Nike). "
        "ללא סלנג וללא המלצות השקעה."
    )

    schema_hint = """
החזר/י אובייקט JSON עם המפתחות הבאים בלבד (הכל בעברית, מלבד סימונים כמו BTC/ETH וטיקרי מטבעות/מותגים):
{
  "date": "DD.MM.YYYY",
  "tldr": "שורה אחת מסכמת בעברית בלבד",
  "market": {
    "cap": "שווי שוק כולל (בעברית)",
    "volume": "נפח מסחר 24ש׳ (בעברית)",
    "movers": "בולטים 24ש׳ בשורה קצרה (בעברית)",
    "btc": "BTC: מחיר, שינוי 24ש׳, טווח 24ש׳ (בעברית)",
    "eth": "ETH: מחיר, שינוי 24ש׳, טווח 24ש׳ (בעברית)"
  },
  "news": [
    { "title": "כותרת בעברית", "summary": "2–3 שורות בעברית ולמה זה חשוב", "source": "שם מקור (בעברית אם אפשר)", "link": "URL" }
  ],
  "regulation": ["נקודה בעברית", "נקודה בעברית"],
  "points": ["נקודה לימודית בעברית", "נקודה לימודית בעברית"],
  "future": ["דברים למעקב בעברית", "עוד נקודה"],
  "links": [
    {"title":"כותרת קצרה בעברית לקישור 1", "url":"https://..."},
    {"title":"כותרת קצרה בעברית לקישור 2", "url":"https://..."}
  ]
}
"""

    user_prompt = f"""
צר/י תקציר יומי בעברית לפי הסכמה שמעל.
דרישות:
- עברית בלבד. לתרגם כותרות וסיכומים; להשאיר BTC/ETH כסימונים.
- בחר/י 5–10 ידיעות משמעותיות בלבד לשדה "news".
- "links": החזר/י רשימת אובייקטים {{"title","url"}} בעברית (כותרת קצרה שמתארת את הכתבה).
- ללא המלצות קנייה/מכירה. אם נתון חסר – לדלג.
- "date" בפורמט DD.MM.YYYY לפי התאריך בישראל.

נתוני רקע (JSON):
{json.dumps(payload, ensure_ascii=False)}
"""
    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": schema_hint.strip()},
            {"role": "user", "content": user_prompt}
        ],
        response_format={"type": "json_object"},
        timeout=120
    )
    txt = resp.choices[0].message.content
    return json.loads(txt)


# ===== Translation guard (Hebrewize) =====
_HEB_RX = re.compile(r"[א-ת]")
_ENG_RX = re.compile(r"[A-Za-z]")

def needs_translation(s: str) -> bool:
    if not s or not isinstance(s, str):
        return False
    eng = len(_ENG_RX.findall(s))
    heb = len(_HEB_RX.findall(s))
    return eng > 0 and heb == 0  # יש לטינית ואין עברית

def translate_to_hebrew(text: str) -> str:
    """תרגום קצר לעברית – שומר BTC/ETH וטיקרי מטבעות/מותגים באנגלית."""
    if not OPENAI_API_KEY or not needs_translation(text):
        return text
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        prompt = (
            "תרגם לעברית בלבד, קצר וברור. השאר קיצורים כמו BTC/ETH וטיקרי מטבעות/מותגים באנגלית:\n"
            f"{text}"
        )
        resp = client.responses.create(
            model="gpt-4.1-mini",
            input=[{"role":"user","content":prompt}],
            timeout=40
        )
        return (resp.output_text or "").strip() or text
    except Exception:
        return text  # לא מפיל את הזרימה אם אין מכסה/שגיאה

def hebrewize_summary_dict(d: dict) -> dict:
    """מבטיח שכל הטקסטים בעברית ככל האפשר."""
    if not isinstance(d, dict):
        return d
    d = dict(d)

    # שדות פשוטים
    for k in ["tldr"]:
        if k in d and isinstance(d[k], str):
            d[k] = translate_to_hebrew(d[k])

    # market
    mk = d.get("market") or {}
    for k in ["cap","volume","movers","btc","eth"]:
        if isinstance(mk.get(k), str):
            mk[k] = translate_to_hebrew(mk[k])
    d["market"] = mk

    # news
    news = d.get("news") or []
    fixed_news = []
    for n in news:
        if not isinstance(n, dict):
            continue
        n = dict(n)
        for fld in ["title","summary","source"]:
            if isinstance(n.get(fld), str):
                n[fld] = translate_to_hebrew(n[fld])
        fixed_news.append(n)
    d["news"] = fixed_news

    # רשימות טקסט
    for fld in ["regulation","points","future"]:
        arr = d.get(fld) or []
        fixed = []
        for item in arr:
            fixed.append(translate_to_hebrew(item) if isinstance(item, str) else item)
        d[fld] = fixed

    # links
    links = d.get("links") or []
    fixed_links = []
    for l in links:
        if isinstance(l, dict):
            l = dict(l)
            if isinstance(l.get("title"), str):
                l["title"] = translate_to_hebrew(l["title"])
            fixed_links.append(l)
    d["links"] = fixed_links

    return d

# ===== HTML (RTL, clean) =====
def format_email_html(summary_dict):
    """RTL Hebrew HTML email – קריא וברור מימין לשמאל"""
    tldr = summary_dict.get("tldr", "")
    mk  = summary_dict.get("market", {}) or {}
    news = summary_dict.get("news", []) or []
    regulation = summary_dict.get("regulation", []) or []
    points = summary_dict.get("points", []) or []
    future = summary_dict.get("future", []) or []
    links = summary_dict.get("links", []) or []

    def li_list(items):
        return "".join(f'<li style="margin-bottom:6px; text-align:right;">{clean(str(x))}</li>' for x in items if x)

    news_html = "".join(
        '<li style="margin-bottom:12px; text-align:right;">'
        f'<div style="font-weight:600; margin-bottom:2px; text-align:right;">{clean(n.get("title",""))}</div>'
        f'<div style="color:#374151; text-align:right;">{clean(n.get("summary",""))} '
        f'<span style="color:#6b7280; font-style:italic;">({clean(n.get("source",""))})</span></div>'
        '</li>'
        for n in news
    )

    links_html = "".join(
        f'<li style="margin-bottom:6px; text-align:right;"><a href="{l.get("url")}" target="_blank" style="color:#2563eb; text-decoration:none; direction:rtl; text-align:right;">{clean(l.get("title","קישור"))}</a></li>'
        for l in links if l.get("url")
    )

    return f"""
    <html dir="rtl" lang="he">
      <body style="direction:rtl; text-align:right; font-family: Arial, Helvetica, sans-serif; background:#ffffff; color:#111827; margin:0;">
        <div style="direction:rtl; text-align:right; max-width:820px; margin:auto; padding:22px; line-height:1.9; font-size:16.5px;">
          
          <h1 style="margin:0 0 12px; font-size:22px; text-align:right;">📊 עדכון יומי – קריפטו | {NOW.strftime('%d.%m.%Y')}</h1>
          
          <p style="margin:0 0 20px; color:#1f2937; text-align:right;">
            <span style="font-weight:700;">תקציר:</span> {clean(tldr)}
          </p>

          <section style="background:#f3f4f6; padding:14px 16px; border-radius:12px; margin:16px 0 22px;">
            <h2 style="margin:0 0 10px; font-size:18px; text-align:right;">שוק בזמן אמת</h2>
            <ul style="margin:0; padding-inline-start:22px; direction:rtl; text-align:right;">
              <li style="margin-bottom:6px;"><b>שווי שוק כולל:</b> {clean(mk.get("cap",""))}</li>
              <li style="margin-bottom:6px;"><b>נפח מסחר 24ש׳:</b> {clean(mk.get("volume",""))}</li>
              <li style="margin-bottom:6px;"><b>בולטים 24ש׳:</b> {clean(mk.get("movers",""))}</li>
              <li style="margin-bottom:6px;">{clean(mk.get("btc",""))}</li>
              <li style="margin-bottom:0;">{clean(mk.get("eth",""))}</li>
            </ul>
          </section>

          <section style="margin:20px 0;">
            <h2 style="margin:0 0 10px; font-size:18px; text-align:right;">חדשות מרכזיות</h2>
            <ul style="margin:0; padding-inline-start:22px; list-style-type: disc; direction:rtl; text-align:right;">
              {news_html}
            </ul>
          </section>

          <section style="margin:20px 0;">
            <h2 style="margin:0 0 10px; font-size:18px; text-align:right;">רגולציה ואכיפה</h2>
            <ul style="margin:0; padding-inline-start:22px; direction:rtl; text-align:right;">
              {li_list(regulation)}
            </ul>
          </section>

          <section style="margin:20px 0;">
            <h2 style="margin:0 0 10px; font-size:18px; text-align:right;">נקודות לימודיות</h2>
            <ul style="margin:0; padding-inline-start:22px; direction:rtl; text-align:right;">
              {li_list(points)}
            </ul>
          </section>

          <section style="margin:20px 0;">
            <h2 style="margin:0 0 10px; font-size:18px; text-align:right;">ראדרים להמשך</h2>
            <ul style="margin:0; padding-inline-start:22px; direction:rtl; text-align:right;">
              {li_list(future)}
            </ul>
          </section>

          <section style="margin:20px 0;">
            <h2 style="margin:0 0 10px; font-size:18px; text-align:right;">🔗 קישורים למקורות</h2>
            <ol style="margin:0; padding-inline-start:22px; direction:rtl; text-align:right;">
              {links_html}
            </ol>
          </section>

          <p style="color:#6b7280; font-size:12px; margin-top:16px; text-align:right;">
            נשלח אוטומטית ע״י הבוט. אין לראות באמור ייעוץ או שיווק השקעות.
          </p>

        </div>
      </body>
    </html>
    """

# ===== Fallback (basic Hebrew dict) =====
def build_fallback_summary_dict(news_items, market):
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

    # BTC / ETH lines
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

    # News top
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
            links.append({"title": clean(n.get("title","קישור")), "url": n["link"]})

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

# ===== Email (SMTP / Gmail) =====
def _parse_recipients(val):
    """ממיר מחרוזת מיילים מופרדים בפסיקים/; לרשימה נקייה."""
    if not val:
        return []
    parts = [p.strip() for p in re.split(r"[;,]", str(val)) if p.strip()]
    return parts

def send_email_html(subject, html_body, plain_fallback=""):
    host = EMAIL_HOST
    port = EMAIL_PORT
    user = EMAIL_USER
    pwd  = EMAIL_PASS

    # תמיכה לאחור: אם אין EMAIL_TO_LIST → נשתמש ב-EMAIL_TO
    to_list = _parse_recipients(os.environ.get("EMAIL_TO_LIST") or EMAIL_TO)

    if not all([host, port, user, pwd]) or not to_list:
        raise RuntimeError("SMTP env vars missing or recipient list empty.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Daily Crypto Bot", user))
    msg["To"] = ", ".join(to_list)

    if plain_fallback:
        msg.attach(MIMEText(plain_fallback, "plain", _charset="utf-8"))
    msg.attach(MIMEText(html_body or "<html><body>—</body></html>", "html", _charset="utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.set_debuglevel(1)
        s.ehlo(); s.starttls(context=ctx); s.ehlo()
        s.login(user, pwd)
        resp = s.sendmail(user, to_list, msg.as_string())
        if resp:
            raise RuntimeError(f"SMTP sendmail returned errors: {resp}")


# ===== Main =====
def main():
    if not EMAIL_TO:
        print("Missing EMAIL_TO.", file=sys.stderr)
        sys.exit(1)

    news = fetch_news()
    market = fetch_market()

    # Try OpenAI → JSON → Hebrewize → HTML (retry x3)
    summary_dict = None
    if OPENAI_API_KEY:
        last_err = None
        for attempt in range(3):
            try:
                summary_dict = generate_summary_json(news, market)
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

    # הבטחת עברית מלאה ככל האפשר
    summary_dict = hebrewize_summary_dict(summary_dict)

    subject = f"עדכון יומי – קריפטו | {NOW.strftime('%d.%m.%Y')}"
    html_body = format_email_html(summary_dict)
    plain = f"עדכון יומי – קריפטו | {NOW.strftime('%d.%m.%Y')}\n\nתקציר: {summary_dict.get('tldr','')}\n\nלתצוגה מיטבית פתח/י את המייל ב-HTML."

    send_email_html(subject, html_body, plain_fallback=plain)
    print("Email sent (HTML).")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        sys.exit(1)
