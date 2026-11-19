import os, json, time
from http.server import HTTPServer, BaseHTTPRequestHandler

_CACHE = {"products": [], "time": 0}

def fetch_all_active_products():
    if _CACHE["products"] and (time.time() - _CACHE["time"] < 300):
        return _CACHE["products"]
    cat_path = "boostix_store_catalog.json" if os.path.exists("boostix_store_catalog.json") else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "boostix_store_catalog.json")
    try:
        with open(cat_path, "r", encoding="utf-8") as f:
            c = json.load(f)
        fallback = []
        for p in c.get("products", []):
            main_c = (p.get("main_category") or "الألعاب").strip()
            sub_c = (p.get("sub_category") or main_c).strip()
            fallback.append({
                "id": int(p.get("id", 0)) if str(p.get("id", "")).isdigit() else 0,
                "game": f"{main_c} | {sub_c}",
                "pack": p.get("cardName") or p.get("name"),
                "price_usd": float(p.get("price_usd") or 1.99),
                "icon": p.get("image_url") or "🎮"
            })
        _CACHE["products"] = fallback
        _CACHE["time"] = time.time()
    except: pass
    return _CACHE["products"]

class CloudHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            with open("public/store.html", "rb") as f:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(f.read())
        elif self.path == '/api/customer/bootstrap':
            data = json.dumps({"ok": True, "products": fetch_all_active_products()})
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(data.encode('utf-8'))
            
    def do_POST(self):
        if self.path == "/api/log_error":
            content_len = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_len)
            with open("frontend_error.log", "a", encoding="utf-8") as log_f:
                log_f.write(post_data.decode('utf-8') + "\n")
            self.send_response(200)
            self.end_headers()

if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8090), CloudHandler).serve_forever()
