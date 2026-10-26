#!/usr/bin/env python3
"""Import a local mi_trabajo.db SQLite backup into Neon/PostgreSQL.

Safe by default: prints a preview only. Pass --apply to write.
The destination must be empty, except for the app's known first-run demo seed.
Never put DATABASE_URL in this file or commit it.
"""
import argparse
import os
import sqlite3
import sys
from urllib.parse import urlparse

TABLES = ("jornadas", "viajes", "pagos", "ajustes", "notas")
BUSINESS_TABLES = ("jornadas", "viajes", "pagos", "notas")
DEFAULT_SETTINGS = {
    "hora_extra_euros": "",
    "horas_normales_dia": "8",
    "dias_trabajo": "Lunes a viernes",
    "horario_habitual": "08:00-16:00, sin pausa",
    "seeded_demo": "no",
}


def sqlite_rows(conn, table):
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')]


def pg_rows(conn, table):
    with conn.cursor() as cur:
        cur.execute(f'SELECT * FROM "{table}"')
        names = [d.name for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def is_known_demo_destination(conn):
    """Accept only the app's exact first-run seed, never arbitrary user data."""
    rows = {t: pg_rows(conn, t) for t in TABLES}
    if any(rows[t] for t in BUSINESS_TABLES):
        # The app seeds two jornadas and two viajes. Accept only those exact
        # demo records, with their known notes/concepts and amounts.
        j = rows["jornadas"]
        v = rows["viajes"]
        if len(j) != 2 or len(v) != 2 or rows["pagos"] or rows["notas"]:
            return False
        j_sig = sorted((str(x["fecha"])[:10], float(x["horas_extra"]), str(x["notas"]))
                       for x in j)
        if sorted((x[0], x[1]) for x in j_sig) != [
            ("2026-09-30", 18.5), ("2026-10-26", 21.0)
        ]:
            return False
        if any("Total" not in x[2] for x in j_sig):
            return False
        if sorted((str(x["fecha"])[:10], str(x["concepto"]), float(x["importe"]), int(x["pagado"]))
                  for x in v) != [
            ("2026-10-31", "Viaje de trabajo de octubre 1", 200.0, 0),
            ("2026-10-31", "Viaje de trabajo de octubre 2", 200.0, 0),
        ]:
            return False

    settings = {str(x["clave"]): str(x["valor"]) for x in rows["ajustes"]}
    # Only default keys plus seeded_demo=yes are allowed on destination.
    allowed = dict(DEFAULT_SETTINGS)
    allowed["seeded_demo"] = "yes"
    return all(k in allowed and v == allowed[k] for k, v in settings.items())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite_file", help="Ruta al archivo mi_trabajo.db de la copia")
    parser.add_argument("--apply", action="store_true", help="Realizar la importación (sin esto solo previsualiza)")
    args = parser.parse_args()

    path = os.path.abspath(args.sqlite_file)
    if not os.path.isfile(path):
        sys.exit(f"ERROR: no existe el archivo: {path}")
    if not os.environ.get("DATABASE_URL", "").strip():
        sys.exit("ERROR: configura DATABASE_URL solo en la terminal/entorno local; no lo escribas en el script.")

    try:
        import psycopg
    except ImportError:
        sys.exit("Falta psycopg. Instala las dependencias con: py -m pip install 'psycopg[binary]>=3.2,<4.0'")

    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        table_names = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = set(TABLES) - table_names
        if missing:
            sys.exit("ERROR: la copia no contiene las tablas esperadas: " + ", ".join(sorted(missing)))
        data = {t: sqlite_rows(source, t) for t in TABLES}
    finally:
        source.close()

    print("Copia SQLite:", path)
    for table in TABLES:
        print(f"  {table}: {len(data[table])} registros")
    if not any(data.values()):
        sys.exit("ERROR: la copia no contiene registros; no se ha importado nada.")

    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10) as dest:
        if not is_known_demo_destination(dest):
            sys.exit(
                "ERROR: Neon no está vacío o contiene datos que no son la semilla conocida. "
                "No se ha cambiado nada. No borres registros manualmente; pide revisión primero."
            )
        print("\nDestino: Neon accesible; contiene solo tablas vacías/semilla inicial conocida.")
        print("Modo:", "IMPORTACIÓN REAL" if args.apply else "VISTA PREVIA (sin cambios)")
        if not args.apply:
            print("Si los recuentos son correctos, repite el comando añadiendo --apply.")
            return

        with dest.transaction():
            # Delete only after verifying the destination contains no user data.
            for table in ("notas", "pagos", "viajes", "jornadas", "ajustes"):
                with dest.cursor() as cur:
                    cur.execute(f'DELETE FROM "{table}"')
            for table in TABLES:
                if not data[table]:
                    continue
                columns = list(data[table][0].keys())
                col_sql = ", ".join('"' + c.replace('"', '""') + '"' for c in columns)
                placeholders = ", ".join(["%s"] * len(columns))
                with dest.cursor() as cur:
                    cur.executemany(
                        f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders})',
                        [tuple(row[c] for c in columns) for row in data[table]],
                    )
            # Keep future generated IDs above imported IDs.
            for table in ("jornadas", "viajes", "pagos", "notas"):
                with dest.cursor() as cur:
                    cur.execute(
                        "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                        "COALESCE((SELECT MAX(id) FROM " + table + "), 1), "
                        "EXISTS(SELECT 1 FROM " + table + "))",
                        (table,),
                    )

        print("\nImportación completada. Recuento comprobado:")
        for table in TABLES:
            actual = len(pg_rows(dest, table))
            expected = len(data[table])
            print(f"  {table}: {actual}/{expected}")
            if actual != expected:
                sys.exit("ERROR: el recuento no coincide. No conectes Render todavía.")
        print("IMPORTACIÓN VERIFICADA. Aún no se ha cambiado Render ni desplegado nada.")


if __name__ == "__main__":
    main()
