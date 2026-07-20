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

# סימנים לכך שמדובר בדף מכירה (ולא מאמר/מוזיאון)
SALE_SIGNALS = [
    r"add to cart", r"buy it now", r"place bid", r"checkout", r"free shipping",
    r"הוסף לסל", r"הוספה לסל", r"קנה עכשיו", r"לרכישה", r"הגש הצעה",
    r"[₪$€£]\s?\d", r"\d+\s?(₪|ש\"ח|שח)\b", r"\bUS \$\d", r"\bprice\b", r"מחיר",
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


def relevance_score(product: dict, title: str, snippet: str) -> int:
    """
    ניקוד התאמה (0-100) מבוסס מילים.
    מחושב לכל שפה בנפרד ונלקח הציון הגבוה — כדי שדף באנגלית
    לא ייענש על כך שאינו מכיל את המילים בעברית, ולהפך.
    """
    text = f"{title} {snippet}".lower()
    best = 0
    for name in product.get("names", {}).values():
        words = {w.lower() for w in re.findall(r"\w+", name or "") if len(w) > 2}
        if not words:
            continue
        hits = sum(1 for w in words if w in text)
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


def _smtp_config():
    """
    קורא הגדרות SMTP גנריות — עובד עם כל שירות (Brevo, Mailjet, SendGrid...).
    תאימות לאחור: אם הוגדרו GMAIL_USER/GMAIL_APP_PASSWORD הישנים — ישתמש בהם.
    """
    host = os.environ.get("SMTP_HOST", "").strip()
    if host:
        return {
            "host": host,
            "port": int(os.environ.get("SMTP_PORT", "587")),
            "user": os.environ.get("SMTP_USER", "").strip(),
            "password": os.environ.get("SMTP_PASSWORD", "").strip(),
            "from_addr": os.environ.get("SMTP_FROM", "").strip()
            or os.environ.get("SMTP_USER", "").strip(),
        }
    # תאימות לאחור ל-Gmail
    if os.environ.get("GMAIL_USER"):
        return {
            "host": "smtp.gmail.com",
            "port": 465,
            "user": os.environ.get("GMAIL_USER", "").strip(),
            "password": os.environ.get("GMAIL_APP_PASSWORD", "").strip(),
            "from_addr": os.environ.get("GMAIL_USER", "").strip(),
        }
    return None


def _smtp_connect(c):
    """פורט 465 ← SSL ישיר; אחרת (587) ← STARTTLS."""
    if c["port"] == 465:
        server = smtplib.SMTP_SSL(c["host"], c["port"], timeout=30)
    else:
        server = smtplib.SMTP(c["host"], c["port"], timeout=30)
        server.starttls()
    server.login(c["user"], c["password"])
    return server


def send_email(new_matches: list, pending: list):
    """
    שולח התראות לפי הנמען של כל מוצר:
    - למוצר עם מייל מוצפן — ההתראות שלו נשלחות לאותה כתובת
    - למוצר בלי — נשלח לכתובת הכללית מה-Secret (ALERT_EMAIL)
    כתובת שמופיעה בכמה מוצרים מקבלת מייל אחד מרוכז.
    """
    c = _smtp_config()
    default_to = os.environ.get("ALERT_EMAIL", "").strip() or (c and c["from_addr"])

    if not c or not c["user"] or not c["password"]:
        print("[!] לא הוגדרו פרטי SMTP (SMTP_HOST/USER/PASSWORD) — מדלג על שליחת מייל.")
        return

    # קיבוץ מציאות לפי נמען (הפענוח קורה כאן בלבד, בזיכרון)
    by_recipient = {}
    for m in new_matches:
        to = resolve_alert_email(m) or default_to
        by_recipient.setdefault(to, []).append(m)

    # אם אין מציאות אבל יש ממתינות — עדכון קצר לכתובת הכללית בלבד
    if not by_recipient and pending:
        by_recipient[default_to] = []

    server = _smtp_connect(c)
    try:
        for to_addr, matches in by_recipient.items():
            n = len(matches)
            subject = (
                f"🔔 נמצאו {n} מציאות חדשות למוצרים שלך"
                if n
                else "🔎 סריקה הסתיימה — מועמדים ממתינים לבדיקה"
            )
            # מונה הממתינות רלוונטי רק לנמען הכללי (שם מתבצע הסינון בלוח הבקרה)
            pend_n = len(pending) if to_addr == default_to else 0

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = c["from_addr"]
            msg["To"] = to_addr
            msg.attach(MIMEText(_build_html(matches, pend_n), "html", "utf-8"))
            server.sendmail(c["from_addr"], [to_addr], msg.as_string())
            print(f"[✓] נשלח מייל אל {to_addr} ({n} מציאות)")
    finally:
        server.quit()


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


def main():
    products = load_json(PRODUCTS_FILE, [])
    found = load_json(FOUND_FILE, {"seen_urls": [], "matches": []})
    seen = set(found.get("seen_urls", []))

    if not products:
        print("[!] אין מוצרים ב-products.json — אין מה לסרוק.")
        return

    new_matches, pending = [], []
    last_scans = found.get("product_last_scan", {})
    now = datetime.now(timezone.utc)

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
            if score < 30:
                continue  # רעש — לא שווה אפילו בדיקת דף

            if r.get("pre_verified"):
                # תוצאת API רשמי (eBay) — מסוננת מראש למודעות פעילות בלבד
                verdict = {"alive": True, "available": True, "is_sale_page": True,
                           "reason": f"אומת מראש דרך {r.get('source', 'API')}"}
            else:
                verdict = verify_listing(r["url"])
                time.sleep(SLEEP_BETWEEN_REQUESTS)
            print(f"    [{score}%] ({r.get('source','?')}) {r['url'][:70]} → {verdict['reason']}")

            if not verdict["available"]:
                seen.add(norm)  # לא נבדוק שוב דף מת/נמכר
                continue

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
                "found_at": datetime.now(timezone.utc).isoformat(),
            }

            if score >= 60 and verdict["is_sale_page"]:
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
    save_json(PENDING_FILE, (old_pending + pending)[-200:])

    print(f"\n[✓] סיכום: {len(new_matches)} מציאות מאומתות, {len(pending)} ממתינות לסינון AI.")

    if new_matches or pending:
        try:
            send_email(new_matches, pending)
        except Exception as e:
            print(f"[!] שליחת המייל נכשלה: {e}")
            sys.exit(0)  # לא מפילים את ה-workflow בגלל מייל


if __name__ == "__main__":
    main()
