import os
import sqlite3
import json
from datetime import datetime, timezone, timedelta

DATA_DIR = os.environ.get("DATA_DIR", ".")
DB_PATH = os.path.join(DATA_DIR, "chilango.db")
PERU_TZ = timezone(timedelta(hours=-5))


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        # ── Perfiles de clientes (memoria cross-sesión) ────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS customer_profiles (
                phone TEXT PRIMARY KEY,
                nombre TEXT DEFAULT '',
                ultima_dir TEXT DEFAULT '',
                ultimo_pedido TEXT DEFAULT '',
                ultimo_pago TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                phone TEXT PRIMARY KEY,
                messages TEXT NOT NULL DEFAULT '[]',
                welcomed INTEGER NOT NULL DEFAULT 0,
                leida INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Migraciones de conversations
        for _m in [
            "ALTER TABLE conversations ADD COLUMN leida INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE conversations ADD COLUMN escalado INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE conversations ADD COLUMN last_msg_at TEXT DEFAULT ''",
        ]:
            try:
                c.execute(_m)
            except Exception:
                pass
        c.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha TEXT,
                hora TEXT,
                phone TEXT,
                items TEXT,
                total TEXT,
                estado TEXT DEFAULT 'Nuevo',
                metodo_pago TEXT DEFAULT 'Efectivo'
            )
        """)
        # Migraciones: agregar columnas nuevas si la BD ya existía sin ellas
        for migration in [
            "ALTER TABLE orders ADD COLUMN metodo_pago TEXT DEFAULT 'Efectivo'",
            "ALTER TABLE orders ADD COLUMN modificado INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE orders ADD COLUMN direccion TEXT DEFAULT ''",
            "ALTER TABLE orders ADD COLUMN notas TEXT DEFAULT ''",
        ]:
            try:
                c.execute(migration)
            except Exception:
                pass  # La columna ya existe
        # Migración: columna puntos en perfiles de clientes
        try:
            c.execute("ALTER TABLE customer_profiles ADD COLUMN puntos INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        # Migración: timestamp del último recordatorio enviado (evita spam)
        try:
            c.execute("ALTER TABLE conversations ADD COLUMN reminder_sent_at TEXT DEFAULT ''")
        except Exception:
            pass
        # Migración: timestamp de la última re-notificación de escalación urgente
        try:
            c.execute("ALTER TABLE conversations ADD COLUMN last_reescalation_at TEXT DEFAULT ''")
        except Exception:
            pass
        # Migraciones: seguimiento de encuesta post-entrega y timestamp "en camino"
        for _m2 in [
            "ALTER TABLE orders ADD COLUMN survey_sent INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE orders ADD COLUMN camino_at TEXT DEFAULT ''",
            "ALTER TABLE conversations ADD COLUMN carta_followup_sent_at TEXT DEFAULT ''",
        ]:
            try:
                c.execute(_m2)
            except Exception:
                pass

        # ── Configuración general del negocio ─────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            )
        """)

        # ── Consultas de costo de delivery pendientes ──────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS delivery_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_phone TEXT,
                client_phone TEXT,
                subtotal TEXT,
                items TEXT,
                pago TEXT,
                direccion TEXT,
                created_at TEXT
            )
        """)

        # ── Historial de costos de delivery (aprendizaje de zonas) ────
        c.execute("""
            CREATE TABLE IF NOT EXISTS delivery_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                direccion TEXT,
                costo REAL,
                subtotal TEXT,
                items TEXT,
                fecha TEXT
            )
        """)

        # ── Solicitudes de motorizado desde el panel ────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS moto_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER,
                client_phone TEXT,
                direccion TEXT,
                notas TEXT,
                items TEXT,
                estado TEXT DEFAULT 'pendiente',
                accepted_by TEXT DEFAULT '',
                created_at TEXT
            )
        """)

        # Normalizar estados sin emoji que quedaron por el DEFAULT antiguo
        c.execute("UPDATE orders SET estado='Nuevo 🆕' WHERE estado='Nuevo' OR estado IS NULL OR estado=''")
        c.execute("UPDATE orders SET estado='En preparación 👨‍🍳' WHERE estado='En preparación'")
        c.execute("UPDATE orders SET estado='En camino 🛵' WHERE estado='En camino'")
        c.execute("UPDATE orders SET estado='Entregado ✅' WHERE estado='Entregado'")

    # Inicializar menú editable (fuera del with para evitar conflictos de lock)
    init_menu_items()


# ── Customer profiles ─────────────────────────────────────────

def get_customer_profile(phone: str) -> dict:
    with _conn() as c:
        row = c.execute("SELECT * FROM customer_profiles WHERE phone=?", (phone,)).fetchone()
        return dict(row) if row else {}


