import os
import sqlite3
import csv
import io
import json
import math
import secrets
import tempfile
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, abort
from werkzeug.security import check_password_hash, generate_password_hash
from auto_clock import record_scheduled_shift


APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(APP_DIR, "mi_trabajo.db"))
BACKUP_DIR = os.environ.get("BACKUP_DIR", os.path.join(APP_DIR, "copias_seguridad"))
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": csrf_token}


@app.before_request
def protect_state_changes():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        expected = session.get("_csrf_token", "")
        supplied = request.form.get("_csrf_token", "") or request.headers.get("X-CSRF-Token", "")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            abort(400, description="Token de formulario inválido. Recarga la página e inténtalo de nuevo.")


@app.before_request
def record_usual_shift_after_work():
    if session.get("logged_in") and request.method == "GET" and request.endpoint not in {"static", "service_worker"}:
        record_scheduled_shift(db)


class PostgresCompat:
    def __init__(self, connection): self.connection = connection
    def __enter__(self):
        self.connection.__enter__()
        return self
    def __exit__(self, exc_type, exc, tb): return self.connection.__exit__(exc_type, exc, tb)
    def execute(self, sql, params=()):
        sql = sql.replace("?", "%s")
        sql = sql.replace("ROUND(SUM(horas_extra),2)", "ROUND(SUM(horas_extra)::numeric,2)")
        sql = sql.replace("INSERT OR IGNORE INTO ajustes", "INSERT INTO ajustes")
        if sql.lstrip().upper().startswith("INSERT INTO AJUSTES") and "ON CONFLICT" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT (clave) DO NOTHING"
        return self.connection.execute(sql, params)
    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip(): self.execute(statement)
        return self


class SQLiteCompat:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        try:
            return self.connection.__exit__(exc_type, exc, tb)
        finally:
            self.connection.close()

def db():
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        return PostgresCompat(psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10))
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return SQLiteCompat(con)


def init_db():
    if not DATABASE_URL:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as con:
        schema = """
        CREATE TABLE IF NOT EXISTS jornadas (
          id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT NOT NULL,
          entrada TEXT DEFAULT '', salida TEXT DEFAULT '',
          horas_normales REAL NOT NULL DEFAULT 8, horas_extra REAL NOT NULL DEFAULT 0,
          notas TEXT DEFAULT '', creado TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS viajes (
          id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT NOT NULL,
          concepto TEXT NOT NULL, importe REAL NOT NULL DEFAULT 200,
          pagado INTEGER NOT NULL DEFAULT 0, fecha_pago TEXT DEFAULT '',
          notas TEXT DEFAULT '', creado TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pagos (
          id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT NOT NULL,
          concepto TEXT NOT NULL, importe REAL NOT NULL,
          categoria TEXT NOT NULL DEFAULT 'otro', notas TEXT DEFAULT '',
          creado TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notas (
          id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT NOT NULL,
          titulo TEXT NOT NULL, contenido TEXT NOT NULL, creado TEXT NOT NULL
        );
        """
        if DATABASE_URL:
            schema = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        con.executescript(schema)
        defaults = {
            "hora_extra_euros": "", "horas_normales_dia": "8",
            "dias_trabajo": "Lunes a viernes", "horario_habitual": "08:00-16:00, sin pausa",
            "seeded_demo": "no"
        }
        for k, v in defaults.items():
            con.execute("INSERT INTO ajustes(clave,valor) VALUES (?,?) ON CONFLICT(clave) DO NOTHING", (k,v))


def get_setting(key, default=""):
    with db() as con:
        row = con.execute("SELECT valor FROM ajustes WHERE clave=?", (key,)).fetchone()
    return row["valor"] if row else default


def set_setting(key, value):
    with db() as con:
        con.execute("""INSERT INTO ajustes(clave,valor) VALUES (?,?)
        ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor""", (key, str(value)))


def seed_known_records():
    """Carga los datos mencionados en la conversación una sola vez."""
    if get_setting("seeded_demo", "no") == "yes":
        return
    with db() as con:
        exists = con.execute("SELECT COUNT(*) n FROM jornadas").fetchone()["n"]
        if exists == 0:
            con.execute("""INSERT INTO jornadas(fecha,horas_normales,horas_extra,notas,creado)
                VALUES(?,?,?,?,?)""",
                ("2026-09-30", 0, 18.5, "Total de horas extra pendientes de septiembre; total mensual aproximado, no asignado a días concretos.", datetime.now().isoformat()))
            con.execute("""INSERT INTO jornadas(fecha,horas_normales,horas_extra,notas,creado)
                VALUES(?,?,?,?,?)""",
                ("2026-10-26", 0, 21, "Total provisional de horas extra de octubre a fecha 26/10/2026; actualizar cuando cambie.", datetime.now().isoformat()))
        ntrips = con.execute("SELECT COUNT(*) n FROM viajes").fetchone()["n"]
        if ntrips == 0:
            for i in range(2):
                con.execute("""INSERT INTO viajes(fecha,concepto,importe,pagado,notas,creado)
                    VALUES(?,?,?,?,?,?)""",
                    ("2026-10-31", f"Viaje de trabajo de octubre {i+1}", 200, 0,
                     "Fecha aproximada de registro; cambiar a la fecha real si se conoce.", datetime.now().isoformat()))
        con.execute("UPDATE ajustes SET valor='yes' WHERE clave='seeded_demo'")


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped


def money(n):
    return f"{n:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def parse_nonnegative_number(value, default=None):
    number = float(value if value not in (None, "") else default)
    if not math.isfinite(number) or number < 0:
        raise ValueError("Expected a finite non-negative number")
    return number


def parse_form_date(value):
    return date.fromisoformat(value)


def optional_date_filter(name, label):
    value = request.args.get(name, "").strip()
    if not value:
        return ""
    try:
        parse_form_date(value)
        return value
    except (ValueError, TypeError):
        flash(f"El filtro de {label} no es una fecha válida.", "warning")
        return ""


def date_range_from_request():
    start = optional_date_filter("desde", "inicio")
    end = optional_date_filter("hasta", "fin")
    if start and end and start > end:
        flash("La fecha de inicio debe ser anterior o igual a la fecha final.", "warning")
        return "", ""
    return start, end


def duplicate_journey_exists(con, fecha, exclude_id=None):
    if exclude_id is None:
        return con.execute("SELECT id FROM jornadas WHERE fecha=? LIMIT 1", (fecha,)).fetchone()
    return con.execute("SELECT id FROM jornadas WHERE fecha=? AND id<>? LIMIT 1",
                       (fecha, exclude_id)).fetchone()


