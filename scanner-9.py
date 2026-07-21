# -*- coding: utf-8 -*-
"""
Product Tracker Scanner
=======================
רץ אוטומטית ב-GitHub Actions לפי לוח זמנים.
1. קורא את products.json
2. מחפש כל מוצר בכל השפות (DuckDuckGo - חינמי, ללא מפתח)
3. מאמת שכל תוצאה היא דף חי עם מוצר זמין (לא 404, לא "נמכר")
4. מסנן תוצאות שכבר נשלחו בעבר (found.json)
5. שולח התראה במייל רק על מציאות חדשות ומאומתות
6. שומר מועמדים לבדיקת AI ידנית ב-pending_review.json
"""

import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse

import requests

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS  # fallback לשם הישן של החבילה

# ---------------------------------------------------------------------------
# הגדרות
# ---------------------------------------------------------------------------

PRODUCTS_FILE = "products.json"
FOUND_FILE = "found.json"
PENDING_FILE = "pending_review.json"

RESULTS_PER_QUERY = 8          # כמה תוצאות לבקש לכל שאילתה
REQUEST_TIMEOUT = 15           # שניות לבדיקת דף חי
SLEEP_BETWEEN_REQUESTS = 2     # נימוס כלפי שרתים

# ביטויים שמעידים שהמוצר כבר לא זמין (עברית + אנגלית)
SOLD_PATTERNS = [
    r"this listing (has )?ended",
    r"\bitem (is )?sold\b",
    r"\bsold out\b",
    r"out of stock",
    r"no longer available",
    r"listing was ended",
    r"bidding has ended",
    r"auction (has )?ended",
    r"אזל מהמלאי",
    r"המוצר נמכר",
    r"המכירה הסתיימה",
    r"לא זמין במלאי",
    r"פריט זה נמכר",
]
SOLD_RE = re.compile("|".join(SOLD_PATTERNS), re.IGNORECASE)

# סימנים לכך שמדובר בדף מכירה (ולא מאמר/מוזיאון).
# בכוונה אין כאן "price"/"מחיר" לבד — כל כתבה מכילה אותם; רק מחיר עם מטבע או כפתור קנייה נחשבים.
SALE_SIGNALS = [
    r"add to cart", r"buy it now", r"place bid", r"checkout", r"free shipping",
    r"הוסף לסל", r"הוספה לסל", r"קנה עכשיו", r"לרכישה", r"הגש הצעה", r"הוספה לעגלה",
    r"[₪$€£]\s?\d", r"\d+\s?(₪|ש\"ח|שח)\b", r"\bUS \$\d",
]
SALE_RE = re.compile("|".join(SALE_SIGNALS), re.IGNORECASE)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "he,en;q=0.8",
}

