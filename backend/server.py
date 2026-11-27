import re, gzip
from urllib.parse import urlencode, quote
import os, json, time, hmac, hashlib, base64, struct, secrets, threading
import urllib.request, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from urllib.parse import urlparse, quote, parse_qs

CUR_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(CUR_DIR)
BASE_DIR = PARENT_DIR if os.path.basename(CUR_DIR) == "backend" else CUR_DIR

def _resolve_file(*rels):
    for r in rels:
        p = os.path.join(BASE_DIR, r)
        if os.path.exists(p):
            return p
    return os.path.join(BASE_DIR, rels[0])

ENV_FILE = _resolve_file("backend/.env", ".env")
STORE_HTML = _resolve_file("public/store.html", "store.html")
ADMIN_HTML = _resolve_file("public/admin.html", "admin.html")
CATALOG_FILE = _resolve_file("boostix_store_catalog.json", "backend/boostix_store_catalog.json")

def load_env():
    cfg = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    cfg[k.strip()] = v.strip()
    return cfg

CFG = load_env()
SUPABASE_URL = os.environ.get("SUPABASE_URL") or CFG.get("SUPABASE_URL", "https://lxeloayuowzatrrlsasn.supabase.co")
SUPABASE_PUB = os.environ.get("SUPABASE_PUBLISHABLE_KEY") or CFG.get("SUPABASE_PUBLISHABLE_KEY", "")
SUPABASE_SECRET = os.environ.get("SUPABASE_SECRET_KEY") or CFG.get("SUPABASE_SECRET_KEY", "")

# لا توجد كلمة مرور افتراضية في الكود إطلاقاً
ADMIN_SECRET = (os.environ.get("ADMIN_SECRET") or CFG.get("ADMIN_SECRET", "")).strip()
RAW_ADMIN_PATH = (os.environ.get("ADMIN_PATH") or CFG.get("ADMIN_PATH", "")).strip()
ADMIN_PATH = ("/" + RAW_ADMIN_PATH.strip("/")) if RAW_ADMIN_PATH else ""
ADMIN_TOTP_SECRET = (os.environ.get("ADMIN_TOTP_SECRET") or CFG.get("ADMIN_TOTP_SECRET", "")).strip()
SESSION_SIGN_KEY = (os.environ.get("SESSION_SIGN_KEY") or CFG.get("SESSION_SIGN_KEY", "") or secrets.token_hex(32)).encode("utf-8")
PORT = int(os.environ.get("PORT", 8090))

MAX_BODY_BYTES = 2 * 1024 * 1024
SESSION_TTL_SEC = 4 * 3600  # 4 ساعات
MAX_LOGIN_FAILS = 5
LOCKOUT_SEC = 30 * 60       # 30 دقيقة حظر بعد 5 محاولات خاطئة
FAIL_WINDOW_SEC = 15 * 60

_SEC_LOCK = threading.Lock()
_FAILED_LOGINS = {}   # ip -> {"fails": [timestamps], "locked_until": float}
_RATE_BUCKETS = {}    # (ip, bucket) -> [timestamps]
_REVOKED_TOKENS = {}  # jti -> exp_ts

SETTINGS_SKU = "__STORE_SETTINGS__"
DEFAULT_SETTINGS = {
    "rate_syp": 15000,
    "sham_account": "f3bdb7354d6e1b254d2b1418e9ee916d",
    "syriatel_account": "0931735665",
    "usdt_account": "Binance ID: 554433221",
    "default_margin_pct": 15.0,
    "hero_banners": [
        {
            "id": "default_1",
            "title": "مرحباً بك في Boostix Store",
            "img_dark": "/assets/hero_dark.webp",
            "img_light": "/assets/hero_light.webp",
            "target_cat": "الألعاب",
            "active": True
        }
    ],
    "promo_banner": {
        "img_dark": "/assets/promo_dark.webp",
        "img_light": "/assets/promo_light.webp",
        "target_cat": "شاشة VIP",
        "active": True
    },
    "category_images": {},
    "suppliers": []
}

# === ذاكرة التخزين المؤقت السريعة (RAM Cache) لتسريع فتح المتجر لأقل من ثانية ===
_CACHE_LOCK = threading.Lock()
_CACHE = {
    "products_active": {"ts": 0, "data": None},
    "settings": {"ts": 0, "data": None},
    "catalog_tree": None,
    "html_files": {},
    "media": {}
}
CACHE_TTL_SEC = 600

def invalidate_cache(kind="all"):
    with _CACHE_LOCK:
        if kind in ("all", "products"):
            _CACHE["products_active"] = {"ts": 0, "data": None}
        if kind in ("all", "settings"):
            _CACHE["settings"] = {"ts": 0, "data": None}


def _is_sonic8(api_url):
    return "sonic8store.com" in str(api_url or "").lower()

