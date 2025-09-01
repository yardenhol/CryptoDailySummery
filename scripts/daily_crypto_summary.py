import os, sys, json, smtplib, ssl, requests, feedparser
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from dateutil import parser as dateparser
import pytz
from bs4 import BeautifulSoup

# ==== קונפיג בסיסי ====
TZ = pytz.timezone("Asia/Jerusalem")
NOW = datetime.now(TZ)
YEST = NOW - timedelta(days=1)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
EMAIL_HOST = os.environ.get("EMAIL_HOST")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_TO   = os.environ.get("EMAIL_TO")

# ==== מקורות RSS לחדשות ====
RSS_SOURCES = [
    # חדשות כלליות קריפטו
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",  # CoinDesk
    "https://cointelegraph.com/rss",                                   # Cointelegraph
    "https://cryptopotato.com/feed/",                                   # CryptoPotato
    "https://cryptoslate.com/feed/",                                    # CryptoSlate
    "https://cryptonews.com/news/feed/",                                # CryptoNews
    # רגולציה/SEC
    "https://www.sec.gov/news/pressreleases.rss",                       # SEC Press Releases (קיים דרך דף ה-RSS)
]

# ==== עזר: ניקוי טקסט קצר ====
def clean(text: str) -> str:
    if not text:
        return ""
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())

# ==== שלב 1: משיכת חדשות אחרונות (24 שעות) ====
def fetch_news():
    items = []
    since = YEST.astimezone(timezone.utc)
    for url in RSS_SOURCES:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                # תאריך פרסום
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

                if pub_utc >= since:
                    items.append({
                        "source": feed.feed.title if getattr(feed, "feed", None) and getattr(feed.feed, "title", None) else url,
                        "title": clean(getattr(e, "title", "")),
                        "summary": clean(getattr(e, "summary", "")),
                        "link": getattr(e, "link", ""),
                        "published": pub_utc.isoformat()
                    })
        except Exception as ex:
            # לא מפיל ריצה בגלל מקור אחד שנכשל
            print(f"[WARN] RSS failed for {url}: {ex}", file=sys.stderr)
    # סינון כפילויות לפי כותרת/לינק
    seen = set()
    deduped = []
    for it in sorted(items, key=lambda x: x["published"], reverse=True):
        key = (it["title"], it["link"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
    return deduped[:120]  # תקרת בטיחות

# ==== שלב 2: נתוני שוק מרכזיים (CoinGecko) ====
def fetch_market():
    base = "https://api.coingecko.com/api/v3"
    headers = {"Accept": "application/json"}
    out = {}

    # גלובלי
    try:
        g = requests.get(f"{base}/global", headers=headers, timeout=30).json()
        out["global"] = g.get("data", {})
    except Exception as ex:
        print(f"[WARN] global failed: {ex}", file=sys.stderr)

    # טופ 20 לפי שווי שוק
    try:
        m = requests.get(
            f"{base}/coins/markets",
            params=dict(vs_currency="usd", order="market_cap_desc", per_page=50, page=1, price_change_percentage="1h,24h,7d"),
            headers=headers, timeout=45
        ).json()
        # בוחרים רק שדות חשובים
        trimmed = []
        for c in m[:50]:
            trimmed.append({
                "id": c.get("id"),
                "symbol": c.get("symbol"),
                "name": c.get("name"),
                "current_price": c.get("current_price"),
                "market_cap": c.get("market_cap"),
                "price_change_percentage_24h": c.get("price_change_percentage_24h"),
                "price_change_percentage_7d_in_currency": (c.get("price_change_percentage_7d_in_currency")),
                "high_24h": c.get("high_24h"),
                "low_24h": c.get("low_24h"),
                "total_volume": c.get("total_volume"),
            })
        out["markets"] = trimmed
    except Exception as ex:
        print(f"[WARN] markets failed: {ex}", file=sys.stderr)

    return out

# ==== שלב 3: יצירת פרומפט ושיחה ל-OpenAI לכתיבת סיכום בעברית ====
def generate_summary(news_items, market_data):
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    # נכין קונטקסט מקוצר למודל (לא שולחים טקסטים ארוכים מדי)
    news_for_model = [
        {
            "source": n["source"],
            "title": n["title"],
            "summary": n["summary"][:600],
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
        "שמור/שמרי על עובדות מדויקות, ללא היפותזות לא מבוססות."
    )

    user_prompt = f"""
מפרט הסיכום:
1) כותרת: "עדכון יומי – קריפטו | {NOW.strftime('%d.%m.%Y')}"
2) פתיח חד-שורה (TL;DR).
3) שוק בזמן אמת:
   • שווי שוק כולל, נפח 24ש', בולטי 24ש' (Top Movers).
   • BTC/ETH: מחיר, שינוי 24ש', טווח 24ש'.
4) חדשות מרכזיות (5–10 נקודות): כותרת קצרה → 2–3 שורות מהות + למה זה חשוב. ציין מקור בסוגריים.
5) רגולציה ואכיפה: פעולות SEC/רגולטורים, תיקים/אזהרות.
6) בנקודות לימודיות קצרות ("איך זה משפיע על משקיע"): 3–5 נק'.
7) ראדרים להמשך: נתונים/אירועים לשים לב אליהם מחר.
8) בסוף: קישורים למקורות (רשימה ממוספרת, רק מהחדשות ששימשו).

הנתונים (JSON):
{json.dumps(payload, ensure_ascii=False) }

כללי סגנון:
- עברית ברורה, לא סלנג, בלי רמיזות השקעה או המלצות קנייה/מכירה.
- ללא טבלאות כבדות; להשתמש בבולטים קצרים וברורים.
- אם נתון חסר, לדלג בשקט (לא להמציא).
"""

    # שים לב: מודל – השתמש במודל טקסט עדכני. ניתן לבחור מודל “קטן-מהיר” או “גדול”.
    # כאן בחרתי מודל כללי שמתאים לטקסטים. אפשר להחליף לפי העדפה.
    resp = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
    )
    # שליפת הטקסט
    content = resp.output_text
    return content.strip()

# ==== שלב 4: שליחת מייל ====
def send_email(subject, body_md):
    # נשלח כ-plain text (אפשר גם HTML). כאן נשתמש ב-UTF-8.
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO

    part = MIMEText(body_md, "plain", _charset="utf-8")
    msg.attach(part)

    context = ssl.create_default_context()
    with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
        server.starttls(context=context)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())

def main():
    if not all([OPENAI_API_KEY, EMAIL_HOST, EMAIL_USER, EMAIL_PASS, EMAIL_TO]):
        print("Missing required environment variables.", file=sys.stderr)
        sys.exit(1)

    news = fetch_news()
    market = fetch_market()
    summary = generate_summary(news, market)

    subject = f"עדכון יומי – קריפטו | {NOW.strftime('%d.%m.%Y')}"
    send_email(subject, summary)
    print("Email sent.")

if __name__ == "__main__":
    main()
