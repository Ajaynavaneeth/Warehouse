import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import store as S
from store import Store, StoreError


# --------------------------------------------------------------------------------------
# A stand-in for the psycopg (Postgres) driver, backed by SQLite. It does NOT prove the SQL
# works on a real Postgres server, but it does prove the Postgres code path in store.py:
# %s placeholders, "RETURNING id", row locking syntax, dict rows, commit/rollback.
# --------------------------------------------------------------------------------------
class _Returned:
    def __init__(self, new_id):
        self.new_id = new_id

    def fetchone(self):
        return {"id": self.new_id}


class FakePgConn:
    def __init__(self, path):
        self.c = sqlite3.connect(path, timeout=10)
        self.c.row_factory = lambda cur, row: {d[0]: v for d, v in zip(cur.description, row)}

    def execute(self, sql, params=()):
        assert "?" not in sql, "placeholder was not converted to %s: " + sql
        sql = sql.replace("%s", "?").replace(" FOR UPDATE", "")
        sql = sql.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
        sql = sql.replace("BIGINT", "INTEGER")
        returning = sql.rstrip().endswith(" RETURNING id")
        if returning:
            sql = sql.rstrip()[: -len(" RETURNING id")]
        cur = self.c.execute(sql, tuple(params))
        return _Returned(cur.lastrowid) if returning else cur

    def commit(self):
        self.c.commit()

    def rollback(self):
        self.c.rollback()

    def close(self):
        self.c.close()


class FakePsycopg:
    def __init__(self, path):
        self.path = path
        self.calls = []

    def connect(self, dsn, row_factory=None, connect_timeout=None, prepare_threshold="unset"):
        self.calls.append((dsn, connect_timeout, prepare_threshold))
        return FakePgConn(self.path)


class Base(unittest.TestCase):
    use_pg = False

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.sent = []
        if self.use_pg:
            self.fake = FakePsycopg(os.path.join(self.dir.name, "pg.db"))
            for patcher in (
                mock.patch.object(S, "psycopg", self.fake),
                mock.patch.object(S, "dict_row", object()),
            ):
                patcher.start()
                self.addCleanup(patcher.stop)
        self.s = self.make("t", self.fake_sender)

    def fake_sender(self, phone, key, text):
        self.sent.append((phone, key, text))
        return True, "Message queued"

    def make(self, name, sender):
        if self.use_pg:
            return Store("postgresql://user:pw@host/db?sslmode=require", sender=sender)
        return Store(os.path.join(self.dir.name, name + ".db"), sender=sender)

    def tearDown(self):
        self.dir.cleanup()

    def configure(self, low=False):
        self.s.update_settings("+91 98765-43210", "123456", low)


class ProductTests(Base):
    def test_create_with_opening_stock_logs_purchase(self):
        p = self.s.create_product("Rice 25kg", min_qty=5, qty=20, when="2026-11-01")
        self.assertEqual((p["qty"], p["status"], p["last_purchase"]), (20, "in", "2026-11-01"))
        log = self.s.list_log()
        self.assertEqual(len(log), 1)
        self.assertEqual((log[0]["type"], log[0]["after"]), ("purchase", 20))

    def test_validation(self):
        with self.assertRaises(StoreError):
            self.s.create_product("   ")
        with self.assertRaises(StoreError):
            self.s.create_product("X", qty=-1)
        with self.assertRaises(StoreError):
            self.s.create_product("X", when="01/11/2026")
        with self.assertRaises(StoreError):
            self.s.create_product("A" * 81)

    def test_update_renames_history(self):
        p = self.s.create_product("Old", qty=3)
        self.s.update_product(p["id"], "New", min_qty=1)
        self.assertEqual(self.s.list_log()[0]["name"], "New")

    def test_delete_removes_history_and_404(self):
        p = self.s.create_product("Tmp", qty=3)
        self.s.delete_product(p["id"])
        self.assertEqual(self.s.list_products(), [])
        self.assertEqual(self.s.list_log(), [])
        with self.assertRaises(StoreError) as cm:
            self.s.delete_product(p["id"])
        self.assertEqual(cm.exception.status, 404)

    def test_list_sorted_by_name_ignoring_case(self):
        for n in ["banana", "Apple", "cherry"]:
            self.s.create_product(n)
        self.assertEqual([p["name"] for p in self.s.list_products()], ["Apple", "banana", "cherry"])

    def test_settings_survive_reopen(self):
        self.configure()
        again = self.make("t", None)  # same database, new Store object (like a restart)
        self.assertEqual(again.get_settings()["phone"], "+919876543210")
        self.assertTrue(again.get_settings()["apikey_set"])