# ---------------------------------------------------------------------------
# עזרי קבצים
# ---------------------------------------------------------------------------


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return default
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_url(url: str) -> str:
    """מסיר פרמטרים שיווקיים כדי שאותו מוצר לא ייחשב פעמיים."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/").lower()


# ---------------------------------------------------------------------------
# חיפוש
# ---------------------------------------------------------------------------


def build_queries(product: dict) -> list:
    """בונה שאילתות לכל שפה + מילות מפתח של 'מכירה'."""
    queries = []
    names = product.get("names", {})
    for lang, name in names.items():
        if not name:
            continue
        if lang == "he":
            queries.append(f"{name} למכירה")
            queries.append(f"{name} מכירה פומבית")
        else:
            queries.append(f"{name} for sale")
            queries.append(f"{name} auction")
    for kw in product.get("keywords", []):
        queries.append(kw)
    return queries


def search_ddg(query: str) -> list:
    """חיפוש חינמי ב-DuckDuckGo (ללא מפתח). תמיד פעיל כגיבוי."""
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=RESULTS_PER_QUERY):
                results.append(
                    {
                        "title": r.get("title", ""),
                        "url": r.get("href") or r.get("url", ""),
                        "snippet": r.get("body", ""),
                        "source": "duckduckgo",
                        "pre_verified": False,
                    }
                )
    except Exception as e:
        print(f"  [!] DuckDuckGo נכשל עבור '{query}': {e}")
    return results


# --- eBay Browse API (חינמי: 5,000 קריאות/יום) -----------------------------

_EBAY_TOKEN = {"value": None, "expires": 0}


def _ebay_token():
    """OAuth client-credentials. דורש EBAY_CLIENT_ID + EBAY_CLIENT_SECRET."""
    import base64

    if _EBAY_TOKEN["value"] and time.time() < _EBAY_TOKEN["expires"] - 60:
        return _EBAY_TOKEN["value"]
    cid = os.environ.get("EBAY_CLIENT_ID")
    sec = os.environ.get("EBAY_CLIENT_SECRET")
    if not cid or not sec:
        return None
    auth = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    j = resp.json()
    _EBAY_TOKEN["value"] = j["access_token"]
    _EBAY_TOKEN["expires"] = time.time() + int(j.get("expires_in", 7200))
    return _EBAY_TOKEN["value"]


def search_ebay(query: str) -> list:
    """
    חיפוש ישיר ב-eBay עם סינון מובנה למודעות פעילות בלבד —
    התוצאות מגיעות מאומתות מראש (pre_verified) ולא צריכות בדיקת דף.
    """
    token = None
    try:
        token = _ebay_token()
    except Exception as e:
        print(f"  [!] eBay OAuth נכשל: {e}")
    if not token:
        return []
    try:
        resp = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query, "limit": RESULTS_PER_QUERY},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        out = []
        for it in resp.json().get("itemSummaries", []):
            price = it.get("price", {})
            price_txt = f"{price.get('value', '')} {price.get('currency', '')}".strip()
            out.append(
                {
                    "title": it.get("title", ""),
                    "url": it.get("itemWebUrl", ""),
                    "snippet": f"מחיר: {price_txt} · מצב: {it.get('condition', '')} · מודעה פעילה ב-eBay",
                    "source": "ebay",
                    "pre_verified": True,  # ה-API מחזיר רק מודעות חיות
                }
            )
        return out
    except Exception as e:
        print(f"  [!] eBay search נכשל עבור '{query}': {e}")
        return []


# --- Google Programmable Search (חינמי: 100 קריאות/יום) ---------------------


def search_google(query: str) -> list:
    key = os.environ.get("GOOGLE_CSE_KEY")
    cx = os.environ.get("GOOGLE_CSE_ID")
    if not key or not cx:
        return []
    try:
        resp = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": key, "cx": cx, "q": query, "num": min(RESULTS_PER_QUERY, 10)},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return [
            {
                "title": i.get("title", ""),
                "url": i.get("link", ""),
                "snippet": i.get("snippet", ""),
                "source": "google",
                "pre_verified": False,
            }
            for i in resp.json().get("items", [])
        ]
    except Exception as e:
        print(f"  [!] Google CSE נכשל עבור '{query}': {e}")
        return []


# --- Brave Search API (חינמי: 2,000 קריאות/חודש) ----------------------------


def search_brave(query: str) -> list:
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        return []
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            params={"q": query, "count": RESULTS_PER_QUERY},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("description", ""),
                "source": "brave",
                "pre_verified": False,
            }
            for r in resp.json().get("web", {}).get("results", [])
        ]
    except Exception as e:
        print(f"  [!] Brave נכשל עבור '{query}': {e}")
        return []


def search_web(query: str) -> list:
    """
    מריץ את השאילתה בכל הספקים שהוגדרו להם מפתחות,
    ותמיד גם ב-DuckDuckGo (שלא דורש כלום).
    """
    results = []
    results += search_ebay(query)
    results += search_google(query)
    results += search_brave(query)
    results += search_ddg(query)
    return results


# ---------------------------------------------------------------------------
# אימות זמינות — הלקח מהסימולציה :)
# ---------------------------------------------------------------------------


def verify_listing(url: str) -> dict:
    """
    נכנס לדף עצמו ובודק:
    - הדף קיים (לא 404/410)
    - אין ביטויי "נמכר / אזל / הסתיים"
    - יש סימני דף מכירה (מחיר / כפתור קנייה)
    """
    verdict = {"alive": False, "available": False, "is_sale_page": False, "reason": ""}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        verdict["reason"] = f"שגיאת רשת: {type(e).__name__}"
        return verdict

    if resp.status_code >= 400:
        verdict["reason"] = f"הדף לא קיים (HTTP {resp.status_code})"
        return verdict

    verdict["alive"] = True
    text = resp.text[:400_000]  # מספיק לבדיקה, חוסך זיכרון

    if SOLD_RE.search(text):
        verdict["reason"] = "נמצא ביטוי 'נמכר / אזל מהמלאי / המכירה הסתיימה'"
        return verdict

    verdict["available"] = True
    verdict["is_sale_page"] = bool(SALE_RE.search(text))
    verdict["reason"] = "דף חי, לא נמצאו סימני מכירה שהסתיימה"
    return verdict


# דפים חדשותיים/אנציקלופדיים — לעולם לא התאמה אוטומטית (גם אם מזכירים את המוצר)
NEWS_RE = re.compile(
    r"(news|/article|/blog|blogs?\.|wikipedia|/wiki/|museum|magazine|/press|editorial|jns\.org)",
    re.IGNORECASE,
)


_FX_CACHE = {"rates": None, "day": None}


def get_fx_rates():
    """
    שערי המרה מול USD, מ-API חינמי (ללא מפתח). נשמר בקאש ליום.
    מחזיר dict כמו {'USD':1, 'ILS':3.7, 'EUR':0.92} או None בכשל.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _FX_CACHE["rates"] and _FX_CACHE["day"] == today:
        return _FX_CACHE["rates"]
    for url in (
        "https://api.frankfurter.app/latest?from=USD",  # בנק מרכזי אירופי, אמין
        "https://open.er-api.com/v6/latest/USD",         # גיבוי
    ):
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            rates = data.get("rates") or {}
            if rates:
                rates["USD"] = 1.0
                _FX_CACHE.update(rates=rates, day=today)
                return rates
        except Exception as e:
            print(f"  [!] שער מטבע מ-{url[:30]} נכשל: {e}")
    return None


