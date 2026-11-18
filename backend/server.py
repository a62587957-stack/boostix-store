import os, json, urllib.request, urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, quote

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(BASE_DIR, "backend", ".env")
STORE_HTML = os.path.join(BASE_DIR, "public", "store.html")
ADMIN_HTML = os.path.join(BASE_DIR, "public", "admin.html")

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

def sb_request(method, path, body=None, prefer_rep=False, custom_token=None):
    url = f"{SUPABASE_URL}{path}"
    auth_token = custom_token if custom_token else SUPABASE_SECRET
    headers = {
        "apikey": SUPABASE_SECRET,
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json"
    }
    if prefer_rep:
        headers["Prefer"] = "return=representation"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
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
        if p == "/admin":
            with open(ADMIN_HTML, "rb") as f: b = f.read()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(b)
            return

        if p == "/api/customer/bootstrap":
            u = self._auth_user()
            prods = sb_request("GET", "/rest/v1/products?active=eq.true&select=id,game,pack,price_usd,icon&order=id.asc")
            if not isinstance(prods, list): prods = []
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
                "myOrders": my_ords,
                "myDeposits": my_deps,
                "payInfo": {
                    "rate_syp": 15000,
                    "sham_account": "شام كاش المعتمد: 0931735665 (هاشم الظاهر)",
                    "syriatel_account": "سيريتل كاش: 0931735665",
                    "usdt_account": "USDT TRC20 / Binance ID: 554433221"
                }
            })
            return

        if p == "/api/admin/overview":
            if self.headers.get("X-Admin-Key") != ADMIN_SECRET:
                self._json({"ok": False}, 403); return
            users = sb_request("GET", "/rest/v1/users?select=*&order=created_at.desc")
            if not isinstance(users, list): users = []
            for u in users: u["created_at"] = fmt_date(u.get("created_at"))
            prods = sb_request("GET", "/rest/v1/products?select=*&order=id.asc")
            if not isinstance(prods, list): prods = []
            deps = sb_request("GET", "/rest/v1/deposits?select=*&order=id.desc")
            if not isinstance(deps, list): deps = []
            for d in deps: d["date"] = fmt_date(d.get("created_at"))
            ords = sb_request("GET", "/rest/v1/orders?select=*&order=id.desc")
            if not isinstance(ords, list): ords = []
            for o in ords: o["date"] = fmt_date(o.get("created_at"))
            self._json({"ok": True, "users": users, "products": prods, "deposits": deps, "orders": ords})
            return

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
            self._json({"ok": False}, 403); return

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
                    note = "رفض المورد الطلب وتم إرجاع المبلغ لمحفظتك تلقائياً"
                else:
                    note = "تم إتمام الشحن بنجاح"
                sb_request("PATCH", f"/rest/v1/orders?id=eq.{oid}", {"status": st, "supplier_note": note})
            self._json({"ok": True})
            return

        if p == "/api/admin/product-add":
            sb_request("POST", "/rest/v1/products", {
                "game": body["game"],
                "pack": body["pack"],
                "price_usd": float(body["price_usd"]),
                "supplier_cost_usd": float(body["supplier_cost_usd"]),
                "sku": body["sku"],
                "icon": "🎮"
            })
            self._json({"ok": True})
            return

        if p == "/api/admin/product-del":
            pid = int(body.get("id", 0))
            sb_request("DELETE", f"/rest/v1/products?id=eq.{pid}")
            self._json({"ok": True})
            return

if __name__ == "__main__":
    HTTPServer.allow_reuse_address = True
    HTTPServer(("0.0.0.0", PORT), CloudHandler).serve_forever()