def save_customer_profile(phone: str, nombre: str = None, ultima_dir: str = None,
                           ultimo_pedido: str = None, ultimo_pago: str = None):
    """Actualiza solo los campos que se pasen (no-None). La dirección 'Recojo' no se guarda."""
    now = datetime.now(PERU_TZ).strftime("%d/%m/%Y %H:%M")
    # No guardar "Recojo" como dirección permanente
    if ultima_dir and ultima_dir.strip().lower() == "recojo":
        ultima_dir = None
    with _conn() as c:
        exists = c.execute("SELECT 1 FROM customer_profiles WHERE phone=?", (phone,)).fetchone()
        if exists:
            updates, vals = [], []
            if nombre is not None and nombre.strip():
                updates.append("nombre=?"); vals.append(nombre.strip())
            if ultima_dir is not None and ultima_dir.strip():
                updates.append("ultima_dir=?"); vals.append(ultima_dir.strip())
            if ultimo_pedido is not None:
                updates.append("ultimo_pedido=?"); vals.append(ultimo_pedido)
            if ultimo_pago is not None:
                updates.append("ultimo_pago=?"); vals.append(ultimo_pago)
            if updates:
                updates.append("updated_at=?"); vals.append(now); vals.append(phone)
                c.execute(f"UPDATE customer_profiles SET {', '.join(updates)} WHERE phone=?", vals)
        else:
            c.execute(
                "INSERT INTO customer_profiles (phone, nombre, ultima_dir, ultimo_pedido, ultimo_pago, updated_at) VALUES (?,?,?,?,?,?)",
                (phone,
                 nombre.strip() if nombre else "",
                 ultima_dir.strip() if ultima_dir else "",
                 ultimo_pedido or "",
                 ultimo_pago or "",
                 now),
            )


# ── Conversations ─────────────────────────────────────────────

def get_messages(phone: str) -> list:
    with _conn() as c:
        row = c.execute("SELECT messages FROM conversations WHERE phone=?", (phone,)).fetchone()
        return json.loads(row["messages"]) if row else []


def save_messages(phone: str, messages: list):
    now = datetime.now(PERU_TZ).isoformat()
    with _conn() as c:
        c.execute("""
            INSERT INTO conversations (phone, messages, welcomed, last_msg_at)
            VALUES (?, ?, 0, ?)
            ON CONFLICT(phone) DO UPDATE SET messages=excluded.messages, last_msg_at=excluded.last_msg_at
        """, (phone, json.dumps(messages, ensure_ascii=False), now))


def is_welcomed(phone: str) -> bool:
    with _conn() as c:
        row = c.execute("SELECT welcomed FROM conversations WHERE phone=?", (phone,)).fetchone()
        return bool(row and row["welcomed"])


def mark_welcomed(phone: str):
    with _conn() as c:
        c.execute("""
            INSERT INTO conversations (phone, messages, welcomed)
            VALUES (?, '[]', 1)
            ON CONFLICT(phone) DO UPDATE SET welcomed=1
        """, (phone,))


def reset_conv(phone: str):
    with _conn() as c:
        c.execute("DELETE FROM conversations WHERE phone=?", (phone,))


def delete_conversation(phone: str):
    """Elimina completamente el historial de chat de un teléfono."""
    with _conn() as c:
        c.execute("DELETE FROM conversations WHERE phone=?", (phone,))


def mark_read(phone: str):
    with _conn() as c:
        c.execute("UPDATE conversations SET leida=1 WHERE phone=?", (phone,))


def mark_unread(phone: str):
    with _conn() as c:
        c.execute("UPDATE conversations SET leida=0 WHERE phone=?", (phone,))


def get_all_conversations() -> dict[str, list]:
    with _conn() as c:
        rows = c.execute(
            "SELECT phone, messages FROM conversations WHERE welcomed=1 AND messages != '[]'"
        ).fetchall()
        return {r["phone"]: json.loads(r["messages"]) for r in rows}


def get_conversations_with_status() -> dict[str, dict]:
    """Retorna {phone: {messages, leida, escalado, last_msg_at}} ordenado por actividad reciente."""
    with _conn() as c:
        rows = c.execute(
            "SELECT phone, messages, leida, escalado, last_msg_at "
            "FROM conversations WHERE welcomed=1 AND messages != '[]' "
            "ORDER BY last_msg_at DESC"
        ).fetchall()
        return {
            r["phone"]: {
                "messages": json.loads(r["messages"]),
                "leida": bool(r["leida"]),
                "escalado": bool(r["escalado"]),
                "last_msg_at": r["last_msg_at"] or "",
            }
            for r in rows
        }


def append_message(phone: str, role: str, content: str, ts: str = "", manual: bool = False):
    """Agrega un mensaje al historial sin reemplazarlo (para mensajes no procesados por Claude)."""
    now = datetime.now(PERU_TZ).isoformat()
    with _conn() as c:
        row = c.execute("SELECT messages FROM conversations WHERE phone=?", (phone,)).fetchone()
        msgs = json.loads(row["messages"]) if row else []
        entry: dict = {"role": role, "content": content}
        if ts:
            entry["ts"] = ts
        if manual:
            entry["manual"] = True
        msgs.append(entry)
        c.execute("""
            INSERT INTO conversations (phone, messages, welcomed, last_msg_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(phone) DO UPDATE SET messages=excluded.messages, last_msg_at=excluded.last_msg_at
        """, (phone, json.dumps(msgs, ensure_ascii=False), now))


# ── Orders ────────────────────────────────────────────────────

def save_order_db(phone: str, items: str, total: str, metodo_pago: str = "Efectivo", direccion: str = "", notas: str = ""):
    now = datetime.now(PERU_TZ)
    with _conn() as c:
        c.execute(
            "INSERT INTO orders (fecha, hora, phone, items, total, estado, metodo_pago, direccion, notas) VALUES (?,?,?,?,?,?,?,?,?)",
            (now.strftime("%d/%m/%Y"), now.strftime("%H:%M"), phone, items, total, "Nuevo 🆕", metodo_pago, direccion, notas),
        )


def get_orders_count() -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM orders").fetchone()
        return row["n"] if row else 0