# סמלי מטבע → קוד, לזיהוי מחיר בטקסט של מודעה
CURRENCY_SYMBOLS = {
    "$": "USD", "₪": "ILS", "€": "EUR", "£": "GBP",
    'ש"ח': "ILS", "שח": "ILS", "nis": "ILS", "ils": "ILS", "usd": "USD", "eur": "EUR", "gbp": "GBP",
}
# מוצא מספר עם סמל/קוד מטבע לפניו או אחריו (סמל עברי ₪ בא בד"כ אחרי)
PRICE_RE = re.compile(
    r"(?:([$₪€£])\s?)?(\d[\d,]*(?:\.\d+)?)\s?([$₪€£]|usd|ils|eur|gbp|nis|שח|ש\"ח)?",
    re.IGNORECASE,
)


def extract_price_usd(text: str, fx: dict):
    """
    מנסה לחלץ מחיר מטקסט המודעה ולהמיר ל-USD.
    מחזיר (price_usd, raw_str) או (None, None) אם לא נמצא מחיר ברור.
    שמרני: אם אין סמל/קוד מטבע — לא מנחש, מחזיר None.
    """
    if not fx:
        return None, None
    for m in PRICE_RE.finditer(text):
        sym_before, num, after = m.group(1), m.group(2), m.group(3)
        token = sym_before or after  # מטבע יכול להיות משני צדי המספר
        if not token:
            continue  # מספר בלי מטבע — לא אמין, מדלגים
        cur = CURRENCY_SYMBOLS.get(token) or CURRENCY_SYMBOLS.get(token.lower().replace(" ", ""))
        if not cur:
            continue
        try:
            val = float(num.replace(",", ""))
        except ValueError:
            continue
        rate = fx.get(cur)
        if not rate:
            continue
        return val / rate, m.group(0).strip()  # USD = מחיר-מקומי חלקי שער-מול-USD
    return None, None


