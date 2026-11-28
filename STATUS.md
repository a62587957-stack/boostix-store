# Boostix Store - سجل التقدم (STATUS)

## المرحلة 1: تنظيف الأساس وتوحيد الملفات
- [x] فحص بنية المجلدات وإعدادات Git
- [x] توحيد الملفات في هيكل نظيف (server.py, store.html, admin.html, boostix_store_catalog.json)
- [ ] ضبط الرفع المباشر إلى GitHub

## المرحلة 2: ضبط الواجهة المحلية وتسجيل الدخول
- [x] اختبار المتجر محلياً (12 قسم + 2,206 باقة + البحث)
- [ ] اختبار تسجيل الدخول بحساب Google وحفظ الجلسة

## المرحلة 3: النشر على Render
- [x] رفع النسخة النظيفة ومراقبة البناء على Render

## المرحلة 4: لوحة التحكم والتسعير
- [ ] فحص admin.html وإدارة الطلبات والإيداعات والأسعار

## المرحلة 5: التحسينات واللمسات الأخيرة
- [ ] تحسين عرض الموبايل وإضافة روابط الدعم الفني
- 2026-11-27: تم تجهيز ~/boostix_full_seed.sql (2207 سطر)؛ /api/customer/bootstrap يعيد HTTP 200 مع products=0 بانتظار تنفيذ الإدراج في Supabase SQL Editor وتحديث/إعادة تشغيل SUPABASE_SECRET_KEY في Render.
- 2026-11-27: تم تجهيز ~/boostix_final_seed.sql؛ public.products ما زال */0 و/api/customer/bootstrap يعيد 0 منتج.
- 2026-11-27: public.products ما زال */0 وpublic.categories غير موجود؛ تم تجهيز ~/boostix_seed_batch.sql.
- 2026-11-27: public.products ما زال */0 و/api/customer/bootstrap يعيد 0 منتج؛ تجهيز إدراج على دفعات في SQL Editor.
- 2026-11-27: تم تنظيف ~/boostix_chunk_*.sql؛ public.products ما زال */0 و/api/customer/bootstrap يعيد 0 منتج.
- 2026-11-27: /rest/v1/products يعيد HTTP 200 */0؛ فحص ملفات البذر وصلاحيات الإدراج عبر REST.
- 2026-11-27: فحص ./backend/.env ومراجع قاعدة البيانات في server.py وbackend.
- 2026-11-27: تنفيذ إدراج seed_products.sql عبر REST باستخدام SUPABASE_SECRET_KEY وفحص bootstrap.
- 2026-11-27: SUPABASE_SECRET_KEY في .env أعطى 401 Unregistered API key؛ يلزم تشغيل SQL مباشرة في Supabase SQL Editor وتحديث المفتاح على Render.
- 2026-11-28: تم سحب كتالوج sonic8store.com بالكامل (12 رئيسي | 215 فرعي | 103 مستوى 3 | 2408 منتج) ودمج حقول الأسعار والتوفر في boostix_store_catalog.json.
- 2026-11-28: تم سحب كتالوج sonic8store.com بالكامل (12 رئيسي | 215 فرعي | 103 مستوى 3 | 2408 منتج) ودمج حقول الأسعار والتوفر في boostix_store_catalog.json.
- 2026-11-28: تم تشغيل clean_catalog.py لتصحيح أسعار جواكر، توضيح الأسماء المبهمة، إزالة الأقسام المتداخلة، ووسم المكررات بسيرفر 1/2.
