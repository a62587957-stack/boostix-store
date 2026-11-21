import os, json, urllib.request, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, quote

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
                    cfg[k] = v
    return cfg

CFG = load_env()
SUPABASE_URL = os.environ.get("SUPABASE_URL") or CFG.get("SUPABASE_URL", "https://lxeloayuowzatrrlsasn.supabase.co")
SUPABASE_PUB = os.environ.get("SUPABASE_PUBLISHABLE_KEY") or CFG.get("SUPABASE_PUBLISHABLE_KEY", "")
SUPABASE_SECRET = os.environ.get("SUPABASE_SECRET_KEY") or CFG.get("SUPABASE_SECRET_KEY", "")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET") or CFG.get("ADMIN_SECRET", "SakerAdmin2026")
PORT = int(os.environ.get("PORT", 8090))

SETTINGS_SKU = "__STORE_SETTINGS__"
DEFAULT_SETTINGS = {
    "rate_syp": 15000,
    "sham_account": "شام كاش المعتمد: 0931735665 (هاشم الظاهر)",
    "syriatel_account": "سيريتل كاش: 0931735665",
    "usdt_account": "USDT TRC20 / Binance ID: 554433221",
    "default_margin_pct": 15.0
}

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

def get_store_settings():
    res = sb_request("GET", f"/rest/v1/products?sku=eq.{SETTINGS_SKU}&select=id,pack&limit=1")
    settings = dict(DEFAULT_SETTINGS)
    if isinstance(res, list) and res:
        try:
            saved = json.loads(res[0].get("pack", "{}"))
            if isinstance(saved, dict):
                settings.update(saved)
        except Exception:
            pass
    return settings

def save_store_settings(new_data):
    cur = get_store_settings()
    if "rate_syp" in new_data:
        cur["rate_syp"] = max(1, int(float(new_data["rate_syp"])))
    if "sham_account" in new_data:
        cur["sham_account"] = str(new_data["sham_account"]).strip()
    if "syriatel_account" in new_data:
        cur["syriatel_account"] = str(new_data["syriatel_account"]).strip()
    if "usdt_account" in new_data:
        cur["usdt_account"] = str(new_data["usdt_account"]).strip()
    if "default_margin_pct" in new_data:
        cur["default_margin_pct"] = round(max(0.0, float(new_data["default_margin_pct"])), 2)

    payload_str = json.dumps(cur, ensure_ascii=False)
    res = sb_request("GET", f"/rest/v1/products?sku=eq.{SETTINGS_SKU}&select=id&limit=1")
    if isinstance(res, list) and res:
        sb_request("PATCH", f"/rest/v1/products?id=eq.{res[0]['id']}", {"pack": payload_str, "active": False})
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
    return cur

def fetch_all_products(active_only=True):
    all_p = []
    select = "id,game,pack,price_usd,icon" if active_only else "id,game,pack,price_usd,supplier_cost_usd,sku,icon,active"
    filt = "active=eq.true&" if active_only else ""
    offset = 0
    while True:
        chunk = sb_request("GET", f"/rest/v1/products?{filt}select={select}&order=id.asc&limit=1000&offset={offset}")
        if isinstance(chunk, list):
            for item in chunk:
                if item.get("sku") != SETTINGS_SKU and item.get("game") != "__SYSTEM__":
                    all_p.append(item)
            if len(chunk) < 1000:
                break
            offset += 1000
        else:
            break
    return all_p

def load_catalog_tree():
    if os.path.exists(CATALOG_FILE):
        try:
            with open(CATALOG_FILE, "r", encoding="utf-8") as f:
                c = json.load(f)
            return {
                "main_categories": c.get("main_categories", []),
                "sub_categories": c.get("sub_categories", []),
                "level3_categories": c.get("level3_categories", [])
            }
        except Exception:
            pass
    return {"main_categories": [], "sub_categories": [], "level3_categories": []}