def relevance_score(product: dict, title: str, snippet: str) -> int:
    """
    ניקוד התאמה (0-100) מבוסס מילים שלמות.
    - התאמת מילה שלמה בלבד (\\b) עם סובלנות לרבים — "cup" תואם "cups" אבל לא "cupboard"
    - מחושב לכל שפה בנפרד ונלקח הגבוה, כדי שדף באנגלית לא ייענש על היעדר עברית
    שימו לב: 100 = כל מילות השם קיימות, כולל המילה המבחינה (למשל "עירקי") —
    רק ציון כזה זכאי להתראה אוטומטית; פחות מזה הולך לסינון AI.
    """
    text = f"{title} {snippet}".lower()
    best = 0
    for name in product.get("names", {}).values():
        words = [w.lower() for w in re.findall(r"\w+", name or "") if len(w) > 2]
        if not words:
            continue
        hits = sum(
            1 for w in words
            if re.search(rf"\b{re.escape(w)}(s|es|ים|ות)?\b", text)
        )
        best = max(best, int(100 * hits / len(words)))
    return best


# ---------------------------------------------------------------------------
# מייל
# ---------------------------------------------------------------------------


def _build_html(matches: list, pending_count: int) -> str:
    rows = ""
    for m in matches:
        rows += f"""
        <div style="border:1px solid #ddd;border-radius:8px;padding:12px;margin:10px 0;">
          <div style="font-size:15px;font-weight:bold;">{m['product_name']}</div>
          <div style="margin:6px 0;">{m['title']}</div>
          <div style="color:#666;font-size:13px;">{m['snippet'][:200]}</div>
          {f'<div style="margin-top:6px;font-weight:bold;color:#2E6E63;">מחיר: {m["price_raw"]} (~${m["price_usd"]:.0f})</div>' if m.get('price_raw') else ''}
          <div style="margin-top:8px;">
            ציון התאמה: <b>{m['score']}%</b> | מקור: {m.get('source', 'חיפוש')} |
            {"✅ דף מכירה פעיל" if m['is_sale_page'] else "ℹ️ דף חי (לא זוהה כדף מכירה מובהק)"}
          </div>
          <a href="{m['url']}" style="display:inline-block;margin-top:8px;background:#2E6E63;color:#fff;
             padding:8px 16px;border-radius:6px;text-decoration:none;">מעבר לדף המוצר ←</a>
        </div>"""

    pending_note = ""
    if pending_count:
        pending_note = f"""<p style="color:#666;">בנוסף, {pending_count} תוצאות בציון נמוך ממתינות
        לסינון AI ידני בלוח הבקרה.</p>"""

    return f"""<html dir="rtl"><body style="font-family:Arial,sans-serif;direction:rtl;">
      <h2>עדכון ממערכת מעקב המוצרים</h2>
      <p>הסריקה בוצעה ב-{datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC.
      כל הקישורים אומתו כדפים חיים ללא סימני "נמכר".</p>
      {rows or "<p>לא נמצאו התאמות חדשות בסריקה זו.</p>"}
      {pending_note}
    </body></html>"""


