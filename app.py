import os
import sqlite3
import csv
import io
import json
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
app.secret_key = os.environ.get("SECRET_KEY", "cambia-esta-clave-antes-de-publicar")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


class PostgresCompat:
    def __init__(self, connection): self.connection = connection
    def __enter__(self):
        self.connection.__enter__()
        return self
    def __exit__(self, exc_type, exc, tb): return self.connection.__exit__(exc_type, exc, tb)
    def execute(self, sql, params=()):
        sql = sql.replace("?", "%s")
        sql = sql.replace("INSERT OR IGNORE INTO ajustes", "INSERT INTO ajustes")
        if sql.lstrip().upper().startswith("INSERT INTO AJUSTES") and "ON CONFLICT" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT (clave) DO NOTHING"
        return self.connection.execute(sql, params)
    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip(): self.execute(statement)
        return self

def db():
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        return PostgresCompat(psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10))
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


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
            return redirect(request.args.get("next") or url_for("dashboard"))
        else:
            flash("Contraseña incorrecta.", "danger")
    return render_template("login.html")


@app.route("/logout")
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
        payments = con.execute("SELECT COALESCE(SUM(importe),0) n FROM pagos").fetchone()["n"]
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
            date.fromisoformat(fecha)
            normales = max(0, float(request.form.get("horas_normales") or 0))
            extra = max(0, float(request.form.get("horas_extra") or 0))
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
        normal_default=get_setting("horas_normales_dia", "8"))


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
        try:
            date.fromisoformat(fecha)
            importe = max(0, float(request.form.get("importe", "200").replace(",", ".")))
        except (ValueError, TypeError):
            flash("Revisa la fecha y el importe.", "danger")
            return redirect(url_for("viajes"))
        with db() as con:
            con.execute("""INSERT INTO viajes(fecha,concepto,importe,pagado,notas,creado)
                VALUES(?,?,?,?,?,?)""", (fecha, request.form.get("concepto","Viaje de trabajo").strip() or "Viaje de trabajo",
                importe, 0, request.form.get("notas","").strip(), datetime.now().isoformat()))
        flash("Viaje añadido como pendiente de pago.", "success")
        return redirect(url_for("viajes"))
    with db() as con:
        rows = con.execute("SELECT * FROM viajes ORDER BY fecha DESC,id DESC").fetchall()
    return render_template("viajes.html", rows=rows, today=date.today().isoformat())


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
        try:
            fecha = request.form.get("fecha", "")
            date.fromisoformat(fecha)
            importe = max(0, float(request.form.get("importe", "0").replace(",", ".")))
        except (ValueError, TypeError):
            flash("Revisa la fecha y el importe.", "danger")
            return redirect(url_for("pagos"))
        with db() as con:
            con.execute("""INSERT INTO pagos(fecha,concepto,importe,categoria,notas,creado)
                VALUES(?,?,?,?,?,?)""", (fecha, request.form.get("concepto","Pago").strip() or "Pago",
                importe, request.form.get("categoria","otro"), request.form.get("notas","").strip(),
                datetime.now().isoformat()))
        flash("Pago registrado en el histórico.", "success")
        return redirect(url_for("pagos"))
    with db() as con:
        rows = con.execute("SELECT * FROM pagos ORDER BY fecha DESC,id DESC").fetchall()
    return render_template("pagos.html", rows=rows, today=date.today().isoformat())


@app.route("/ajustes", methods=["GET", "POST"])
@login_required
def ajustes():
    if request.method == "POST":
        rate = request.form.get("hora_extra_euros", "").strip().replace(",", ".")
        if rate:
            try:
                if float(rate) < 0: raise ValueError
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
                settings = con.execute("SELECT clave,valor FROM ajustes").fetchall()
            context = {
                "jornadas_recientes": [dict(x) for x in hrs],
                "viajes": [dict(x) for x in trs],
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
            except Exception as e:
                respuesta = "No se pudo contactar con el servicio de IA. Revisa la clave y la configuración del servidor. Detalle: " + str(e)[:300]
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
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if DATABASE_URL:
            dest = os.path.join(BACKUP_DIR, f"mi_trabajo_{stamp}.json")
            snapshot = {}
            with db() as source:
                for table in ("jornadas", "viajes", "pagos", "ajustes", "notas"):
                    snapshot[table] = [dict(row) for row in source.execute(f"SELECT * FROM {table}").fetchall()]
            with open(dest, "w", encoding="utf-8") as output:
                json.dump(snapshot, output, ensure_ascii=False, indent=2, default=str)
        else:
            dest = os.path.join(BACKUP_DIR, f"mi_trabajo_{stamp}.db")
            with db() as source, sqlite3.connect(dest) as target:
                source.backup(target)
        flash(f"Copia creada: {os.path.basename(dest)}", "success")
        return redirect(url_for("backup"))
    files = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith((".db", ".json"))], reverse=True)
    return render_template("backup.html", files=files)


@app.route("/backup/<path:filename>")
@login_required
def download_backup(filename):
    if "/" in filename or "\\" in filename or not filename.endswith((".db", ".json")):
        abort(400)
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename)


with app.app_context():
    init_db()
    seed_known_records()

if __name__ == "__main__":
    # Solo para pruebas locales. Para usar desde fuera, despliega detrás de HTTPS.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