def duplicate_payment_exists(con, fecha, concepto, importe, categoria, exclude_id=None):
    query = """SELECT id FROM pagos
               WHERE fecha=? AND LOWER(TRIM(concepto))=LOWER(TRIM(?))
               AND ABS(importe-?)<0.005 AND categoria=?"""
    params = [fecha, concepto, importe, categoria]
    if exclude_id is not None:
        query += " AND id<>?"
        params.append(exclude_id)
    return con.execute(query + " LIMIT 1", tuple(params)).fetchone()


PROVISIONAL_JOURNEY_MARKER = "[SIN REVISAR]"
REVIEWED_JOURNEY_MARKER = "[REVISADO]"
try:
    CANARY_TZ = ZoneInfo("Atlantic/Canary")
except ZoneInfoNotFoundError:
    # Windows may not include an IANA database; use the machine's local zone there.
    CANARY_TZ = None


def canary_now():
    return datetime.now(CANARY_TZ) if CANARY_TZ else datetime.now().astimezone()


def is_provisional_journey(row):
    notes = (row["notas"] or "").strip()
    if notes.startswith(PROVISIONAL_JOURNEY_MARKER):
        return True
    if notes.startswith(REVIEWED_JOURNEY_MARKER):
        return False
    normalized = unicodedata.normalize("NFKD", notes.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return any(term in normalized for term in ("provisional", "aproximad", "no verificado", "sin revisar", "estimacion"))


def clean_journey_notes(notes):
    notes = (notes or "").strip()
    for marker in (PROVISIONAL_JOURNEY_MARKER, REVIEWED_JOURNEY_MARKER):
        if notes.startswith(marker):
            return notes[len(marker):].strip()
    return notes


def store_journey_notes(notes, provisional):
    clean_notes = clean_journey_notes(notes)
    marker = PROVISIONAL_JOURNEY_MARKER if provisional else REVIEWED_JOURNEY_MARKER
    return f"{marker} {clean_notes}".strip()


def journey_for_display(row):
    item = dict(row)
    item["provisional"] = is_provisional_journey(row)
    item["notas"] = clean_journey_notes(item.get("notas"))
    return item


@app.template_filter("money")
def money_filter(n):
    return money(float(n or 0))


@app.template_filter("date_es")
def date_es(value):
    if not value:
        return "—"
    try:
        return date.fromisoformat(value[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return value


@app.get("/service-worker.js")
def service_worker():
    response = send_file(os.path.join(APP_DIR, "static", "service-worker.js"),
                         mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        expected = os.environ.get("APP_PASSWORD", "")
        if not expected:
            flash("Falta configurar APP_PASSWORD en las variables de entorno. No publiques la app sin contraseña.", "danger")
        elif request.form.get("password", "") == expected:
            session.clear()
            session["logged_in"] = True
            session.permanent = True
            next_url = request.args.get("next", "")
            # Solo permitir rutas locales para evitar redirecciones externas.
            if next_url.startswith("/") and not next_url.startswith("//") and chr(92) not in next_url:
                return redirect(next_url)
            return redirect(url_for("dashboard"))
        else:
            flash("Contraseña incorrecta.", "danger")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    with db() as con:
        total_h = con.execute("SELECT COALESCE(SUM(horas_extra),0) n FROM jornadas").fetchone()["n"]
        trips_due = con.execute("SELECT COALESCE(SUM(importe),0) n FROM viajes WHERE pagado=0").fetchone()["n"]
        trips_count = con.execute("SELECT COUNT(*) n FROM viajes WHERE pagado=0").fetchone()["n"]
        months = con.execute("""SELECT substr(fecha,1,7) mes, ROUND(SUM(horas_extra),2) horas
            FROM jornadas GROUP BY substr(fecha,1,7) ORDER BY mes""").fetchall()
        trips = con.execute("SELECT * FROM viajes ORDER BY fecha DESC,id DESC LIMIT 6").fetchall()
        recent = con.execute("SELECT * FROM jornadas ORDER BY fecha DESC,id DESC LIMIT 6").fetchall()
        payment_summary = con.execute("""SELECT
            (SELECT COALESCE(SUM(importe),0) FROM pagos) pagos_anotados,
            (SELECT COALESCE(SUM(importe),0) FROM pagos WHERE categoria='horas_extra') horas_extra_cobradas,
            (SELECT COALESCE(SUM(importe),0) FROM viajes WHERE pagado=1) viajes_cobrados
        """).fetchone()
        today_date = canary_now().date().isoformat()
        today_row = con.execute("SELECT * FROM jornadas WHERE fecha=? ORDER BY id DESC LIMIT 1",
            (today_date,)).fetchone()
    rate = get_setting("hora_extra_euros", "").strip()
    try:
        rate_num = float(rate.replace(",", ".")) if rate else None
    except ValueError:
        rate_num = None
    overtime_estimated = total_h * rate_num if rate_num is not None else None
    overtime_paid = float(payment_summary["horas_extra_cobradas"] or 0)
    overtime_due = max(0, overtime_estimated - overtime_paid) if overtime_estimated is not None else None
    recent = [journey_for_display(row) for row in recent]
    today_entry = journey_for_display(today_row) if today_row else None
    payments_registered = payment_summary["pagos_anotados"]
    trips_paid = payment_summary["viajes_cobrados"]
    payments = payments_registered + trips_paid
    current_month_date = canary_now().date().replace(day=1)
    previous_month_date = current_month_date - timedelta(days=1)
    current_report = build_monthly_report(current_month_date.strftime("%Y-%m"))
    previous_report = build_monthly_report(previous_month_date.strftime("%Y-%m"))
    month_comparison = {
        "current_label": current_report["mes_label"],
        "previous_label": previous_report["mes_label"],
        "current_hours": current_report["horas_extra"],
        "previous_hours": previous_report["horas_extra"],
        "current_received": current_report["total_recibido"],
        "previous_received": previous_report["total_recibido"],
        "hours_delta": current_report["horas_extra"] - previous_report["horas_extra"],
        "received_delta": current_report["total_recibido"] - previous_report["total_recibido"],
    }
    return render_template("dashboard.html", total_h=total_h, trips_due=trips_due, trips_count=trips_count,
        months=[dict(x) for x in months], trips=trips, recent=recent, payments=payments,
        payments_registered=payments_registered, trips_paid=trips_paid, today_date=today_date,
        today_entry=today_entry, rate=rate_num, overtime_due=overtime_due,
        overtime_estimated=overtime_estimated, overtime_paid=overtime_paid,
        month_comparison=month_comparison, work_schedule=get_setting("horario_habitual"))


@app.route("/jornadas", methods=["GET", "POST"])
@login_required
def jornadas():
    if request.method == "POST":
        fecha = request.form.get("fecha", "")
        entrada = request.form.get("entrada", "").strip()
        salida = request.form.get("salida", "").strip()
        try:
            parse_form_date(fecha)
            normales = parse_nonnegative_number(request.form.get("horas_normales"), 0)
            extra = parse_nonnegative_number(request.form.get("horas_extra"), 0)
        except (ValueError, TypeError):
            flash("Revisa la fecha y las horas introducidas.", "danger")
            return redirect(url_for("jornadas"))
        provisional = request.form.get("provisional") == "1"
        notes = store_journey_notes(request.form.get("notas", "").strip(), provisional)
        with db() as con:
            duplicate = duplicate_journey_exists(con, fecha)
            if duplicate:
                flash("Ya existe un fichaje para ese día. Puedes abrirlo para corregirlo en lugar de crear otro.", "warning")
                return redirect(url_for("jornadas", duplicado=duplicate["id"]))
            con.execute("""INSERT INTO jornadas(fecha,entrada,salida,horas_normales,horas_extra,notas,creado)
                VALUES(?,?,?,?,?,?,?)""",
                (fecha, entrada, salida, normales, extra, notes, datetime.now().isoformat()))
        missing = [label for label, value in (("entrada", entrada), ("salida", salida)) if not value]
        if missing:
            flash("Jornada guardada, pero falta " + " y ".join(missing) + ". Completa ese dato para cerrar el mes.", "warning")
        else:
            flash("Jornada guardada como provisional." if provisional else "Jornada guardada y marcada como revisada.", "success")
        return redirect(url_for("jornadas"))
    with db() as con:
        raw_rows = con.execute("SELECT * FROM jornadas ORDER BY fecha DESC,id DESC").fetchall()
    all_rows = [journey_for_display(row) for row in raw_rows]
    pending_data_count = sum(1 for row in all_rows if row["provisional"] or not row.get("entrada") or not row.get("salida"))
    start, end = date_range_from_request()
    status = request.args.get("estado", "todos")
    if status not in {"todos", "provisional", "revisado"}:
        status = "todos"
    search = request.args.get("buscar", "").strip().casefold()
    rows = [row for row in all_rows
            if (not start or row["fecha"] >= start)
            and (not end or row["fecha"] <= end)
            and (status == "todos" or (status == "provisional" and row["provisional"])
                 or (status == "revisado" and not row["provisional"]))
            and (not search or search in " ".join(str(row.get(key) or "") for key in
                ("fecha", "entrada", "salida", "notas")).casefold())]
    duplicate_id = request.args.get("duplicado", type=int)
    duplicate_item = next((row for row in all_rows if row["id"] == duplicate_id), None) if duplicate_id else None
    return render_template("jornadas.html", rows=rows, today=canary_now().date().isoformat(),
        normal_default=get_setting("horas_normales_dia", "8"), editing=None,
        pending_data_count=pending_data_count, filtered_count=len(rows), duplicate_item=duplicate_item,
        filters={"desde": start, "hasta": end, "estado": status, "buscar": request.args.get("buscar", "")})


@app.route("/jornadas/<int:item_id>/editar", methods=["GET", "POST"])
@login_required
def edit_jornada(item_id):
    if request.method == "POST":
        fecha = request.form.get("fecha", "")
        try:
            parse_form_date(fecha)
            normales = parse_nonnegative_number(request.form.get("horas_normales"), 0)
            extra = parse_nonnegative_number(request.form.get("horas_extra"), 0)
        except (ValueError, TypeError):
            flash("Revisa la fecha y las horas introducidas.", "danger")
            return redirect(url_for("edit_jornada", item_id=item_id))
        entrada = request.form.get("entrada", "").strip()
        salida = request.form.get("salida", "").strip()
        provisional = request.form.get("provisional") == "1"
        notes = store_journey_notes(request.form.get("notas", "").strip(), provisional)
        with db() as con:
            if duplicate_journey_exists(con, fecha, exclude_id=item_id):
                flash("Ya hay otro fichaje para ese día. Elige otra fecha para evitar duplicados.", "warning")
                return redirect(url_for("edit_jornada", item_id=item_id))
            con.execute("""UPDATE jornadas SET fecha=?,entrada=?,salida=?,horas_normales=?,horas_extra=?,notas=?
                WHERE id=?""", (fecha, entrada, salida, normales, extra, notes, item_id))
        missing = [label for label, value in (("entrada", entrada), ("salida", salida)) if not value]
        if missing:
            flash("Jornada actualizada, pero falta " + " y ".join(missing) + ". Completa ese dato para cerrar el mes.", "warning")
        else:
            flash("Jornada actualizada; queda pendiente de revisión." if provisional else "Jornada actualizada y marcada como revisada.", "success")
        return redirect(url_for("jornadas"))
    with db() as con:
        editing = con.execute("SELECT * FROM jornadas WHERE id=?", (item_id,)).fetchone()
        rows = con.execute("SELECT * FROM jornadas ORDER BY fecha DESC,id DESC").fetchall()
    if not editing:
        abort(404)
    editing = journey_for_display(editing)
    rows = [journey_for_display(row) for row in rows]
    return render_template("jornadas.html", rows=rows, today=canary_now().date().isoformat(),
        normal_default=get_setting("horas_normales_dia", "8"), editing=editing)


@app.post("/jornadas/<int:item_id>/delete")
@login_required
def delete_jornada(item_id):
    with db() as con:
        con.execute("DELETE FROM jornadas WHERE id=?", (item_id,))
    flash("Registro eliminado.", "success")
    return redirect(url_for("jornadas"))


@app.route("/viajes", methods=["GET", "POST"])
@login_required
def viajes():
    if request.method == "POST":
        fecha = request.form.get("fecha", "")
        concepto = request.form.get("concepto", "").strip()
        try:
            parse_form_date(fecha)
            importe = parse_nonnegative_number(request.form.get("importe", "200").replace(",", "."))
        except (ValueError, TypeError):
            flash("Revisa la fecha y el importe.", "danger")
            return redirect(url_for("viajes"))
        if not concepto:
            flash("El concepto del viaje es obligatorio.", "danger")
            return redirect(url_for("viajes"))
        with db() as con:
            con.execute("""INSERT INTO viajes(fecha,concepto,importe,pagado,notas,creado)
                VALUES(?,?,?,?,?,?)""", (fecha, concepto,
                importe, 0, request.form.get("notas","").strip(), datetime.now().isoformat()))
        flash("Viaje añadido como pendiente de pago.", "success")
        return redirect(url_for("viajes"))
    with db() as con:
        raw_rows = [dict(row) for row in con.execute("SELECT * FROM viajes ORDER BY fecha DESC,id DESC").fetchall()]
    start, end = date_range_from_request()
    status = request.args.get("estado", "todos")
    if status not in {"todos", "pagado", "pendiente"}:
        status = "todos"
    search = request.args.get("buscar", "").strip().casefold()
    rows = [row for row in raw_rows
            if (not start or row["fecha"] >= start)
            and (not end or row["fecha"] <= end)
            and (status == "todos" or (status == "pagado" and row["pagado"])
                 or (status == "pendiente" and not row["pagado"]))
            and (not search or search in " ".join(str(row.get(key) or "") for key in
                ("concepto", "notas")).casefold())]
    return render_template("viajes.html", rows=rows, today=canary_now().date().isoformat(), editing=None,
        filters={"desde": start, "hasta": end, "estado": status, "buscar": request.args.get("buscar", "")})


@app.route("/viajes/<int:item_id>/editar", methods=["GET", "POST"])
@login_required
def edit_viaje(item_id):
    if request.method == "POST":
        fecha = request.form.get("fecha", "")
        concepto = request.form.get("concepto", "").strip()
        try:
            parse_form_date(fecha)
            importe = parse_nonnegative_number(request.form.get("importe", "").replace(",", "."))
        except (ValueError, TypeError):
            flash("Revisa la fecha y el importe.", "danger")
            return redirect(url_for("edit_viaje", item_id=item_id))
        if not concepto:
            flash("El concepto del viaje es obligatorio.", "danger")
            return redirect(url_for("edit_viaje", item_id=item_id))
        with db() as con:
            con.execute("UPDATE viajes SET fecha=?,concepto=?,importe=?,notas=? WHERE id=?",
                (fecha, concepto, importe, request.form.get("notas", "").strip(), item_id))
        flash("Viaje actualizado.", "success")
        return redirect(url_for("viajes"))
    with db() as con:
        editing = con.execute("SELECT * FROM viajes WHERE id=?", (item_id,)).fetchone()
        rows = con.execute("SELECT * FROM viajes ORDER BY fecha DESC,id DESC").fetchall()
    if not editing:
        abort(404)
    return render_template("viajes.html", rows=rows, today=canary_now().date().isoformat(), editing=editing)


@app.post("/viajes/<int:item_id>/toggle")
@login_required
def toggle_viaje(item_id):
    with db() as con:
        row = con.execute("SELECT pagado FROM viajes WHERE id=?", (item_id,)).fetchone()
        if row:
            new_status = 0 if row["pagado"] else 1
            con.execute("UPDATE viajes SET pagado=?,fecha_pago=? WHERE id=?",
                        (new_status, date.today().isoformat() if new_status else "", item_id))
    flash("Estado del viaje actualizado.", "success")
    return redirect(url_for("viajes"))


@app.post("/viajes/<int:item_id>/delete")
@login_required
def delete_viaje(item_id):
    with db() as con:
        con.execute("DELETE FROM viajes WHERE id=?", (item_id,))
    flash("Viaje eliminado.", "success")
    return redirect(url_for("viajes"))


def get_payment_history(con):
    payment_rows = con.execute("SELECT * FROM pagos ORDER BY fecha DESC,id DESC").fetchall()
    paid_trips = con.execute("SELECT * FROM viajes WHERE pagado=1 ORDER BY COALESCE(NULLIF(fecha_pago,''),fecha) DESC,id DESC").fetchall()
    rows = [
        {"id": row["id"], "fecha": row["fecha"], "fecha_tipo": "Fecha del pago", "concepto": row["concepto"],
         "categoria": {"horas_extra": "Horas extra", "viaje": "Viaje", "otro": "Otro"}.get(row["categoria"], row["categoria"]),
         "importe": row["importe"], "notas": row["notas"], "origen": "Pago registrado",
         "url": url_for("edit_pago", item_id=row["id"])}
        for row in payment_rows
    ]
    rows.extend(
        {"id": row["id"], "fecha": row["fecha_pago"] or row["fecha"],
         "fecha_tipo": "Fecha del cobro" if row["fecha_pago"] else "Fecha del viaje", "concepto": row["concepto"],
         "categoria": "Viaje", "importe": row["importe"], "notas": row["notas"],
         "origen": "Viaje marcado como pagado", "url": url_for("viajes")}
        for row in paid_trips
    )
    rows.sort(key=lambda row: (row["fecha"], row["id"], row["origen"]), reverse=True)
    total_received = sum(float(row["importe"] or 0) for row in rows)
    return rows, total_received


@app.route("/pagos", methods=["GET", "POST"])
@login_required
def pagos():
    if request.method == "POST":
        concepto = request.form.get("concepto", "").strip()
        categoria = request.form.get("categoria", "otro")
        try:
            fecha = request.form.get("fecha", "")
            parse_form_date(fecha)
            importe = parse_nonnegative_number(request.form.get("importe", "0").replace(",", "."))
        except (ValueError, TypeError):
            flash("Revisa la fecha y el importe.", "danger")
            return redirect(url_for("pagos"))
        notas = request.form.get("notas", "").strip()
        if not concepto or categoria not in {"horas_extra", "viaje", "otro"}:
            flash("Revisa el concepto y la categoría del pago.", "danger")
            return redirect(url_for("pagos"))
        with db() as con:
            duplicate = duplicate_payment_exists(con, fecha, concepto, importe, categoria)
            if duplicate and request.form.get("confirmar_duplicado") != "1":
                session["pending_duplicate_payment"] = {
                    "fecha": fecha, "concepto": concepto, "importe": request.form.get("importe", ""),
                    "categoria": categoria, "notas": notas,
                }
                flash("Ya existe un pago con la misma fecha, concepto e importe. Revisa el formulario antes de guardarlo otra vez.", "warning")
                return redirect(url_for("pagos", confirmar_duplicado="1"))
            con.execute("""INSERT INTO pagos(fecha,concepto,importe,categoria,notas,creado)
                VALUES(?,?,?,?,?,?)""", (fecha, concepto, importe, categoria, notas, datetime.now().isoformat()))
        session.pop("pending_duplicate_payment", None)
        flash("Pago registrado en el histórico.", "success")
        return redirect(url_for("pagos"))
    with db() as con:
        all_rows, total_received = get_payment_history(con)
    duplicate_form = session.get("pending_duplicate_payment") if request.args.get("confirmar_duplicado") == "1" else None
    if not duplicate_form:
        session.pop("pending_duplicate_payment", None)
    start, end = date_range_from_request()
    category = request.args.get("categoria", "todas")
    if category not in {"todas", "Horas extra", "Viaje", "Otro"}:
        category = "todas"
    search = request.args.get("buscar", "").strip().casefold()
    rows = [row for row in all_rows
            if (not start or row["fecha"] >= start)
            and (not end or row["fecha"] <= end)
            and (category == "todas" or row["categoria"] == category)
            and (not search or search in " ".join(str(row.get(key) or "") for key in
                ("concepto", "categoria", "origen", "notas")).casefold())]
    return render_template("pagos.html", rows=rows, total_received=total_received,
        today=canary_now().date().isoformat(), editing=None, duplicate_form=duplicate_form,
        filters={"desde": start, "hasta": end, "categoria": category, "buscar": request.args.get("buscar", "")})


@app.route("/pagos/<int:item_id>/editar", methods=["GET", "POST"])
@login_required
def edit_pago(item_id):
    if request.method == "POST":
        fecha = request.form.get("fecha", "")
        concepto = request.form.get("concepto", "").strip()
        categoria = request.form.get("categoria", "otro")
        try:
            parse_form_date(fecha)
            importe = parse_nonnegative_number(request.form.get("importe", "").replace(",", "."))
        except (ValueError, TypeError):
            flash("Revisa la fecha y el importe.", "danger")
            return redirect(url_for("edit_pago", item_id=item_id))
        if not concepto or categoria not in {"horas_extra", "viaje", "otro"}:
            flash("Revisa el concepto y la categoría del pago.", "danger")
            return redirect(url_for("edit_pago", item_id=item_id))
        with db() as con:
            if duplicate_payment_exists(con, fecha, concepto, importe, categoria, exclude_id=item_id):
                flash("Ya hay otro pago con esa fecha, concepto e importe. Comprueba los datos antes de guardarlo.", "warning")
                return redirect(url_for("edit_pago", item_id=item_id))
            con.execute("UPDATE pagos SET fecha=?,concepto=?,importe=?,categoria=?,notas=? WHERE id=?",
                (fecha, concepto, importe, categoria, request.form.get("notas", "").strip(), item_id))
        flash("Pago actualizado.", "success")
        return redirect(url_for("pagos"))
    with db() as con:
        editing = con.execute("SELECT * FROM pagos WHERE id=?", (item_id,)).fetchone()
        rows, total_received = get_payment_history(con)
    if not editing:
        abort(404)
    return render_template("pagos.html", rows=rows, total_received=total_received,
        today=canary_now().date().isoformat(), editing=editing)


def month_bounds(month_key):
    if not isinstance(month_key, str) or len(month_key) != 7 or month_key[4] != "-":
        raise ValueError("Invalid month")
    year = int(month_key[:4])
    month = int(month_key[5:])
    first = date(year, month, 1)
    following = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return first.isoformat(), following.isoformat()


def build_monthly_report(month_key):
    first_day, next_month = month_bounds(month_key)
    with db() as con:
        jornada_rows = con.execute(
            "SELECT * FROM jornadas WHERE fecha>=? AND fecha<? ORDER BY fecha,id",
            (first_day, next_month),
        ).fetchall()
        trip_rows = con.execute(
            "SELECT * FROM viajes WHERE fecha>=? AND fecha<? ORDER BY fecha,id",
            (first_day, next_month),
        ).fetchall()
        payment_rows = con.execute(
            "SELECT * FROM pagos WHERE fecha>=? AND fecha<? ORDER BY fecha,id",
            (first_day, next_month),
        ).fetchall()
        paid_trip_receipts = con.execute(
            """SELECT * FROM viajes WHERE pagado=1
               AND COALESCE(NULLIF(fecha_pago,''),fecha)>=?
               AND COALESCE(NULLIF(fecha_pago,''),fecha)<? ORDER BY COALESCE(NULLIF(fecha_pago,''),fecha),id""",
            (first_day, next_month),
        ).fetchall()

    jornadas = [journey_for_display(row) for row in jornada_rows]
    viajes = [dict(row) for row in trip_rows]
    pagos = [dict(row) for row in payment_rows]
    cobros = [
        {"fecha": row["fecha"], "fecha_tipo": "Fecha del pago", "concepto": row["concepto"],
         "importe": row["importe"], "origen": "Pago anotado", "notas": row["notas"]}
        for row in pagos
    ]
    cobros.extend(
        {"fecha": row["fecha_pago"] or row["fecha"],
         "fecha_tipo": "Fecha del cobro" if row["fecha_pago"] else "Fecha del viaje (estimada)",
         "concepto": row["concepto"], "importe": row["importe"], "origen": "Viaje pagado", "notas": row["notas"]}
        for row in paid_trip_receipts
    )
    cobros.sort(key=lambda row: (row["fecha"], row["concepto"], row["origen"]), reverse=True)

    horas_normales = sum(float(row["horas_normales"] or 0) for row in jornadas)
    horas_extra = sum(float(row["horas_extra"] or 0) for row in jornadas)
    provisional_count = sum(1 for row in jornadas if row["provisional"])
    incomplete_count = sum(1 for row in jornadas if not row.get("entrada") or not row.get("salida"))
    viajes_total = sum(float(row["importe"] or 0) for row in viajes)
    viajes_pagados = sum(float(row["importe"] or 0) for row in viajes if row["pagado"])
    viajes_pendientes = sum(float(row["importe"] or 0) for row in viajes if not row["pagado"])
    total_recibido = sum(float(row["importe"] or 0) for row in cobros)

    rate = get_setting("hora_extra_euros", "").strip()
    try:
        hourly_rate = float(rate.replace(",", ".")) if rate else None
        if hourly_rate is not None and (not math.isfinite(hourly_rate) or hourly_rate < 0):
            hourly_rate = None
    except ValueError:
        hourly_rate = None
    importe_extra = horas_extra * hourly_rate if hourly_rate is not None else None

    nombres_meses = ("enero", "febrero", "marzo", "abril", "mayo", "junio",
                     "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")
    year, month = int(month_key[:4]), int(month_key[5:])
    return {
        "mes": month_key,
        "mes_label": f"{nombres_meses[month - 1].capitalize()} {year}",
        "jornadas": jornadas,
        "viajes": viajes,
        "cobros": cobros,
        "dias_registrados": len(jornadas),
        "horas_normales": horas_normales,
        "horas_extra": horas_extra,
        "provisional_count": provisional_count,
        "incomplete_count": incomplete_count,
        "viajes_total": viajes_total,
        "viajes_pagados": viajes_pagados,
        "viajes_pendientes": viajes_pendientes,
        "total_recibido": total_recibido,
        "importe_extra": importe_extra,
        "hourly_rate": hourly_rate,
    }


def build_annual_report(year_value):
    if not isinstance(year_value, str) or len(year_value) != 4:
        raise ValueError("Invalid year")
    year = int(year_value)
    if year < 2000 or year > 2100:
        raise ValueError("Invalid year")
    first_day = date(year, 1, 1).isoformat()
    next_year = date(year + 1, 1, 1).isoformat()
    with db() as con:
        jornadas = con.execute(
            "SELECT * FROM jornadas WHERE fecha>=? AND fecha<? ORDER BY fecha,id",
            (first_day, next_year),
        ).fetchall()
        viajes = con.execute(
            "SELECT * FROM viajes WHERE fecha>=? AND fecha<? ORDER BY fecha,id",
            (first_day, next_year),
        ).fetchall()
        pagos = con.execute(
            "SELECT * FROM pagos WHERE fecha>=? AND fecha<? ORDER BY fecha,id",
            (first_day, next_year),
        ).fetchall()
        viajes_cobrados = con.execute(
            """SELECT * FROM viajes WHERE pagado=1
               AND COALESCE(NULLIF(fecha_pago,''),fecha)>=?
               AND COALESCE(NULLIF(fecha_pago,''),fecha)<?""",
            (first_day, next_year),
        ).fetchall()

    jornada_rows = [journey_for_display(row) for row in jornadas]
    extra_hours = sum(float(row["horas_extra"] or 0) for row in jornada_rows)
    normal_hours = sum(float(row["horas_normales"] or 0) for row in jornada_rows)
    provisional_count = sum(1 for row in jornada_rows if row["provisional"])
    incomplete_count = sum(1 for row in jornada_rows if not row.get("entrada") or not row.get("salida"))
    pending_review_count = sum(1 for row in jornada_rows
                               if row["provisional"] or not row.get("entrada") or not row.get("salida"))
    trips_due = sum(float(row["importe"] or 0) for row in viajes if not row["pagado"])
    trips_paid_total = sum(float(row["importe"] or 0) for row in viajes if row["pagado"])
    payments_received = sum(float(row["importe"] or 0) for row in pagos)
    trip_receipts = sum(float(row["importe"] or 0) for row in viajes_cobrados)
    payments_for_hours = sum(float(row["importe"] or 0) for row in pagos if row["categoria"] == "horas_extra")

    rate = get_setting("hora_extra_euros", "").strip()
    try:
        hourly_rate = float(rate.replace(",", ".")) if rate else None
        if hourly_rate is not None and (not math.isfinite(hourly_rate) or hourly_rate < 0):
            hourly_rate = None
    except ValueError:
        hourly_rate = None
    overtime_estimated = extra_hours * hourly_rate if hourly_rate is not None else None
    overtime_due = max(0, overtime_estimated - payments_for_hours) if overtime_estimated is not None else None

    return {
        "year": year,
        "first_day": first_day,
        "days_registered": len(jornada_rows),
        "normal_hours": normal_hours,
        "extra_hours": extra_hours,
        "provisional_count": provisional_count,
        "incomplete_count": incomplete_count,
        "pending_review_count": pending_review_count,
        "trips_due": trips_due,
        "trips_paid": trips_paid_total,
        "received_total": payments_received + trip_receipts,
        "received_payments": payments_received,
        "received_trips": trip_receipts,
        "overtime_estimated": overtime_estimated,
        "overtime_paid": payments_for_hours,
        "overtime_due": overtime_due,
        "hourly_rate": hourly_rate,
    }


@app.route("/informes/anual")
@login_required
def informe_anual():
    year_value = request.args.get("anio") or str(canary_now().date().year)
    try:
        report = build_annual_report(year_value)
    except (ValueError, TypeError):
        flash("Elige un año válido para generar el resumen.", "danger")
        return redirect(url_for("informe_anual"))
    return render_template("informe_anual.html", **report)


@app.route("/informes")
@login_required
def informes():
    mes = request.args.get("mes") or canary_now().date().strftime("%Y-%m")
    try:
        report = build_monthly_report(mes)
    except (ValueError, TypeError):
        flash("Elige un mes válido para generar el informe.", "danger")
        return redirect(url_for("informes"))

    if request.args.get("descargar") == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter=";")
        headers = ["Tipo", "Fecha", "Concepto", "Horas normales", "Horas extra",
                   "Importe de viajes (€)", "Importe cobrado (€)", "Estado", "Notas"]
        writer.writerow(headers)
        writer.writerow(["Resumen", mes, "Registros de jornada", report["dias_registrados"], "", "", "", "", ""])
        writer.writerow(["Resumen", mes, "Horas normales", report["horas_normales"], "", "", "", "", ""])
        writer.writerow(["Resumen", mes, "Horas extra", "", report["horas_extra"], "", "", "", ""])
        if report["importe_extra"] is not None:
            writer.writerow(["Resumen", mes, "Importe extra estimado", "", "", "", report["importe_extra"], "", ""])
        writer.writerow(["Resumen", mes, "Viajes del mes", "", "", report["viajes_total"], "", "", ""])
        writer.writerow(["Resumen", mes, "Viajes pendientes", "", "", report["viajes_pendientes"], "", "", ""])
        writer.writerow(["Resumen", mes, "Total recibido", "", "", "", report["total_recibido"], "", ""])
        for row in report["jornadas"]:
            estado = "Provisional · revisar" if row["provisional"] else "Revisado"
            writer.writerow(["Jornada", row["fecha"], f'{row["entrada"] or ""}-{row["salida"] or ""}',
                             row["horas_normales"], row["horas_extra"], "", "", estado, row["notas"]])
        for row in report["viajes"]:
            estado = "Pagado" if row["pagado"] else "Pendiente"
            writer.writerow(["Viaje", row["fecha"], row["concepto"], "", "", row["importe"], "", estado, row["notas"]])
        for row in report["cobros"]:
            writer.writerow([row["origen"], row["fecha"], row["concepto"], "", "", "",
                             row["importe"], row["fecha_tipo"], row["notas"]])
        data = io.BytesIO(output.getvalue().encode("utf-8-sig"))
        data.seek(0)
        return send_file(data, mimetype="text/csv; charset=utf-8",
                         as_attachment=True, download_name=f"informe-{mes}.csv")

    return render_template("informes.html", **report)


@app.route("/ajustes", methods=["GET", "POST"])
@login_required
def ajustes():
    if request.method == "POST":
        rate = request.form.get("hora_extra_euros", "").strip().replace(",", ".")
        if rate:
            try:
                if not math.isfinite(float(rate)) or float(rate) < 0: raise ValueError
            except ValueError:
                flash("La tarifa debe ser un número válido no negativo.", "danger")
                return redirect(url_for("ajustes"))
        set_setting("hora_extra_euros", rate)
        set_setting("horas_normales_dia", request.form.get("horas_normales_dia", "8"))
        set_setting("horario_habitual", request.form.get("horario_habitual", "08:00-16:00, sin pausa"))
        set_setting("dias_trabajo", request.form.get("dias_trabajo", "Lunes a viernes"))
        flash("Configuración guardada.", "success")
        return redirect(url_for("ajustes"))
    return render_template("ajustes.html", rate=get_setting("hora_extra_euros"),
        normal=get_setting("horas_normales_dia","8"), schedule=get_setting("horario_habitual"),
        days=get_setting("dias_trabajo","Lunes a viernes"))


def assistant_local_response(prompt, monthly_hours, totals, hourly_rate):
    question = unicodedata.normalize("NFKD", prompt.casefold())
    question = "".join(char for char in question if not unicodedata.combining(char))

    overtime_hours = float(totals["horas_extra"] or 0)
    pending_trips = float(totals["viajes_pendientes_eur"] or 0)
    paid_trips = float(totals["viajes_pagados_eur"] or 0)
    recorded_payments = float(totals["pagos_registrados_eur"] or 0)
    estimated_overtime = overtime_hours * hourly_rate if hourly_rate is not None else None

    if any(term in question for term in ("viaje", "desplazamiento")):
        pending_count = int(totals["viajes_pendientes_count"] or 0)
        paid_count = int(totals["viajes_pagados_count"] or 0)
        return (
            f"Viajes registrados: {pending_count} pendientes por {money(pending_trips)} "
            f"y {paid_count} pagados por {money(paid_trips)}."
        )

    if any(term in question for term in ("hora", "jornada", "extra")):
        lines = ["Horas extra registradas por mes:"]
        if monthly_hours:
            for row in monthly_hours:
                month = row["mes"]
                month_hours = f"{float(row['horas'] or 0):.2f}".replace(".", ",")
                lines.append(f"- {month[5:7]}/{month[:4]}: {month_hours} h")
        else:
            lines.append("- No hay horas extra registradas.")
        total_hours_text = f"{overtime_hours:.2f}".replace(".", ",")
        lines.append(f"Total acumulado: {total_hours_text} h.")
        if estimated_overtime is None:
            lines.append("No hay una tarifa por hora configurada para estimar el importe.")
        else:
            lines.append(f"Importe estimado a {money(hourly_rate)}/h: {money(estimated_overtime)}.")
        lines.append("El cálculo usa los registros guardados; revisa los apuntes que sean provisionales.")
        return "\n".join(lines)

    if any(term in question for term in ("pago", "cobrado", "recibido")):
        total_received = paid_trips + recorded_payments
        return (
            f"Pagos anotados en el histórico: {money(recorded_payments)}. "
            f"Viajes marcados como pagados: {money(paid_trips)}. "
            f"Total recibido registrado entre ambos: {money(total_received)}."
        )

    lines = [
        "Resumen de tus datos guardados:",
        f"- Horas extra: {overtime_hours:.2f}".replace(".", ",") + " h.",
    ]
    if estimated_overtime is None:
        lines.append("- Importe de horas extra: configura la tarifa en Ajustes para calcularlo.")
    else:
        lines.append(f"- Importe estimado de horas extra: {money(estimated_overtime)}.")
    lines.extend([
        f"- Viajes pendientes: {money(pending_trips)}.",
        f"- Viajes pagados: {money(paid_trips)}.",
        f"- Pagos anotados en el histórico: {money(recorded_payments)}.",
        "Las horas marcadas como provisionales deben comprobarse.",
    ])
    return "\n".join(lines)


@app.route("/asistente", methods=["GET", "POST"])
@login_required
def asistente():
    respuesta = None
    prompt = ""
    if request.method == "POST":
        prompt = request.form.get("prompt", "").strip()
        if not prompt:
            respuesta = "Escribe qué quieres consultar."
        else:
            with db() as con:
                monthly_hours = con.execute("""SELECT substr(fecha,1,7) mes,
                    ROUND(SUM(horas_extra),2) horas
                    FROM jornadas GROUP BY substr(fecha,1,7) ORDER BY mes""").fetchall()
                totals = con.execute("""SELECT
                    (SELECT COALESCE(SUM(horas_extra),0) FROM jornadas) horas_extra,
                    (SELECT COALESCE(SUM(importe),0) FROM viajes WHERE pagado=0) viajes_pendientes_eur,
                    (SELECT COALESCE(SUM(importe),0) FROM viajes WHERE pagado=1) viajes_pagados_eur,
                    (SELECT COALESCE(SUM(importe),0) FROM pagos) pagos_registrados_eur,
                    (SELECT COUNT(*) FROM viajes WHERE pagado=0) viajes_pendientes_count,
                    (SELECT COUNT(*) FROM viajes WHERE pagado=1) viajes_pagados_count
                """).fetchone()
            rate = get_setting("hora_extra_euros", "").strip()
            try:
                hourly_rate = float(rate.replace(",", ".")) if rate else None
                if hourly_rate is not None and (not math.isfinite(hourly_rate) or hourly_rate < 0):
                    hourly_rate = None
            except ValueError:
                hourly_rate = None
            respuesta = assistant_local_response(prompt, monthly_hours, totals, hourly_rate)
    return render_template("asistente.html", respuesta=respuesta, prompt=prompt)


@app.route("/exportar")
@login_required
def exportar():
    table = request.args.get("tabla", "jornadas")
    allowed = {"jornadas", "viajes", "pagos", "notas", "ajustes"}
    if table not in allowed:
        abort(400)
    with db() as con:
        if table == "pagos":
            rows, _ = get_payment_history(con)
            rows = [{key: row[key] for key in ("fecha", "fecha_tipo", "concepto", "categoria", "importe", "origen", "notas")} for row in rows]
        elif table == "jornadas":
            records = con.execute("SELECT * FROM jornadas ORDER BY fecha DESC,id DESC").fetchall()
            rows = []
            for record in records:
                item = journey_for_display(record)
                item["estado"] = "Provisional · revisar" if item["provisional"] else "Revisado"
                rows.append({key: item[key] for key in ("id", "fecha", "entrada", "salida", "horas_normales", "horas_extra", "estado", "notas", "creado")})
        else:
            rows = con.execute(f"SELECT * FROM {table}").fetchall()
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys(), delimiter=";")
        writer.writeheader()
        writer.writerows([dict(r) for r in rows])
    else:
        output.write("")
    data = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    data.seek(0)
    return send_file(data, mimetype="text/csv; charset=utf-8", as_attachment=True, download_name=f"{table}.csv")


@app.route("/backup", methods=["GET", "POST"])
@login_required
def backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if request.method == "POST":
        try:
            dest = create_backup_snapshot()
            flash(f"Copia creada y comprobada: {os.path.basename(dest)}", "success")
        except Exception:
            app.logger.exception("Backup creation failed")
            flash("No se pudo crear o comprobar la copia. Revisa el espacio disponible.", "danger")
        return redirect(url_for("backup"))
    files = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith((".db", ".json"))], reverse=True)
    return render_template("backup.html", files=files)


