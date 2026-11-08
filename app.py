#!/usr/bin/env python3
"""Comparador de precios Tienda Inglesa / Disco. Sin dependencias: python3 app.py"""
import html
import os
import json
import re
import sqlite3
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = Path(os.environ.get("PRECIOS_DB") or BASE / "precios.db")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
TI = "https://www.tiendainglesa.com.uy"
GDU = {"disco": "https://www.disco.com.uy", "devoto": "https://www.devoto.com.uy"}
STORES = {"tiendainglesa": "Tienda Inglesa", "disco": "Disco", "devoto": "Devoto"}
POOL = ThreadPoolExecutor(max_workers=8)  # búsquedas por súper
FETCH_POOL = ThreadPoolExecutor(max_workers=16)  # páginas de producto (separado para no trabar POOL)


# ---------- db ----------

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            store TEXT NOT NULL, store_pid TEXT NOT NULL, name TEXT, url TEXT, image TEXT,
            UNIQUE(item_id, store, store_pid));
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY,
            link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
            price REAL, note TEXT, fetched_at TEXT DEFAULT CURRENT_TIMESTAMP);
        DROP TABLE IF EXISTS disco_catalog;
        CREATE TABLE IF NOT EXISTS gdu_catalog (
            store TEXT NOT NULL, pid TEXT NOT NULL, slug TEXT NOT NULL, PRIMARY KEY (store, pid));
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS lists (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS list_entries (
            list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            qty REAL NOT NULL DEFAULT 1,
            PRIMARY KEY (list_id, item_id));
        """)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(prices)")}
        if "special" not in cols:
            con.execute("ALTER TABLE prices ADD COLUMN special REAL")


# ---------- matching ----------

UNITS = {"l": "l", "lt": "l", "lts": "l", "litro": "l", "litros": "l", "ml": "ml", "cc": "ml",
         "g": "g", "gr": "g", "grs": "g", "gramos": "g", "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg",
         "un": "un", "u": "un", "unid": "un", "unidades": "un", "uds": "un"}
STOP = {"de", "x", "en", "el", "la", "los", "las", "con", "y", "por", "para", "a", "al", "del", "sin"}
# "sin" está en STOP pero importa ("sin lactosa"): lo conservamos pegado a la palabra siguiente.


def norm(s):
    s = unicodedata.normalize("NFKD", html.unescape(s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"(\d),(\d)", r"\1.\2", s)
    s = re.sub(r"(\d)([a-z])", r"\1 \2", s)
    s = re.sub(r"\bsin\s+", "sin", s)
    return re.sub(r"[^a-z0-9.]+", " ", s).strip()


def analyze(s):
    """-> (palabras, tamaño normalizado (cantidad, unidad base) o None)"""
    toks = norm(s).split()
    words, size = [], None
    for i, t in enumerate(toks):
        if t in UNITS:
            if i and re.fullmatch(r"\d+(\.\d+)?", toks[i - 1]) and size is None:
                q, u = float(toks[i - 1]), UNITS[t]
                if u == "kg":
                    q, u = q * 1000, "g"
                if u == "l":
                    q, u = q * 1000, "ml"
                size = (round(q, 1), u)
            continue
        if re.fullmatch(r"[\d.]+", t) or t in STOP or len(t) < 2:
            continue
        words.append(t.rstrip("s") if len(t) > 4 else t)  # plural tosco
    return set(words), size


def _tok_match(a, b):
    return a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))


def score(query, name):
    qw, qs = query if isinstance(query, tuple) else analyze(query)
    cw, cs = analyze(name)
    if not qw:
        return 0.0
    hit = sum(1 for q in qw if any(_tok_match(q, c) for c in cw))
    recall = hit / len(qw)
    precision = hit / len(cw) if cw else 0
    s = 0.75 * recall + 0.25 * precision
    if qs and cs and qs != cs:
        s *= 0.6
    elif qs and not cs:
        s *= 0.85
    return round(s, 3)


# ---------- http ----------

def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "es-UY,es;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def parse_price(s):
    s = re.sub(r"[^\d.,]", "", s or "")
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".") if "," in s else s.replace(".", "")
    return float(s)


def ld_products(page):
    out = []
    for m in re.finditer(r'<script[^>]*application/ld(?:\+|&#x2B;)json[^>]*>(.*?)</script>', page, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        for o in d if isinstance(d, list) else [d]:
            if isinstance(o, dict) and o.get("@type") == "Product":
                out.append(o)
    return out


# ---------- Tienda Inglesa ----------

def _ti_cards(page):
    dec = json.JSONDecoder()
    for m in re.finditer(r'"W\w*vPRODUCTUI":(\{)', page):
        try:
            o, _ = dec.raw_decode(page, m.start(1))
        except ValueError:
            continue
        info = o.get("Info") or {}
        if not info.get("Id"):
            continue
        prices = {p.get("PriceDisplayType"): p for p in o.get("Prices") or []}
        std = prices.get("STD") or next(iter(prices.values()), None)
        notes = []
        if "ST" in prices:
            notes.append(f"antes $ {_ti_amount(prices['ST']):g}")
        special = _ti_amount(prices["SPECIAL"]) if "SPECIAL" in prices else None
        if special is not None:
            notes.append(f"ClubCard $ {special:g}")
        yield {
            "store": "tiendainglesa", "pid": str(info["Id"]), "name": html.unescape(info.get("Name", "")),
            "url": urllib.parse.urljoin(TI, info.get("Uri", "")), "image": (info.get("ImageUrl") or "").split(",")[0],
            "price": _ti_amount(std) if std else None, "special": special, "note": " · ".join(notes),
        }


def _ti_amount(p):
    # según la página, el "$ 135" viene en PriceStr o en Label
    return next((parse_price(v) for v in (p.get("PriceStr"), p.get("Label")) if v and re.search(r"\d", v)), None)


def ti_search(q):
    url = f"{TI}/supermercado/busqueda?0,0,{urllib.parse.quote(q)},0"
    seen, out = set(), []
    for c in _ti_cards(fetch(url)):
        if c["pid"] not in seen:
            seen.add(c["pid"])
            out.append(c)
    return out


def ti_product(pid, url):
    page = fetch(url)
    for c in _ti_cards(page):
        if c["pid"] == pid:
            return c
    for p in ld_products(page):
        offers = p.get("offers") or {}
        offers = offers[0] if isinstance(offers, list) else offers
        return {"store": "tiendainglesa", "pid": pid, "name": p.get("name"), "url": url,
                "price": parse_price(str(offers.get("price", ""))), "note": ""}
    return None


# ---------- Disco / Devoto (GDU) ----------
# El buscador es Blazor (websocket): no hay HTML con resultados. Se indexa el sitemap
# (slug + id de todos los productos) y el precio sale del JSON-LD de cada página de producto.
# Disco y Devoto son la misma plataforma y comparten los ids de producto.

_gdu_index = {s: [] for s in GDU}  # store -> [(pid, slug, palabras)]


def gdu_load_catalog(store, force=False):
    base = GDU[store]
    with db() as con:
        row = con.execute("SELECT value FROM meta WHERE key=?", (f"{store}_sitemap_at",)).fetchone()
        empty = not con.execute("SELECT 1 FROM gdu_catalog WHERE store=? LIMIT 1", (store,)).fetchone()
        stale = force or empty or not row or time.time() - float(row["value"]) > 86400
        if stale:
            rows = []
            for i in (1, 2, 3):
                try:
                    xml = fetch(f"{base}/sitemap-{i}.xml", timeout=60)
                except Exception:
                    break
                rows += re.findall(r"<loc>" + re.escape(base) + r"/product/([^<]+)/(\d+)</loc>", xml)
            if rows:
                con.execute("DELETE FROM gdu_catalog WHERE store=?", (store,))
                con.executemany("INSERT OR REPLACE INTO gdu_catalog(store, pid, slug) VALUES (?, ?, ?)",
                                [(store, pid, slug) for slug, pid in rows])
                con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"{store}_sitemap_at", str(time.time())))
        if stale or not _gdu_index[store]:
            _gdu_index[store] = [(r["pid"], r["slug"], analyze(r["slug"].replace("-", " "))[0])
                                 for r in con.execute("SELECT pid, slug FROM gdu_catalog WHERE store=?", (store,))]


def gdu_product(store, pid, slug_or_url):
    url = slug_or_url if slug_or_url.startswith("http") else f"{GDU[store]}/product/{slug_or_url}/{pid}"
    for p in ld_products(fetch(url)):
        offers = p.get("offers") or {}
        offers = offers[0] if isinstance(offers, list) else offers
        price = offers.get("price")
        avail = (offers.get("availability") or "").rsplit("/", 1)[-1]
        img = p.get("image")
        return {"store": store, "pid": pid, "name": p.get("name"), "url": url,
                "image": img[0] if isinstance(img, list) else img,
                "price": float(price) if price not in (None, "") else None,
                "note": "" if avail in ("", "InStock") else avail, "gtin": p.get("gtin")}
    return None


def gdu_search(store, q, limit=15):
    if not _gdu_index[store]:
        gdu_load_catalog(store)
    qw, _ = analyze(q)
    if not qw:
        return []
    # prefiltro barato sobre el slug, después se trae la página real de los mejores
    cands = []
    for pid, slug, words in _gdu_index[store]:
        hit = sum(1 for w in qw if any(_tok_match(w, c) for c in words))
        if hit:
            cands.append((hit / len(qw) + 0.1 * hit / max(len(words), 1), pid, slug))
    cands.sort(reverse=True)
    res = FETCH_POOL.map(lambda c: _safe(gdu_product, store, c[1], c[2]), cands[:limit])
    return [r for r in res if r]


def product(store, pid, url):
    return ti_product(pid, url) if store == "tiendainglesa" else gdu_product(store, pid, url)


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        print("error", fn.__name__, a, e)
        return None


def search(q):
    fut = {s: POOL.submit(ti_search, q) if s == "tiendainglesa" else POOL.submit(gdu_search, s, q) for s in STORES}
    out, errors = {}, {}
    for store, f in fut.items():
        try:
            rows = f.result()
        except Exception as e:
            rows, errors[store] = [], str(e)
        for r in rows:
            r["score"] = score(q, r["name"] or "")
        rows.sort(key=lambda r: -r["score"])
        out[store] = rows[:20]
    return {"results": out, "errors": errors}


def sync_gdu_links(item_ids=None):
    """Completa en los otros súper GDU los artículos que tienen un producto de uno de ellos (mismo id)."""
    with db() as con:
        links = [dict(r) for r in con.execute(
            f"SELECT * FROM links WHERE store IN ({','.join('?' * len(GDU))})", list(GDU))]
        have = {(l["item_id"], l["store"], l["store_pid"]) for l in links}
        slugs = {(r["store"], r["pid"]): r["slug"] for r in con.execute("SELECT * FROM gdu_catalog")}
    todo = {(l["item_id"], s, l["store_pid"]) for l in links for s in GDU
            if (item_ids is None or l["item_id"] in item_ids)
            and (l["item_id"], s, l["store_pid"]) not in have and (s, l["store_pid"]) in slugs}
    res = FETCH_POOL.map(lambda t: (t, _safe(gdu_product, t[1], t[2], slugs[(t[1], t[2])])), todo)
    added = 0
    with db() as con:
        for (item_id, store, pid), p in res:
            if not p:
                continue
            cur = con.execute(
                "INSERT OR IGNORE INTO links(item_id, store, store_pid, name, url, image) VALUES (?,?,?,?,?,?)",
                (item_id, store, pid, p["name"], p["url"], p.get("image")))
            if cur.rowcount and p.get("price") is not None:
                con.execute("INSERT INTO prices(link_id, price, note) VALUES (?,?,?)",
                            (cur.lastrowid, p["price"], p.get("note", "")))
                added += 1
    return added


# ---------- items ----------

def save_item(name, products):
    with db() as con:
        cur = con.execute("INSERT INTO items(name) VALUES (?)", (name,))
        item_id = cur.lastrowid
        for p in products:
            cur = con.execute(
                "INSERT OR IGNORE INTO links(item_id, store, store_pid, name, url, image) VALUES (?,?,?,?,?,?)",
                (item_id, p["store"], p["pid"], p.get("name"), p.get("url"), p.get("image")))
            if cur.rowcount and p.get("price") is not None:
                con.execute("INSERT INTO prices(link_id, price, special, note) VALUES (?,?,?,?)",
                            (cur.lastrowid, p["price"], p.get("special"), p.get("note", "")))
    return item_id


def list_items(q=""):
    with db() as con:
        items = [dict(r) for r in con.execute("SELECT * FROM items ORDER BY name")]
        links = con.execute("""
            SELECT l.*, p.price, p.special, p.note, p.fetched_at FROM links l
            LEFT JOIN prices p ON p.id = (SELECT id FROM prices WHERE link_id = l.id ORDER BY id DESC LIMIT 1)
        """).fetchall()
    by_item = {}
    for l in links:
        by_item.setdefault(l["item_id"], []).append(dict(l))
    qa = analyze(q) if q.strip() else None
    out = []
    for it in items:
        it["links"] = by_item.get(it["id"], [])
        if qa:
            it["score"] = max([score(qa, it["name"])] + [score(qa, l["name"] or "") for l in it["links"]])
            if it["score"] < 0.4:
                continue
        prices = [l["price"] for l in it["links"] if l["price"] is not None]
        it["best"] = min(prices) if prices else None
        out.append(it)
    if qa:
        out.sort(key=lambda i: -i["score"])
    return out


def refresh_items(item_ids):
    if not item_ids:
        return
    with db() as con:
        links = [dict(r) for r in con.execute(
            f"SELECT * FROM links WHERE item_id IN ({','.join('?' * len(item_ids))})", list(item_ids))]

    def one(l):
        return l, _safe(product, l["store"], l["store_pid"], l["url"])

    with db() as con:
        for l, p in FETCH_POOL.map(one, links):
            if p and p.get("price") is not None:
                con.execute("INSERT INTO prices(link_id, price, special, note) VALUES (?,?,?,?)",
                            (l["id"], p["price"], p.get("special"), p.get("note", "")))


# ---------- listas de compras ----------

def get_lists():
    with db() as con:
        return [dict(r) for r in con.execute("""
            SELECT l.*, COUNT(e.item_id) AS n FROM lists l
            LEFT JOIN list_entries e ON e.list_id = l.id GROUP BY l.id ORDER BY l.name""")]


def create_list(name):
    with db() as con:
        return con.execute("INSERT INTO lists(name) VALUES (?)", (name,)).lastrowid


def delete_list(list_id):
    with db() as con:
        con.execute("DELETE FROM lists WHERE id=?", (list_id,))


def get_list(list_id):
    with db() as con:
        l = con.execute("SELECT * FROM lists WHERE id=?", (list_id,)).fetchone()
        if not l:
            return None
        entries = [dict(r) for r in con.execute("""
            SELECT e.item_id, e.qty, i.name FROM list_entries e JOIN items i ON i.id = e.item_id
            WHERE e.list_id=? ORDER BY i.name""", (list_id,))]
    return {**dict(l), "entries": entries}


def set_entry(list_id, item_id, qty):
    with db() as con:
        if qty > 0:
            con.execute("""INSERT INTO list_entries(list_id, item_id, qty) VALUES (?,?,?)
                ON CONFLICT(list_id, item_id) DO UPDATE SET qty=excluded.qty""", (list_id, item_id, qty))
        else:
            con.execute("DELETE FROM list_entries WHERE list_id=? AND item_id=?", (list_id, item_id))


def store_prices(item_ids, clubcard):
    """-> {item_id: {store: oferta más barata entre los productos vinculados de ese súper}}"""
    with db() as con:
        rows = con.execute(f"""
            SELECT l.item_id, l.store, l.name, l.url, p.price, p.special, p.fetched_at FROM links l
            JOIN prices p ON p.id = (SELECT id FROM prices WHERE link_id = l.id ORDER BY id DESC LIMIT 1)
            WHERE l.item_id IN ({','.join('?' * len(item_ids))})""", list(item_ids)).fetchall()
    out = {}
    for r in rows:
        if r["price"] is None:
            continue
        eff = min(r["price"], r["special"]) if clubcard and r["special"] else r["price"]
        cur = out.setdefault(r["item_id"], {}).get(r["store"])
        if not cur or eff < cur["price"]:
            out[r["item_id"]][r["store"]] = {"price": eff, "name": r["name"], "url": r["url"],
                                             "clubcard": eff != r["price"], "fetched_at": r["fetched_at"]}
    return out


def compute_list(list_id, refresh=True, clubcard=False, store_cost=0.0):
    lst = get_list(list_id)
    if not lst:
        return None
    entries = lst["entries"]
    ids = [e["item_id"] for e in entries]
    if refresh:
        refresh_items(ids)
    prices = store_prices(ids, clubcard) if ids else {}
    stores = list(STORES)

    def plan(subset):
        lines, missing, goods = [], [], 0.0
        for e in entries:
            offers = {s: o for s, o in prices.get(e["item_id"], {}).items() if s in subset}
            if not offers:
                missing.append(e["name"])
                continue
            s = min(offers, key=lambda k: offers[k]["price"])
            sub = offers[s]["price"] * e["qty"]
            goods += sub
            lines.append({**e, "store": s, "unit": offers[s]["price"], "subtotal": round(sub, 2),
                          "product": offers[s]["name"], "url": offers[s]["url"], "clubcard": offers[s]["clubcard"]})
        used = sorted({l["store"] for l in lines})
        return {"stores": used, "lines": lines, "missing": missing, "goods": round(goods, 2),
                "total": round(goods + store_cost * len(used), 2)}

    # con pocos súper alcanza con probar todas las combinaciones
    plans = [plan(set(c)) for n in range(1, len(stores) + 1) for c in combinations(stores, n)]
    best = min(plans, key=lambda p: (len(p["missing"]), p["total"]))
    per_store = {s: plan({s}) for s in stores}
    common = [e for e in entries if all(s in prices.get(e["item_id"], {}) for s in stores)]
    for s, p in per_store.items():
        p["common_total"] = round(sum(prices[e["item_id"]][s]["price"] * e["qty"] for e in common), 2)
    comparable = [p for p in per_store.values() if len(p["missing"]) == len(best["missing"])]
    best_single = min(comparable, key=lambda p: p["total"]) if comparable else None
    return {
        "list": lst, "store_cost": store_cost, "clubcard": clubcard, "common_count": len(common),
        "per_store": per_store, "best": best,
        "best_single": best_single["stores"][0] if best_single and best_single["stores"] else None,
        "savings": round(best_single["total"] - best["total"], 2) if best_single else None,
        "detail": {e["item_id"]: prices.get(e["item_id"], {}) for e in entries},
    }


def delete_item(item_id):
    with db() as con:
        con.execute("DELETE FROM items WHERE id=?", (item_id,))


def price_history(item_id):
    with db() as con:
        return [dict(r) for r in con.execute("""
            SELECT l.store, l.name, p.price, p.note, p.fetched_at FROM prices p
            JOIN links l ON l.id = p.link_id WHERE l.item_id=? ORDER BY p.fetched_at DESC""", (item_id,))]


# ---------- web ----------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        q = (qs.get("q") or [""])[0]
        try:
            if u.path == "/":
                return self._send(200, (BASE / "index.html").read_bytes(), "text/html")
            if u.path == "/api/search":
                return self._send(200, search(q))
            if u.path == "/api/items":
                return self._send(200, list_items(q))
            m = re.fullmatch(r"/api/items/(\d+)/history", u.path)
            if m:
                return self._send(200, price_history(int(m.group(1))))
            if u.path == "/api/lists":
                return self._send(200, get_lists())
            m = re.fullmatch(r"/api/lists/(\d+)", u.path)
            if m:
                lst = get_list(int(m.group(1)))
                return self._send(200, lst) if lst else self._send(404, {"error": "no existe la lista"})
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        try:
            if self.path == "/api/items":
                b = self._body()
                if not b.get("name") or not b.get("products"):
                    return self._send(400, {"error": "falta nombre o productos"})
                item_id = save_item(b["name"].strip(), b["products"])
                return self._send(200, {"id": item_id, "auto_added": sync_gdu_links({item_id})})
            if self.path == "/api/items/refresh":
                with db() as con:
                    refresh_items([r["id"] for r in con.execute("SELECT id FROM items")])
                return self._send(200, {"ok": True})
            m = re.fullmatch(r"/api/items/(\d+)/refresh", self.path)
            if m:
                refresh_items([int(m.group(1))])
                return self._send(200, {"ok": True})
            if self.path == "/api/lists":
                name = (self._body().get("name") or "").strip()
                if not name:
                    return self._send(400, {"error": "falta el nombre"})
                return self._send(200, {"id": create_list(name)})
            m = re.fullmatch(r"/api/lists/(\d+)/entries", self.path)
            if m:
                b = self._body()
                set_entry(int(m.group(1)), int(b["item_id"]), float(b.get("qty", 1)))
                return self._send(200, get_list(int(m.group(1))))
            m = re.fullmatch(r"/api/lists/(\d+)/compute", self.path)
            if m:
                b = self._body()
                r = compute_list(int(m.group(1)), refresh=bool(b.get("refresh", True)),
                                 clubcard=bool(b.get("clubcard")), store_cost=float(b.get("store_cost") or 0))
                return self._send(200, r) if r else self._send(404, {"error": "no existe la lista"})
            if self.path == "/api/catalog/reindex":
                for st in GDU:
                    gdu_load_catalog(st, force=True)
                return self._send(200, {st: len(ix) for st, ix in _gdu_index.items()})
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_DELETE(self):
        m = re.fullmatch(r"/api/(items|lists)/(\d+)", self.path)
        if not m:
            return self._send(404, {"error": "not found"})
        (delete_item if m.group(1) == "items" else delete_list)(int(m.group(2)))
        self._send(200, {"ok": True})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    init_db()
    print("indexando catálogos de " + ", ".join(STORES[s] for s in GDU) + "…", flush=True)
    list(POOL.map(gdu_load_catalog, GDU))
    print(", ".join(f"{STORES[s]}: {len(ix)} productos" for s, ix in _gdu_index.items()), flush=True)
    n = sync_gdu_links()
    if n:
        print(f"{n} artículos guardados completados con el mismo producto en otro súper", flush=True)
    print(f"http://localhost:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
