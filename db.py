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
        # Migración: agregar columna leida si la BD ya existía sin ella
        try:
            c.execute("ALTER TABLE conversations ADD COLUMN leida INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # La columna ya existe
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
        ]:
            try:
                c.execute(migration)
            except Exception:
                pass  # La columna ya existe


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
    with _conn() as c:
        c.execute("""
            INSERT INTO conversations (phone, messages, welcomed)
            VALUES (?, ?, 0)
            ON CONFLICT(phone) DO UPDATE SET messages=excluded.messages
        """, (phone, json.dumps(messages, ensure_ascii=False)))


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
    """Retorna {phone: {messages: [...], leida: bool}} para el panel admin."""
    with _conn() as c:
        rows = c.execute(
            "SELECT phone, messages, leida FROM conversations WHERE welcomed=1 AND messages != '[]'"
        ).fetchall()
        return {
            r["phone"]: {
                "messages": json.loads(r["messages"]),
                "leida": bool(r["leida"]),
            }
            for r in rows
        }


def append_message(phone: str, role: str, content: str):
    """Agrega un mensaje al historial sin reemplazarlo (para mensajes no procesados por Claude)."""
    with _conn() as c:
        row = c.execute("SELECT messages FROM conversations WHERE phone=?", (phone,)).fetchone()
        msgs = json.loads(row["messages"]) if row else []
        msgs.append({"role": role, "content": content})
        c.execute("""
            INSERT INTO conversations (phone, messages, welcomed)
            VALUES (?, ?, 1)
            ON CONFLICT(phone) DO UPDATE SET messages=excluded.messages
        """, (phone, json.dumps(msgs, ensure_ascii=False)))


# ── Orders ────────────────────────────────────────────────────

def save_order_db(phone: str, items: str, total: str, metodo_pago: str = "Efectivo", direccion: str = ""):
    now = datetime.now(PERU_TZ)
    with _conn() as c:
        c.execute(
            "INSERT INTO orders (fecha, hora, phone, items, total, metodo_pago, direccion) VALUES (?,?,?,?,?,?,?)",
            (now.strftime("%d/%m/%Y"), now.strftime("%H:%M"), phone, items, total, metodo_pago, direccion),
        )


def get_orders_count() -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM orders").fetchone()
        return row["n"] if row else 0


def get_active_orders_count() -> int:
    """Cuenta pedidos en preparación ahora mismo (Nuevo + En preparación)."""
    today = datetime.now(PERU_TZ).strftime("%d/%m/%Y")
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE fecha=? AND estado IN ('Nuevo 🆕','En preparación 👨‍🍳')",
            (today,)
        ).fetchone()
        return row["n"] if row else 0


def get_orders_today() -> list:
    now = datetime.now(PERU_TZ)
    today = now.strftime("%d/%m/%Y")
    with _conn() as c:
        rows = c.execute(
            "SELECT id, fecha, hora, phone, items, total, estado, metodo_pago, modificado, direccion FROM orders WHERE fecha=? ORDER BY id DESC",
            (today,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_order_estado(order_id: int, estado: str):
    with _conn() as c:
        c.execute("UPDATE orders SET estado=? WHERE id=?", (estado, order_id))


def get_order_by_id(order_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT id, phone, items, total, estado, direccion FROM orders WHERE id=?", (order_id,)
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


def update_latest_order(phone: str, items: str, total: str, metodo_pago: str, direccion: str = "") -> bool:
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
            "UPDATE orders SET items=?, total=?, metodo_pago=?, modificado=1, direccion=CASE WHEN ?!='' THEN ? ELSE direccion END WHERE id=?",
            (items, total, metodo_pago, direccion, direccion, row["id"]),
        )
        return True