def get_orders_for_date(date_str: str) -> list:
    """Retorna pedidos de una fecha específica (formato DD/MM/YYYY)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, fecha, hora, phone, items, total, estado, metodo_pago, modificado, direccion, notas FROM orders WHERE fecha=? ORDER BY id DESC",
            (date_str,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_available_dates() -> list:
    """Retorna lista de fechas con pedidos, más recientes primero."""
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT fecha FROM orders ORDER BY fecha DESC LIMIT 30"
        ).fetchall()
        return [r["fecha"] for r in rows]


def get_active_orders_count() -> int:
    """Cuenta pedidos en preparación ahora mismo (Nuevo + En preparación)."""
    today = datetime.now(PERU_TZ).strftime("%d/%m/%Y")
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE fecha=? AND estado IN ('Nuevo 🆕','En preparación 👨‍🍳')",
            (today,)
        ).fetchone()
        return row["n"] if row else 0


def get_active_orders_items() -> list[str]:
    """Retorna los items de pedidos activos (Nuevo + En preparación) de hoy."""
    today = datetime.now(PERU_TZ).strftime("%d/%m/%Y")
    with _conn() as c:
        rows = c.execute(
            "SELECT items FROM orders WHERE fecha=? AND estado IN ('Nuevo 🆕','En preparación 👨‍🍳')",
            (today,)
        ).fetchall()
        return [r["items"] or "" for r in rows]


def get_active_orders_with_time() -> list[dict]:
    """Retorna items y hora de inicio de pedidos activos (Nuevo + En preparación) de hoy.
    Permite calcular el tiempo ya transcurrido de cada pedido."""
    today = datetime.now(PERU_TZ).strftime("%d/%m/%Y")
    with _conn() as c:
        rows = c.execute(
            "SELECT items, hora FROM orders WHERE fecha=? AND estado IN ('Nuevo 🆕','En preparación 👨‍🍳')",
            (today,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_orders_today() -> list:
    now = datetime.now(PERU_TZ)
    today = now.strftime("%d/%m/%Y")
    with _conn() as c:
        rows = c.execute(
            "SELECT id, fecha, hora, phone, items, total, estado, metodo_pago, modificado, direccion, notas FROM orders WHERE fecha=? ORDER BY id DESC",
            (today,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_order_estado(order_id: int, estado: str):
    with _conn() as c:
        c.execute("UPDATE orders SET estado=? WHERE id=?", (estado, order_id))


def get_order_by_id(order_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT id, phone, items, total, estado, direccion, notas FROM orders WHERE id=?", (order_id,)
        ).fetchone()
        return dict(row) if row else None


def cancel_latest_order(phone: str) -> bool:
    """Marca el pedido más reciente del cliente como Cancelado. Retorna True si lo encontró."""
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM orders WHERE phone=? AND estado NOT IN ('Entregado ✅','Cancelado ❌') ORDER BY id DESC LIMIT 1",
            (phone,)
        ).fetchone()
        if not row:
            return False
        c.execute("UPDATE orders SET estado='Cancelado ❌' WHERE id=?", (row["id"],))
        return True


def delete_order(order_id: int):
    with _conn() as c:
        c.execute("DELETE FROM orders WHERE id=?", (order_id,))


def update_latest_order(phone: str, items: str, total: str, metodo_pago: str, direccion: str = "", notas: str = "") -> bool:
    """Actualiza el pedido más reciente del cliente que no esté entregado.
    Retorna True si se encontró y actualizó, False si no había pedido activo."""
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM orders WHERE phone=? AND estado != 'Entregado ✅' ORDER BY id DESC LIMIT 1",
            (phone,)
        ).fetchone()
        if not row:
            return False
        c.execute(
            "UPDATE orders SET items=?, total=?, metodo_pago=?, modificado=1, notas=?,"
            " direccion=CASE WHEN ?!='' THEN ? ELSE direccion END WHERE id=?",
            (items, total, metodo_pago, notas, direccion, direccion, row["id"]),
        )
        return True


# ── Consultas de costo de delivery ────────────────────────────

def save_delivery_query(delivery_phone: str, client_phone: str, subtotal: str,
                        items: str, pago: str, direccion: str):
    """Guarda una consulta de costo de delivery pendiente."""
    now = datetime.now(PERU_TZ).strftime("%d/%m/%Y %H:%M")
    with _conn() as c:
        # Solo una consulta activa por teléfono de delivery
        c.execute("DELETE FROM delivery_queries WHERE delivery_phone=?", (delivery_phone,))
        c.execute(
            "INSERT INTO delivery_queries (delivery_phone, client_phone, subtotal, items, pago, direccion, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (delivery_phone, client_phone, subtotal, items, pago, direccion, now),
        )


def get_pending_delivery_query(delivery_phone: str) -> dict | None:
    """Retorna la consulta pendiente para este teléfono de delivery, si existe."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM delivery_queries WHERE delivery_phone=? ORDER BY id DESC LIMIT 1",
            (delivery_phone,)
        ).fetchone()
        return dict(row) if row else None


def delete_delivery_query(query_id: int):
    """Elimina la consulta resuelta."""
    with _conn() as c:
        c.execute("DELETE FROM delivery_queries WHERE id=?", (query_id,))


# ── Consultas de costo pendientes (gestión manual por el dueño) ──

