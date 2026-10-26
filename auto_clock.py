"""Create today's usual weekday shift after 16:00 Canary Islands time."""

from datetime import datetime
from zoneinfo import ZoneInfo

CANARY_TZ = ZoneInfo("Atlantic/Canary")


def record_scheduled_shift(db_factory, now=None):
    now = now or datetime.now(CANARY_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CANARY_TZ)
    else:
        now = now.astimezone(CANARY_TZ)

    if now.weekday() >= 5:
        print("No se crea fichaje: fin de semana.")
        return False
    if now.hour < 16:
        print(f"No se crea fichaje: son las {now:%H:%M} en Canarias.")
        return False

    work_date = now.date().isoformat()
    with db_factory() as con:
        existing = con.execute(
            "SELECT id FROM jornadas WHERE fecha=? LIMIT 1", (work_date,)
        ).fetchone()
        if existing:
            print(f"No se crea fichaje para {work_date}: ya existe un registro.")
            return False

        con.execute(
            """INSERT INTO jornadas
               (fecha, entrada, salida, horas_normales, horas_extra, notas, creado)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                work_date,
                "08:00",
                "16:00",
                8,
                0,
                "Fichaje automático según el horario habitual; no verificado manualmente. Corrige este registro si el horario real fue distinto.",
                now.isoformat(),
            ),
        )

    print(f"Fichaje automático creado para {work_date}: 08:00–16:00.")
    return True


if __name__ == "__main__":
    from app import db

    record_scheduled_shift(db)