BACKUP_TABLES = ("jornadas", "viajes", "pagos", "ajustes", "notas")
BACKUP_COLUMNS = {
    "jornadas": {"id", "fecha", "entrada", "salida", "horas_normales", "horas_extra", "notas", "creado"},
    "viajes": {"id", "fecha", "concepto", "importe", "pagado", "fecha_pago", "notas", "creado"},
    "pagos": {"id", "fecha", "concepto", "importe", "categoria", "notas", "creado"},
    "ajustes": {"clave", "valor"},
    "notas": {"id", "fecha", "titulo", "contenido", "creado"},
}


def create_backup_snapshot():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if DATABASE_URL:
        dest = os.path.join(BACKUP_DIR, f"mi_trabajo_{stamp}.json")
        snapshot = {}
        with db() as source:
            for table in BACKUP_TABLES:
                snapshot[table] = [dict(row) for row in source.execute(f"SELECT * FROM {table}").fetchall()]
        with open(dest, "w", encoding="utf-8") as output:
            json.dump(snapshot, output, ensure_ascii=False, indent=2, default=str)
    else:
        dest = os.path.join(BACKUP_DIR, f"mi_trabajo_{stamp}.db")
        with db() as source, SQLiteCompat(sqlite3.connect(dest)) as target:
            source.backup(target)
    if not validate_backup_file(dest):
        os.remove(dest)
        raise ValueError("Backup validation failed")
    return dest