def _sonic8_get(api_url, api_key, endpoint_path, timeout=15):
    import urllib.parse
    p = urllib.parse.urlparse(str(api_url).strip())
    base = f"{p.scheme or 'https'}://{p.netloc or 'api.sonic8store.com'}"
    full_url = base.rstrip("/") + endpoint_path
    req = urllib.request.Request(
        full_url,
        headers={
            "api-token": str(api_key).strip(),
            "Accept": "application/json",
            "User-Agent": "BoostixStore/2.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="ignore")
    return json.loads(raw)

def _supplier_http_post(url, form_data, timeout=15):
    data = urlencode(form_data).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", "BoostixStore/2.0")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        txt = resp.read().decode("utf-8", errors="ignore").strip()
        return json.loads(txt) if txt else {}

def _find_supplier_by_id(settings, sid):
    for s in (settings.get("suppliers") or []):
        if str(s.get("id")) == str(sid) and s.get("active", True):
            return s
    return None

def verify_totp(secret_b32, code_str, window=1):
    code_str = str(code_str or "").strip().replace(" ", "")
    if not code_str.isdigit() or len(code_str) != 6:
        return False
    try:
        s = secret_b32.strip().replace(" ", "").upper()
        pad = (-len(s)) % 8
        key = base64.b32decode(s + ("=" * pad), casefold=True)
    except Exception:
        return False
    step = int(time.time() // 30)
    for delta in range(-window, window + 1):
        msg = struct.pack(">Q", step + delta)
        digest = hmac.new(key, msg, hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        truncated = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
        expected = f"{truncated % 1000000:06d}"
        if hmac.compare_digest(expected, code_str):
            return True
    return False

def create_admin_token():
    exp = int(time.time()) + SESSION_TTL_SEC
    jti = secrets.token_hex(16)
    payload = f"adm.{exp}.{jti}".encode("utf-8")
    sig = hmac.new(SESSION_SIGN_KEY, payload, hashlib.sha256).hexdigest()
    return f"adm.{exp}.{jti}.{sig}"

def verify_admin_token(token):
    if not token or not ADMIN_SECRET:
        return None
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != "adm":
        return None
    _, exp_s, jti, sig = parts
    if not exp_s.isdigit():
        return None
    exp = int(exp_s)
    if time.time() > exp:
        return None
    payload = f"adm.{exp_s}.{jti}".encode("utf-8")
    expected_sig = hmac.new(SESSION_SIGN_KEY, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    with _SEC_LOCK:
        # تنظيف الجلسات الملغاة المنتهية
        now = time.time()
        for k in [k for k, v in _REVOKED_TOKENS.items() if v < now]:
            _REVOKED_TOKENS.pop(k, None)
        if jti in _REVOKED_TOKENS:
            return None
    return {"jti": jti, "exp": exp}

def revoke_admin_token(token):
    info = verify_admin_token(token)
    if info:
        with _SEC_LOCK:
            _REVOKED_TOKENS[info["jti"]] = info["exp"]

def check_rate_limit(ip, bucket, limit, window_sec):
    now = time.time()
    key = (ip, bucket)
    with _SEC_LOCK:
        arr = [t for t in _RATE_BUCKETS.get(key, []) if now - t < window_sec]
        if len(arr) >= limit:
            _RATE_BUCKETS[key] = arr
            return False
        arr.append(now)
        _RATE_BUCKETS[key] = arr
        return True

def check_login_lockout(ip):
    now = time.time()
    with _SEC_LOCK:
        rec = _FAILED_LOGINS.get(ip)
        if rec and rec.get("locked_until", 0) > now:
            return int(rec["locked_until"] - now)
    return 0

def record_login_fail(ip):
    now = time.time()
    with _SEC_LOCK:
        rec = _FAILED_LOGINS.get(ip, {"fails": [], "locked_until": 0})
        fails = [t for t in rec["fails"] if now - t < FAIL_WINDOW_SEC]
        fails.append(now)
        rec["fails"] = fails
        if len(fails) >= MAX_LOGIN_FAILS:
            rec["locked_until"] = now + LOCKOUT_SEC
            rec["fails"] = []
            print(f"[SECURITY] IP {ip} LOCKED for {LOCKOUT_SEC}s after {MAX_LOGIN_FAILS} failed admin logins", flush=True)
        else:
            print(f"[SECURITY] Failed admin login from {ip} ({len(fails)}/{MAX_LOGIN_FAILS})", flush=True)
        _FAILED_LOGINS[ip] = rec

def clear_login_fails(ip):
    with _SEC_LOCK:
        _FAILED_LOGINS.pop(ip, None)

def sb_request(method, path, body=None, prefer_rep=False, custom_token=None, extra_headers=None, timeout=15):
    url = f"{SUPABASE_URL}{path}"
    auth_token = custom_token if custom_token else SUPABASE_SECRET
    headers = {
        "apikey": SUPABASE_SECRET,
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json"
    }
    if prefer_rep:
        headers["Prefer"] = "return=representation"
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            txt = resp.read().decode("utf-8").strip()
            return json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        err_txt = e.read().decode("utf-8", errors="ignore").strip()
        try:
            return json.loads(err_txt)
        except Exception:
            return {"error": err_txt, "status": e.code}
    except Exception as e:
        return {"error": str(e)}

def get_store_settings(use_cache=True):
    now = time.time()
    if use_cache:
        with _CACHE_LOCK:
            c = _CACHE["settings"]
            if c["data"] is not None and (now - c["ts"]) < CACHE_TTL_SEC:
                return dict(c["data"])
    res = sb_request("GET", f"/rest/v1/products?sku=eq.{SETTINGS_SKU}&select=id,pack&limit=1")
    settings = dict(DEFAULT_SETTINGS)
    if isinstance(res, list) and res:
        try:
            saved = json.loads(res[0].get("pack", "{}"))
            if isinstance(saved, dict):
                settings.update(saved)
        except Exception:
            pass
    with _CACHE_LOCK:
        _CACHE["settings"] = {"ts": now, "data": dict(settings)}
    return settings

def save_store_settings(new_data):
    cur = get_store_settings(use_cache=False)
    if "rate_syp" in new_data:
        cur["rate_syp"] = max(1, int(float(new_data["rate_syp"])))
    if "sham_account" in new_data:
        cur["sham_account"] = str(new_data["sham_account"]).strip()[:200]
    if "syriatel_account" in new_data:
        cur["syriatel_account"] = str(new_data["syriatel_account"]).strip()[:200]
    if "usdt_account" in new_data:
        cur["usdt_account"] = str(new_data["usdt_account"]).strip()[:200]
    if "default_margin_pct" in new_data:
        cur["default_margin_pct"] = round(min(500.0, max(0.0, float(new_data["default_margin_pct"]))), 2)
    if "hero_banners" in new_data and isinstance(new_data["hero_banners"], list):
        clean_b = []
        for b in new_data["hero_banners"][:20]:
            if isinstance(b, dict):
                clean_b.append({
                    "id": str(b.get("id") or secrets.token_hex(4))[:40],
                    "title": str(b.get("title") or "")[:120],
                    "img_dark": str(b.get("img_dark") or "").strip()[:500],
                    "img_light": str(b.get("img_light") or b.get("img_dark") or "").strip()[:500],
                    "target_cat": str(b.get("target_cat") or "").strip()[:120],
                    "link": str(b.get("link") or "").strip()[:300],
                    "active": bool(b.get("active", True))
                })
        cur["hero_banners"] = clean_b
    if "promo_banner" in new_data and isinstance(new_data["promo_banner"], dict):
        pb = new_data["promo_banner"]
        cur["promo_banner"] = {
            "img_dark": str(pb.get("img_dark") or "/assets/promo_dark.webp").strip()[:500],
            "img_light": str(pb.get("img_light") or pb.get("img_dark") or "/assets/promo_light.webp").strip()[:500],
            "target_cat": str(pb.get("target_cat") or "شاشة VIP").strip()[:120],
            "active": bool(pb.get("active", True))
        }
    if "category_images" in new_data and isinstance(new_data["category_images"], dict):
        cur["category_images"] = {str(k)[:100]: str(v).strip()[:500] for k, v in new_data["category_images"].items() if str(v).strip()}
    if "suppliers" in new_data and isinstance(new_data["suppliers"], list):
        clean_s = []
        for s in new_data["suppliers"][:20]:
            if isinstance(s, dict):
                clean_s.append({
                    "id": str(s.get("id") or secrets.token_hex(4))[:40],
                    "name": str(s.get("name") or "مزود جديد").strip()[:80],
                    "type": str(s.get("type") or "smm").strip()[:20],
                    "api_url": str(s.get("api_url") or "").strip()[:300],
                    "api_key": str(s.get("api_key") or "").strip()[:300],
                    "margin_pct": round(min(500.0, max(0.0, float(s.get("margin_pct", 15)))), 2),
                    "active": bool(s.get("active", True))
                })
        cur["suppliers"] = clean_s

    payload_str = json.dumps(cur, ensure_ascii=False)
    res = sb_request("GET", f"/rest/v1/products?sku=eq.{SETTINGS_SKU}&select=id&limit=1")
    if isinstance(res, list) and res:
        sb_request("PATCH", f"/rest/v1/products?id=eq.{int(res[0]['id'])}", {"pack": payload_str, "active": False})
    else:
        sb_request("POST", "/rest/v1/products", {
            "game": "__SYSTEM__",
            "pack": payload_str,
            "price_usd": 1.00,
            "supplier_cost_usd": 0.00,
            "sku": SETTINGS_SKU,
            "icon": "⚙️",
            "active": False
        })
    invalidate_cache("settings")
    return cur

def fetch_all_products(active_only=True, full_fields=False):
    now = time.time()
    if active_only and not full_fields:
        with _CACHE_LOCK:
            c = _CACHE["products_active"]
            if c["data"] is not None and (now - c["ts"]) < CACHE_TTL_SEC:
                return c["data"]
    all_p = []
    select = "id,game,pack,price_usd,supplier_cost_usd,sku,icon,active" if (not active_only or full_fields) else "id,game,pack,price_usd,sku,icon"
    filt = "active=eq.true&" if active_only else ""
    offset = 0
    while True:
        chunk = None
        for _ in range(3):
            res = sb_request("GET", f"/rest/v1/products?{filt}select={select}&order=id.asc&limit=1000&offset={offset}", timeout=20)
            if isinstance(res, list):
                chunk = res
                break
        if isinstance(chunk, list):
            for item in chunk:
                if item.get("sku") != SETTINGS_SKU and item.get("game") not in ("__SYSTEM__", "__MEDIA__"):
                    all_p.append(item)
            if len(chunk) < 1000:
                break
            offset += 1000
        else:
            break
    if active_only and not full_fields:
        with _CACHE_LOCK:
            _CACHE["products_active"] = {"ts": now, "data": all_p}
    return all_p

def load_catalog_tree():
    with _CACHE_LOCK:
        if _CACHE["catalog_tree"] is not None:
            return _CACHE["catalog_tree"]
    tree = {"main_categories": [], "sub_categories": [], "level3_categories": []}
    if os.path.exists(CATALOG_FILE):
        try:
            with open(CATALOG_FILE, "r", encoding="utf-8") as f:
                c = json.load(f)
            tree = {
                "main_categories": c.get("main_categories", []),
                "sub_categories": c.get("sub_categories", []),
                "level3_categories": c.get("level3_categories", [])
            }
        except Exception:
            pass
    with _CACHE_LOCK:
        _CACHE["catalog_tree"] = tree
    return tree

def verify_user_session(token):
    if not token or len(token) > 4096:
        return None
    auth_user = sb_request("GET", "/auth/v1/user", custom_token=token)
    if isinstance(auth_user, dict) and "id" in auth_user:
        uid = auth_user["id"]
        email = auth_user.get("email", "")
        meta = auth_user.get("user_metadata") or {}
        name = meta.get("full_name") or meta.get("name") or (email.split("@")[0] if email else "زبون")
        rows = sb_request("GET", f"/rest/v1/users?id=eq.{quote(str(uid))}&select=*")
        if isinstance(rows, list) and len(rows) > 0:
            return rows[0]
        created = sb_request("POST", "/rest/v1/users", {
            "id": uid,
            "email": email,
            "name": str(name)[:100],
            "balance": 0.00,
            "role": "customer"
        }, prefer_rep=True)
        if isinstance(created, list) and len(created) > 0:
            return created[0]
    return None

def fmt_date(iso_str):
    if not iso_str:
        return ""
    return str(iso_str)[:16].replace("T", " ")


# === محرك الدفع الأوتوماتيكي (شام كاش + سيريتل كاش) ===
LOCAL_PAY_SECRET = os.environ.get("LOCAL_PAY_SECRET", "boostix_auto_pay_2026")
INCOMING_TRANSFERS = {}  # {tx_id: {"amount": float, "currency": "USD"|"SYP", "method": str, "ts": float}}

def _norm_digits(s):
    return str(s or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٬٫", "0123456789,."))

def _parse_local_pay_text(text, source=""):
    t = _norm_digits(text)
    low = (t + " " + str(source)).lower()
    method = "سيريتل كاش" if any(k in low for k in ["syriatel", "سيريتل", "سيرياتيل", "cash"]) and "sham" not in low and "شام" not in low else "شام كاش"

    # استخراج رقم العملية (يدعم صيغ سيريتل كاش وشام كاش)
    tx = ""
    tx_patterns = [
        r"(?:رقم العملية|عملية رقم|معرف العملية|المرجع|رقم الحوالة|رقم التحويل|tx|ref|id|transaction)\s*[:#\-]?\s*([A-Za-z0-9\-]{5,40})",
        r"\b(\d{7,16})\b",
    ]
    for pat in tx_patterns:
        m = re.search(pat, t, flags=re.I)
        if m:
            tx = m.group(1).strip()
            break

    # استخراج المبلغ والعملة
    amt = 0.0
    currency = "SYP" if ("ل.س" in t or "ليرة" in t or "syp" in low or method == "سيريتل كاش") else "USD"
    amt_patterns = [
        r"(?:مبلغ|بمبلغ|تم تحويل|استلمت|تم استلام|amount|received)\s*[:\-]?\s*([0-9,]+(?:\.[0-9]+)?)",
        r"([0-9,]+(?:\.[0-9]+)?)\s*(?:ل\.س|ليرة|syp|\$|دولار|usd)",
    ]
    for pat in amt_patterns:
        m = re.search(pat, t, flags=re.I)
        if m:
            try:
                amt = float(m.group(1).replace(",", ""))
                break
            except Exception:
                pass
    if "$" in t or "دولار" in t or "usd" in low:
        currency = "USD"
    return method, tx, amt, currency

def _auto_approve_deposit_by_tx(tx_id, paid_amt, currency):
    """مطابقة الحوالة الواردة مع إيداع معلق في Supabase واعتماده فوراً"""
    tx = str(tx_id).strip()
    if not tx:
        return False, "empty_tx"
    rows = sb_request("GET", f"/rest/v1/deposits?tx_id=eq.{quote(tx)}&status=eq.{quote('معلق')}&select=*")
    if not isinstance(rows, list) or not rows:
        return False, "no_pending_deposit"
    d = rows[0]
    expected_usd = float(d.get("amount_usd", 0))
    # التحقق من المبلغ (بهامش تسامح 3% لفروقات التقريب)
    syp_rate = float(os.environ.get("SYP_RATE", "15000"))
    paid_usd = paid_amt if currency == "USD" else (paid_amt / syp_rate)
    if paid_amt > 0 and expected_usd > 0 and paid_usd < expected_usd * 0.97:
        return False, f"amount_mismatch_paid_{paid_usd:.2f}_expected_{expected_usd:.2f}"

    updated = sb_request(
        "PATCH",
        f"/rest/v1/deposits?id=eq.{d['id']}&status=eq.{quote('معلق')}",
        {"status": "مقبول"},
        prefer_rep=True
    )
    if isinstance(updated, list) and len(updated) == 1:
        uid_q = quote(str(d["user_id"]))
        usrs = sb_request("GET", f"/rest/v1/users?id=eq.{uid_q}&select=balance")
        if isinstance(usrs, list) and usrs:
            nb = round(float(usrs[0]["balance"]) + expected_usd, 2)
            sb_request("PATCH", f"/rest/v1/users?id=eq.{uid_q}", {"balance": nb})
            return True, f"approved_deposit_{d['id']}_new_balance_{nb}"
    return False, "race_or_update_failed"


# ==================== Telegram Bot Notifications & Webhook ====================
def _tg_cfg():
    token = str(os.environ.get("TG_BOT_TOKEN", "")).strip()
    chat_id = str(os.environ.get("TG_CHAT_ID", "")).strip()
    return token, chat_id

def _tg_api(method, payload):
    token, _ = _tg_cfg()
    if not token:
        return None
    try:
        import urllib.request
        url = f"https://api.telegram.org/bot{token}/{method}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:
        return None

TG_AUDIT_LOG = []

def _short_uid(uid):
    import hashlib
    s = str(uid or "").strip()
    if not s:
        return "#00000"
    h = int(hashlib.sha256(s.encode("utf-8")).hexdigest()[:8], 16)
    return f"#{10000 + (h % 89999)}"

def _tg_staff_ids():
    raw = str(os.environ.get("TG_STAFF_CHAT_ID") or "").strip()
    return [x.strip() for x in raw.split(",") if x.strip()]

def _tg_allowed_ids():
    _, admin_chat = _tg_cfg()
    ids = []
    if admin_chat:
        ids.append(str(admin_chat).strip())
    for s in _tg_staff_ids():
        if s not in ids:
            ids.append(s)
    return ids

def _tg_menu_markup(is_owner=True):
    rows = [
        [
            {"text": "✅ الإيداعات المقبولة", "callback_data": "rep:deps_ok"},
            {"text": "❌ الإيداعات المرفوضة", "callback_data": "rep:deps_no"}
        ],
        [
            {"text": "⏳ الإيداعات المعلقة", "callback_data": "rep:deps_wait"},
            {"text": "🛒 آخر المشتريات", "callback_data": "rep:orders"}
        ]
    ]
    if is_owner:
        rows.extend([
            [
                {"text": "📊 الملخص المالي", "callback_data": "rep:summary"},
                {"text": "🛡️ سجل الرقابة", "callback_data": "rep:audit"}
            ],
            [
                {"text": "🏦 أرصدة المزودين", "callback_data": "rep:suppliers"},
                {"text": "📈 الأرباح والمزودين", "callback_data": "rep:pnl_menu"}
            ]
        ])
    return {"inline_keyboard": rows}

def _tg_notify(text, reply_markup=None, owner_only=False):
    token, admin_chat = _tg_cfg()
    if not token or not admin_chat:
        return
    targets = [admin_chat] if owner_only else _tg_allowed_ids()
    import threading
    for cid in targets:
        payload = {"chat_id": cid, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        threading.Thread(target=_tg_api, args=("sendMessage", payload), daemon=True).start()

def _tg_register_webhook():
    token, _ = _tg_cfg()
    if not token:
        return
    base = str(os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL") or "https://boostix-store.onrender.com").rstrip("/")
    _tg_api("setWebhook", {"url": f"{base}/api/telegram/webhook", "allowed_updates": ["callback_query", "message"]})

def _tg_pnl_markup():
    return {
        "inline_keyboard": [
            [
                {"text": "📅 اليوم", "callback_data": "rep:pnl_day"},
                {"text": "🗓️ آخر 7 أيام", "callback_data": "rep:pnl_week"}
            ],
            [
                {"text": "📆 هذا الشهر", "callback_data": "rep:pnl_month"},
                {"text": "📊 هذه السنة", "callback_data": "rep:pnl_year"}
            ],
            [
                {"text": "🔙 القائمة الرئيسية", "callback_data": "rep:main_menu"}
            ]
        ]
    }

def _tg_build_pnl(start_dt, end_dt, title):
    from urllib.parse import quote as _q
    s_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    e_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    ords = sb_request("GET", f"/rest/v1/orders?created_at=gte.{_q(s_iso)}&created_at=lt.{_q(e_iso)}&select=price_usd,supplier_cost_usd,status") or []
    deps = sb_request("GET", f"/rest/v1/deposits?created_at=gte.{_q(s_iso)}&created_at=lt.{_q(e_iso)}&select=amount_usd,status") or []
    valid_ords = [o for o in ords if o.get("status") != "مرفوض من المورد"]
    sales = sum(float(o.get("price_usd") or 0) for o in valid_ords)
    sup_paid = sum(float(o.get("supplier_cost_usd") or 0) for o in valid_ords)
    profit = sales - sup_paid
    margin = (profit / sales * 100.0) if sales > 0 else 0.0
    ok_deps = [float(d.get("amount_usd") or 0) for d in deps if d.get("status") == "مقبول"]
    return (
        f"📈 <b>{title}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🛒 الطلبات المنفّذة: <b>{len(valid_ords)}</b>\n"
        f"💵 إجمالي المبيعات: <b>${sales:.2f}</b>\n"
        f"🏦 المدفوع للمزودين: <b>${sup_paid:.2f}</b>\n"
        f"💰 صافي الربح: <b>${profit:.2f}</b> ({margin:.1f}%)\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ الإيداعات المقبولة: <b>{len(ok_deps)}</b> (${sum(ok_deps):.2f})\n"
        "💡 لتاريخ محدد أرسل:\n"
        "<code>/pnl 2026-11-26</code> أو <code>/pnl 2026-11-01 2026-11-26</code>"
    )

def _tg_build_report(rep_type):
    if rep_type in ("pnl_day", "pnl_week", "pnl_month", "pnl_year"):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        if rep_type == "pnl_day":
            return _tg_build_pnl(today_start, tomorrow_start, f"جرد اليوم ({today_start.strftime('%Y-%m-%d')})")
        if rep_type == "pnl_week":
            return _tg_build_pnl(today_start - timedelta(days=6), tomorrow_start, "جرد آخر 7 أيام")
        if rep_type == "pnl_month":
            m_start = today_start.replace(day=1)
            return _tg_build_pnl(m_start, tomorrow_start, f"جرد هذا الشهر ({m_start.strftime('%Y-%m')})")
        y_start = today_start.replace(month=1, day=1)
        return _tg_build_pnl(y_start, tomorrow_start, f"جرد هذه السنة ({y_start.strftime('%Y')})")

    if rep_type == "suppliers":
        st = get_store_settings(use_cache=False) or {}
        sups = st.get("suppliers") or []
        if not sups:
            return "🏦 <b>أرصدة المزودين</b>\nلا يوجد مزودون مضافون في إعدادات المتجر حالياً."
        lines = ["🏦 <b>كشف أرصدة حساباتك عند المزودين:</b>\n━━━━━━━━━━━━━━━━━━━━"]
        low_count = 0
        for s in sups:
            name = str(s.get("name") or "مزود").strip()
            if not s.get("active", True):
                lines.append(f"⏸️ <b>{name}</b>: متوقف (غير مفعّل)")
                continue
            api_url = str(s.get("api_url") or "").strip()
            api_key = str(s.get("api_key") or "").strip()
            if not api_url or not api_key:
                lines.append(f"⚙️ <b>{name}</b>: بيانات API غير مكتملة")
                continue
            try:
                res = _sonic8_get(api_url, api_key, "/client/api/profile", timeout=8) if _is_sonic8(api_url) else _supplier_http_post(api_url, {"key": api_key, "action": "balance"}, timeout=8)
                if isinstance(res, dict) and "balance" in res:
                    bal_val = float(str(res.get("balance")).replace(",", "").strip())
                    cur = str(res.get("currency") or "USD").strip()
                    if bal_val <= 0:
                        icon, note = "🔴", " (نفد الرصيد!)"
                        low_count += 1
                    elif bal_val < 10:
                        icon, note = "⚠️", " (رصيد منخفض)"
                        low_count += 1
                    else:
                        icon, note = "🟢", ""
                    lines.append(f"{icon} <b>{name}</b>: <code>{bal_val:.2f} {cur}</code>{note}")
                else:
                    err = res.get("error") if isinstance(res, dict) else str(res)[:80]
                    lines.append(f"❌ <b>{name}</b>: رد غير متوقع ({err})")
            except Exception as ex:
                lines.append(f"❌ <b>{name}</b>: تعذر الاتصال ({str(ex)[:60]})")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        if low_count > 0:
            lines.append(f"⚠️ تنبيه: يوجد <b>{low_count}</b> مزود بحاجة لتعبئة رصيد.")
        else:
            lines.append("✅ جميع المزودين المفعّلين بأرصدة جيدة.")
        return "\n".join(lines)

    if rep_type == "audit":
        if not TG_AUDIT_LOG:
            return "🛡️ <b>سجل رقابة العمليات</b>\nلا توجد عمليات مسجّلة منذ آخر تشغيل للسيرفر."
        lines = ["🛡️ <b>سجل رقابة الإيداعات (آخر 15 عملية):</b>\n"]
        for item in TG_AUDIT_LOG[:15]:
            lines.append(f"• [{item['time']}] {item['action']} #{item['dep_id']} (${item['amount']:.2f})\n   👤 العميل: {item['user_id']} | 👮 المنفّذ: {item['actor']}")
        return "\n".join(lines)

    if rep_type == "summary":
        deps = sb_request("GET", "/rest/v1/deposits?select=amount_usd,status") or []
        ords = sb_request("GET", "/rest/v1/orders?select=price_usd,status") or []
        ok_deps = [float(d.get("amount_usd") or 0) for d in deps if d.get("status") == "مقبول"]
        no_deps = [float(d.get("amount_usd") or 0) for d in deps if d.get("status") == "مرفوض"]
        wt_deps = [float(d.get("amount_usd") or 0) for d in deps if d.get("status") == "معلق"]
        tot_ords = [float(o.get("price_usd") or 0) for o in ords if o.get("status") != "مرفوض من المورد"]
        return (
            "📊 <b>الملخص المالي العام لمتجر Boostix</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ إيداعات مقبولة: <b>{len(ok_deps)}</b> (${sum(ok_deps):.2f})\n"
            f"⏳ إيداعات معلقة: <b>{len(wt_deps)}</b> (${sum(wt_deps):.2f})\n"
            f"❌ إيداعات مرفوضة: <b>{len(no_deps)}</b> (${sum(no_deps):.2f})\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🛒 إجمالي المشتريات: <b>{len(tot_ords)}</b> (${sum(tot_ords):.2f})"
        )

    if rep_type == "orders":
        ords = sb_request("GET", "/rest/v1/orders?select=id,user_id,game,pack_name,price_usd,status,created_at&order=id.desc&limit=12") or []
        if not ords:
            return "🛒 لا توجد عمليات شراء مسجلة حالياً."
        lines = ["🛒 <b>آخر 12 عملية شراء:</b>\n"]
        for o in ords:
            suid = _short_uid(o.get("user_id"))
            lines.append(f"• #{o.get('id')} | <code>{suid}</code> | <b>${float(o.get('price_usd') or 0):.2f}</b>\n   🎮 {o.get('pack_name') or o.get('game')} ({o.get('status')})")
        return "\n".join(lines)

    status_map = {"deps_ok": ("مقبول", "✅ الإيداعات المقبولة"), "deps_no": ("مرفوض", "❌ الإيداعات المرفوضة"), "deps_wait": ("معلق", "⏳ الإيداعات المعلقة")}
    if rep_type in status_map:
        st, title = status_map[rep_type]
        deps = sb_request("GET", f"/rest/v1/deposits?status=eq.{quote(st)}&select=id,user_id,method,amount_usd,tx_id,created_at&order=id.desc&limit=12") or []
        if not deps:
            return f"{title}\nلا توجد عمليات بهذه الحالة حالياً."
        lines = [f"<b>{title} (آخر 12 عملية):</b>\n"]
        for d in deps:
            suid = _short_uid(d.get("user_id"))
            lines.append(f"• #{d.get('id')} | <code>{suid}</code> | <b>${float(d.get('amount_usd') or 0):.2f}</b>\n   🏦 {d.get('method')} | TX: <code>{d.get('tx_id')}</code>")
        return "\n".join(lines)

    return "اختر تقريراً من القائمة."

def _tg_handle_webhook(body):
    token, admin_chat = _tg_cfg()
    if not token or not admin_chat:
        return
    allowed = _tg_allowed_ids()

    # 1. معالجة الأوامر النصية (/start, /menu, /report)
    msg_in = body.get("message")
    if isinstance(msg_in, dict):
        from_id = str((msg_in.get("from") or {}).get("id", ""))
        chat_id = (msg_in.get("chat") or {}).get("id")
        raw_txt = str(msg_in.get("text") or "").strip()
        parts = raw_txt.split()
        txt = (parts[0].lower().split("@")[0]) if parts else ""
        if from_id not in allowed and str(chat_id) not in allowed:
            return
        is_owner = (from_id == str(admin_chat) or str(chat_id) == str(admin_chat))
        if txt in ("/pnl", "جرد", "ارباح", "أرباح"):
            if not is_owner:
                _tg_api("sendMessage", {"chat_id": chat_id, "text": "⛔ هذا التقرير مخصص للمالك فقط."})
                return
            from datetime import datetime, timedelta, timezone
            try:
                if len(parts) == 2:
                    d1 = datetime.strptime(parts[1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    rep_txt = _tg_build_pnl(d1, d1 + timedelta(days=1), f"جرد يوم {parts[1]}")
                elif len(parts) >= 3:
                    d1 = datetime.strptime(parts[1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    d2 = datetime.strptime(parts[2], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    rep_txt = _tg_build_pnl(d1, d2 + timedelta(days=1), f"جرد من {parts[1]} إلى {parts[2]}")
                else:
                    rep_txt = "📅 <b>كشف الأرباح والمزودين</b>\nاختر الفترة من الأزرار أو أرسل:\n<code>/pnl 2026-11-26</code>\n<code>/pnl 2026-11-01 2026-11-26</code>"
            except Exception:
                rep_txt = "⚠️ صيغة التاريخ غير صحيحة. استخدم:\n<code>/pnl 2026-11-26</code> أو <code>/pnl 2026-11-01 2026-11-26</code>"
            _tg_api("sendMessage", {"chat_id": chat_id, "text": rep_txt, "parse_mode": "HTML", "reply_markup": _tg_pnl_markup()})
            return
        if txt in ("/start", "/menu", "/report", "تقرير", "القائمة", "كشف"):
            _tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": "🎛️ <b>لوحة إدارة وتقارير Boostix Store</b>\nاختر الكشف المطلوب من الأزرار أدناه:",
                "parse_mode": "HTML",
                "reply_markup": _tg_menu_markup(is_owner=is_owner)
            })
        return

    # 2. معالجة أزرار الإنلاين (القبول/الرفض + التقارير)
    cq = body.get("callback_query")
    if not isinstance(cq, dict):
        return
    from_user = cq.get("from") or {}
    from_id = str(from_user.get("id", ""))
    actor_name = (from_user.get("first_name") or "") + (" @" + from_user["username"] if from_user.get("username") else "")
    actor_name = actor_name.strip() or from_id

    if from_id not in allowed:
        _tg_api("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": "غير مصرح لك", "show_alert": True})
        return

    data = str(cq.get("data") or "")
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    msg_id = msg.get("message_id")
    orig_text = msg.get("text") or ""

    if data.startswith("rep:"):
        rep_type = data.split(":", 1)[1]
        is_owner = (from_id == str(admin_chat) or str(chat_id) == str(admin_chat))
        owner_only_reps = {"summary", "audit", "suppliers", "pnl_menu", "pnl_day", "pnl_week", "pnl_month", "pnl_year"}
        if rep_type in owner_only_reps and not is_owner:
            _tg_api("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": "⛔ هذا التقرير مخصص للمالك فقط", "show_alert": True})
            return
        if rep_type == "pnl_menu":
            _tg_api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
            _tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": "📈 <b>كشف الأرباح والمدفوع للمزودين</b>\nاختر الفترة المطلوبة، أو أرسل تاريخاً محدداً مثل:\n<code>/pnl 2026-11-26</code> أو <code>/pnl 2026-11-01 2026-11-26</code>",
                "parse_mode": "HTML",
                "reply_markup": _tg_pnl_markup()
            })
            return
        if rep_type == "main_menu":
            _tg_api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
            _tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": "🎛️ <b>لوحة إدارة وتقارير Boostix Store</b>\nاختر الكشف المطلوب من الأزرار أدناه:",
                "parse_mode": "HTML",
                "reply_markup": _tg_menu_markup(is_owner=is_owner)
            })
            return
        _tg_api("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": "جاري جلب الكشف..."})
        rep_text = _tg_build_report(rep_type)
        _tg_api("sendMessage", {
            "chat_id": chat_id,
            "text": rep_text,
            "parse_mode": "HTML",
            "reply_markup": _tg_menu_markup(is_owner=is_owner)
        })
        return

    if data.startswith("dep_ok:") or data.startswith("dep_no:"):
        dec = "مقبول" if data.startswith("dep_ok:") else "مرفوض"
        try:
            did = int(data.split(":", 1)[1])
        except Exception:
            return
        updated = sb_request(
            "PATCH",
            f"/rest/v1/deposits?id=eq.{did}&status=eq.{quote('معلق')}",
            {"status": dec},
            prefer_rep=True
        )
        if isinstance(updated, list) and len(updated) == 1:
            d = updated[0]
            amt = float(d["amount_usd"])
            suid = _short_uid(d.get("user_id"))
            now_str = time.strftime("%H:%M:%S")
            TG_AUDIT_LOG.insert(0, {
                "time": now_str,
                "action": "✅ قبول" if dec == "مقبول" else "❌ رفض",
                "dep_id": did,
                "amount": amt,
                "user_id": suid,
                "actor": f"{actor_name} ({from_id})"
            })
            del TG_AUDIT_LOG[50:]

            if dec == "مقبول":
                uid_q = quote(str(d["user_id"]))
                usrs = sb_request("GET", f"/rest/v1/users?id=eq.{uid_q}&select=balance")
                if isinstance(usrs, list) and usrs:
                    nb = round(float(usrs[0]["balance"]) + amt, 2)
                    sb_request("PATCH", f"/rest/v1/users?id=eq.{uid_q}", {"balance": nb})
                status_line = f"\n\n✅ <b>تم القبول وشحن الرصيد (+${amt:.2f})</b>\n👮 بواسطة: {actor_name} | 🕒 {now_str}"
                alert = "تم قبول الإيداع وشحن الرصيد بنجاح"
            else:
                status_line = f"\n\n❌ <b>تم رفض طلب الإيداع</b>\n👮 بواسطة: {actor_name} | 🕒 {now_str}"
                alert = "تم رفض الإيداع"

            _tg_api("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": alert})
            if chat_id and msg_id:
                _tg_api("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": orig_text + status_line, "parse_mode": "HTML"})

            # إشعار رقابة فوري للمالك إذا كان المنفّذ موظفاً
            if from_id != str(admin_chat).strip():
                _tg_notify(
                    f"🛡️ <b>تنبيه رقابة موظف</b>\n"
                    f"الإجراء: {'✅ قبول إيداع' if dec == 'مقبول' else '❌ رفض إيداع'} #{did}\n"
                    f"👤 العميل: <code>{suid}</code>\n"
                    f"💵 المبلغ: <b>${amt:.2f}</b>\n"
                    f"👮 المنفّذ: {actor_name} (<code>{from_id}</code>)",
                    owner_only=True
                )
        else:
            _tg_api("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": "تمت معالجة هذا الطلب مسبقاً", "show_alert": True})
            if chat_id and msg_id:
                _tg_api("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": msg_id, "reply_markup": {"inline_keyboard": []}})
