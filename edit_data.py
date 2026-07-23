# -*- coding: utf-8 -*-
"""
Edit Data — מבצע עריכות על קבצי הנתונים בתוך GitHub Actions.
כך הטוקן והסיסמה נשארים מוסתרים ב-Secrets ולא נחשפים בדפדפן.

מאמת את סיסמת העריכה מול EDIT_PASSWORD (Secret), ואם תקין —
מבצע את הפעולה המבוקשת ושומר את הקבצים (ה-workflow עושה commit).
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

PRODUCTS_FILE = "products.json"
FOUND_FILE = "found.json"
PENDING_FILE = "pending_review.json"

FREQ_VALID = {"hourly", "daily", "weekly", "monthly"}


def load(path, default):
    """
    טוען קובץ JSON. אם הקובץ קיים אך פגום — נכשל בקול רם ולא מחזיר ברירת מחדל.
    זה קריטי: החזרת ברירת מחדל על קובץ פגום גרמה בעבר לאיפוס כל המוצרים
    (הפעולה הבאה הייתה שומרת רשימה ריקה מעל הנתונים האמיתיים).
    """
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"הקובץ {path} פגום ({e}). העריכה בוטלה כדי לא לאבד נתונים. "
             f"שחזר את הקובץ מהיסטוריית git ונסה שוב.")


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_products(products, before_count, allow_empty=False):
    """
    שומר מוצרים עם הגנה מפני מחיקה המונית לא־מכוונת.
    allow_empty=True רק במחיקה מפורשת של מוצר (שם הגעה ל-0 היא לגיטימית).
    """
    if not allow_empty and before_count > 0 and len(products) == 0:
        fail(f"עצירת בטיחות: הפעולה הייתה מוחקת את כל {before_count} המוצרים. "
             f"לא נשמר דבר.")
    save(PRODUCTS_FILE, products)


def fail(msg):
    print(f"[✗] {msg}")
    sys.exit(1)


def main():
    expected = (os.environ.get("EDIT_PASSWORD") or "").strip()
    given = (os.environ.get("IN_PASSWORD") or "").strip()
    action = (os.environ.get("IN_ACTION") or "").strip()

    if not expected:
        fail("לא הוגדר EDIT_PASSWORD ב-Secrets של הריפו.")
    if given != expected:
        fail("סיסמת עריכה שגויה.")

    try:
        payload = json.loads(os.environ.get("IN_PAYLOAD") or "{}")
    except json.JSONDecodeError:
        fail("payload אינו JSON תקין.")

    products = load(PRODUCTS_FILE, [])
    found = load(FOUND_FILE, {"seen_urls": [], "matches": []})
    pending = load(PENDING_FILE, [])
    products_before = len(products)  # להגנת בטיחות מפני מחיקה המונית

    print(f"פעולה: {action}")

    # ---- מוצרים ----
    if action == "add_product":
        prod = payload.get("product")
        if not isinstance(prod, dict) or not prod.get("names"):
            fail("חסרים נתוני מוצר.")
        # מזהה ייחודי אם לא סופק
        if not prod.get("id"):
            base = (prod["names"].get("en") or "product").lower()
            base = "".join(c if c.isalnum() else "-" for c in base).strip("-")
            prod["id"] = f"{base}-{int(time.time())}"
        prod.setdefault("active", True)
        prod.setdefault("added_at", datetime.now(timezone.utc).isoformat())
        products.append(prod)
        save_products(products, products_before)
        print(f"[✓] נוסף מוצר: {prod['names'].get('he') or prod['names'].get('en')}")

    elif action == "update_product":
        pid = payload.get("id")
        updates = payload.get("product")
        if not isinstance(updates, dict):
            fail("חסרים נתוני עדכון.")
        found_prod = False
        for p in products:
            if p.get("id") == pid:
                # שומרים על שדות מערכת, מעדכנים את השאר
                for key in ("names", "keywords", "image", "notes",
                            "scan_frequency", "max_price", "max_price_currency", "alert_email",
                            "ship_to", "ship_to_he", "ship_to_en", "site_group"):
                    if key in updates:
                        p[key] = updates[key]
                found_prod = True
        if not found_prod:
            fail("המוצר לא נמצא.")
        save_products(products, products_before)
        print(f"[✓] מוצר {pid} עודכן")

    elif action == "delete_product":
        pid = payload.get("id")
        if not any(p.get("id") == pid for p in products):
            print(f"[i] המוצר {pid} כבר לא קיים — אין מה למחוק.")
            print("[✓] הפעולה הושלמה.")
            return
        products = [p for p in products if p.get("id") != pid]
        # מחיקה מפורשת של מוצר בודד — מותר להגיע ל-0
        save_products(products, products_before, allow_empty=True)
        print(f"[✓] נמחק מוצר {pid}")

    elif action == "toggle_product":
        pid = payload.get("id")
        for p in products:
            if p.get("id") == pid:
                p["active"] = not p.get("active", True)
        save_products(products, products_before)
        print(f"[✓] סטטוס מוצר {pid} עודכן")

    elif action == "set_freq":
        pid = payload.get("id")
        freq = payload.get("frequency")
        if freq not in FREQ_VALID:
            fail("תדירות לא חוקית.")
        for p in products:
            if p.get("id") == pid:
                p["scan_frequency"] = freq
        save_products(products, products_before)
        print(f"[✓] תדירות מוצר {pid} → {freq}")

    # ---- מציאות ----
    # מזוהות לפי URL ולא לפי אינדקס: כשמוחקים כמה ברצף, כל ריצה עובדת על
    # צילום מצב אחר והאינדקסים מזדחלים. URL הוא מזהה יציב.
    elif action == "delete_match":
        url = (payload.get("url") or "").strip()
        idx = payload.get("index")
        matches = found.get("matches", [])
        before = len(matches)
        if url:
            found["matches"] = [m for m in matches if (m.get("url") or "") != url]
        elif isinstance(idx, int) and 0 <= idx < before:
            matches.pop(idx)  # תאימות לאחור
            found["matches"] = matches
        else:
            print(f"[i] המציאה כבר לא קיימת (url={url or idx}) — אין מה למחוק.")
            print("[✓] הפעולה הושלמה.")
            return
        removed = before - len(found["matches"])
        save(FOUND_FILE, found)
        print(f"[✓] נמחקו {removed} מציאות")

    elif action == "resend_match":
        url = (payload.get("url") or "").strip()
        idx = payload.get("index")
        matches = found.get("matches", [])
        hit = False
        if url:
            for m in matches:
                if (m.get("url") or "") == url:
                    m["notified"] = False
                    hit = True
        elif isinstance(idx, int) and 0 <= idx < len(matches):
            matches[idx]["notified"] = False
            hit = True
        if not hit:
            print("[i] המציאה לא נמצאה — אין מה לשלוח.")
            print("[✓] הפעולה הושלמה.")
            return
        save(FOUND_FILE, found)
        print("[✓] המציאה סומנה לשליחה חוזרת")

    # ---- סינון AI ----
    elif action == "approve_ai":
        approved = payload.get("approved", [])
        if not isinstance(approved, list):
            fail("approved חייב להיות רשימה.")
        # ה-URLs שהיו בתור לפני האישור — כולם צריכים להיכנס ל-seen_urls,
        # אחרת הסריקה הבאה תמצא אותם שוב ותחזיר אותם לתור (זה היה הבאג).
        from urllib.parse import urlparse

        def norm_url(u):
            p = urlparse(u or "")
            return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/").lower()

        seen = set(found.get("seen_urls", []))
        for item in pending:  # כל התור הישן — מאושרים ונדחים כאחד
            if item.get("url"):
                seen.add(norm_url(item["url"]))
        for a in approved:
            a["notified"] = False
            if a.get("url"):
                seen.add(norm_url(a["url"]))
        found["seen_urls"] = sorted(seen)
        found.setdefault("matches", []).extend(approved)
        save(FOUND_FILE, found)
        save(PENDING_FILE, [])  # מנקים את התור אחרי אישור
        print(f"[✓] {len(approved)} מציאות אושרו, {len(pending)} URLs נוספו ל-seen")

    else:
        fail(f"פעולה לא מוכרת: {action}")

    print("[✓] הפעולה הושלמה.")


if __name__ == "__main__":
    main()
