import os
from datetime import datetime, timezone, timedelta

import httpx
import openpyxl
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

import db

EXCEL_FILE = "pedidos_chilango.xlsx"
PERU_TZ = timezone(timedelta(hours=-5))
OWNER_PHONE = "51954713696"


async def _send_whatsapp(to: str, body: str):
    """Envía un mensaje WhatsApp usando la API de Meta."""
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    phone_number_id = os.environ.get("META_PHONE_NUMBER_ID", "").strip()
    if not token or not phone_number_id:
        print("[WA] META_ACCESS_TOKEN o META_PHONE_NUMBER_ID no configurados")
        return
    url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            print(f"[WA] Error al enviar a {to}: {resp.status_code} {resp.text}")


async def notify_delivery_cost_query(phone_client: str, direccion: str,
                                      subtotal: str = "", items: str = "", pago: str = ""):
    """Envía consulta de costo de delivery al motorizado y guarda la consulta pendiente en BD."""
    delivery_phone = os.environ.get("DELIVERY_1_PHONE", "").strip()
    if not delivery_phone:
        print("[CONSULTAR_COSTO] No hay DELIVERY_1_PHONE configurado — no se envió consulta")
        return
    delivery_name = os.environ.get("DELIVERY_1_NAME", "Delivery").strip()
    mensaje = (
        f"¿Cual es el costo a la siguiente dirección?\n"
        f"Dirección: {direccion or 'Sin especificar'}\n"
        f"Cliente: +{phone_client}"
    )
    await _send_whatsapp(delivery_phone, mensaje)
    # Guardar consulta pendiente para poder auto-responder al cliente cuando el motorizado conteste
    db.save_delivery_query(delivery_phone, phone_client, subtotal, items, pago, direccion)
    print(f"[CONSULTAR_COSTO] Consulta guardada — {delivery_name} ({delivery_phone}) → cliente +{phone_client}")


def _init_excel():
    if not os.path.exists(EXCEL_FILE):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Pedidos"

        headers = ["Fecha", "Hora", "Teléfono", "Items del Pedido", "Total", "Estado"]
        ws.append(headers)

        header_fill = PatternFill(start_color="2D5016", end_color="2D5016", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF", size=11)
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        ws.column_dimensions["A"].width = 12
        ws.column_dimensions["B"].width = 8
        ws.column_dimensions["C"].width = 18
        ws.column_dimensions["D"].width = 50
        ws.column_dimensions["E"].width = 12
        ws.column_dimensions["F"].width = 12

        wb.save(EXCEL_FILE)


async def _notify_owner(phone_clean: str, items: str, total: str, metodo_pago: str, now: datetime, titulo: str = "🆕 *NUEVO PEDIDO — Chilango*", direccion: str = ""):
    try:
        token = os.environ.get("META_ACCESS_TOKEN", "").strip()
        phone_number_id = os.environ.get("META_PHONE_NUMBER_ID", "").strip()
        if not token or not phone_number_id:
            print("[NOTIFICACIÓN] META_ACCESS_TOKEN o META_PHONE_NUMBER_ID no configurados")
            return

        url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        hora_str = now.strftime("%d/%m · %I:%M %p")

        template_name = os.environ.get("NOTIFY_TEMPLATE_NAME", "").strip()

        if template_name:
            # ── Modo template (permanente, sin restricción de 24 h) ──────────
            payload = {
                "messaging_product": "whatsapp",
                "to": OWNER_PHONE,
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": "es"},
                    "components": [{
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": f"+{phone_clean}"},
                            {"type": "text", "text": items},
                            {"type": "text", "text": total},
                            {"type": "text", "text": metodo_pago},
                            {"type": "text", "text": hora_str},
                        ],
                    }],
                },
            }
            modo = "template"
        else:
            # ── Modo texto (requiere que el dueño haya escrito al bot hoy) ───
            pago_emoji = {"Yape/Plin": "💜 Yape/Plin", "Yape": "💜 Yape", "Plin": "💜 Plin", "Efectivo": "💵 Efectivo"}.get(metodo_pago, metodo_pago)
            dir_linea = f"\n📍 {direccion}" if direccion else ""
            mensaje = (
                f"{titulo}\n"
                f"👤 Cliente: +{phone_clean}\n"
                f"🛒 {items}\n"
                f"💰 {total}\n"
                f"💳 {pago_emoji}"
                f"{dir_linea}\n"
                f"🕒 {hora_str}"
            )
            payload = {
                "messaging_product": "whatsapp",
                "to": OWNER_PHONE,
                "type": "text",
                "text": {"body": mensaje},
            }
            modo = "texto"

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                print(f"[NOTIFICACIÓN] ✅ Enviada al dueño ({OWNER_PHONE}) — modo {modo}")
            else:
                data = resp.json()
                error_msg = data.get("error", {}).get("message", resp.text)
                error_code = data.get("error", {}).get("code", resp.status_code)
                print(f"[ERROR NOTIFICACIÓN] Código {error_code}: {error_msg}")
                if error_code in (131047, 131026):
                    print("[ERROR NOTIFICACIÓN] El dueño no ha escrito al bot en las últimas 24h.")
                    print("[ERROR NOTIFICACIÓN] Solución: configura NOTIFY_TEMPLATE_NAME en Railway.")
    except Exception as e:
        print(f"[ERROR NOTIFICACIÓN] Excepción: {e}")