# ==============================================================================

class CloudHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _client_ip(self):
        cf_ip = self.headers.get("CF-Connecting-IP", "").strip()
        if cf_ip:
            return cf_ip
        xff = self.headers.get("X-Forwarded-For", "").strip()
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "unknown"

    def _is_https(self):
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _sec_headers(self, no_store=False):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if no_store:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")

    def _accepts_gzip(self):
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    def _json(self, data, code=200, extra_headers=None):
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        use_gz = self._accepts_gzip() and len(raw) > 512
        payload = gzip.compress(raw, compresslevel=5) if use_gz else raw
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(payload)))
        self._sec_headers(no_store=True)
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, path, no_store=False):
        mtime = os.path.getmtime(path)
        with _CACHE_LOCK:
            cached = _CACHE["html_files"].get(path)
        if cached and cached["mtime"] == mtime:
            raw, gz = cached["raw"], cached["gz"]
        else:
            with open(path, "rb") as f:
                raw = f.read()
            gz = gzip.compress(raw, compresslevel=5)
            with _CACHE_LOCK:
                _CACHE["html_files"][path] = {"mtime": mtime, "raw": raw, "gz": gz}
        use_gz = self._accepts_gzip()
        payload = gz if use_gz else raw
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(payload)))
        self._sec_headers(no_store=no_store)
        self.end_headers()
        self.wfile.write(payload)

    def _body(self):
        try:
            l = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if l < 0 or l > MAX_BODY_BYTES:
            return None
        if l == 0:
            return {}
        raw = self.rfile.read(l).decode("utf-8", errors="ignore")
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/x-www-form-urlencoded" in ctype:
            qs = parse_qs(raw, keep_blank_values=True)
            return {k: v[0] if v else "" for k, v in qs.items()}
        try:
            return json.loads(raw)
        except Exception:
            return None

    def _check_origin(self):
        origin = (self.headers.get("Origin") or "").strip()
        if not origin or origin.lower() == "null":
            return True
        raw_host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",")[0].strip().lower()
        host_no_port = raw_host.split(":")[0]
        orig_host = (urlparse(origin).netloc or "").strip().lower().split(":")[0]
        return (orig_host == host_no_port) or (orig_host == "boostix-store.onrender.com")

    def _get_admin_cookie(self):
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return ""
        c = SimpleCookie()
        try:
            c.load(raw_cookie)
        except Exception:
            return ""
        return c["bx_adm_sess"].value if "bx_adm_sess" in c else ""

    def _is_admin(self):
        tok = self._get_admin_cookie()
        return verify_admin_token(tok) is not None

    def _auth_user(self):
        auth = self.headers.get("Authorization", "")
        tok = auth.replace("Bearer ", "").strip()
        return verify_user_session(tok)

    def do_GET(self):
        p = urlparse(self.path).path.rstrip("/") or "/"
        ip = self._client_ip()

        if p == "/api/telegram/sync":
            base = str(os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL") or "https://boostix-store.onrender.com").rstrip("/")
            r_set = _tg_api("setWebhook", {"url": f"{base}/api/telegram/webhook", "allowed_updates": ["callback_query", "message"]})
            r_cmd = _tg_api("setMyCommands", {"commands": [{"command": "menu", "description": "لوحة الكشوفات والتقارير"}, {"command": "report", "description": "تقرير سريع"}]})
            r_info = _tg_api("getWebhookInfo", {})
            self._json({"ok": True, "base": base, "setWebhook": r_set, "setMyCommands": r_cmd, "webhookInfo": r_info})
            return

        if p == "/ping" or p == "/health":
            self._json({"ok": True, "status": "awake", "ts": int(time.time())})
            return

        if p.startswith("/api/media/"):
            fname = p[len("/api/media/"):].strip("/")
            sku_key = fname.rsplit(".", 1)[0]
            if not re.match(r"^MEDIA_[a-zA-Z0-9_-]{4,40}$", sku_key):
                self._json({"ok": False, "msg": "Not Found"}, 404); return
            with _CACHE_LOCK:
                m_cached = _CACHE["media"].get(sku_key)
            if not m_cached:
                rows = sb_request("GET", f"/rest/v1/products?sku=eq.{quote(sku_key)}&game=eq.__MEDIA__&select=pack&limit=1")
                if not isinstance(rows, list) or not rows:
                    self._json({"ok": False, "msg": "Media not found"}, 404); return
                data_url = rows[0].get("pack") or ""
                m = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", data_url, re.S)
                if not m:
                    self._json({"ok": False, "msg": "Invalid media"}, 404); return
                try:
                    m_cached = (m.group(1), base64.b64decode(m.group(2)))
                except Exception:
                    self._json({"ok": False, "msg": "Decode error"}, 500); return
                with _CACHE_LOCK:
                    _CACHE["media"][sku_key] = m_cached
            mime, raw_img = m_cached
            self.send_response(200)
            self._sec_headers(no_store=False)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "public, max-age=2592000, immutable")
            self.send_header("Content-Length", str(len(raw_img)))
            self.end_headers()
            self.wfile.write(raw_img)
            return

        if p == "/" or p == "/index.html":
            self._html(STORE_HTML)
            return

        if p.startswith("/assets/"):
            rel = p[len("/assets/"):].lstrip("/")
            assets_root = os.path.realpath("public/assets") if os.path.isdir("public/assets") else os.path.realpath(os.path.join(os.path.dirname(STORE_HTML), "assets"))
            target = os.path.realpath(os.path.join(assets_root, rel))
            if not target.startswith(assets_root + os.sep) or not os.path.isfile(target):
                self._json({"ok": False, "msg": "Not Found"}, 404)
                return
            ext = os.path.splitext(target)[1].lower()
            mime = {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".json": "application/json; charset=utf-8"}.get(ext, "application/octet-stream")
            with open(target, "rb") as f:
                raw = f.read()
            self.send_response(200)
            self._sec_headers(no_store=False)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "public, max-age=604800")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if p.startswith("/proxy-img/"):
            import re as _re, urllib.request as _ureq
            rel = p[len("/proxy-img/"):].lstrip("/")
            if not _re.match(r"^images/(categories|products)/[A-Za-z0-9_.-]+\.(webp|png|jpg|jpeg)$", rel):
                self._json({"ok": False, "msg": "Invalid image path"}, 400)
                return
            cache_dir = "/tmp/boostix_img_cache"
            os.makedirs(cache_dir, exist_ok=True)
            cache_file = os.path.join(cache_dir, rel.replace("/", "_"))
            raw = None
            if os.path.isfile(cache_file):
                try:
                    with open(cache_file, "rb") as f:
                        raw = f.read()
                except Exception:
                    raw = None
            if not raw:
                try:
                    req = _ureq.Request(
                        "https://sonic8store.com/" + rel,
                        headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/120.0.0.0 Mobile"}
                    )
                    with _ureq.urlopen(req, timeout=12) as resp:
                        raw = resp.read()
                    if raw:
                        with open(cache_file, "wb") as f:
                            f.write(raw)
                except Exception:
                    self._json({"ok": False, "msg": "Image fetch failed"}, 502)
                    return
            ext = os.path.splitext(rel)[1].lower()
            mime = {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(ext, "image/webp")
            self.send_response(200)
            self._sec_headers(no_store=False)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "public, max-age=2592000, immutable")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        # بوابة الإدارة لا تفتح إلا على المسار السري ADMIN_PATH فقط
        if ADMIN_PATH and p == ADMIN_PATH:
            if not ADMIN_SECRET:
                self._json({"ok": False, "msg": "Admin disabled"}, 503)
                return
            self._html(ADMIN_HTML, no_store=True)
            return

        if p == "/api/customer/bootstrap":
            if not check_rate_limit(ip, "bootstrap", 60, 60):
                self._json({"ok": False, "msg": "طلبات كثيرة جداً، انتظر لحظة"}, 429); return
            u = self._auth_user()
            prods = fetch_all_products(active_only=True)
            cat_tree = load_catalog_tree()
            pay_info = dict(get_store_settings())
            pay_info.pop("suppliers", None)
            my_ords, my_deps, u_dict = [], [], None
            if u:
                uid_q = quote(str(u["id"]))
                u_dict = {
                    "id": u["id"],
                    "name": u["name"],
                    "email": u["email"],
                    "phone": u.get("phone") or "",
                    "avatar_url": u.get("avatar_url") or "",
                    "balance": float(u["balance"]),
                    "role": u.get("role", "customer")
                }
                raw_ords = sb_request("GET", f"/rest/v1/orders?user_id=eq.{uid_q}&select=id,created_at,game,pack_name,player_id,price_usd,status,supplier_note&order=id.desc")
                if isinstance(raw_ords, list):
                    for o in raw_ords:
                        o["date"] = fmt_date(o.get("created_at"))
                        my_ords.append(o)
                raw_deps = sb_request("GET", f"/rest/v1/deposits?user_id=eq.{uid_q}&select=id,created_at,method,amount_usd,tx_id,status&order=id.desc")
                if isinstance(raw_deps, list):
                    for d in raw_deps:
                        d["date"] = fmt_date(d.get("created_at"))
                        my_deps.append(d)

            self._json({
                "ok": True,
                "supabaseUrl": SUPABASE_URL,
                "supabaseAnonKey": SUPABASE_PUB,
                "user": (dict(u_dict, short_id=_short_uid(u_dict.get("id") or u_dict.get("email"))) if isinstance(u_dict, dict) else u_dict),
                "products": prods,
                "catalogTree": cat_tree,
                "myOrders": my_ords,
                "myDeposits": my_deps,
                "payInfo": pay_info
            })
            return

        if p == "/api/admin/overview":
            if not self._is_admin():
                self._json({"ok": False, "requires_totp": bool(ADMIN_TOTP_SECRET)}, 401); return
            users = sb_request("GET", "/rest/v1/users?select=*&order=created_at.desc")
            if isinstance(users, list):
                for _u in users:
                    if isinstance(_u, dict):
                        _u["short_id"] = _short_uid(_u.get("id"))
            if not isinstance(users, list): users = []
            for u in users: u["created_at"] = fmt_date(u.get("created_at"))
            prods = fetch_all_products(active_only=False)
            cat_tree = load_catalog_tree()
            pay_info = get_store_settings()
            deps = sb_request("GET", "/rest/v1/deposits?select=*&order=id.desc")
            if not isinstance(deps, list): deps = []
            for d in deps: d["date"] = fmt_date(d.get("created_at"))
            ords = sb_request("GET", "/rest/v1/orders?select=*&order=id.desc")
            if not isinstance(ords, list): ords = []
            for o in ords: o["date"] = fmt_date(o.get("created_at"))
            self._json({
                "ok": True,
                "totp_enabled": bool(ADMIN_TOTP_SECRET),
                "users": users,
                "products": prods,
                "catalogTree": cat_tree,
                "payInfo": pay_info,
                "deposits": deps,
                "orders": ords,
                "incomingTransfers": [dict({"tx_id": k}, **v) for k, v in sorted(INCOMING_TRANSFERS.items(), key=lambda x: x[1].get("ts", 0), reverse=True)[:50]]
            })
            return

        self._json({"ok": False, "msg": "Not Found"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path.rstrip("/") or "/"
        ip = self._client_ip()
        body = self._body()
        if body is None or not isinstance(body, dict):
            self._json({"ok": False, "msg": "طلب غير صالح"}, 400); return

        # 1. تسجيل دخول المدير (فقط عبر المسار السري + حماية Brute-Force + TOTP 2FA)
        if ADMIN_PATH and p == f"{ADMIN_PATH}/login":
            if not self._check_origin():
                self._json({"ok": False, "msg": "Origin غير مصرح"}, 403); return
            if not ADMIN_SECRET:
                self._json({"ok": False, "msg": "لوحة الإدارة معطّلة حتى ضبط ADMIN_SECRET"}, 503); return
            wait_sec = check_login_lockout(ip)
            if wait_sec > 0:
                mins = max(1, (wait_sec + 59) // 60)
                self._json({"ok": False, "msg": f"⛔ تم حظر محاولات الدخول مؤقتاً بسبب تكرار الخطأ. حاول بعد {mins} دقيقة."}, 429)
                return

            pwd = str(body.get("password", ""))
            totp_code = str(body.get("totp", "")).strip()

            pwd_ok = hmac.compare_digest(pwd, ADMIN_SECRET)
            totp_ok = True if not ADMIN_TOTP_SECRET else verify_totp(ADMIN_TOTP_SECRET, totp_code)

            if not (pwd_ok and totp_ok):
                record_login_fail(ip)
                time.sleep(1.0)
                ctype = (self.headers.get("Content-Type") or "").lower()
                if "application/x-www-form-urlencoded" in ctype:
                    self.send_response(303)
                    self.send_header("Location", ADMIN_PATH + "?err=bad_creds")
                    self._sec_headers(no_store=True)
                    self.end_headers()
                    return
                self._json({"ok": False, "msg": "❌ كلمة المرور أو رمز التحقق (2FA) غير صحيح!"}, 403)
                return

            clear_login_fails(ip)
            tok = create_admin_token()
            sec_flag = "; Secure" if self._is_https() else ""
            cookie = f"bx_adm_sess={tok}; Path=/; Max-Age={SESSION_TTL_SEC}; HttpOnly; SameSite=Strict{sec_flag}"
            print(f"[SECURITY] Admin login SUCCESS from {ip}", flush=True)
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "application/x-www-form-urlencoded" in ctype:
                self.send_response(303)
                self.send_header("Set-Cookie", cookie)
                self.send_header("Location", ADMIN_PATH)
                self._sec_headers(no_store=True)
                self.end_headers()
                return
            self._json({"ok": True}, 200, extra_headers=[("Set-Cookie", cookie)])
            return

        if p == "/api/admin/logout":
            if not self._check_origin():
                self._json({"ok": False}, 403); return
            revoke_admin_token(self._get_admin_cookie())
            sec_flag = "; Secure" if self._is_https() else ""
            cookie = f"bx_adm_sess=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{sec_flag}"
            self._json({"ok": True}, 200, extra_headers=[("Set-Cookie", cookie)])
            return

        # 2. مسارات الزبائن (مع Rate Limit وقفل مالي ضد الشراء المتزامن)
        if p == "/api/customer/profile":
            if not check_rate_limit(ip, "profile", 20, 60):
                self._json({"ok": False, "msg": "طلبات كثيرة، انتظر لحظة"}, 429); return
            u = self._auth_user()
            if not u:
                self._json({"ok": False, "msg": "Unauthorized"}, 401); return
            patch = {}
            if "name" in body:
                nm = str(body.get("name") or "").strip()[:60]
                if len(nm) < 2:
                    self._json({"ok": False, "msg": "الاسم قصير جداً"}, 400); return
                patch["name"] = nm
            if "phone" in body:
                ph = str(body.get("phone") or "").strip()[:25]
                if ph and len(ph) < 7:
                    self._json({"ok": False, "msg": "يرجى إدخال رقم اتصال صحيح"}, 400); return
                patch["phone"] = ph
            if "avatar_url" in body:
                av = str(body.get("avatar_url") or "").strip()
                if av and not (av.startswith("data:image/") or av.startswith("https://")):
                    self._json({"ok": False, "msg": "صيغة الصورة غير مدعومة"}, 400); return
                if len(av) > 350000:
                    self._json({"ok": False, "msg": "حجم الصورة كبير جداً"}, 400); return
                patch["avatar_url"] = av
            if not patch:
                self._json({"ok": False, "msg": "لا توجد بيانات للتحديث"}, 400); return
            uid_q = quote(str(u["id"]))
            res = sb_request("PATCH", f"/rest/v1/users?id=eq.{uid_q}", patch)
            if isinstance(res, dict) and res.get("error"):
                self._json({"ok": False, "msg": "تعذر الحفظ — تأكد من إضافة أعمدة phone و avatar_url في Supabase"}, 500); return
            self._json({"ok": True, "updated": patch})
            return

        if p == "/api/customer/deposit":
            if not check_rate_limit(ip, "deposit", 10, 300):
                self._json({"ok": False, "msg": "تم إرسال طلبات كثيرة، انتظر قليلاً"}, 429); return
            u = self._auth_user()
            if not u: self._json({"ok": False, "msg": "يرجى تسجيل الدخول بحساب Google أولاً"}, 401); return
            try:
                amt = round(float(body.get("amount_usd", 0)), 2)
            except Exception:
                amt = 0
            tx = str(body.get("tx_id", "")).strip()[:120]
            method = str(body.get("method", "شام كاش")).strip()[:60]
            if amt <= 0 or amt > 50000 or not tx:
                self._json({"ok": False, "msg": "أدخل مبلغاً ورقم عملية صحيحين"}, 400); return

            dup = sb_request("GET", f"/rest/v1/deposits?tx_id=eq.{quote(tx)}&select=id")
            if isinstance(dup, list) and len(dup) > 0:
                self._json({"ok": False, "msg": "رقم عملية التحويل هذا مسجل مسبقاً!"}, 400); return

            is_local_auto = any(k in method for k in ["شام كاش", "سيريتل", "سيرياتيل"])
            if is_local_auto and not _tg_cfg()[0]:
                if tx not in INCOMING_TRANSFERS:
                    self._json({"ok": False, "msg": "لا يوجد رقم عملية مطابق حالياً! تأكد من الرقم أو انتظر دقيقة بعد التحويل وأعد المحاولة."}, 400)
                    return
            if is_local_auto and tx in INCOMING_TRANSFERS:
                tr = INCOMING_TRANSFERS[tx]
                syp_rate = float(os.environ.get("SYP_RATE", "15000"))
                paid_usd = tr["amount"] if tr["currency"] == "USD" else (tr["amount"] / syp_rate)
                if tr["amount"] > 0 and amt > 0 and paid_usd < amt * 0.97:
                    self._json({"ok": False, "msg": f"المبلغ المستلم (${paid_usd:.2f}) أقل من قيمة الفاتورة (${amt:.2f})!"}, 400)
                    return

            created_dep = sb_request("POST", "/rest/v1/deposits", {
                "user_id": u["id"],
                "user_name": str(u["name"])[:100],
                "amount_usd": amt,
                "method": method,
                "tx_id": tx,
                "status": "معلق"
            }, prefer_rep=True)
            dep_id = created_dep[0]["id"] if isinstance(created_dep, list) and created_dep else None
            auto_ok = False
            if tx in INCOMING_TRANSFERS:
                tr = INCOMING_TRANSFERS[tx]
                auto_ok, _ = _auto_approve_deposit_by_tx(tx, tr["amount"], tr["currency"])
                if auto_ok:
                    INCOMING_TRANSFERS.pop(tx, None)
            if auto_ok:
                _tg_notify(f"💰 <b>إيداع تلقائي مقبول</b>\n👤 العميل: {u['name']}\n💵 المبلغ: <b>${amt:.2f}</b>\n🏦 الطريقة: {method}\n🔢 رقم العملية: <code>{tx}</code>")
            elif dep_id:
                kb = {"inline_keyboard": [[{"text": "✅ قبول وشحن", "callback_data": f"dep_ok:{dep_id}"}, {"text": "❌ رفض", "callback_data": f"dep_no:{dep_id}"}]]}
                _tg_notify(f"🔔 <b>طلب إيداع جديد #{dep_id}</b>\n👤 العميل: {u['name']}\n💵 المبلغ: <b>${amt:.2f}</b>\n🏦 الطريقة: {method}\n🔢 رقم العملية: <code>{tx}</code>", reply_markup=kb)
            self._json({"ok": True, "auto_approved": auto_ok})
            return

        if p == "/api/telegram/webhook":
            _tg_handle_webhook(body)
            self._json({"ok": True})
            return

        if p == "/api/webhook/sonic8":
            try:
                d = body.get("data") if isinstance(body.get("data"), dict) else body
                oid = str(d.get("order_id") or body.get("order_id") or "").strip()
                st_raw = str(d.get("status") or body.get("status") or "").strip().lower()
                if oid and st_raw in ("accept", "reject"):
                    new_st = "مكتمل" if st_raw == "accept" else "مرفوض"
                    note = f"تحديث تلقائي من Sonic8Store: {st_raw}"
                    sb_request("PATCH", f"/rest/v1/orders?supplier_order_id=eq.{urllib.parse.quote(oid)}", {"status": new_st, "supplier_note": note})
                    _tg_notify(f"🔔 <b>تحديث طلب Sonic8Store</b>\n🆔 رقم المورد: <code>{oid}</code>\n📌 الحالة: <b>{new_st}</b>")
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)[:120]}, 500)
            return

        if p == "/api/webhook/local-pay":
            sec = str(body.get("secret") or self.headers.get("X-Webhook-Secret") or "").strip()
            if sec != LOCAL_PAY_SECRET:
                self._json({"ok": False, "msg": "Unauthorized"}, 401); return
            raw_text = str(body.get("text") or body.get("message") or "").strip()
            source = str(body.get("source") or body.get("app") or "").strip()
            method, tx, amt, currency = _parse_local_pay_text(raw_text, source)
            if body.get("tx_id"):
                tx = _norm_digits(body.get("tx_id")).strip()
            if body.get("amount"):
                try: amt = float(_norm_digits(body.get("amount")).replace(",", ""))
                except Exception: pass
            if body.get("currency"):
                currency = str(body.get("currency")).upper()
            if not tx:
                self._json({"ok": False, "msg": "لم يتم العثور على رقم العملية في النص", "raw": raw_text[:120]}, 400); return

            # تنظيف الذاكرة المؤقتة الأقدم من 24 ساعة وحفظ الحوالة
            now = time.time()
            for k in list(INCOMING_TRANSFERS.keys()):
                if now - INCOMING_TRANSFERS[k]["ts"] > 86400:
                    INCOMING_TRANSFERS.pop(k, None)
            INCOMING_TRANSFERS[tx] = {"amount": amt, "currency": currency, "method": method, "ts": now}

            try:
                ok_app, info = _auto_approve_deposit_by_tx(tx, amt, currency)
            except Exception as e:
                self._json({"ok": False, "msg": "webhook_error", "error": str(e)}, 500); return
            if ok_app:
                INCOMING_TRANSFERS.pop(tx, None)
            self._json({"ok": True, "tx_id": tx, "amount": amt, "currency": currency, "method": method, "auto_approved": ok_app, "info": info})
            return

        if p == "/api/customer/purchase":
            if not check_rate_limit(ip, "purchase", 15, 60):
                self._json({"ok": False, "msg": "طلبات شراء كثيرة، انتظر لحظة"}, 429); return
            u = self._auth_user()
            if not u: self._json({"ok": False, "msg": "يرجى تسجيل الدخول بحساب Google أولاً"}, 401); return
            try:
                pid = int(body.get("product_id", 0))
            except Exception:
                pid = 0
            player_id = str(body.get("player_id", "")).strip()[:120]
            if pid <= 0 or not player_id:
                self._json({"ok": False, "msg": "أدخل معرف اللاعب (Player ID) بشكل صحيح"}, 400); return

            prods = sb_request("GET", f"/rest/v1/products?id=eq.{pid}&active=eq.true&select=*")
            if not isinstance(prods, list) or not prods:
                self._json({"ok": False, "msg": "الباقة غير موجودة أو غير متاحة"}, 404); return
            prod = prods[0]
            try:
                req_qty = max(1, min(1000000, int(body.get("quantity", 1) or 1)))
            except Exception:
                req_qty = 1
            price = round(float(prod["price_usd"]) * req_qty, 2)
            bal = round(float(u["balance"]), 2)
            if price <= 0 or bal < price:
                self._json({"ok": False, "need_deposit": True, "msg": f"رصيد محفظتك (${bal:.2f}) غير كافٍ لشراء الباقة (${price:.2f}). اشحن محفظتك أولاً."}, 400)
                return

            cost = round(float(prod["supplier_cost_usd"]) * req_qty, 2)
            profit = round(price - cost, 2)
            new_bal = round(bal - price, 2)
            uid_q = quote(str(u["id"]))

            # قفل ذري لمنع الشراء المتزامن بنفس الرصيد (Double-Spend Prevention)
            deducted = sb_request(
                "PATCH",
                f"/rest/v1/users?id=eq.{uid_q}&balance=eq.{bal}",
                {"balance": new_bal},
                prefer_rep=True
            )
            if not isinstance(deducted, list) or len(deducted) == 0:
                self._json({"ok": False, "msg": "تم تحديث رصيدك في عملية أخرى، أعد المحاولة"}, 409)
                return

            sup_order_id = None
            sup_note = "تم استلام الطلب وإرساله للمورد للشحن الفوري"
            sku_str = str(prod.get("sku") or "")
            if sku_str.upper().startswith("SMM:"):
                parts = sku_str.split(":")
                if len(parts) >= 3:
                    st = get_store_settings()
                    sup = _find_supplier_by_id(st, parts[1])
                    if sup and sup.get("api_url") and sup.get("api_key"):
                        try:
                            if len(parts) >= 4 and parts[-1].isdigit():
                                qty = int(parts[-1])
                                svc_id = ":".join(parts[2:-1])
                            else:
                                qty = 100
                                svc_id = ":".join(parts[2:])
                            if _is_sonic8(sup["api_url"]):
                                import uuid as _uuid, urllib.parse as _up
                                s_qty = req_qty if req_qty > 1 else (int(parts[-1]) if (len(parts) >= 4 and parts[-1].isdigit()) else 1)
                                o_uuid = str(_uuid.uuid4())
                                q_str = _up.urlencode({"qty": s_qty, "playerId": str(player_id).strip(), "order_uuid": o_uuid})
                                sr = _sonic8_get(sup["api_url"], sup["api_key"], f"/client/api/newOrder/{svc_id}/params?{q_str}", timeout=20)
                                if isinstance(sr, dict) and str(sr.get("status", "")).upper() == "OK" and isinstance(sr.get("data"), dict):
                                    sup_order_id = str(sr["data"].get("order_id") or o_uuid)
                                    s_st = sr["data"].get("status", "wait")
                                    sup_note = f"تم الإرسال التلقائي إلى Sonic8Store رقم #{sup_order_id} ({s_st})"
                                else:
                                    sup_note = f"رد Sonic8Store: {str(sr)[:140]}"
                            else:
                                payload = {
                                    "key": sup["api_key"],
                                    "action": "add",
                                    "service": svc_id,
                                }
                                is_serva = "serva-s.com" in str(sup.get("api_url", "")).lower()
                                is_social = "تواصل اجتماعي" in str(prod.get("game", "")) or str(player_id).strip().lower().startswith(("http://", "https://"))
                                if is_serva and not is_social:
                                    payload["player_id"] = player_id
                                    if qty > 1:
                                        payload["quantity"] = qty
                                else:
                                    payload["link"] = player_id
                                    payload["quantity"] = qty
                                sr = _supplier_http_post(sup["api_url"], payload)
                                if isinstance(sr, dict) and sr.get("order"):
                                    sup_order_id = str(sr["order"])
                                    sup_note = f"تم الإرسال التلقائي للمزود ({sup['name']}) رقم #{sup_order_id}"
                                else:
                                    sup_note = f"رد المزود: {str(sr)[:140]}"
                        except Exception as ex:
                            sup_note = f"تعذر الإرسال التلقائي للمزود: {str(ex)[:120]}"

            sb_request("POST", "/rest/v1/orders", {
                "user_id": u["id"],
                "user_name": str(u["name"])[:100],
                "product_id": pid,
                "game": prod["game"],
                "pack_name": prod["pack"],
                "sku": prod["sku"],
                "player_id": player_id,
                "price_usd": price,
                "supplier_cost_usd": cost,
                "admin_profit_usd": profit,
                "status": "قيد التنفيذ",
                "supplier_order_id": sup_order_id,
                "supplier_note": sup_note
            }, prefer_rep=True)
            _tg_notify(
                f"🛒 <b>طلب شراء جديد</b>\n"
                f"👤 العميل: {u['name']}\n"
                f"🎮 الباقة: {prod['pack']}\n"
                f"🆔 المعرف: <code>{player_id}</code>\n"
                f"💵 السعر: <b>${price:.2f}</b> (ربح: ${profit:.2f})\n"
                f"📡 الحالة: {sup_note}"
            )
            self._json({"ok": True})
            return

        # 3. مسارات الإدارة المحمية بـ HttpOnly Cookie + التحقق من Origin
        if not self._is_admin() or not self._check_origin():
            self._json({"ok": False, "msg": "Unauthorized"}, 403); return

        if p == "/api/admin/deposit-decide":
            did = int(body.get("dep_id", 0))
            dec = str(body.get("decision", "")).strip()
            if did <= 0 or dec not in ("مقبول", "مرفوض"):
                self._json({"ok": False, "msg": "قرار غير صالح"}, 400); return
            # انتقال ذري من "معلق" فقط لمنع تكرار الشحن عند النقر المزدوج
            updated = sb_request(
                "PATCH",
                f"/rest/v1/deposits?id=eq.{did}&status=eq.{quote('معلق')}",
                {"status": dec},
                prefer_rep=True
            )
            if isinstance(updated, list) and len(updated) == 1 and dec == "مقبول":
                d = updated[0]
                uid_q = quote(str(d["user_id"]))
                usrs = sb_request("GET", f"/rest/v1/users?id=eq.{uid_q}&select=balance")
                if isinstance(usrs, list) and usrs:
                    nb = round(float(usrs[0]["balance"]) + float(d["amount_usd"]), 2)
                    sb_request("PATCH", f"/rest/v1/users?id=eq.{uid_q}", {"balance": nb})
            self._json({"ok": True})
            return

        if p == "/api/admin/order-status":
            oid = int(body.get("order_id", 0))
            st = str(body.get("status", "")).strip()
            if oid <= 0 or st not in ("قيد التنفيذ", "مكتمل", "مرفوض من المورد"):
                self._json({"ok": False, "msg": "حالة غير صالحة"}, 400); return
            note = str(body.get("note") or ("رفض المورد الطلب وتم إرجاع المبلغ لمحفظتك تلقائياً" if st == "مرفوض من المورد" else "تم إتمام الشحن بنجاح"))[:200]
            if st == "مرفوض من المورد":
                # انتقال ذري لمنع الاسترداد المزدوج
                updated = sb_request(
                    "PATCH",
                    f"/rest/v1/orders?id=eq.{oid}&status=neq.{quote('مرفوض من المورد')}",
                    {"status": st, "supplier_note": note},
                    prefer_rep=True
                )
                if isinstance(updated, list) and len(updated) == 1:
                    o = updated[0]
                    uid_q = quote(str(o["user_id"]))
                    usrs = sb_request("GET", f"/rest/v1/users?id=eq.{uid_q}&select=balance")
                    if isinstance(usrs, list) and usrs:
                        nb = round(float(usrs[0]["balance"]) + float(o["price_usd"]), 2)
                        sb_request("PATCH", f"/rest/v1/users?id=eq.{uid_q}", {"balance": nb})
            else:
                sb_request("PATCH", f"/rest/v1/orders?id=eq.{oid}", {"status": st, "supplier_note": note})
            self._json({"ok": True})
            return

        if p == "/api/admin/product-add":
            sb_request("POST", "/rest/v1/products", {
                "game": str(body["game"]).strip()[:150],
                "pack": str(body["pack"]).strip()[:150],
                "price_usd": round(max(0.01, float(body["price_usd"])), 2),
                "supplier_cost_usd": round(max(0.0, float(body.get("supplier_cost_usd", 0))), 2),
                "sku": str(body.get("sku") or "BX-CUSTOM").strip()[:80],
                "icon": str(body.get("icon") or "🎮").strip()[:500],
                "active": True
            })
            invalidate_cache("products")
            self._json({"ok": True})
            return

        if p == "/api/admin/product-update":
            pid = int(body.get("id", 0))
            if pid <= 0:
                self._json({"ok": False, "msg": "معرف الباقة غير صالح"}, 400); return
            patch = {}
            if "price_usd" in body: patch["price_usd"] = round(max(0.01, float(body["price_usd"])), 2)
            if "supplier_cost_usd" in body: patch["supplier_cost_usd"] = round(max(0.0, float(body["supplier_cost_usd"])), 2)
            if "pack" in body: patch["pack"] = str(body["pack"]).strip()[:150]
            if "game" in body: patch["game"] = str(body["game"]).strip()[:150]
            if "sku" in body: patch["sku"] = str(body["sku"]).strip()[:80]
            if "icon" in body: patch["icon"] = str(body["icon"]).strip()[:500]
            if "active" in body: patch["active"] = bool(body["active"])
            sb_request("PATCH", f"/rest/v1/products?id=eq.{pid}", patch)
            invalidate_cache("products")
            self._json({"ok": True})
            return

        if p == "/api/admin/cat-rename":
            old_cat = str(body.get("old_cat", "")).strip()[:100]
            new_cat = str(body.get("new_cat", "")).strip()[:100]
            if not old_cat or not new_cat or old_cat == new_cat:
                self._json({"ok": False, "msg": "اسم القسم غير صالح"}, 400); return
            prods = fetch_all_products(active_only=False)
            for pr in prods:
                if str(pr.get("sku", "")) == str(SETTINGS_SKU): continue
                g = str(pr.get("game", "")).strip()
                if g == old_cat:
                    sb_request("PATCH", f"/rest/v1/products?id=eq.{pr['id']}", {"game": new_cat})
                elif g.startswith(old_cat + " | "):
                    ng = new_cat + g[len(old_cat):]
                    sb_request("PATCH", f"/rest/v1/products?id=eq.{pr['id']}", {"game": ng[:150]})
            invalidate_cache("products")
            self._json({"ok": True})
            return

        if p == "/api/admin/cat-delete":
            target_cat = str(body.get("cat", "")).strip()[:100]
            if not target_cat:
                self._json({"ok": False, "msg": "حدد القسم أولاً"}, 400); return
            prods = fetch_all_products(active_only=False)
            ids = [
                str(pr["id"]) for pr in prods
                if str(pr.get("sku", "")) != str(SETTINGS_SKU)
                and str(pr.get("game", "")).split("|")[0].strip() == target_cat
            ]
            for i in range(0, len(ids), 100):
                chunk = ",".join(ids[i:i+100])
                sb_request("PATCH", f"/rest/v1/products?id=in.({chunk})", {"active": False})
            invalidate_cache("products")
            self._json({"ok": True, "count": len(ids)})
            return

        if p == "/api/admin/sub-rename":
            old_game = str(body.get("old_game", "")).strip()[:150]
            new_game = str(body.get("new_game", "")).strip()[:150]
            if not old_game or not new_game or old_game == new_game:
                self._json({"ok": False, "msg": "الاسم غير صالح"}, 400); return
            sb_request("PATCH", f"/rest/v1/products?game=eq.{quote(old_game)}", {"game": new_game})
            invalidate_cache("products")
            self._json({"ok": True})
            return

        if p == "/api/admin/sub-delete":
            target_game = str(body.get("game", "")).strip()[:150]
            if not target_game:
                self._json({"ok": False, "msg": "حدد الفئة أولاً"}, 400); return
            sb_request("PATCH", f"/rest/v1/products?game=eq.{quote(target_game)}", {"active": False})
            invalidate_cache("products")
            self._json({"ok": True})
            return

        if p == "/api/admin/sub-icon-update":
            target_game = str(body.get("game", "")).strip()[:150]
            icon_url = str(body.get("icon", "")).strip()[:500]
            if not target_game or not icon_url:
                self._json({"ok": False, "msg": "حدد القسم والرابط/الصورة أولاً"}, 400); return
            sb_request("PATCH", f"/rest/v1/products?game=eq.{quote(target_game)}", {"icon": icon_url})
            invalidate_cache("products")
            self._json({"ok": True})
            return

        if p == "/api/admin/upload-image":
            data_url = str(body.get("data_url", "")).strip()
            m = re.match(r"^data:(image/(?:webp|png|jpeg|jpg));base64,(.+)$", data_url, re.S)
            if not m:
                self._json({"ok": False, "msg": "صيغة الصورة غير مدعومة (استخدم WebP/PNG/JPG)"}, 400); return
            try:
                raw_bytes = base64.b64decode(m.group(2))
            except Exception:
                self._json({"ok": False, "msg": "فشل فك تشفير الصورة"}, 400); return
            if len(raw_bytes) > 950 * 1024:
                self._json({"ok": False, "msg": "حجم الصورة بعد الضغط كبير جداً (الحد 950KB)"}, 400); return
            sku_key = "MEDIA_" + secrets.token_hex(6)
            res = sb_request("POST", "/rest/v1/products", {
                "game": "__MEDIA__",
                "pack": data_url,
                "price_usd": 1.00,
                "supplier_cost_usd": 0.00,
                "sku": sku_key,
                "icon": "🖼️",
                "active": False
            })
            if isinstance(res, dict) and "error" in res:
                self._json({"ok": False, "msg": "فشل حفظ الصورة في Supabase"}, 500); return
            with _CACHE_LOCK:
                _CACHE["media"][sku_key] = (m.group(1), raw_bytes)
            ext = "webp" if "webp" in m.group(1) else ("png" if "png" in m.group(1) else "jpg")
            self._json({"ok": True, "url": f"/api/media/{sku_key}.{ext}", "sku": sku_key})
            return

        if p == "/api/admin/supplier-test":
            api_url = str(body.get("api_url", "")).strip()
            api_key = str(body.get("api_key", "")).strip()
            stype = str(body.get("type", "smm")).strip().lower()
            if not api_url or not api_key:
                self._json({"ok": False, "msg": "أدخل رابط API ومفتاح المزود أولاً"}, 400); return
            try:
                if _is_sonic8(api_url):
                    resp = _sonic8_get(api_url, api_key, "/client/api/profile", timeout=15)
                    self._json({"ok": True, "result": resp})
                elif stype == "smm":
                    resp = _supplier_http_post(api_url, {"key": api_key, "action": "balance"})
                    self._json({"ok": True, "result": resp})
                else:
                    req = urllib.request.Request(api_url, headers={"Authorization": f"Bearer {api_key}", "User-Agent": "BoostixStore/2.0"})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        txt = r.read().decode("utf-8", errors="ignore")[:500]
                    self._json({"ok": True, "result": {"status": 200, "preview": txt}})
            except Exception as e:
                self._json({"ok": False, "msg": f"فشل الاتصال بالمزود: {str(e)[:180]}"}, 400)
            return

        if p == "/api/admin/supplier-services":
            api_url = str(body.get("api_url", "")).strip()
            api_key = str(body.get("api_key", "")).strip()
            if not api_url or not api_key:
                self._json({"ok": False, "msg": "أدخل رابط API ومفتاح المزود"}, 400); return
            try:
                if _is_sonic8(api_url):
                    resp = _sonic8_get(api_url, api_key, "/client/api/products", timeout=25)
                    mapped = []
                    if isinstance(resp, list):
                        for it in resp:
                            if not isinstance(it, dict): continue
                            qv = it.get("qty_values")
                            mn, mx = (qv.get("min", 1), qv.get("max", 1)) if isinstance(qv, dict) else ((qv[0], qv[-1]) if isinstance(qv, list) and qv else (1, 1))
                            mapped.append({"service": it.get("id"), "name": it.get("name", ""), "category": it.get("category_name", "Sonic8Store"), "rate": it.get("price", "0"), "min": mn, "max": mx, "available": it.get("available", True)})
                    self._json({"ok": True, "total": len(mapped), "services": mapped[:250]})
                else:
                    resp = _supplier_http_post(api_url, {"key": api_key, "action": "services"}, timeout=25)
                    services = resp[:250] if isinstance(resp, list) else []
                    self._json({"ok": True, "total": len(resp) if isinstance(resp, list) else 0, "services": services})
            except Exception as e:
                self._json({"ok": False, "msg": f"تعذر جلب الخدمات: {str(e)[:180]}"}, 400)
            return

        if p == "/api/admin/product-del":
            pid = int(body.get("id", 0))
            if pid > 0:
                sb_request("DELETE", f"/rest/v1/products?id=eq.{pid}")
                invalidate_cache("products")
            self._json({"ok": True})
            return

        if p == "/api/admin/bulk-margin":
            margin_pct = float(body.get("margin_pct", 15))
            category = str(body.get("category", "ALL")).strip()
            round_mode = str(body.get("round_mode", "exact")).strip()
            if margin_pct < 0 or margin_pct > 500:
                self._json({"ok": False, "msg": "نسبة الربح يجب أن تكون بين 0% و 500%"}, 400); return

            prods = fetch_all_products(active_only=True, full_fields=True)
            updates = []
            for pr in prods:
                if category != "ALL" and not str(pr.get("game", "")).startswith(category):
                    continue
                cost = float(pr.get("supplier_cost_usd") or 0)
                if cost <= 0:
                    continue
                raw_price = cost * (1.0 + margin_pct / 100.0)
                if round_mode == "0.05":
                    new_price = round(round(raw_price * 20) / 20, 2)
                elif round_mode == "0.99" and raw_price >= 1.0:
                    new_price = round(int(raw_price) + 0.99, 2)
                    if new_price < cost:
                        new_price = round(int(raw_price) + 1.99, 2)
                else:
                    new_price = round(raw_price, 2)
                if new_price < 0.01:
                    new_price = 0.01
                updates.append({
                    "id": pr["id"],
                    "game": pr["game"],
                    "pack": pr["pack"],
                    "sku": pr["sku"],
                    "icon": pr.get("icon") or "🎮",
                    "active": True,
                    "supplier_cost_usd": cost,
                    "price_usd": new_price
                })

            updated_count = 0
            for i in range(0, len(updates), 250):
                chunk = updates[i:i+250]
                res = sb_request(
                    "POST",
                    "/rest/v1/products?on_conflict=id",
                    chunk,
                    extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                    timeout=25
                )
                if not (isinstance(res, dict) and "error" in res):
                    updated_count += len(chunk)

            invalidate_cache("products")
            self._json({"ok": True, "updated": updated_count})
            return

        if p == "/api/admin/settings-save":
            saved = save_store_settings(body)
            self._json({"ok": True, "payInfo": saved})
            return

        if p == "/api/admin/user-balance":
            uid = str(body.get("user_id", "")).strip()
            delta = round(float(body.get("delta_usd", 0)), 2)
            if not uid or delta == 0 or abs(delta) > 50000:
                self._json({"ok": False, "msg": "بيانات التعديل غير صالحة"}, 400); return
            uid_q = quote(uid)
            usrs = sb_request("GET", f"/rest/v1/users?id=eq.{uid_q}&select=balance")
            if not isinstance(usrs, list) or not usrs:
                self._json({"ok": False, "msg": "المستخدم غير موجود"}, 404); return
            nb = round(max(0.0, float(usrs[0]["balance"]) + delta), 2)
            sb_request("PATCH", f"/rest/v1/users?id=eq.{uid_q}", {"balance": nb})
            self._json({"ok": True, "balance": nb})
            return

        self._json({"ok": False, "msg": "Not Found"}, 404)

if __name__ == "__main__":
    import threading
    threading.Thread(target=_tg_register_webhook, daemon=True).start()
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer(("0.0.0.0", PORT), CloudHandler).serve_forever()