def validate_backup_file(path):
    try:
        if path.endswith(".json"):
            with open(path, encoding="utf-8") as source:
                data = json.load(source)
            return all(
                isinstance(data.get(table), list)
                and all(isinstance(row, dict) and set(row).issubset(BACKUP_COLUMNS[table]) for row in data[table])
                for table in BACKUP_TABLES
            )
        if path.endswith(".db"):
            with SQLiteCompat(sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)) as con:
                integrity = con.execute("PRAGMA quick_check").fetchone()[0]
                tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return integrity == "ok" and {"jornadas", "viajes", "pagos", "ajustes", "notas"}.issubset(tables)
    except (OSError, ValueError, sqlite3.Error, TypeError):
        app.logger.exception("Backup validation failed")
    return False


def restore_postgres_snapshot(snapshot):
    with db() as con:
        for table in reversed(BACKUP_TABLES):
            con.execute(f"DELETE FROM {table}")
        for table in BACKUP_TABLES:
            for row in snapshot[table]:
                columns = sorted(row)
                if not columns:
                    continue
                names = ",".join(columns)
                placeholders = ",".join("?" for _ in columns)
                con.execute(f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                    tuple(row[column] for column in columns))
        for table in ("jornadas", "viajes", "pagos", "notas"):
            con.execute(f"""SELECT setval(pg_get_serial_sequence('{table}','id'),
                COALESCE((SELECT MAX(id) FROM {table}),1), EXISTS(SELECT 1 FROM {table}))""")


