import os
import re
import shutil
import tempfile
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(TEMP_DIR.name, "test.db")
os.environ["BACKUP_DIR"] = os.path.join(TEMP_DIR.name, "backups")
os.environ["DATABASE_URL"] = ""
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["APP_PASSWORD"] = "test-password"
os.environ["SESSION_COOKIE_SECURE"] = "false"

import app as application  # noqa: E402


class ControlLaboralTests(unittest.TestCase):
    def setUp(self):
        application.app.config.update(TESTING=True)
        self.client = application.app.test_client()
        shutil.rmtree(application.BACKUP_DIR, ignore_errors=True)
        with application.db() as con:
            for table in ("jornadas", "viajes", "pagos", "notas"):
                con.execute(f"DELETE FROM {table}")

    def csrf_from(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        match = re.search(r'name="_csrf_token" value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match, f"No CSRF token in {path}")
        return match.group(1)

    def login(self):
        token = self.csrf_from("/login")
        response = self.client.post("/login", data={"_csrf_token": token, "password": "test-password"})
        self.assertEqual(response.status_code, 302)

    def test_post_without_csrf_token_is_rejected(self):
        token = self.csrf_from("/login")
        self.client.post("/login", data={"_csrf_token": token, "password": "test-password"})
        self.assertEqual(self.client.post("/logout").status_code, 400)

    def test_dashboard_includes_paid_but_not_pending_trips(self):
        with application.db() as con:
            con.execute("INSERT INTO viajes(fecha,concepto,importe,pagado,creado) VALUES(?,?,?,?,?)",
                         ("2026-09-26", "Pagado", 400, 1, "now"))
            con.execute("INSERT INTO viajes(fecha,concepto,importe,pagado,creado) VALUES(?,?,?,?,?)",
                         ("2026-10-31", "Pendiente", 200, 0, "now"))
        self.login()
        response = self.client.get("/")
        page = response.get_data(as_text=True)
        self.assertIn("400,00 €", page)
        self.assertIn("200,00 €", page)
        self.assertIn("Pagos registrados y viajes pagados", page)

    def test_payment_history_and_csv_include_paid_trips_but_not_pending_trips(self):
        with application.db() as con:
            con.execute("INSERT INTO viajes(fecha,concepto,importe,pagado,fecha_pago,creado) VALUES(?,?,?,?,?,?)",
                         ("2026-09-26", "Viaje cobrado", 400, 1, "2026-10-02", "now"))
            con.execute("INSERT INTO viajes(fecha,concepto,importe,pagado,creado) VALUES(?,?,?,?,?)",
                         ("2026-10-31", "Viaje pendiente", 200, 0, "now"))
            con.execute("INSERT INTO pagos(fecha,concepto,importe,categoria,creado) VALUES(?,?,?,?,?)",
                         ("2026-10-03", "Pago de horas extra", 50, "horas_extra", "now"))
        self.login()

        response = self.client.get("/pagos")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("450,00 €", page)
        self.assertIn("Viaje cobrado", page)
        self.assertIn("Fecha del cobro", page)
        self.assertIn("Pago de horas extra", page)
        self.assertNotIn("Viaje pendiente", page)

        exported = self.client.get("/exportar?tabla=pagos")
        csv_data = exported.get_data(as_text=True)
        self.assertEqual(exported.status_code, 200)
        self.assertIn("Viaje cobrado", csv_data)
        self.assertIn("Pago de horas extra", csv_data)
        self.assertNotIn("Viaje pendiente", csv_data)

    def test_editing_journey_trip_and_payment_persists_changes(self):
        with application.db() as con:
            jornada_id = con.execute("INSERT INTO jornadas(fecha,horas_normales,horas_extra,notas,creado) VALUES(?,?,?,?,?)",
                         ("2026-09-01", 8, 1, "old", "now")).lastrowid
            viaje_id = con.execute("INSERT INTO viajes(fecha,concepto,importe,pagado,creado) VALUES(?,?,?,?,?)",
                         ("2026-09-02", "old trip", 10, 0, "now")).lastrowid
            pago_id = con.execute("INSERT INTO pagos(fecha,concepto,importe,categoria,creado) VALUES(?,?,?,?,?)",
                         ("2026-09-03", "old payment", 20, "otro", "now")).lastrowid
        self.login()
        cases = [
            (f"/jornadas/{jornada_id}/editar", {"fecha": "2026-09-11", "entrada": "08:00", "salida": "17:00",
                                    "horas_normales": "8", "horas_extra": "2", "notas": "changed"}, "jornadas"),
            (f"/viajes/{viaje_id}/editar", {"fecha": "2026-09-12", "concepto": "new trip", "importe": "30", "notas": "changed"}, "viajes"),
            (f"/pagos/{pago_id}/editar", {"fecha": "2026-09-13", "concepto": "new payment", "importe": "40",
                                 "categoria": "horas_extra", "notas": "changed"}, "pagos"),
        ]
        for path, fields, table in cases:
            fields["_csrf_token"] = self.csrf_from(path)
            response = self.client.post(path, data=fields)
            self.assertEqual(response.status_code, 302)
            with application.db() as con:
                row = con.execute(f"SELECT * FROM {table} WHERE id=?", (jornada_id if table == "jornadas" else viaje_id if table == "viajes" else pago_id,)).fetchone()
                self.assertIn("changed", " ".join(str(value) for value in row))

    def test_assistant_receives_payment_context_without_database_writes(self):
        with application.db() as con:
            con.execute("INSERT INTO pagos(fecha,concepto,importe,categoria,creado) VALUES(?,?,?,?,?)",
                         ("2026-09-03", "Nomina", 120, "otro", "now"))
        captured = {}

        class FakeOpenAI:
            def __init__(self, api_key):
                self.responses = self

            def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(output_text="Resumen listo")

        self.login()
        with patch.object(application, "OpenAI", FakeOpenAI), patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            before = self.row_count("pagos")
            token = self.csrf_from("/asistente")
            response = self.client.post("/asistente", data={"_csrf_token": token, "prompt": "Resume mis pagos"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("Resumen listo", response.get_data(as_text=True))
            self.assertIn("Nomina", captured["input"])
            self.assertIn("pagos_registrados_eur", captured["input"])
            self.assertEqual(self.row_count("pagos"), before)

    def test_backup_is_checked_before_download(self):
        self.login()
        token = self.csrf_from("/backup")
        response = self.client.post("/backup", data={"_csrf_token": token})
        self.assertEqual(response.status_code, 302)
        files = os.listdir(application.BACKUP_DIR)
        self.assertEqual(len(files), 1)
        path = os.path.join(application.BACKUP_DIR, files[0])
        self.assertTrue(application.validate_backup_file(path))
        downloaded = self.client.get(f"/backup/{files[0]}")
        self.assertEqual(downloaded.status_code, 200)
        self.assertTrue(downloaded.data)
        downloaded.close()

    def test_invalid_backup_is_rejected(self):
        self.login()
        os.makedirs(application.BACKUP_DIR, exist_ok=True)
        bad_path = os.path.join(application.BACKUP_DIR, "bad.json")
        with open(bad_path, "w", encoding="utf-8") as output:
            output.write("{}")
        self.assertEqual(self.client.get("/backup/bad.json").status_code, 400)

    def test_restore_validates_copy_and_saves_safety_backup_first(self):
        with application.db() as con:
            con.execute("INSERT INTO pagos(fecha,concepto,importe,categoria,creado) VALUES(?,?,?,?,?)",
                         ("2026-09-03", "Antes", 20, "otro", "now"))
        self.login()
        token = self.csrf_from("/backup")
        self.client.post("/backup", data={"_csrf_token": token})
        backup_name = next(name for name in os.listdir(application.BACKUP_DIR) if name.endswith(".db"))
        backup_path = os.path.join(application.BACKUP_DIR, backup_name)
        with application.db() as con:
            con.execute("INSERT INTO pagos(fecha,concepto,importe,categoria,creado) VALUES(?,?,?,?,?)",
                         ("2026-09-04", "Después", 99, "otro", "now"))
        token = self.csrf_from("/backup")
        with open(backup_path, "rb") as source:
            response = self.client.post("/backup/restaurar", data={
                "_csrf_token": token,
                "confirm_restore": "yes",
                "backup_file": (BytesIO(source.read()), backup_name),
            }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        with application.db() as con:
            rows = con.execute("SELECT concepto,importe FROM pagos ORDER BY id").fetchall()
        self.assertEqual([(row["concepto"], row["importe"]) for row in rows], [("Antes", 20)])
        self.assertEqual(sum(name.endswith(".db") for name in os.listdir(application.BACKUP_DIR)), 2)

    def test_pages_render_and_session_cookie_security_defaults(self):
        self.login()
        for path in ("/", "/jornadas", "/viajes", "/pagos", "/asistente", "/ajustes", "/backup"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            page = response.get_data(as_text=True)
            for form in re.findall(r'<form[^>]*method="post"[^>]*>.*?</form>', page, flags=re.DOTALL):
                self.assertIn('name="_csrf_token"', form, path)
        self.assertTrue(application.app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(application.app.config["SESSION_COOKIE_SAMESITE"], "Lax")

    def row_count(self, table):
        with application.db() as con:
            return con.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]


if __name__ == "__main__":
    unittest.main()