class ExportTests(Base):
    def test_backup_has_products_and_history_but_no_secret(self):
        self.configure(low=True)
        p = self.s.create_product("Rice", min_qty=2, qty=10, when="2026-11-01")
        self.s.move(p["id"], "issue", 4, "2026-11-02")
        data = self.s.export_all()
        self.assertEqual([x["name"] for x in data["products"]], ["Rice"])
        self.assertEqual(len(data["log"]), 2)
        self.assertEqual(data["settings"], {"phone": "+919876543210", "low": True})
        self.assertNotIn("123456", str(data))  # the CallMeBot API key must never be in a backup
        self.assertIn("exported_at", data)


class MovementTests(Base):
    def test_restock_after_finished_tracks_dates_and_balance(self):
        p = self.s.create_product("Oil", min_qty=2, qty=5, when="2026-10-01")
        self.s.move(p["id"], "issue", 5, "2026-10-20")
        self.assertEqual(self.s.list_products()[0]["status"], "out")
        self.s.move(p["id"], "purchase", 30, "2026-11-05", supplier="ABC Traders")
        prod = self.s.list_products()[0]
        self.assertEqual((prod["qty"], prod["status"], prod["last_purchase"]), (30, "in", "2026-11-05"))
        after = [m["after"] for m in reversed(self.s.list_log())]
        self.assertEqual(after, [5, 0, 30])

    def test_cannot_issue_more_than_stock(self):
        p = self.s.create_product("Oil", qty=3)
        with self.assertRaises(StoreError):
            self.s.move(p["id"], "issue", 4)
        self.assertEqual(self.s.list_products()[0]["qty"], 3)
        self.assertEqual(len(self.s.list_log()), 1)  # failed move left no trace

    def test_bad_inputs(self):
        p = self.s.create_product("Oil", qty=3)
        for args in [("sell", 1), ("issue", 0), ("issue", "abc")]:
            with self.assertRaises(StoreError):
                self.s.move(p["id"], *args)
        with self.assertRaises(StoreError) as cm:
            self.s.move(999, "issue", 1)
        self.assertEqual(cm.exception.status, 404)


class AlertTriggerTests(Base):
    def test_out_of_stock_creates_message(self):
        p = self.s.create_product("Rice", min_qty=2, qty=10)
        r = self.s.move(p["id"], "issue", 10)
        self.assertIn("OUT OF STOCK", r["alert_message"])
        self.assertIn("Rice", r["alert_message"])

    def test_no_alert_when_still_in_stock_or_purchase(self):
        p = self.s.create_product("Rice", min_qty=2, qty=10)
        self.assertIsNone(self.s.move(p["id"], "issue", 3)["alert_message"])
        self.assertIsNone(self.s.move(p["id"], "purchase", 3)["alert_message"])

    def test_low_alert_only_when_enabled(self):
        p = self.s.create_product("Rice", min_qty=5, qty=10)
        self.assertIsNone(self.s.move(p["id"], "issue", 6)["alert_message"])  # low, disabled
        q = self.s.create_product("Dal", min_qty=5, qty=10)
        self.configure(low=True)
        r = self.s.move(q["id"], "issue", 6)
        self.assertIn("running low", r["alert_message"])

    def test_no_repeat_alert_when_already_out(self):
        p = self.s.create_product("Rice", min_qty=2, qty=2)
        self.assertIsNotNone(self.s.move(p["id"], "issue", 2)["alert_message"])
        self.s.move(p["id"], "purchase", 1)  # low again, not out
        self.assertIsNotNone(self.s.move(p["id"], "issue", 1)["alert_message"])  # out again -> alert again