def resolve_alert_email(entry: dict) -> str:
    """
    מפענח את כתובת ההתראות של מציאה — רק בזיכרון, ברגע השליחה:
    1. alert_email_key ← נפתר מתוך ALL_SECRETS (הכתובת מוצפנת כ-Secret, לא בקבצים)
    2. alert_email ← תאימות לאחור לרשומות ישנות עם כתובת גלויה
    3. ריק ← הסורק ישתמש ב-ALERT_EMAIL הכללי
    """
    key = (entry.get("alert_email_key") or "").strip()
    if key:
        try:
            all_secrets = json.loads(os.environ.get("ALL_SECRETS", "{}") or "{}")
        except json.JSONDecodeError:
            all_secrets = {}
        addr = (all_secrets.get(key) or "").strip()
        if addr:
            return addr
        print(f"  [!] Secret בשם {key} לא נמצא — ההתראה תלך לכתובת הכללית.")
    return (entry.get("alert_email") or "").strip()


def _clean(s: str) -> str:
    """מנקה כתובת מייל מתווים נסתרים שנדבקים בהעתקה מאפליקציות."""
    if not s:
        return ""
    for ch in ("\u200b", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c",
               "\ufeff", "\u00a0", "\t", "\n", "\r"):
        s = s.replace(ch, "")
    return s.strip()


def _send_via_gmail(to_addr: str, subject: str, html: str) -> bool:
    """
    שולח מייל דרך Gmail SMTP הישיר — חינמי, בלי דומיין, ולכל נמען.
    כשאתה שולח דרך השרת של Gmail אתה הבעלים המאומת, ולכן מותר לשלוח
    לכל כתובת בעולם (עד 500/יום).
    Secrets נדרשים:
      GMAIL_USER          — כתובת ה-Gmail שלך
      GMAIL_APP_PASSWORD  — סיסמת אפליקציה (16 תווים) מ-myaccount.google.com/apppasswords
    """
    user = _clean(os.environ.get("GMAIL_USER", ""))
    password = _clean(os.environ.get("GMAIL_APP_PASSWORD", ""))
    to_addr = _clean(to_addr)
    if not user or not password:
        print("[✗] לא הוגדרו GMAIL_USER / GMAIL_APP_PASSWORD.")
        return False
    if not to_addr:
        print("[!] אין כתובת נמען — מדלג.")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")  # קידוד RFC2047 לעברית
        msg["From"] = f"צייד המציאות <{user}>"
        msg["To"] = to_addr
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(user, password)
            server.sendmail(user, [to_addr], msg.as_bytes())
        return True
    except Exception as e:
        print(f"[✗] שגיאת Gmail SMTP: {type(e).__name__}: {e}")
        return False


def send_one_email(to_addr: str, subject: str, html: str) -> bool:
    """
    מנתב שליחה: בוחר את שיטת המייל לפי מה שמוגדר ב-Secrets.
    עדיפות: Gmail SMTP (כל נמען, בלי דומיין) ← Resend (מהיר, צריך דומיין לכל נמען).
    כך אפשר להחליף שיטה בלי לגעת בקוד — רק לפי אילו Secrets קיימים.
    """
    if os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD"):
        return _send_via_gmail(to_addr, subject, html)
    if os.environ.get("RESEND_API_KEY"):
        return _send_via_resend(to_addr, subject, html)
    print("[✗] לא הוגדרה שום שיטת מייל (GMAIL_USER+GMAIL_APP_PASSWORD או RESEND_API_KEY).")
    return False