def save_pending_cost_query(client_phone: str, subtotal: str, items: str,
                             pago: str, direccion: str):
    """Guarda una consulta de costo pendiente para que el dueño la resuelva desde el panel."""
    now = datetime.now(PERU_TZ).strftime("%d/%m/%Y %H:%M")
    with _conn() as c:
        # Solo una consulta activa por cliente
        c.execute(
            "DELETE FROM delivery_queries WHERE client_phone=? AND delivery_phone='owner'",
            (client_phone,)
        )
        c.execute(
            "INSERT INTO delivery_queries (delivery_phone, client_phone, subtotal, items, pago, direccion, created_at)"
            " VALUES ('owner',?,?,?,?,?,?)",
            (client_phone, subtotal, items, pago, direccion, now),
        )


def get_all_pending_cost_queries() -> list[dict]:
    """Retorna todas las consultas de costo pendientes para el dueño."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM delivery_queries WHERE delivery_phone='owner' ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_pending_cost_query(client_phone: str):
    """Elimina la consulta de costo una vez enviada al cliente."""
    with _conn() as c:
        c.execute(
            "DELETE FROM delivery_queries WHERE client_phone=? AND delivery_phone='owner'",
            (client_phone,)
        )


# ── Historial de costos de delivery (aprendizaje de zonas) ───────

def save_delivery_cost(phone: str, direccion: str, costo: float,
                        subtotal: str, items: str):
    """Guarda el costo de delivery histórico para aprendizaje de zonas."""
    now = datetime.now(PERU_TZ).strftime("%d/%m/%Y %H:%M")
    with _conn() as c:
        c.execute(
            "INSERT INTO delivery_costs (phone, direccion, costo, subtotal, items, fecha)"
            " VALUES (?,?,?,?,?,?)",
            (phone, direccion, costo, subtotal, items, now),
        )


def get_delivery_cost_suggestion(direccion: str) -> dict | None:
    """Busca el costo más frecuente para direcciones similares (aprendizaje automático)."""
    if not direccion or len(direccion.strip()) < 5:
        return None
    # Palabras clave de más de 4 letras para buscar similitudes de zona
    words = [w.strip() for w in direccion.lower().split() if len(w.strip()) > 4]
    if not words:
        return None
    with _conn() as c:
        conditions = " OR ".join(["LOWER(direccion) LIKE ?" for _ in words])
        params = [f"%{w}%" for w in words]
        rows = c.execute(
            f"SELECT costo FROM delivery_costs WHERE {conditions} ORDER BY id DESC LIMIT 30",
            params,
        ).fetchall()
    if not rows:
        return None
    from collections import Counter
    costs = [round(r["costo"], 1) for r in rows]
    counter = Counter(costs)
    most_common_cost, count = counter.most_common(1)[0]
    return {"costo": most_common_cost, "count": count}


# ── Clientes / Programa de puntos ────────────────────────────

def get_customers_with_stats() -> list:
    """Retorna todos los clientes con conteo de pedidos y total gastado, ordenados por gasto."""
    import re as _re
    with _conn() as conn:
        customers = conn.execute(
            "SELECT phone, nombre, ultima_dir, ultimo_pedido, ultimo_pago, puntos, updated_at "
            "FROM customer_profiles ORDER BY updated_at DESC"
        ).fetchall()
        orders = conn.execute(
            "SELECT phone, total FROM orders WHERE estado NOT IN ('Cancelado ❌')"
        ).fetchall()

    # Calcular estadísticas por teléfono
    stats: dict = {}
    for o in orders:
        ph = o["phone"]
        if ph not in stats:
            stats[ph] = {"count": 0, "total": 0.0}
        stats[ph]["count"] += 1
        try:
            m = _re.search(r"(\d+(?:[.,]\d{1,2})?)", (o["total"] or ""))
            if m:
                stats[ph]["total"] += float(m.group(1).replace(",", "."))
        except Exception:
            pass

    result = []
    for row in customers:
        c = dict(row)
        s = stats.get(c["phone"], {"count": 0, "total": 0.0})
        c["total_pedidos"] = s["count"]
        c["total_gastado"] = round(s["total"], 2)
        result.append(c)

    result.sort(key=lambda x: x["total_gastado"], reverse=True)
    return result


def get_customers_with_stats_for_date(fecha: str) -> list:
    """Retorna clientes que compraron en una fecha específica (DD/MM/YYYY),
    con stats del día Y acumulado histórico."""
    import re as _re

    def _parse_total(val):
        try:
            m = _re.search(r"(\d+(?:[.,]\d{1,2})?)", (val or ""))
            return float(m.group(1).replace(",", ".")) if m else 0.0
        except Exception:
            return 0.0

    with _conn() as conn:
        # Pedidos del día
        orders_day = conn.execute(
            "SELECT phone, total FROM orders WHERE fecha=? AND estado NOT IN ('Cancelado ❌')",
            (fecha,)
        ).fetchall()

        if not orders_day:
            return []

        phones = list({o["phone"] for o in orders_day})

        # Stats del día por cliente
        stats_day: dict = {}
        for o in orders_day:
            ph = o["phone"]
            if ph not in stats_day:
                stats_day[ph] = {"count": 0, "total": 0.0}
            stats_day[ph]["count"] += 1
            stats_day[ph]["total"] += _parse_total(o["total"])

        # Stats acumuladas históricas por cliente
        placeholders = ",".join("?" * len(phones))
        orders_all = conn.execute(
            f"SELECT phone, total FROM orders WHERE phone IN ({placeholders}) AND estado NOT IN ('Cancelado ❌')",
            phones
        ).fetchall()

        stats_all: dict = {}
        for o in orders_all:
            ph = o["phone"]
            if ph not in stats_all:
                stats_all[ph] = {"count": 0, "total": 0.0}
            stats_all[ph]["count"] += 1
            stats_all[ph]["total"] += _parse_total(o["total"])

        result = []
        for ph in phones:
            row = conn.execute(
                "SELECT phone, nombre, ultima_dir, ultimo_pedido, ultimo_pago, puntos, updated_at "
                "FROM customer_profiles WHERE phone=?", (ph,)
            ).fetchone()
            c = dict(row) if row else {"phone": ph, "nombre": None, "ultima_dir": None,
                                       "ultimo_pedido": None, "ultimo_pago": None,
                                       "puntos": 0, "updated_at": None}
            d = stats_day.get(ph, {"count": 0, "total": 0.0})
            a = stats_all.get(ph, {"count": 0, "total": 0.0})
            # Stats del día
            c["total_pedidos"] = d["count"]
            c["total_gastado"]  = round(d["total"], 2)
            # Stats acumuladas
            c["total_pedidos_hist"] = a["count"]
            c["total_gastado_hist"]  = round(a["total"], 2)
            result.append(c)

    result.sort(key=lambda x: x["total_gastado"], reverse=True)
    return result


def update_customer_points(phone: str, puntos: int):
    """Actualiza los puntos de un cliente."""
    with _conn() as c:
        c.execute("UPDATE customer_profiles SET puntos=? WHERE phone=?", (puntos, phone))


# ── Configuración general ─────────────────────────────────────

def get_config(key: str, default: str = "") -> str:
    with _conn() as c:
        row = c.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_config(key: str, value: str):
    with _conn() as c:
        c.execute("""
            INSERT INTO config (key, value) VALUES (?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))


