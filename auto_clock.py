"""Create an unverified 08:00–16:00 weekday entry on first app visit after 16:00 local time."""
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    CANARY_TZ = ZoneInfo("Atlantic/Canary")
except ZoneInfoNotFoundError:
    CANARY_TZ = None


def record_scheduled_shift(db_factory, now=None):
    if now is None:
        now = datetime.now(CANARY_TZ) if CANARY_TZ else datetime.now().astimezone()
    elif now.tzinfo is None:
        local_tz = CANARY_TZ or datetime.now().astimezone().tzinfo
        now = now.replace(tzinfo=local_tz)
    elif CANARY_TZ:
        now = now.astimezone(CANARY_TZ)

    if now.weekday() >= 5 or now.hour < 16:
        return False

    work_date = now.date().isoformat()
    with db_factory() as con:
        existing = con.execute(
            "SELECT id FROM jornadas WHERE fecha=? LIMIT 1", (work_date,)
        ).fetchone()
        if existing:
            return False
        con.execute(
            """INSERT INTO jornadas
               (fecha, entrada, salida, horas_normales, horas_extra, notas, creado)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (work_date, "08:00", "16:00", 8, 0,
             "[SIN REVISAR] Fichaje automático según el horario habitual; no verificado manualmente. Corrige este registro si el horario real fue distinto.",
             now.isoformat()),
        )
    return True
