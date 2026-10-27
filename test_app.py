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
        self.assertIn("Pagos anotados", page)
        self.assertIn("Viajes cobrados", page)

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

    def test_auto_clock_records_a_provisional_weekday_once_after_16(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from auto_clock import record_scheduled_shift

        zone = application.CANARY_TZ or datetime.now().astimezone().tzinfo
        before_four = datetime(2026, 10, 27, 15, 59, tzinfo=zone)
        after_four = datetime(2026, 10, 27, 16, 0, tzinfo=zone)
        saturday = datetime(2026, 10, 31, 17, 0, tzinfo=zone)

        self.assertFalse(record_scheduled_shift(application.db, before_four))
        self.assertFalse(record_scheduled_shift(application.db, saturday))
        self.assertTrue(record_scheduled_shift(application.db, after_four))
        self.assertFalse(record_scheduled_shift(application.db, after_four))

        with application.db() as con:
            rows = con.execute("SELECT * FROM jornadas WHERE fecha=?", ("2026-10-27",)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["entrada"], rows[0]["salida"], rows[0]["horas_normales"], rows[0]["horas_extra"]),
                         ("08:00", "16:00", 8, 0))
        self.assertTrue(application.is_provisional_journey(rows[0]))

    def test_monthly_report_filters_work_trips_and_cash_by_the_selected_month(self):
        with application.db() as con:
            con.execute("INSERT INTO jornadas(fecha,horas_normales,horas_extra,notas,creado) VALUES(?,?,?,?,?)",
                         ("2026-09-10", 8, 1.5, "[SIN REVISAR] Estimación", "now"))
            con.execute("INSERT INTO viajes(fecha,concepto,importe,pagado,fecha_pago,creado) VALUES(?,?,?,?,?,?)",
                         ("2026-09-12", "Viaje de septiembre", 200, 1, "2026-09-20", "now"))
            con.execute("INSERT INTO viajes(fecha,concepto,importe,pagado,creado) VALUES(?,?,?,?,?)",
                         ("2026-09-22", "Viaje pendiente", 100, 0, "now"))
            con.execute("INSERT INTO pagos(fecha,concepto,importe,categoria,creado) VALUES(?,?,?,?,?)",
                         ("2026-09-25", "Pago extra", 50, "horas_extra", "now"))
            con.execute("INSERT INTO pagos(fecha,concepto,importe,categoria,creado) VALUES(?,?,?,?,?)",
                         ("2026-10-02", "Pago de octubre", 300, "otro", "now"))

        report = application.build_monthly_report("2026-09")
        self.assertEqual(report["dias_registrados"], 1)
        self.assertEqual(report["horas_normales"], 8)
        self.assertEqual(report["horas_extra"], 1.5)
        self.assertEqual(report["provisional_count"], 1)
        self.assertEqual(report["viajes_total"], 300)
        self.assertEqual(report["viajes_pagados"], 200)
        self.assertEqual(report["viajes_pendientes"], 100)
        self.assertEqual(report["total_recibido"], 250)
        self.login()
        response = self.client.get("/informes?mes=2026-09")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Viaje de septiembre", page)
        self.assertIn("Viaje pendiente", page)
        self.assertIn("Provisional · revisar", page)
        self.assertNotIn("Pago de octubre", page)
        downloaded = self.client.get("/informes?mes=2026-09&descargar=csv")
        csv_data = downloaded.get_data(as_text=True)
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("Viaje de septiembre", csv_data)
        self.assertIn("Pago extra", csv_data)
        self.assertNotIn("Pago de octubre", csv_data)

    def test_journey_can_be_marked_provisional_then_reviewed_while_editing(self):
        self.login()
        fields = {
            "_csrf_token": self.csrf_from("/jornadas"),
            "fecha": "2026-09-15",
            "entrada": "08:00",
            "salida": "16:00",
            "horas_normales": "8",
            "horas_extra": "0",
            "notas": "Horario aproximado",
            "provisional": "1",
        }
        self.assertEqual(self.client.post("/jornadas", data=fields).status_code, 302)
        with application.db() as con:
            row = con.execute("SELECT * FROM jornadas WHERE fecha=?", ("2026-09-15",)).fetchone()
            item_id = row["id"]
            self.assertTrue(application.is_provisional_journey(row))

        csrf = self.csrf_from(f"/jornadas/{item_id}/editar")
        updated = {
            "_csrf_token": csrf,
            "fecha": "2026-09-15",
            "entrada": "08:00",
            "salida": "17:00",
            "horas_normales": "8",
            "horas_extra": "1",
            "notas": "Horario confirmado",
        }
        self.assertEqual(self.client.post(f"/jornadas/{item_id}/editar", data=updated).status_code, 302)
        with application.db() as con:
            row = con.execute("SELECT * FROM jornadas WHERE id=?", (item_id,)).fetchone()
            self.assertFalse(application.is_provisional_journey(row))
            self.assertIn("Horario confirmado", row["notas"])

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

    def test_assistant_summarizes_saved_payments_without_database_writes(self):
        with application.db() as con:
            con.execute("INSERT INTO pagos(fecha,concepto,importe,categoria,creado) VALUES(?,?,?,?,?)",
                         ("2026-09-03", "Nomina", 120, "otro", "now"))
        self.login()
        before = self.row_count("pagos")
        token = self.csrf_from("/asistente")
        response = self.client.post("/asistente", data={"_csrf_token": token, "prompt": "Resume mis pagos"})
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Pagos anotados en el histórico: 120,00 €", page)
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

    def test_goku_background_covers_full_desktop_viewport(self):
        self.login()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("inset:0;width:100vw", page)
        self.assertIn("center center/cover no-repeat", page)

    def test_goku_artwork_is_served_as_static_background_asset(self):
        response = self.client.get("/static/goku-bg.svg")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("data:image/jpeg;base64,", page)
        self.assertIn("aria-label=\"Ilustración de Goku\"", page)
        response.close()

    def test_duplicate_journey_for_same_day_is_blocked_with_edit_link(self):
        self.login()
        token = self.csrf_from("/jornadas")
        data = {"_csrf_token": token, "fecha": "2026-10-01", "entrada": "08:00",
                "salida": "16:00", "horas_normales": "8", "horas_extra": "1",
                "notas": "jornada de prueba"}
        self.client.post("/jornadas", data=data)
        response = self.client.post("/jornadas", data=data)
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response.location).get_data(as_text=True)
        self.assertIn("Ya existe un fichaje para ese día", page)
        self.assertIn("Abrir y corregir el registro existente", page)
        with application.db() as con:
            count = con.execute("SELECT COUNT(*) n FROM jornadas WHERE fecha=?", ("2026-10-01",)).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_duplicate_payment_requires_explicit_confirmation(self):
        self.login()
        token = self.csrf_from("/pagos")
        data = {"_csrf_token": token, "fecha": "2026-10-01", "concepto": "Pago extra",
                "importe": "40.00", "categoria": "horas_extra", "notas": ""}
        self.client.post("/pagos", data=data)
        response = self.client.post("/pagos", data=data)
        page = self.client.get(response.location)
        self.assertIn("Puede ser un pago duplicado", page.get_data(as_text=True))
        token_match = re.search(r'name="_csrf_token" value="([^"]+)"', page.get_data(as_text=True))
        self.assertIsNotNone(token_match)
        data["_csrf_token"] = token_match.group(1)
        data["confirmar_duplicado"] = "1"
        self.client.post("/pagos", data=data)
        with application.db() as con:
            count = con.execute("SELECT COUNT(*) n FROM pagos WHERE fecha=? AND concepto=?",
                                ("2026-10-01", "Pago extra")).fetchone()["n"]
        self.assertEqual(count, 2)

    def test_jornada_filter_limits_results_to_requested_status(self):
        with application.db() as con:
            con.execute("INSERT INTO jornadas(fecha,entrada,salida,horas_normales,horas_extra,notas,creado) VALUES(?,?,?,?,?,?,?)",
                        ("2026-10-01", "08:00", "16:00", 8, 0, "[REVISADO]", "now"))
            con.execute("INSERT INTO jornadas(fecha,entrada,salida,horas_normales,horas_extra,notas,creado) VALUES(?,?,?,?,?,?,?)",
                        ("2026-10-02", "08:00", "16:00", 8, 1, "[SIN REVISAR]", "now"))
        self.login()
        with patch.object(application, "record_scheduled_shift"):
            page = self.client.get("/jornadas?estado=provisional").get_data(as_text=True)
        self.assertIn("02/10/2026", page)
        self.assertNotIn("01/10/2026", page)

    def test_annual_summary_separates_pending_trips_and_received_payments(self):
        with application.db() as con:
            con.execute("INSERT INTO jornadas(fecha,entrada,salida,horas_normales,horas_extra,notas,creado) VALUES(?,?,?,?,?,?,?)",
                        ("2026-03-10", "08:00", "16:00", 8, 2, "[REVISADO]", "now"))
            con.execute("INSERT INTO viajes(fecha,concepto,importe,pagado,fecha_pago,notas,creado) VALUES(?,?,?,?,?,?,?)",
                        ("2026-03-12", "Pendiente", 200, 0, "", "", "now"))
            con.execute("INSERT INTO viajes(fecha,concepto,importe,pagado,fecha_pago,notas,creado) VALUES(?,?,?,?,?,?,?)",
                        ("2026-03-13", "Cobrado", 100, 1, "2026-04-01", "", "now"))
            con.execute("INSERT INTO pagos(fecha,concepto,importe,categoria,notas,creado) VALUES(?,?,?,?,?,?)",
                        ("2026-05-01", "Horas extra", 40, "horas_extra", "", "now"))
        self.login()
        with patch.object(application, "record_scheduled_shift"):
            page = self.client.get("/informes/anual?anio=2026").get_data(as_text=True)
        self.assertIn("2.00 h", page)
        self.assertIn("200,00 €", page)
        self.assertIn("140,00 €", page)
        self.assertIn("40,00 €", page)

    def test_monthly_report_warns_before_closing_incomplete_month(self):
        with application.db() as con:
            con.execute("INSERT INTO jornadas(fecha,entrada,salida,horas_normales,horas_extra,notas,creado) VALUES(?,?,?,?,?,?,?)",
                        ("2026-10-10", "", "16:00", 8, 0, "[SIN REVISAR]", "now"))
        self.login()
        with patch.object(application, "record_scheduled_shift"):
            page = self.client.get("/informes?mes=2026-10").get_data(as_text=True)
        self.assertIn("Este mes todavía no está listo para cerrar", page)
        self.assertIn("Descargar igualmente sin cerrar el mes", page)
        self.assertIn("entrada o salida sin completar", page)

    def test_trip_filter_searches_concept_within_date_range(self):
        with application.db() as con:
            con.execute("INSERT INTO viajes(fecha,concepto,importe,pagado,notas,creado) VALUES(?,?,?,?,?,?)",
                        ("2026-10-05", "Hotel Madrid", 80, 0, "", "now"))
            con.execute("INSERT INTO viajes(fecha,concepto,importe,pagado,notas,creado) VALUES(?,?,?,?,?,?)",
                        ("2026-09-05", "Taxi aeropuerto", 25, 0, "", "now"))
        self.login()
        with patch.object(application, "record_scheduled_shift"):
            page = self.client.get("/viajes?desde=2026-10-01&hasta=2026-10-31&buscar=hotel").get_data(as_text=True)
        self.assertIn("Hotel Madrid", page)
        self.assertNotIn("Taxi aeropuerto", page)

    def test_pages_render_and_session_cookie_security_defaults(self):
        self.login()
        for path in ("/", "/jornadas", "/viajes", "/pagos", "/informes", "/informes/anual", "/asistente", "/ajustes", "/backup"):
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