# ── Escalación (bot en pausa por conversación) ────────────────

def mark_escalated(phone: str):
    """Marca conversación como escalada — el bot no responderá hasta que el equipo la libere."""
    with _conn() as c:
        c.execute("UPDATE conversations SET escalado=1, last_reescalation_at='' WHERE phone=?", (phone,))


def reset_escalation(phone: str):
    """El equipo libera la conversación y el bot puede volver a responder."""
    with _conn() as c:
        c.execute("UPDATE conversations SET escalado=0, last_reescalation_at='' WHERE phone=?", (phone,))


def mark_reescalation_sent(phone: str):
    """Registra cuándo se envió la última re-notificación urgente de escalación."""
    now = datetime.now(PERU_TZ).isoformat()
    with _conn() as c:
        c.execute("UPDATE conversations SET last_reescalation_at=? WHERE phone=?", (now, phone))


def check_reescalation_cooldown(phone: str, minutes: int = 5) -> bool:
    """Devuelve True si ya pasó el cooldown desde la última re-notificación (o nunca se envió)."""
    from datetime import datetime as _dt
    cooldown_cutoff = (_dt.now(PERU_TZ) - timedelta(minutes=minutes)).isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT last_reescalation_at FROM conversations WHERE phone=?", (phone,)
        ).fetchone()
        if not row:
            return True
        last = row["last_reescalation_at"] or ""
        return not last or last < cooldown_cutoff


def is_escalated(phone: str) -> bool:
    with _conn() as c:
        row = c.execute("SELECT escalado FROM conversations WHERE phone=?", (phone,)).fetchone()
        return bool(row and row["escalado"])


# ── Recordatorios de confirmación pendiente ───────────────────

def mark_reminder_sent(phone: str):
    """Registra cuándo se envió el último recordatorio a este teléfono."""
    now = datetime.now(PERU_TZ).isoformat()
    with _conn() as c:
        c.execute("UPDATE conversations SET reminder_sent_at=? WHERE phone=?", (now, phone))


def get_pending_reminders(minutos: int = 10, cooldown_min: int = 30) -> list[dict]:
    """Retorna conversaciones donde el bot esperaba confirmación y el cliente no respondió.
    Solo incluye números que no hayan recibido recordatorio en los últimos cooldown_min minutos."""
    from datetime import datetime as _dt
    cutoff = (_dt.now(PERU_TZ) - timedelta(minutes=minutos)).isoformat()
    cooldown_cutoff = (_dt.now(PERU_TZ) - timedelta(minutes=cooldown_min)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT phone, messages FROM conversations "
            "WHERE welcomed=1 AND escalado=0 AND last_msg_at < ? AND last_msg_at != '' "
            "AND (reminder_sent_at IS NULL OR reminder_sent_at = '' OR reminder_sent_at < ?)",
            (cutoff, cooldown_cutoff)
        ).fetchall()
    result = []
    for r in rows:
        msgs = json.loads(r["messages"])
        if not msgs:
            continue
        last = msgs[-1]
        # Solo si el último mensaje es del bot (assistant) y contiene palabras de confirmación
        if last.get("role") != "assistant":
            continue
        content = str(last.get("content", "")).lower()
        # No recordar si el pedido ya fue confirmado exitosamente
        skip_keywords = ["pedido confirmado", "¡pedido confirmado", "confirmado! 🌮",
                         "¡confirmado!", "pedido guardado", "¡con gusto", "en preparación"]
        if any(k in content for k in skip_keywords):
            continue
        keywords = ["¿confirmamos", "confirmas", "plinea", "plina",
                    "¿cómo pagas", "cómo pagas", "contra entrega", "total:"]
        if any(k in content for k in keywords):
            result.append({"phone": r["phone"], "last_msg": last.get("content", "")})
    return result