def _send_via_resend(to_addr: str, subject: str, html: str) -> bool:
    """
    שולח מייל דרך Resend (https://resend.com) — חינמי (3,000/חודש, 100/יום),
    עובד משרתים אוטומטיים, ותומך בשליחה לכל נמען מכתובת שולח אחת קבועה.
    Secrets נדרשים:
      RESEND_API_KEY  — מפתח API מ-resend.com
      RESEND_FROM     — כתובת שולח מאומתת. בלי דומיין משלך השתמש ב-'onboarding@resend.dev'
                        (אפשר להוסיף שם תצוגה: 'צייד המציאות <onboarding@resend.dev>')
    מחזיר True בהצלחה.
    """
    key = _clean(os.environ.get("RESEND_API_KEY", ""))
    if not key:
        print("[✗] לא הוגדר RESEND_API_KEY — הוסף Secret עם המפתח מ-resend.com.")
        return False
    from_addr = _clean(os.environ.get("RESEND_FROM", "")) or "onboarding@resend.dev"
    to_addr = _clean(to_addr)
    if not to_addr:
        print("[!] אין כתובת נמען — מדלג.")
        return False
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"from": from_addr, "to": [to_addr], "subject": subject, "html": html},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            return True
        # Resend מחזיר JSON עם שדה error מפורט
        try:
            err = resp.json().get("message") or resp.json().get("error") or resp.text[:200]
        except Exception:
            err = resp.text[:200]
        print(f"[✗] Resend החזיר: {resp.status_code} · {err}")
        return False
    except Exception as e:
        print(f"[✗] שגיאת Resend: {e}")
        return False


def send_email(new_matches: list, pending: list):
    """
    שולח התראות לפי הנמען של כל מוצר דרך Resend:
    - למוצר עם מייל מוצפן — ההתראות שלו נשלחות לאותה כתובת
    - למוצר בלי — נשלח לכתובת הכללית מה-Secret (ALERT_EMAIL)
    כתובת שמופיעה בכמה מוצרים מקבלת מייל אחד מרוכז.
    """
    default_to = _clean(os.environ.get("ALERT_EMAIL", ""))
    if not default_to:
        print("[!] לא הוגדר ALERT_EMAIL — מדלג על שליחת מייל.")
        return

    # קיבוץ מציאות לפי נמען (הפענוח קורה כאן בלבד, בזיכרון)
    by_recipient = {}
    for m in new_matches:
        to = resolve_alert_email(m) or default_to
        by_recipient.setdefault(to, []).append(m)

    # הערה: אין שליחת מייל על תוצאות שרק ממתינות לסינון AI —
    # אלה מופיעות בלוח הבקרה, ומייל "לא נמצא כלום" רק מייצר רעש.

    for to_addr, matches in by_recipient.items():
        n = len(matches)
        subject = f"נמצאו {n} מציאות חדשות למוצרים שלך"
        pend_n = len(pending) if to_addr == default_to else 0
        if send_one_email(to_addr, subject, _build_html(matches, pend_n)):
            for m in matches:
                m["notified"] = True  # נשלח בהצלחה — לא יישלח שוב
            print(f"[✓] נשלח מייל אל {to_addr} ({n} מציאות)")


# ---------------------------------------------------------------------------
# ריצה ראשית
# ---------------------------------------------------------------------------


# תדירות סריקה פר-מוצר: ה-workflow רץ כל שעה, וכל מוצר נסרק רק אם עבר זמנו.
# 0.9 = סובלנות של 10% כי תזמוני GitHub לא מדויקים (שלא נפספס ריצה שהגיעה מוקדם ב-2 דקות).
FREQ_SECONDS = {
    "hourly": 3600,
    "daily": 86400,
    "weekly": 604800,
    "monthly": 2592000,
}


def product_is_due(product: dict, last_scans: dict, now_ts: float) -> bool:
    if os.environ.get("FORCE_SCAN", "").lower() == "true":
        return True  # הרצה ידנית — סורקים הכל
    freq = product.get("scan_frequency", "hourly")  # רשומות ישנות בלי שדה ← כל ריצה
    interval = FREQ_SECONDS.get(freq, 3600)
    last_iso = last_scans.get(product.get("id", ""), "")
    if not last_iso:
        return True
    try:
        last_ts = datetime.fromisoformat(last_iso).timestamp()
    except ValueError:
        return True
    return (now_ts - last_ts) >= interval * 0.9


def product_max_usd(product: dict, fx: dict):
    """
    ממיר את תקרת המחיר של המוצר ל-USD.
    max_price ריק/0/None → אין תקרה (מחזיר None).
    """
    raw = product.get("max_price")
    if raw in (None, "", 0, "0"):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    cur = (product.get("max_price_currency") or "USD").upper()
    if not fx:
        return None
    rate = fx.get(cur, 1.0)
    return val / rate  # מחיר-מקומי → USD


