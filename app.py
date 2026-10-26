import os
import sqlite3
import csv
import io
import json
import math
import secrets
import tempfile
from datetime import date, datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, abort
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

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
        payments = con.execute("""SELECT
            (SELECT COALESCE(SUM(importe),0) FROM pagos) +
            (SELECT COALESCE(SUM(importe),0) FROM viajes WHERE pagado=1) n
        """).fetchone()["n"]
    rate = get_setting("hora_extra_euros", "").strip()
    try:
        rate_num = float(rate.replace(",", ".")) if rate else None
    except ValueError:
        rate_num = None
    overtime_due = total_h * rate_num if rate_num is not None else None
    return render_template("dashboard.html", total_h=total_h, trips_due=trips_due, trips_count=trips_count,
        months=[dict(x) for x in months], trips=trips, recent=recent, payments=payments,
        rate=rate_num, overtime_due=overtime_due, work_schedule=get_setting("horario_habitual"))


@app.route("/jornadas", methods=["GET", "POST"])
@login_required
def jornadas():
    if request.method == "POST":
        fecha = request.form.get("fecha", "")
        try:
            parse_form_date(fecha)
            normales = parse_nonnegative_number(request.form.get("horas_normales"), 0)
            extra = parse_nonnegative_number(request.form.get("horas_extra"), 0)
        except (ValueError, TypeError):
            flash("Revisa la fecha y las horas introducidas.", "danger")
            return redirect(url_for("jornadas"))
        with db() as con:
            con.execute("""INSERT INTO jornadas(fecha,entrada,salida,horas_normales,horas_extra,notas,creado)
                VALUES(?,?,?,?,?,?,?)""",
                (fecha, request.form.get("entrada","").strip(), request.form.get("salida","").strip(),
                 normales, extra, request.form.get("notas","").strip(), datetime.now().isoformat()))
        flash("Jornada guardada.", "success")
        return redirect(url_for("jornadas"))
    with db() as con:
        rows = con.execute("SELECT * FROM jornadas ORDER BY fecha DESC,id DESC").fetchall()
    return render_template("jornadas.html", rows=rows, today=date.today().isoformat(),
        normal_default=get_setting("horas_normales_dia", "8"), editing=None)


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
        with db() as con:
            con.execute("""UPDATE jornadas SET fecha=?,entrada=?,salida=?,horas_normales=?,horas_extra=?,notas=?
                WHERE id=?""", (fecha, request.form.get("entrada", "").strip(),
                request.form.get("salida", "").strip(), normales, extra,
                request.form.get("notas", "").strip(), item_id))
        flash("Jornada actualizada.", "success")
        return redirect(url_for("jornadas"))
    with db() as con:
        editing = con.execute("SELECT * FROM jornadas WHERE id=?", (item_id,)).fetchone()
        rows = con.execute("SELECT * FROM jornadas ORDER BY fecha DESC,id DESC").fetchall()
    if not editing:
        abort(404)
    return render_template("jornadas.html", rows=rows, today=date.today().isoformat(),
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
        rows = con.execute("SELECT * FROM viajes ORDER BY fecha DESC,id DESC").fetchall()
    return render_template("viajes.html", rows=rows, today=date.today().isoformat(), editing=None)


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
    return render_template("viajes.html", rows=rows, today=date.today().isoformat(), editing=editing)


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
        if not concepto or categoria not in {"horas_extra", "viaje", "otro"}:
            flash("Revisa el concepto y la categoría del pago.", "danger")
            return redirect(url_for("pagos"))
        with db() as con:
            con.execute("""INSERT INTO pagos(fecha,concepto,importe,categoria,notas,creado)
                VALUES(?,?,?,?,?,?)""", (fecha, concepto,
                importe, categoria, request.form.get("notas","").strip(),
                datetime.now().isoformat()))
        flash("Pago registrado en el histórico.", "success")
        return redirect(url_for("pagos"))
    with db() as con:
        rows = con.execute("SELECT * FROM pagos ORDER BY fecha DESC,id DESC").fetchall()
    return render_template("pagos.html", rows=rows, today=date.today().isoformat(), editing=None)


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
            con.execute("UPDATE pagos SET fecha=?,concepto=?,importe=?,categoria=?,notas=? WHERE id=?",
                (fecha, concepto, importe, categoria, request.form.get("notas", "").strip(), item_id))
        flash("Pago actualizado.", "success")
        return redirect(url_for("pagos"))
    with db() as con:
        editing = con.execute("SELECT * FROM pagos WHERE id=?", (item_id,)).fetchone()
        rows = con.execute("SELECT * FROM pagos ORDER BY fecha DESC,id DESC").fetchall()
    if not editing:
        abort(404)
    return render_template("pagos.html", rows=rows, today=date.today().isoformat(), editing=editing)


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


@app.route("/asistente", methods=["GET", "POST"])
@login_required
def asistente():
    respuesta = None
    prompt = ""
    if request.method == "POST":
        prompt = request.form.get("prompt", "").strip()
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not prompt:
            respuesta = "Escribe qué quieres registrar o consultar."
        elif not api_key:
            respuesta = ("El asistente IA aún no está activado: falta OPENAI_API_KEY en el servidor. "
                         "Tus registros y gráficos funcionan sin esa clave. No pongas la clave aquí en el chat; "
                         "configúrala como variable de entorno en el servidor.")
        elif OpenAI is None:
            respuesta = "Falta instalar la dependencia openai. Ejecuta: pip install -r requirements.txt"
        else:
            with db() as con:
                hrs = con.execute("SELECT fecha,horas_extra,notas FROM jornadas ORDER BY fecha DESC LIMIT 80").fetchall()
                trs = con.execute("SELECT fecha,concepto,importe,pagado,notas FROM viajes ORDER BY fecha DESC LIMIT 40").fetchall()
                payments = con.execute("SELECT fecha,concepto,importe,categoria,notas FROM pagos ORDER BY fecha DESC LIMIT 40").fetchall()
                totals = con.execute("""SELECT
                    (SELECT COALESCE(SUM(horas_extra),0) FROM jornadas) horas_extra,
                    (SELECT COALESCE(SUM(importe),0) FROM viajes WHERE pagado=0) viajes_pendientes_eur,
                    (SELECT COALESCE(SUM(importe),0) FROM viajes WHERE pagado=1) viajes_pagados_eur,
                    (SELECT COALESCE(SUM(importe),0) FROM pagos) pagos_registrados_eur
                """).fetchone()
                settings = con.execute("SELECT clave,valor FROM ajustes").fetchall()
            context = {
                "jornadas_recientes": [dict(x) for x in hrs],
                "viajes": [dict(x) for x in trs],
                "pagos_registrados": [dict(x) for x in payments],
                "totales": dict(totals),
                "ajustes": {x["clave"]: x["valor"] for x in settings}
            }
            try:
                client = OpenAI(api_key=api_key)
                response = client.responses.create(
                    model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
                    instructions=("Eres un asistente para organizar horas de trabajo y cantidades pendientes. "
                    "Responde en español claro. Usa solo los datos incluidos. No afirmes haber guardado o cambiado "
                    "registros: en esta versión solo puedes analizar y aconsejar, no escribir en la base de datos. "
                    "Distingue datos confirmados de provisionales. No des asesoramiento legal definitivo."),
                    input=f"Datos actuales en JSON:\n{context}\n\nPetición del usuario:\n{prompt}"
                )
                respuesta = response.output_text
            except Exception:
                app.logger.exception("AI assistant request failed")
                respuesta = "No se pudo contactar con el servicio de IA. Revisa la clave y la configuración del servidor."
    return render_template("asistente.html", respuesta=respuesta, prompt=prompt)


@app.route("/exportar")
@login_required
def exportar():
    table = request.args.get("tabla", "jornadas")
    allowed = {"jornadas", "viajes", "pagos", "notas", "ajustes"}
    if table not in allowed:
        abort(400)
    with db() as con:
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

