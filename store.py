"""Warehouse stock logic: database storage (SQLite or Postgres) + WhatsApp alert dispatch.

Pass a file path to Store() to use SQLite, or a postgres:// URL to use Postgres
(for hosting, e.g. Neon). Everything else in the app is identical for both.
"""
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timezone

try:  # only needed when a postgres:// URL is used
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    psycopg = None
    dict_row = None


class StoreError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


SCHEMA_SQLITE = [
    """CREATE TABLE IF NOT EXISTS products(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      sku TEXT NOT NULL DEFAULT '',
      cat TEXT NOT NULL DEFAULT '',
      unit TEXT NOT NULL DEFAULT 'pcs',
      min_qty INTEGER NOT NULL DEFAULT 10,
      qty INTEGER NOT NULL DEFAULT 0 CHECK(qty >= 0)
    )""",
    """CREATE TABLE IF NOT EXISTS movements(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      product_id INTEGER NOT NULL,
      name TEXT NOT NULL,
      type TEXT NOT NULL CHECK(type IN ('purchase','issue')),
      qty INTEGER NOT NULL,
      balance_after INTEGER NOT NULL,
      date TEXT NOT NULL,
      supplier TEXT NOT NULL DEFAULT '',
      note TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS settings(
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS alerts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at TEXT NOT NULL,
      message TEXT NOT NULL,
      ok INTEGER NOT NULL,
      detail TEXT NOT NULL DEFAULT ''
    )""",
]

SCHEMA_PG = [
    s.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY").replace(
        "product_id INTEGER", "product_id BIGINT"
    )
    for s in SCHEMA_SQLITE
]

PHONE_RE = re.compile(r"^\+\d{8,15}$")
APIKEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status(qty, min_qty):
    if qty <= 0:
        return "out"
    if qty <= min_qty:
        return "low"
    return "in"


def _text(value, field, maxlen, required=False):
    value = (value or "").strip()
    if required and not value:
        raise StoreError(f"{field} is required")
    if len(value) > maxlen:
        raise StoreError(f"{field} is too long (max {maxlen} characters)")
    return value


def _int(value, field, lo, hi):
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise StoreError(f"{field} must be a whole number")
    if not lo <= value <= hi:
        raise StoreError(f"{field} must be between {lo} and {hi}")
    return value


def _date(value):
    if not value:
        return date.today().isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise StoreError("Date must look like 2026-11-09")


def build_message(name, qty, unit, state):
    if state == "out":
        line = f"{name} is OUT OF STOCK"
    else:
        line = f"{name} is running low: {qty} {unit} left"
    return f"⚠ Warehouse stock alert\n{line}.\nPlease reorder."