def main():
    products = load_json(PRODUCTS_FILE, [])
    found = load_json(FOUND_FILE, {"seen_urls": [], "matches": []})
    seen = set(found.get("seen_urls", []))

    # מצב "בדיקת מייל" — מהריצה הידנית: שולח מייל בדיקה דרך Resend
    if os.environ.get("TEST_EMAIL", "").lower() == "true":
        print("🧪 מצב בדיקת מייל...")
        to = _clean(os.environ.get("ALERT_EMAIL", ""))
        if not to:
            print("[✗] לא הוגדר ALERT_EMAIL — הוסף Secret עם כתובת המייל שלך.")
            return
        print(f"    נמען: {to}")
        ok = send_one_email(
            to,
            "בדיקת מייל - מערכת מעקב המוצרים",
            "<html dir='rtl'><body><h3>הבדיקה עברה בהצלחה</h3>"
            "<p>אם קיבלת את זה — שליחת המיילים עובדת.</p></body></html>",
        )
        if ok:
            print(f"[✓] מייל בדיקה נשלח אל {to} — בדוק גם בספאם.")
        else:
            print("[✗] הבקשה נכשלה — ראה את השגיאה למעלה.")
        return

    # מצב "התראות בלבד" — מופעל מהממשק אחרי אישור מציאות בסינון AI:
    # מדלג על הסריקה כולה ורק שולח מיילים על מציאות שטרם דווחו.
    if os.environ.get("NOTIFY_ONLY", "").lower() == "true":
        print("📨 מצב התראות בלבד — מדלג על סריקה.")
        notify_unsent(found, [])
        return

    if not products:
        print("[!] אין מוצרים ב-products.json — אין מה לסרוק.")
        return

    new_matches, pending = [], []
    last_scans = found.get("product_last_scan", {})
    now = datetime.now(timezone.utc)
    fx = get_fx_rates()  # שערי המרה, פעם אחת לכל הריצה
    if fx:
        print(f"💱 שערי מטבע נטענו (USD→ILS: {fx.get('ILS', '?')}).")
    else:
        print("[!] לא ניתן לטעון שערי מטבע — סינון המחיר יושבת לריצה זו.")

    for product in products:
        if not product.get("active", True):
            continue
        pname = product.get("names", {}).get("he") or product.get("names", {}).get("en", "?")

        if not product_is_due(product, last_scans, now.timestamp()):
            freq = product.get("scan_frequency", "hourly")
            print(f"⏭ מדלג על '{pname}' — תדירות '{freq}', טרם הגיע זמנו.")
            continue

        print(f"\n=== סורק: {pname} ===")
        last_scans[product.get("id", "")] = now.isoformat()

        candidates = {}
        for q in build_queries(product):
            print(f"  🔍 {q}")
            for r in search_web(q):
                url = r["url"]
                if not url:
                    continue
                norm = normalize_url(url)
                if norm in seen or norm in candidates:
                    continue
                candidates[norm] = r
            time.sleep(SLEEP_BETWEEN_REQUESTS)

        print(f"  נמצאו {len(candidates)} מועמדים חדשים, מאמת זמינות...")

        for norm, r in candidates.items():
            score = relevance_score(product, r["title"], r["snippet"])
            if score < 50:
                continue  # רעש — חסרות יותר מדי מילות זיהוי

            is_newsy = bool(NEWS_RE.search(f"{r['url']} {r['title']}"))

            if r.get("pre_verified"):
                # תוצאת API רשמי (eBay) — מסוננת מראש למודעות פעילות בלבד
                verdict = {"alive": True, "available": True, "is_sale_page": True,
                           "reason": f"אומת מראש דרך {r.get('source', 'API')}"}
            else:
                verdict = verify_listing(r["url"])
                time.sleep(SLEEP_BETWEEN_REQUESTS)
            print(f"    [{score}%]{' [חדשותי]' if is_newsy else ''} ({r.get('source','?')}) {r['url'][:70]} → {verdict['reason']}")

            if not verdict["available"]:
                seen.add(norm)  # לא נבדוק שוב דף מת/נמכר
                continue

            # --- סינון מחיר ---
            price_usd, price_raw = extract_price_usd(f"{r['title']} {r['snippet']}", fx)
            max_usd = product_max_usd(product, fx)
            over_budget = False
            if max_usd is not None and price_usd is not None and price_usd > max_usd:
                over_budget = True
                print(f"      💰 מעל התקציב: {price_raw} (~${price_usd:.0f}) > ${max_usd:.0f}")

            entry = {
                "product_id": product.get("id"),
                "product_name": pname,
                # פרטיות: נשמר רק שם ה-Secret — הכתובת מפוענחת רק ברגע השליחה
                "alert_email_key": (product.get("alert_email_key") or "").strip(),
                "alert_email": (product.get("alert_email") or "").strip(),  # תאימות לאחור
                "title": r["title"],
                "snippet": r["snippet"],
                "url": r["url"],
                "source": r.get("source", ""),
                "score": score,
                "is_sale_page": verdict["is_sale_page"],
                "price_usd": round(price_usd, 2) if price_usd is not None else None,
                "price_raw": price_raw,
                "notified": False,  # יסומן True רק אחרי שליחת מייל מוצלחת
                "found_at": datetime.now(timezone.utc).isoformat(),
            }

            # התראה אוטומטית: כל מילות השם + דף מכירה + לא כתבה + לא מעל התקציב.
            # מעל התקציב, או ציון חלקי, או חדשותי — לסינון AI בלוח הבקרה.
            if score >= 100 and verdict["is_sale_page"] and not is_newsy and not over_budget:
                new_matches.append(entry)
                seen.add(norm)
            else:
                pending.append(entry)  # ילך לסינון AI ידני בלוח הבקרה
                seen.add(norm)

    # שמירת מצב
    found["seen_urls"] = sorted(seen)
    found["matches"] = (found.get("matches", []) + new_matches)[-500:]
    found["last_scan"] = datetime.now(timezone.utc).isoformat()
    found["product_last_scan"] = last_scans
    save_json(FOUND_FILE, found)

    old_pending = load_json(PENDING_FILE, [])
    if not isinstance(old_pending, list):
        old_pending = []
    save_json(PENDING_FILE, (old_pending + pending)[-200:])

    print(f"\n[✓] סיכום: {len(new_matches)} מציאות מאומתות, {len(pending)} ממתינות לסינון AI.")

    notify_unsent(found, pending)


def notify_unsent(found: dict, pending: list):
    """
    שולח מייל על כל מציאה מאומתת עם notified=False ומסמן אותה אחרי שליחה מוצלחת.
    מכסה: מציאות חדשות מהסריקה, מציאות שאושרו בסינון AI מהממשק,
    ומיילים שנכשלו בריצה קודמת (יישלחו שוב בריצה הבאה).
    לא שולח כלום על תוצאות שרק ממתינות לסינון AI — אלה מופיעות בלוח הבקרה.
    רשומות ישנות ללא הדגל נחשבות כמדווחות (שלא נציף במיילים ישנים).
    """
    unsent = [m for m in found.get("matches", []) if m.get("notified") is False]
    if not unsent:
        print("אין מציאות חדשות מאומתות לדווח.")
        return
    try:
        send_email(unsent, pending)
        save_json(FOUND_FILE, found)  # שמירת דגלי notified שעודכנו
    except Exception:
        import traceback
        print("[!] שליחת המייל נכשלה — יינתן ניסיון נוסף בריצה הבאה. השגיאה המלאה:")
        traceback.print_exc()


if __name__ == "__main__":
    main()