async def save_order(phone: str, items: str, total: str, metodo_pago: str = "Efectivo", direccion: str = "", notas: str = ""):
    now = datetime.now(PERU_TZ)
    phone_clean = phone.replace("whatsapp:", "").replace("+", "")

    # Persistencia confiable en SQLite
    db.save_order_db(phone_clean, items, total, metodo_pago, direccion, notas)
    print(f"[PEDIDO GUARDADO] {now.strftime('%d/%m %H:%M')} | {phone_clean} | {total} | {metodo_pago}")

    # Excel como backup (se pierde en reinicios sin Railway Volume)
    try:
        _init_excel()
        wb = load_workbook(EXCEL_FILE)
        ws = wb.active
        row = [
            now.strftime("%d/%m/%Y"),
            now.strftime("%H:%M"),
            phone_clean,
            items,
            total,
            "Nuevo 🆕",
            metodo_pago,
        ]
        ws.append(row)
        last_row = ws.max_row
        if last_row % 2 == 0:
            row_fill = PatternFill(start_color="F2F7EE", end_color="F2F7EE", fill_type="solid")
            for cell in ws[last_row]:
                cell.fill = row_fill
        wb.save(EXCEL_FILE)
    except Exception as e:
        print(f"[EXCEL] No se pudo guardar en Excel: {e}")

    await _notify_owner(phone_clean, items, total, metodo_pago, now, direccion=direccion)


async def update_order(phone: str, items: str, total: str, metodo_pago: str = "Efectivo", direccion: str = "", notas: str = ""):
    now = datetime.now(PERU_TZ)
    phone_clean = phone.replace("whatsapp:", "").replace("+", "")

    updated = db.update_latest_order(phone_clean, items, total, metodo_pago, direccion, notas)
    if updated:
        print(f"[PEDIDO MODIFICADO] {now.strftime('%d/%m %H:%M')} | {phone_clean} | {total} | {metodo_pago}")
        await _notify_owner(
            phone_clean, items, total, metodo_pago, now,
            titulo="✏️ *PEDIDO MODIFICADO — Chilango*",
            direccion=direccion,
        )
    else:
        print(f"[PEDIDO MODIFICADO] No se encontró pedido activo para {phone_clean}")


async def cancel_order(phone: str):
    now = datetime.now(PERU_TZ)
    phone_clean = phone.replace("whatsapp:", "").replace("+", "")

    cancelled = db.cancel_latest_order(phone_clean)
    if cancelled:
        print(f"[PEDIDO CANCELADO] {now.strftime('%d/%m %H:%M')} | {phone_clean}")
        await _notify_owner(
            phone_clean, "—", "—", "—", now,
            titulo="❌ *PEDIDO CANCELADO — Chilango*",
        )
    else:
        print(f"[PEDIDO CANCELADO] No se encontró pedido activo para {phone_clean}")


def get_orders_count() -> int:
    return db.get_orders_count()