@app.post("/backup/restaurar")
@login_required
def restore_backup():
    uploaded = request.files.get("backup_file")
    if not uploaded or not uploaded.filename or request.form.get("confirm_restore") != "yes":
        flash("Selecciona una copia y confirma que quieres restaurarla.", "danger")
        return redirect(url_for("backup"))
    extension = ".json" if DATABASE_URL else ".db"
    if os.path.splitext(uploaded.filename)[1].lower() != extension:
        flash(f"Esta instalación solo puede restaurar copias {extension}.", "danger")
        return redirect(url_for("backup"))
    os.makedirs(BACKUP_DIR, exist_ok=True)
    staged_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="restaurar_", suffix=extension, dir=BACKUP_DIR, delete=False) as staged:
            staged_path = staged.name
        uploaded.save(staged_path)
        if not validate_backup_file(staged_path):
            flash("La copia no superó la comprobación; no se modificaron los datos.", "danger")
            return redirect(url_for("backup"))
        safety_copy = create_backup_snapshot()
        if DATABASE_URL:
            with open(staged_path, encoding="utf-8") as source:
                restore_postgres_snapshot(json.load(source))
        else:
            os.replace(staged_path, DB_PATH)
            staged_path = None
        flash(f"Copia restaurada. Se guardó una copia de seguridad previa: {os.path.basename(safety_copy)}", "success")
    except Exception:
        app.logger.exception("Backup restore failed")
        flash("No se pudo restaurar la copia. Se conservaron los datos actuales.", "danger")
    finally:
        if staged_path and os.path.exists(staged_path):
            os.remove(staged_path)
    return redirect(url_for("backup"))


@app.route("/backup/<path:filename>")
@login_required
def download_backup(filename):
    if "/" in filename or "\\" in filename or not filename.endswith((".db", ".json")):
        abort(400)
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.isfile(path):
        abort(404)
    if not validate_backup_file(path):
        abort(400, description="La copia no supera la comprobación de integridad.")
    return send_file(path, as_attachment=True, download_name=filename)


with app.app_context():
    init_db()
    seed_known_records()

if __name__ == "__main__":
    # Solo para pruebas locales. Para usar desde fuera, despliega detrás de HTTPS.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)