def send_whatsapp(phone, apikey, text, timeout=15):
    """Send a WhatsApp message through the free CallMeBot API. Returns (ok, detail)."""
    query = urllib.parse.urlencode(
        {"phone": phone, "text": text, "apikey": apikey}, quote_via=urllib.parse.quote
    )
    url = "https://api.callmebot.com/whatsapp.php?" + query
    req = urllib.request.Request(url, headers={"User-Agent": "warehouse-stock-manager"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(2000).decode("utf-8", "replace")
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"\s+", " ", body).strip()[:200]
            return 200 <= resp.status < 300, body or f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"Network error: {e.reason}"


class _Conn:
    """Thin wrapper so the Store code is the same for SQLite and Postgres."""

    def __init__(self, raw, pg):
        self.raw = raw
        self.pg = pg

    def _sql(self, sql):
        # Postgres drivers use %s placeholders; SQLite uses ?
        return sql.replace("?", "%s") if self.pg else sql

    def execute(self, sql, params=()):
        return self.raw.execute(self._sql(sql), params)

    def insert(self, sql, params=()):
        """Run an INSERT and return the new row's id."""
        if self.pg:
            row = self.raw.execute(self._sql(sql) + " RETURNING id", params).fetchone()
            return row["id"]
        return self.raw.execute(sql, params).lastrowid


class Store:
    def __init__(self, target, sender=None):
        self.target = target
        self.pg = str(target).startswith(("postgres://", "postgresql://"))
        if self.pg and psycopg is None:
            raise RuntimeError(
                "DATABASE_URL points to Postgres but the 'psycopg' package is not installed. "
                "Run: pip install -r requirements.txt"
            )
        self.sender = sender or send_whatsapp
        with self._tx() as c:
            for statement in (SCHEMA_PG if self.pg else SCHEMA_SQLITE):
                c.execute(statement)
        if not self.pg:
            conn = sqlite3.connect(self.target, timeout=10)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            finally:
                conn.close()

    @contextmanager
    def _tx(self):
        if self.pg:
            # A fresh connection per request keeps this working with Neon, which pauses
            # idle databases. prepare_threshold=None also makes it safe behind a pooler.
            raw = psycopg.connect(
                self.target, row_factory=dict_row, connect_timeout=30, prepare_threshold=None
            )
            try:
                yield _Conn(raw, True)
                raw.commit()
            except BaseException:
                raw.rollback()
                raise
            finally:
                raw.close()
        else:
            raw = sqlite3.connect(self.target, timeout=10, isolation_level=None)
            raw.row_factory = sqlite3.Row
            try:
                raw.execute("BEGIN IMMEDIATE")
                yield _Conn(raw, False)
                raw.execute("COMMIT")
            except BaseException:
                if raw.in_transaction:
                    raw.execute("ROLLBACK")
                raise
            finally:
                raw.close()

    # ---------------- products ----------------
    @staticmethod
    def _product(row, last_purchase=None):
        return {
            "id": row["id"],
            "name": row["name"],
            "sku": row["sku"],
            "cat": row["cat"],
            "unit": row["unit"],
            "min": row["min_qty"],
            "qty": row["qty"],
            "status": status(row["qty"], row["min_qty"]),
            "last_purchase": last_purchase,
        }

    def list_products(self):
        with self._tx() as c:
            rows = c.execute("SELECT * FROM products ORDER BY LOWER(name), id").fetchall()
            last = {
                r["product_id"]: r["d"]
                for r in c.execute(
                    "SELECT product_id, MAX(date) AS d FROM movements "
                    "WHERE type='purchase' GROUP BY product_id"
                ).fetchall()
            }
        return [self._product(r, last.get(r["id"])) for r in rows]

    def _get_product(self, c, pid, lock=False):
        sql = "SELECT * FROM products WHERE id=?"
        if lock and c.pg:
            sql += " FOR UPDATE"  # SQLite already locks the whole database in BEGIN IMMEDIATE
        row = c.execute(sql, (pid,)).fetchone()
        if not row:
            raise StoreError("Product not found", 404)
        return row

    def create_product(self, name, sku="", cat="", unit="pcs", min_qty=10, qty=0, when=None):
        name = _text(name, "Product name", 80, required=True)
        sku = _text(sku, "SKU", 40)
        cat = _text(cat, "Category", 40)
        unit = _text(unit, "Unit", 12) or "pcs"
        min_qty = _int(min_qty, "Low-stock level", 0, 1_000_000)
        qty = _int(qty, "Opening quantity", 0, 1_000_000)
        when = _date(when)
        with self._tx() as c:
            pid = c.insert(
                "INSERT INTO products(name, sku, cat, unit, min_qty, qty) VALUES(?,?,?,?,?,?)",
                (name, sku, cat, unit, min_qty, qty),
            )
            if qty > 0:
                c.execute(
                    "INSERT INTO movements(product_id, name, type, qty, balance_after, date, "
                    "supplier, note, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (pid, name, "purchase", qty, qty, when, "", "Opening stock", now_iso()),
                )
            row = self._get_product(c, pid)
        return self._product(row, when if qty > 0 else None)

    def update_product(self, pid, name, sku="", cat="", unit="pcs", min_qty=10):
        name = _text(name, "Product name", 80, required=True)
        sku = _text(sku, "SKU", 40)
        cat = _text(cat, "Category", 40)
        unit = _text(unit, "Unit", 12) or "pcs"
        min_qty = _int(min_qty, "Low-stock level", 0, 1_000_000)
        with self._tx() as c:
            self._get_product(c, pid, lock=True)
            c.execute(
                "UPDATE products SET name=?, sku=?, cat=?, unit=?, min_qty=? WHERE id=?",
                (name, sku, cat, unit, min_qty, pid),
            )
            c.execute("UPDATE movements SET name=? WHERE product_id=?", (name, pid))
            row = self._get_product(c, pid)
        return self._product(row)

    def delete_product(self, pid):
        with self._tx() as c:
            self._get_product(c, pid, lock=True)
            c.execute("DELETE FROM movements WHERE product_id=?", (pid,))
            c.execute("DELETE FROM products WHERE id=?", (pid,))

    # ---------------- stock movements ----------------
    def move(self, pid, mtype, qty, when=None, supplier="", note=""):
        """Record a purchase (stock in) or issue (stock out).

        Returns {"product": {...}, "alert_message": str | None}. alert_message is set when
        this movement made the product run out (or run low, if low alerts are enabled).
        """
        if mtype not in ("purchase", "issue"):
            raise StoreError("Invalid movement type")
        qty = _int(qty, "Quantity", 1, 1_000_000)
        when = _date(when)
        supplier = _text(supplier, "Supplier", 80)
        note = _text(note, "Note", 120)
        with self._tx() as c:
            p = self._get_product(c, pid, lock=True)
            before = status(p["qty"], p["min_qty"])
            new_qty = p["qty"] + qty if mtype == "purchase" else p["qty"] - qty
            if new_qty < 0:
                raise StoreError(f"Only {p['qty']} {p['unit']} in stock")
            c.execute("UPDATE products SET qty=? WHERE id=?", (new_qty, pid))
            c.execute(
                "INSERT INTO movements(product_id, name, type, qty, balance_after, date, "
                "supplier, note, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (pid, p["name"], mtype, qty, new_qty, when, supplier, note, now_iso()),
            )
            low_enabled = self._setting(c, "low") == "1"
            row = self._get_product(c, pid)
        after = status(new_qty, p["min_qty"])
        message = None
        if mtype == "issue" and after != before:
            if after == "out" or (after == "low" and low_enabled):
                message = build_message(p["name"], new_qty, p["unit"], after)
        return {"product": self._product(row), "alert_message": message}

    def list_log(self, limit=500):
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM movements ORDER BY date DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "id": r["id"],
                "product_id": r["product_id"],
                "name": r["name"],
                "type": r["type"],
                "qty": r["qty"],
                "after": r["balance_after"],
                "date": r["date"],
                "supplier": r["supplier"],
                "note": r["note"],
            }
            for r in rows
        ]

    def export_all(self):
        """Everything worth backing up. The WhatsApp API key is deliberately left out."""
        settings = self.get_settings()
        return {
            "exported_at": now_iso(),
            "products": self.list_products(),
            "log": self.list_log(limit=1_000_000),
            "settings": {"phone": settings["phone"], "low": settings["low"]},
        }

    # ---------------- settings ----------------
    @staticmethod
    def _setting(c, key, default=""):
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    @staticmethod
    def _put_setting(c, key, value):
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_settings(self):
        """Settings for the UI. The API key is never sent back, only whether it is set."""
        with self._tx() as c:
            return {
                "phone": self._setting(c, "phone"),
                "low": self._setting(c, "low") == "1",
                "apikey_set": bool(self._setting(c, "apikey")),
            }

    def update_settings(self, phone, apikey=None, low=False):
        phone = re.sub(r"[\s\-()]", "", phone or "")
        if phone and not PHONE_RE.match(phone):
            raise StoreError("Enter the mobile number with country code, like +919876543210")
        if apikey is not None:
            apikey = apikey.strip()
            if apikey and not APIKEY_RE.match(apikey):
                raise StoreError("That API key does not look right")
        with self._tx() as c:
            self._put_setting(c, "phone", phone)
            self._put_setting(c, "low", "1" if low else "0")
            if apikey:  # blank = keep the saved key
                self._put_setting(c, "apikey", apikey)
        return self.get_settings()

    # ---------------- alerts ----------------
    def alerts_configured(self):
        with self._tx() as c:
            return bool(self._setting(c, "phone") and self._setting(c, "apikey"))

    def _log_alert(self, message, ok, detail):
        with self._tx() as c:
            c.execute(
                "INSERT INTO alerts(created_at, message, ok, detail) VALUES(?,?,?,?)",
                (now_iso(), message, 1 if ok else 0, detail[:300]),
            )
            # keep the log small
            c.execute(
                "DELETE FROM alerts WHERE id NOT IN (SELECT id FROM alerts ORDER BY id DESC LIMIT 200)"
            )

    def dispatch_alert(self, message):
        """Send the message to the saved number and record the result. Safe to call in a background task."""
        with self._tx() as c:
            phone = self._setting(c, "phone")
            apikey = self._setting(c, "apikey")
        if not phone or not apikey:
            self._log_alert(message, False, "Alert number or API key not set")
            return False
        try:
            ok, detail = self.sender(phone, apikey, message)
        except Exception as e:  # never let an alert failure break stock updates
            ok, detail = False, f"Error: {type(e).__name__}"
        detail = str(detail).replace(apikey, "***").replace(phone, "***")
        self._log_alert(message, ok, detail)
        return ok

    def send_test(self):
        message = "✅ Test alert from your Warehouse Stock Manager. Alerts are working."
        ok = self.dispatch_alert(message)
        last = self.list_alerts(1)
        return {"ok": ok, "detail": last[0]["detail"] if last else ""}

    def list_alerts(self, limit=50):
        with self._tx() as c:
            rows = c.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [
            {
                "id": r["id"],
                "time": r["created_at"],
                "message": r["message"],
                "ok": bool(r["ok"]),
                "detail": r["detail"],
            }
            for r in rows
        ]