# ── Panel de motorizados ──────────────────────────────────────

def create_moto_request(order_id: int, client_phone: str, direccion: str,
                         notas: str, items: str) -> int:
    now = datetime.now(PERU_TZ).strftime("%d/%m/%Y %H:%M")
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO moto_requests (order_id, client_phone, direccion, notas, items, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (order_id, client_phone, direccion, notas, items, now)
        )
        return cur.lastrowid


def get_pending_moto_requests() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM moto_requests WHERE estado='pendiente' ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def accept_moto_request(request_id: int, delivery_name: str):
    with _conn() as c:
        c.execute(
            "UPDATE moto_requests SET estado='aceptado', accepted_by=? WHERE id=?",
            (delivery_name, request_id)
        )


def get_moto_request(request_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM moto_requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None


# ── Encuesta post-entrega y carta follow-up ───────────────────

def mark_order_camino(order_id: int):
    """Registra la hora en que un pedido pasó a 'En camino' para el temporizador de encuesta."""
    now = datetime.now(PERU_TZ).isoformat()
    with _conn() as c:
        c.execute("UPDATE orders SET camino_at=? WHERE id=?", (now, order_id))


def mark_survey_sent(order_id: int):
    """Marca que ya se envió la encuesta de satisfacción para este pedido."""
    with _conn() as c:
        c.execute("UPDATE orders SET survey_sent=1 WHERE id=?", (order_id,))


def get_orders_for_survey(minutes: int = 60) -> list[dict]:
    """Retorna pedidos En camino que llevan >= minutes sin recibir encuesta."""
    cutoff = (datetime.now(PERU_TZ) - timedelta(minutes=minutes)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT id, phone FROM orders "
            "WHERE estado='En camino 🛵' AND survey_sent=0 "
            "AND camino_at != '' AND camino_at < ?",
            (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_pending_carta_followups(minutos: int = 15) -> list[dict]:
    """Retorna conversaciones donde se envió la carta hace >= minutos pero sin pedido hoy."""
    cutoff = (datetime.now(PERU_TZ) - timedelta(minutes=minutos)).isoformat()
    today = datetime.now(PERU_TZ).strftime("%d/%m/%Y")
    with _conn() as c:
        rows = c.execute(
            "SELECT phone, messages FROM conversations "
            "WHERE welcomed=1 AND escalado=0 AND last_msg_at < ? AND last_msg_at != '' "
            "AND (carta_followup_sent_at IS NULL OR carta_followup_sent_at = '')",
            (cutoff,)
        ).fetchall()
    result = []
    for r in rows:
        msgs = json.loads(r["messages"])
        if not msgs:
            continue
        # Buscar si el último mensaje del bot fue envío de carta
        last_bot = next((m for m in reversed(msgs) if m.get("role") == "assistant"), None)
        if not last_bot:
            continue
        content = last_bot.get("content", "")
        if "carta enviada" not in content.lower():
            continue
        # Verificar que no haya pedido hoy
        with _conn() as c2:
            has_order = c2.execute(
                "SELECT 1 FROM orders WHERE phone=? AND fecha=?", (r["phone"], today)
            ).fetchone()
        if has_order:
            continue
        result.append({"phone": r["phone"]})
    return result


def mark_carta_followup_sent(phone: str):
    """Marca que se envió el follow-up de carta a este teléfono."""
    now = datetime.now(PERU_TZ).isoformat()
    with _conn() as c:
        c.execute("UPDATE conversations SET carta_followup_sent_at=? WHERE phone=?", (now, phone))


# ── Menú editable ─────────────────────────────────────────────

_MENU_INICIAL = [
    # (categoria, nombre, descripcion, precio)
    ("PA' TAQUEAR", "Quesadilla", "Tortilla de harina, queso derretido + guacamole y totopos", 6.50),
    ("PA' TAQUEAR", "Quesabirria", "Queso derretido + birria jugosita · incluye consomé", 10.00),
    ("PA' TAQUEAR", "Taco de Suadero", "Corte entre costilla y piel de res", 6.50),
    ("PA' TAQUEAR", "Taco Campechano", "Carne de res + chorizo de puerco", 6.50),
    ("PA' TAQUEAR", "Taco de Pastor", "Cerdo marinado en adobo con piña", 6.50),
    ("PA' TAQUEAR", "Taco de Choriqueso", "Chorizo de cerdo con queso fundido", 7.50),
    ("PA' TAQUEAR", "Gringa de Pastor", "Tortilla de harina, pastor y queso derretido", 14.00),
    ("UNA BOTANITA", "Esquites", "Elote desgranado con mayo, queso, chile y limón", 8.00),
    ("PA' COMPARTIR", "Orden Quesadillas (3 und)", "", 17.00),
    ("PA' COMPARTIR", "Nachos Chilangos", "Birria, salsa de queso cheddar sobre cama de mozzarella", 28.00),
    ("PA' COMPARTIR", "Orden Guacamole c/ Totopos", "", 4.00),
    ("BURRITOS", "Chilangazo", "Pastor, salchicha huachana, suadero, queso gouda, frijoles, guacamole, cebolla y cilantro", 26.00),
    ("COMBOS", "Plato Chingón", "2 Quesabirrias + 1 Gringa + 2 Tacos + ½ Nachos + Guacamole (para 2-3 personas)", 69.50),
    ("COMBOS", "De Compas", "2 Tacos + 2 Quesabirrias + 1 Gringa + 1 Guacamole + 2 Aguas (para 2)", 57.50),
    ("COMBOS", "Combo Pa' Ti Solito", "3 Quesabirrias + 1 Agua + 1 Guacamole c/totopos (personal)", 29.90),
    ("AGUAS DEL CHAVO", "Agua de Horchata", "½ litro", 8.00),
    ("AGUAS DEL CHAVO", "Agua de Jamaica", "½ litro", 7.00),
    ("AGUAS DEL CHAVO", "Agua de Tamarindo", "½ litro", 7.00),
    ("AGUAS DEL CHAVO", "Chamoyada de Mango", "½ litro", 13.00),
    ("EXTRAS", "Extra queso", "", 2.00),
    ("EXTRAS", "Extra guacamole", "", 2.00),
    ("EXTRAS", "Extra proteína", "", 5.00),
    ("EXTRAS", "Salsa adicional", "", 1.50),
]

_ORDEN_CATEGORIAS = [
    "PA' TAQUEAR", "UNA BOTANITA", "PA' COMPARTIR",
    "BURRITOS", "COMBOS", "AGUAS DEL CHAVO", "EXTRAS",
]

_EMOJI_CAT = {
    "PA' TAQUEAR": "🌮",
    "UNA BOTANITA": "🌽",
    "PA' COMPARTIR": "🥑",
    "BURRITOS": "🌯",
    "COMBOS": "🎉",
    "AGUAS DEL CHAVO": "💧",
    "EXTRAS": "➕",
}


def init_menu_items():
    """Crea la tabla menu_items y la puebla con el menú inicial si está vacía."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS menu_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                categoria TEXT NOT NULL,
                nombre TEXT NOT NULL,
                descripcion TEXT DEFAULT '',
                precio REAL NOT NULL,
                disponible INTEGER NOT NULL DEFAULT 1,
                orden INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Poblar solo si está vacía
        count = c.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0]
        if count == 0:
            for i, (cat, nom, desc, precio) in enumerate(_MENU_INICIAL):
                c.execute(
                    "INSERT INTO menu_items (categoria, nombre, descripcion, precio, disponible, orden) VALUES (?,?,?,?,1,?)",
                    (cat, nom, desc, precio, i)
                )


def get_menu_items() -> list[dict]:
    """Retorna todos los items del menú ordenados por categoría y orden."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM menu_items ORDER BY orden ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def update_menu_item(item_id: int, nombre: str = None, descripcion: str = None,
                     precio: float = None, disponible: int = None):
    """Actualiza un item del menú."""
    updates, vals = [], []
    if nombre is not None:
        updates.append("nombre=?"); vals.append(nombre)
    if descripcion is not None:
        updates.append("descripcion=?"); vals.append(descripcion)
    if precio is not None:
        updates.append("precio=?"); vals.append(precio)
    if disponible is not None:
        updates.append("disponible=?"); vals.append(disponible)
    if not updates:
        return
    vals.append(item_id)
    with _conn() as c:
        c.execute(f"UPDATE menu_items SET {', '.join(updates)} WHERE id=?", vals)


def get_menu_texto() -> str:
    """Genera el MENU_TEXTO formateado desde la BD."""
    items = get_menu_items()
    if not items:
        from menu import MENU_TEXTO as _fallback
        return _fallback

    from menu import EMPAQUE
    grupos: dict = {}
    for it in items:
        cat = it["categoria"]
        if cat not in grupos:
            grupos[cat] = []
        grupos[cat].append(it)

    lineas = ["🌮 *CARTA CHILANGO - DELIVERY* 🌮",
              "_Solo Viernes, Sábado y Domingo · 5:30pm a 11pm · Tacna_", ""]

    for cat in _ORDEN_CATEGORIAS:
        if cat not in grupos:
            continue
        emoji = _EMOJI_CAT.get(cat, "•")
        lineas.append("━━━━━━━━━━━━━━━━━━━━")
        lineas.append(f"{emoji} *{cat}*")
        lineas.append("━━━━━━━━━━━━━━━━━━━━")
        for it in grupos[cat]:
            if not it.get("disponible", 1):
                continue
            precio_str = f"S/ {it['precio']:.2f}".rstrip("0").rstrip(".")
            if not precio_str.endswith("0") and "." in precio_str:
                pass
            # Siempre 2 decimales
            precio_str = f"S/ {it['precio']:.2f}"
            lineas.append(f"• {it['nombre']} — {precio_str}")
            if it.get("descripcion"):
                lineas.append(f"  _{it['descripcion']}_")
        lineas.append("")

    lineas.append(f"📦 _Empaque eco resistente: S/ {EMPAQUE:.2f} por pedido_")
    lineas.append("💳 _Pagos: Plin · Contra entrega_")
    return "\n".join(lineas)


# ── Métricas / Dashboard ──────────────────────────────────────

def get_metricas() -> dict:
    """Retorna datos agregados para el dashboard de métricas."""
    import re as _re
    from datetime import datetime as _dt

    def _parse_total(val):
        try:
            m = _re.search(r"(\d+(?:[.,]\d{1,2})?)", (val or ""))
            return float(m.group(1).replace(",", ".")) if m else 0.0
        except Exception:
            return 0.0

    hoy = datetime.now(PERU_TZ).strftime("%d/%m/%Y")
    with _conn() as c:
        # Todos los pedidos no cancelados
        rows = c.execute(
            "SELECT fecha, hora, items, total, metodo_pago FROM orders WHERE estado != 'Cancelado ❌'"
        ).fetchall()

    pedidos = [dict(r) for r in rows]

    # ── Ventas por día (últimos 14 días) ──────────────────────
    from collections import defaultdict
    ventas_dia: dict = defaultdict(float)
    pedidos_dia: dict = defaultdict(int)
    for p in pedidos:
        ventas_dia[p["fecha"]] += _parse_total(p["total"])
        pedidos_dia[p["fecha"]] += 1

    # Generar últimos 14 días
    dias_labels, dias_ventas, dias_pedidos = [], [], []
    for i in range(13, -1, -1):
        d = (datetime.now(PERU_TZ) - timedelta(days=i)).strftime("%d/%m/%Y")
        label = (datetime.now(PERU_TZ) - timedelta(days=i)).strftime("%d/%m")
        dias_labels.append(label)
        dias_ventas.append(round(ventas_dia.get(d, 0), 2))
        dias_pedidos.append(pedidos_dia.get(d, 0))

    # ── Hora pico ─────────────────────────────────────────────
    hora_conteo: dict = defaultdict(int)
    for p in pedidos:
        try:
            h = int((p["hora"] or "0:0").split(":")[0])
            hora_conteo[h] += 1
        except Exception:
            pass
    horas_labels = [f"{h}:00" for h in range(17, 23)]
    horas_data   = [hora_conteo.get(h, 0) for h in range(17, 23)]

    # ── Top productos ─────────────────────────────────────────
    producto_conteo: dict = defaultdict(int)
    import re as _re2
    for p in pedidos:
        items_str = p["items"] or ""
        for m in _re2.finditer(r'(\d+)x\s+([^,\n\|\-]+)', items_str):
            qty  = int(m.group(1))
            name = m.group(2).strip().rstrip(" —")
            if len(name) > 3:
                producto_conteo[name] += qty
    top_productos = sorted(producto_conteo.items(), key=lambda x: x[1], reverse=True)[:7]

    # ── Totales generales ─────────────────────────────────────
    total_hoy    = sum(_parse_total(p["total"]) for p in pedidos if p["fecha"] == hoy)
    pedidos_hoy  = sum(1 for p in pedidos if p["fecha"] == hoy)

    # Semana actual (últimos 7 días)
    semana_fechas = {(datetime.now(PERU_TZ) - timedelta(days=i)).strftime("%d/%m/%Y") for i in range(7)}
    total_semana  = sum(_parse_total(p["total"]) for p in pedidos if p["fecha"] in semana_fechas)
    pedidos_semana = sum(1 for p in pedidos if p["fecha"] in semana_fechas)

    # Mes actual
    mes_actual = datetime.now(PERU_TZ).strftime("%m/%Y")
    total_mes   = sum(_parse_total(p["total"]) for p in pedidos if (p["fecha"] or "")[-7:] == mes_actual)
    pedidos_mes  = sum(1 for p in pedidos if (p["fecha"] or "")[-7:] == mes_actual)

    # ── Métodos de pago ───────────────────────────────────────
    pago_conteo: dict = defaultdict(int)
    for p in pedidos:
        pago_conteo[p.get("metodo_pago") or "Efectivo"] += 1

    return {
        "dias_labels":    dias_labels,
        "dias_ventas":    dias_ventas,
        "dias_pedidos":   dias_pedidos,
        "horas_labels":   horas_labels,
        "horas_data":     horas_data,
        "top_productos":  [{"nombre": n, "qty": q} for n, q in top_productos],
        "total_hoy":      round(total_hoy, 2),
        "pedidos_hoy":    pedidos_hoy,
        "total_semana":   round(total_semana, 2),
        "pedidos_semana": pedidos_semana,
        "total_mes":      round(total_mes, 2),
        "pedidos_mes":    pedidos_mes,
        "pago_conteo":    dict(pago_conteo),
    }


# ── Historial de costos por zona ──────────────────────────────

def get_delivery_zones_summary() -> list[dict]:
    """Retorna un resumen de costos de delivery aprendidos por zona."""
    import re as _re
    with _conn() as c:
        rows = c.execute(
            "SELECT direccion, costo, fecha FROM delivery_costs ORDER BY id DESC"
        ).fetchall()

    if not rows:
        return []

    # Agrupar por palabras clave de la dirección (>4 letras)
    from collections import defaultdict
    zonas: dict = defaultdict(list)
    for r in rows:
        dir_clean = (r["direccion"] or "").strip()
        if not dir_clean:
            continue
        # Usar los primeros 2-3 tokens significativos como clave de zona
        palabras = [w for w in dir_clean.split() if len(w) > 3][:3]
        if not palabras:
            continue
        zona_key = " ".join(palabras).title()
        zonas[zona_key].append({"costo": r["costo"], "fecha": r["fecha"], "dir": dir_clean})

    result = []
    for zona, registros in zonas.items():
        costos = [r["costo"] for r in registros]
        result.append({
            "zona":      zona,
            "ultima_dir": registros[0]["dir"],
            "ultimo_costo": registros[0]["costo"],
            "costo_promedio": round(sum(costos) / len(costos), 1),
            "costo_min":  min(costos),
            "costo_max":  max(costos),
            "frecuencia": len(registros),
            "ultima_vez": registros[0]["fecha"],
        })

    result.sort(key=lambda x: x["frecuencia"], reverse=True)
    return result
