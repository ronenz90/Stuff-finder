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
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return default
    return default


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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
        save(PRODUCTS_FILE, products)
        print(f"[✓] נוסף מוצר: {prod['names'].get('he') or prod['names'].get('en')}")

    elif action == "delete_product":
        pid = payload.get("id")
        products = [p for p in products if p.get("id") != pid]
        save(PRODUCTS_FILE, products)
        print(f"[✓] נמחק מוצר {pid}")

    elif action == "toggle_product":
        pid = payload.get("id")
        for p in products:
            if p.get("id") == pid:
                p["active"] = not p.get("active", True)
        save(PRODUCTS_FILE, products)
        print(f"[✓] סטטוס מוצר {pid} עודכן")

    elif action == "set_freq":
        pid = payload.get("id")
        freq = payload.get("frequency")
        if freq not in FREQ_VALID:
            fail("תדירות לא חוקית.")
        for p in products:
            if p.get("id") == pid:
                p["scan_frequency"] = freq
        save(PRODUCTS_FILE, products)
        print(f"[✓] תדירות מוצר {pid} → {freq}")

    # ---- מציאות ----
    elif action == "delete_match":
        idx = payload.get("index")
        matches = found.get("matches", [])
        if isinstance(idx, int) and 0 <= idx < len(matches):
            matches.pop(idx)
            save(FOUND_FILE, found)
            print(f"[✓] נמחקה מציאה {idx}")
        else:
            fail("אינדקס מציאה שגוי.")

    elif action == "resend_match":
        idx = payload.get("index")
        matches = found.get("matches", [])
        if isinstance(idx, int) and 0 <= idx < len(matches):
            matches[idx]["notified"] = False
            save(FOUND_FILE, found)
            print(f"[✓] מציאה {idx} סומנה לשליחה חוזרת")
        else:
            fail("אינדקס מציאה שגוי.")

    # ---- סינון AI ----
    elif action == "approve_ai":
        approved = payload.get("approved", [])
        if not isinstance(approved, list):
            fail("approved חייב להיות רשימה.")
        for a in approved:
            a["notified"] = False
        found.setdefault("matches", []).extend(approved)
        save(FOUND_FILE, found)
        save(PENDING_FILE, [])  # מנקים את התור אחרי אישור
        print(f"[✓] {len(approved)} מציאות אושרו מסינון AI")

    else:
        fail(f"פעולה לא מוכרת: {action}")

    print("[✓] הפעולה הושלמה.")


if __name__ == "__main__":
    main()
