import json, shutil, os, re
from collections import defaultdict

with open("boostix_store_catalog.json", encoding="utf-8") as f:
    data = json.load(f)

# 1) تصحيح أسعار جواكر ومنع خطأ الضرب (10000 توكن = 100$)
jawaker_prices = {
    "5186": (1.03, False),   # جواكر (10,000 توكن)
    "72":   (18.30, False),  # عرض جواكر الاسبوعي
    "5522": (5.05, False),   # جواكر premium فضي
    "5523": (25.13, False),  # جواكر premium +بلس احمر
}

# 2) إزالة الأقسام الفرعية التي تحمل نفس اسم القسم الرئيسي (مثل تطبيقات دردشة داخل تطبيقات دردشة)
main_names = {m.get("cardName", "").strip() for m in data.get("main_categories", [])}
orig_sub_len = len(data.get("sub_categories", []))
data["sub_categories"] = [
    s for s in data.get("sub_categories", [])
    if not (s.get("cardName", "").strip() == s.get("parent_name", "").strip())
]
removed_nested = orig_sub_len - len(data["sub_categories"])

# 3) تنظيف أسماء المنتجات المبهمة + ضبط أسعار جواكر + تفعيل الباقات
duration_words = ["شهري", "سنوي", "اسبوعي", "أسبوعي", "شهر", "أشهر", "اشهر", "سنة", "عضوية"]
renamed_count = 0
fixed_zero_price = 0

for p in data.get("products", []):
    pid = str(p.get("id", ""))
    
    # ضبط جواكر
    if pid in jawaker_prices:
        price, frac = jawaker_prices[pid]
        p["price_usd"] = price
        p["cardPrice"] = price
        p["fraction"] = frac
        if pid == "5186":
            p["name"] = "جواكر - 10,000 توكن"
            p["cardName"] = "جواكر - 10,000 توكن"

    # توضيح الأسماء القصيرة/المبهمة
    name = (p.get("name") or "").strip()
    sub = (p.get("sub_category") or "").strip()
    if len(name) <= 18 and any(w in name for w in duration_words):
        # استخراج اسم الخدمة النظيف من sub_category
        service_prefix = sub.split(" - ")[0].strip()
        if service_prefix and service_prefix.lower() not in name.lower():
            new_name = f"{service_prefix} - {name}"
            p["name"] = new_name
            p["cardName"] = new_name
            renamed_count += 1

    # ضمان تفعيل الباقات وعدم اختفائها بسبب السعر 0
    p["enabled"] = True
    p["available"] = True
    if not p.get("price_usd") or float(p.get("price_usd") or 0) <= 0:
        p["price_usd"] = 1.00
        p["cardPrice"] = 1.00
        fixed_zero_price += 1

# 4) تمييز الخدمات المكررة داخل نفس القسم بـ (سيرفر 1) و(سيرفر 2)
grouped = defaultdict(list)
for p in data.get("products", []):
    key = (p.get("sub_category", "").strip(), p.get("name", "").strip())
    grouped[key].append(p)

dup_tagged = 0
for key, items in grouped.items():
    if len(items) > 1:
        for idx, item in enumerate(items, start=1):
            if f"سيرفر {idx}" not in item["name"]:
                item["name"] = f"{item['name']} (سيرفر {idx})"
                item["cardName"] = item["name"]
                dup_tagged += 1

with open("boostix_store_catalog.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

if os.path.exists("backend/boostix_store_catalog.json"):
    shutil.copy2("boostix_store_catalog.json", "backend/boostix_store_catalog.json")

print(f"✅ تم تنظيف الكتالوج بنجاح:")
print(f" - أقسام متداخلة مكررة أُزيلت: {removed_nested}")
print(f" - بطاقات مبهمة تم توضيح أسمائها: {renamed_count}")
print(f" - منتجات مكررة تم وسمها (سيرفر 1/2): {dup_tagged}")
print(f" - باقات كانت بسعر 0 وتم تفعيلها: {fixed_zero_price}")
print(f" - إجمالي المنتجات الجاهزة: {len(data['products'])}")