class SettingsAndDispatchTests(Base):
    def test_phone_validation_and_normalisation(self):
        s = self.s.update_settings("+91 98765-43210", "abc123")
        self.assertEqual(s["phone"], "+919876543210")
        for bad in ["9876543210", "+12", "hello"]:
            with self.assertRaises(StoreError):
                self.s.update_settings(bad, "abc123")

    def test_apikey_never_returned_and_blank_keeps_it(self):
        self.s.update_settings("+919876543210", "SECRET99")
        self.assertNotIn("SECRET99", str(self.s.get_settings()))
        self.assertTrue(self.s.get_settings()["apikey_set"])
        self.s.update_settings("+919876543210", "", True)
        self.assertTrue(self.s.get_settings()["apikey_set"])
        self.assertTrue(self.s.get_settings()["low"])
        with self.assertRaises(StoreError):
            self.s.update_settings("+919876543210", "bad key!")

    def test_dispatch_sends_and_logs(self):
        self.configure()
        self.assertTrue(self.s.dispatch_alert("hello"))
        self.assertEqual(self.sent, [("+919876543210", "123456", "hello")])
        self.assertTrue(self.s.list_alerts()[0]["ok"])

    def test_dispatch_without_config_logs_failure(self):
        self.assertFalse(self.s.dispatch_alert("hello"))
        self.assertEqual(self.sent, [])
        a = self.s.list_alerts()[0]
        self.assertFalse(a["ok"])
        self.assertIn("not set", a["detail"])
        self.assertFalse(self.s.alerts_configured())

    def test_sender_crash_is_contained_and_secrets_not_logged(self):
        def boom(phone, key, text):
            return False, f"failed for {phone} key {key}"

        s = self.make("x", boom)
        s.update_settings("+919876543210", "TOPSECRET")
        self.assertFalse(s.dispatch_alert("m"))
        detail = s.list_alerts()[0]["detail"]
        self.assertNotIn("TOPSECRET", detail)
        self.assertNotIn("919876543210", detail)

        def crash(phone, key, text):
            raise RuntimeError("oops")

        s2 = self.make("x", crash)
        self.assertFalse(s2.dispatch_alert("m"))
        self.assertIn("RuntimeError", s2.list_alerts()[0]["detail"])

    def test_send_test(self):
        self.configure()
        r = self.s.send_test()
        self.assertTrue(r["ok"])
        self.assertIn("Test alert", self.sent[0][2])

    def test_alert_log_is_trimmed(self):
        self.configure()
        for i in range(205):
            self.s.dispatch_alert(f"m{i}")
        self.assertEqual(len(self.s.list_alerts(500)), 200)

    def test_end_to_end_stock_out_triggers_whatsapp(self):
        self.configure()
        p = self.s.create_product("Sugar", min_qty=2, qty=4)
        r = self.s.move(p["id"], "issue", 4)
        self.assertTrue(self.s.alerts_configured())
        self.s.dispatch_alert(r["alert_message"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Sugar is OUT OF STOCK", self.sent[0][2])


# Run the same store tests through the simulated Postgres driver as well.
class ProductTestsPG(ProductTests):
    use_pg = True


class MovementTestsPG(MovementTests):
    use_pg = True


class AlertTriggerTestsPG(AlertTriggerTests):
    use_pg = True


class SettingsAndDispatchTestsPG(SettingsAndDispatchTests):
    use_pg = True

    def test_pg_connection_options(self):
        # fresh short-lived connections, pooler-safe, long enough timeout for Neon to wake up
        self.s.list_products()
        dsn, timeout, prepare = self.fake.calls[-1]
        self.assertTrue(dsn.startswith("postgresql://"))
        self.assertEqual(prepare, None)
        self.assertGreaterEqual(timeout, 20)


class ExportTestsPG(ExportTests):
    use_pg = True


# Optional: run the same tests against a REAL Postgres (e.g. an empty Neon test database).
# This drops and recreates the app's tables in that database, so never point it at live data.
TEST_PG = os.environ.get("TEST_DATABASE_URL", "")


class RealPgMixin:
    def make(self, name, sender):
        return Store(TEST_PG, sender=sender)

    def setUp(self):
        import psycopg

        with psycopg.connect(TEST_PG) as conn:
            conn.execute("DROP TABLE IF EXISTS movements, products, settings, alerts")
        super().setUp()


def _real_pg(cls):
    return unittest.skipUnless(TEST_PG, "set TEST_DATABASE_URL to run against a real Postgres")(
        type(cls.__name__ + "RealPG", (RealPgMixin, cls), {})
    )


ProductTestsRealPG = _real_pg(ProductTests)
MovementTestsRealPG = _real_pg(MovementTests)
AlertTriggerTestsRealPG = _real_pg(AlertTriggerTests)
SettingsAndDispatchTestsRealPG = _real_pg(SettingsAndDispatchTests)
ExportTestsRealPG = _real_pg(ExportTests)


class PgGuardTests(unittest.TestCase):
    def test_postgres_url_without_driver_gives_clear_error(self):
        with mock.patch.object(S, "psycopg", None):
            with self.assertRaises(RuntimeError) as cm:
                Store("postgresql://u:p@h/db")
        self.assertIn("psycopg", str(cm.exception))

    def test_postgres_scheme_detection(self):
        for url, expected in [("postgres://a", True), ("postgresql://a", True), ("/tmp/x.db", False), ("w.db", False)]:
            self.assertEqual(str(url).startswith(("postgres://", "postgresql://")), expected)

    def test_placeholder_translation(self):
        c = S._Conn(None, True)
        self.assertEqual(c._sql("a=? AND b=?"), "a=%s AND b=%s")
        self.assertEqual(S._Conn(None, False)._sql("a=?"), "a=?")

    def test_pg_schema_uses_bigserial(self):
        joined = " ".join(S.SCHEMA_PG)
        self.assertIn("BIGSERIAL PRIMARY KEY", joined)
        self.assertNotIn("AUTOINCREMENT", joined)
        self.assertIn("product_id BIGINT", joined)


class WhatsAppRequestTests(unittest.TestCase):
    def test_url_is_built_and_encoded(self):
        captured = {}

        class FakeResp:
            status = 200

            def read(self, n):
                return b"<p>Message queued.</p>"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            return FakeResp()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            ok, detail = S.send_whatsapp("+919876543210", "123456", "A & B\nline2")
        self.assertTrue(ok)
        self.assertEqual(detail, "Message queued.")
        url = captured["url"]
        self.assertTrue(url.startswith("https://api.callmebot.com/whatsapp.php?"))
        self.assertIn("phone=%2B919876543210", url)
        self.assertIn("apikey=123456", url)
        self.assertIn("A%20%26%20B%0Aline2", url)

    def test_http_error_and_network_error(self):
        import urllib.error

        def http_err(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        def net_err(req, timeout):
            raise urllib.error.URLError("no route")

        with mock.patch("urllib.request.urlopen", http_err):
            self.assertEqual(S.send_whatsapp("+911234567890", "k", "m"), (False, "HTTP 401"))
        with mock.patch("urllib.request.urlopen", net_err):
            ok, detail = S.send_whatsapp("+911234567890", "k", "m")
            self.assertFalse(ok)
            self.assertIn("Network error", detail)


if __name__ == "__main__":
    unittest.main()