def verify_user_session(token):
    if not token:
        return None
    auth_user = sb_request("GET", "/auth/v1/user", custom_token=token)
    if isinstance(auth_user, dict) and "id" in auth_user:
        uid = auth_user["id"]
        email = auth_user.get("email", "")
        meta = auth_user.get("user_metadata") or {}
        name = meta.get("full_name") or meta.get("name") or (email.split("@")[0] if email else "زبون")
        rows = sb_request("GET", f"/rest/v1/users?id=eq.{quote(uid)}&select=*")
        if isinstance(rows, list) and len(rows) > 0:
            return rows[0]
        created = sb_request("POST", "/rest/v1/users", {
            "id": uid,
            "email": email,
            "name": name,
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

class CloudHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _json(self, data, code=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        l = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(l).decode("utf-8")) if l > 0 else {}

    def _auth_user(self):
        auth = self.headers.get("Authorization", "")
        tok = auth.replace("Bearer ", "").strip()
        return verify_user_session(tok)

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/" or p == "/index.html":
            with open(STORE_HTML, "rb") as f: b = f.read()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(b)
            return
        if p == "/admin" or p == "/admin.html":
            with open(ADMIN_HTML, "rb") as f: b = f.read()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(b)
            return

        if p == "/api/customer/bootstrap":
            u = self._auth_user()
            prods = fetch_all_products(active_only=True)
            cat_tree = load_catalog_tree()
            pay_info = get_store_settings()
            my_ords, my_deps, u_dict = [], [], None
            if u:
                u_dict = {
                    "id": u["id"],
                    "name": u["name"],
                    "email": u["email"],
                    "balance": float(u["balance"]),
                    "role": u.get("role", "customer")
                }
                raw_ords = sb_request("GET", f"/rest/v1/orders?user_id=eq.{u['id']}&select=id,created_at,game,pack_name,player_id,price_usd,status,supplier_note&order=id.desc")
                if isinstance(raw_ords, list):
                    for o in raw_ords:
                        o["date"] = fmt_date(o.get("created_at"))
                        my_ords.append(o)
                raw_deps = sb_request("GET", f"/rest/v1/deposits?user_id=eq.{u['id']}&select=id,created_at,method,amount_usd,tx_id,status&order=id.desc")
                if isinstance(raw_deps, list):
                    for d in raw_deps:
                        d["date"] = fmt_date(d.get("created_at"))
                        my_deps.append(d)

            self._json({
                "ok": True,
                "supabaseUrl": SUPABASE_URL,
                "supabaseAnonKey": SUPABASE_PUB,
                "user": u_dict,
                "products": prods,
                "catalogTree": cat_tree,
                "myOrders": my_ords,
                "myDeposits": my_deps,
                "payInfo": pay_info
            })
            return

        if p == "/api/admin/overview":
            if self.headers.get("X-Admin-Key") != ADMIN_SECRET:
                self._json({"ok": False, "msg": "Unauthorized"}, 403); return
            users = sb_request("GET", "/rest/v1/users?select=*&order=created_at.desc")
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
                "users": users,
                "products": prods,
                "catalogTree": cat_tree,
                "payInfo": pay_info,
                "deposits": deps,
                "orders": ords
            })
            return

        self._json({"ok": False, "msg": "Not Found"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path
        body = self._body()

        if p == "/api/customer/deposit":
            u = self._auth_user()
            if not u: self._json({"ok": False, "msg": "يرجى تسجيل الدخول بحساب Google أولاً"}, 401); return
            amt = round(float(body.get("amount_usd", 0)), 2)
            tx = str(body.get("tx_id", "")).strip()
            method = str(body.get("method", "شام كاش")).strip()
            if amt <= 0 or not tx:
                self._json({"ok": False, "msg": "أدخل المبلغ ورقم العملية"}, 400); return

            dup = sb_request("GET", f"/rest/v1/deposits?tx_id=eq.{quote(tx)}&select=id")
            if isinstance(dup, list) and len(dup) > 0:
                self._json({"ok": False, "msg": "رقم عملية التحويل هذا مسجل مسبقاً!"}, 400); return

            sb_request("POST", "/rest/v1/deposits", {
                "user_id": u["id"],
                "user_name": u["name"],
                "amount_usd": amt,
                "method": method,
                "tx_id": tx,
                "status": "معلق"
            }, prefer_rep=True)
            self._json({"ok": True})
            return

        if p == "/api/customer/purchase":
            u = self._auth_user()
            if not u: self._json({"ok": False, "msg": "يرجى تسجيل الدخول بحساب Google أولاً"}, 401); return
            pid = int(body.get("product_id", 0))
            player_id = str(body.get("player_id", "")).strip()
            prods = sb_request("GET", f"/rest/v1/products?id=eq.{pid}&select=*")
            if not isinstance(prods, list) or not prods:
                self._json({"ok": False, "msg": "الباقة غير موجودة"}, 404); return
            prod = prods[0]
            price = round(float(prod["price_usd"]), 2)
            bal = round(float(u["balance"]), 2)
            if bal < price:
                self._json({"ok": False, "need_deposit": True, "msg": f"رصيد محفظتك (${bal:.2f}) غير كافٍ لشراء الباقة (${price:.2f}). اشحن محفظتك أولاً."}, 400)
                return

            cost = round(float(prod["supplier_cost_usd"]), 2)
            profit = round(price - cost, 2)
            new_bal = round(bal - price, 2)

            sb_request("PATCH", f"/rest/v1/users?id=eq.{u['id']}", {"balance": new_bal})
            sb_request("POST", "/rest/v1/orders", {
                "user_id": u["id"],
                "user_name": u["name"],
                "product_id": pid,
                "game": prod["game"],
                "pack_name": prod["pack"],
                "sku": prod["sku"],
                "player_id": player_id,
                "price_usd": price,
                "supplier_cost_usd": cost,
                "admin_profit_usd": profit,
                "status": "قيد التنفيذ",
                "supplier_note": "تم استلام الطلب وإرساله للمورد للشحن الفوري"
            }, prefer_rep=True)
            self._json({"ok": True})
            return

        if self.headers.get("X-Admin-Key") != ADMIN_SECRET:
            self._json({"ok": False, "msg": "Unauthorized"}, 403); return

        if p == "/api/admin/deposit-decide":
            did = body.get("dep_id")
            dec = body.get("decision")
            deps = sb_request("GET", f"/rest/v1/deposits?id=eq.{did}&select=*")
            if isinstance(deps, list) and deps and deps[0]["status"] == "معلق":
                d = deps[0]
                sb_request("PATCH", f"/rest/v1/deposits?id=eq.{did}", {"status": dec})
                if dec == "مقبول":
                    usrs = sb_request("GET", f"/rest/v1/users?id=eq.{d['user_id']}&select=balance")
                    if isinstance(usrs, list) and usrs:
                        nb = round(float(usrs[0]["balance"]) + float(d["amount_usd"]), 2)
                        sb_request("PATCH", f"/rest/v1/users?id=eq.{d['user_id']}", {"balance": nb})
            self._json({"ok": True})
            return

        if p == "/api/admin/order-status":
            oid = body.get("order_id")
            st = body.get("status")
            ords = sb_request("GET", f"/rest/v1/orders?id=eq.{oid}&select=*")
            if isinstance(ords, list) and ords and ords[0]["status"] != st:
                o = ords[0]
                if st == "مرفوض من المورد" and o["status"] != "مرفوض من المورد":
                    usrs = sb_request("GET", f"/rest/v1/users?id=eq.{o['user_id']}&select=balance")
                    if isinstance(usrs, list) and usrs:
                        nb = round(float(usrs[0]["balance"]) + float(o["price_usd"]), 2)
                        sb_request("PATCH", f"/rest/v1/users?id=eq.{o['user_id']}", {"balance": nb})
                    note = body.get("note") or "رفض المورد الطلب وتم إرجاع المبلغ لمحفظتك تلقائياً"
                else:
                    note = body.get("note") or "تم إتمام الشحن بنجاح"
                sb_request("PATCH", f"/rest/v1/orders?id=eq.{oid}", {"status": st, "supplier_note": note})
            self._json({"ok": True})
            return

        if p == "/api/admin/product-add":
            sb_request("POST", "/rest/v1/products", {
                "game": str(body["game"]).strip(),
                "pack": str(body["pack"]).strip(),
                "price_usd": round(float(body["price_usd"]), 2),
                "supplier_cost_usd": round(float(body.get("supplier_cost_usd", 0)), 2),
                "sku": str(body.get("sku") or "BX-CUSTOM").strip(),
                "icon": str(body.get("icon") or "🎮").strip(),
                "active": True
            })
            self._json({"ok": True})
            return

        if p == "/api/admin/product-update":
            pid = int(body.get("id", 0))
            if pid <= 0:
                self._json({"ok": False, "msg": "معرف الباقة غير صالح"}, 400); return
            patch = {}
            if "price_usd" in body: patch["price_usd"] = round(max(0.01, float(body["price_usd"])), 2)
            if "supplier_cost_usd" in body: patch["supplier_cost_usd"] = round(max(0.0, float(body["supplier_cost_usd"])), 2)
            if "pack" in body: patch["pack"] = str(body["pack"]).strip()
            if "game" in body: patch["game"] = str(body["game"]).strip()
            if "sku" in body: patch["sku"] = str(body["sku"]).strip()
            if "active" in body: patch["active"] = bool(body["active"])
            sb_request("PATCH", f"/rest/v1/products?id=eq.{pid}", patch)
            self._json({"ok": True})
            return

        if p == "/api/admin/product-del":
            pid = int(body.get("id", 0))
            sb_request("DELETE", f"/rest/v1/products?id=eq.{pid}")
            self._json({"ok": True})
            return

        if p == "/api/admin/bulk-margin":
            margin_pct = float(body.get("margin_pct", 15))
            category = str(body.get("category", "ALL")).strip()
            round_mode = str(body.get("round_mode", "exact")).strip()
            if margin_pct < 0 or margin_pct > 500:
                self._json({"ok": False, "msg": "نسبة الربح يجب أن تكون بين 0% و 500%"}, 400); return

            prods = fetch_all_products(active_only=True)
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

            self._json({"ok": True, "updated": updated_count})
            return

        if p == "/api/admin/settings-save":
            saved = save_store_settings(body)
            self._json({"ok": True, "payInfo": saved})
            return

        if p == "/api/admin/user-balance":
            uid = str(body.get("user_id", "")).strip()
            delta = round(float(body.get("delta_usd", 0)), 2)
            if not uid or delta == 0:
                self._json({"ok": False, "msg": "بيانات التعديل غير صالحة"}, 400); return
            usrs = sb_request("GET", f"/rest/v1/users?id=eq.{quote(uid)}&select=balance")
            if not isinstance(usrs, list) or not usrs:
                self._json({"ok": False, "msg": "المستخدم غير موجود"}, 404); return
            nb = round(max(0.0, float(usrs[0]["balance"]) + delta), 2)
            sb_request("PATCH", f"/rest/v1/users?id=eq.{quote(uid)}", {"balance": nb})
            self._json({"ok": True, "balance": nb})
            return

        self._json({"ok": False, "msg": "Not Found"}, 404)

if __name__ == "__main__":
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer(("0.0.0.0", PORT), CloudHandler).serve_forever()
