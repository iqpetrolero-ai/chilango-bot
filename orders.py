import os
from datetime import datetime, timezone, timedelta

import httpx
import openpyxl
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

import db

EXCEL_FILE = "pedidos_chilango.xlsx"
PERU_TZ = timezone(timedelta(hours=-5))
OWNER_PHONE = "51955500153"


async def _send_whatsapp(to: str, body: str):
    """Envía un mensaje WhatsApp usando la API de Meta."""
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    phone_number_id = os.environ.get("META_PHONE_NUMBER_ID", "").strip()
    to_clean = to.replace("+", "").replace(" ", "")
    if not token or not phone_number_id:
        print(f"[WA] ⚠️ META_ACCESS_TOKEN o META_PHONE_NUMBER_ID no configurados — no se envió WA a {to_clean}")
        return
    url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to_clean, "type": "text", "text": {"body": body}}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code == 200:
            print(f"[WA] ✅ Enviado a {to_clean}")
        else:
            print(f"[WA] ❌ Error al enviar a {to_clean}: {resp.status_code} {resp.text}")


async def _send_telegram(chat_id: str, text: str):
    """Envía un mensaje por Telegram. Sin restricción de 24 horas."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        })
        if resp.status_code == 200:
            print(f"[TELEGRAM] ✅ Enviado a chat_id {chat_id}")
            return True
        else:
            print(f"[TELEGRAM] ❌ Error a {chat_id}: {resp.status_code} {resp.text}")
            return False


async def _notify_delivery(delivery_phone: str, delivery_name: str,
                            delivery_index: int, mensaje_wa: str, mensaje_tg: str):
    """Envía notificación al motorizado por Telegram (preferido) o WhatsApp (fallback)."""
    tg_id = os.environ.get(f"DELIVERY_{delivery_index}_TELEGRAM_ID", "").strip()
    if tg_id:
        sent = await _send_telegram(tg_id, mensaje_tg)
        if sent:
            return  # Telegram OK — no necesita WA
    # Fallback a WhatsApp si no hay Telegram configurado o falló
    print(f"[DELIVERY] Usando WhatsApp para {delivery_name} (sin Telegram configurado)")
    await _send_whatsapp(delivery_phone, mensaje_wa)


async def notify_delivery_cost_query(phone_client: str, direccion: str,
                                      subtotal: str = "", items: str = "", pago: str = ""):
    """Notifica al dueño que un cliente necesita delivery — él gestionará manualmente el motorizado."""
    # ── Líneas de delivery (mantenidas, no activas en este flujo) ──────────
    # deliveries = []
    # for i in range(1, 5):
    #     ph = (os.environ.get(f"DELIVERY_{i}_PHONE") or (os.environ.get("DELIVERY_PHONE","") if i==1 else "")).strip()
    #     name = os.environ.get(f"DELIVERY_{i}_NAME", f"Motorizado {i}").strip()
    #     if ph:
    #         deliveries.append({"phone": ph.replace("+",""), "name": name, "index": i})
    # msg_wa = (
    #     f"🛵 ¿Cuál es el costo de delivery?\n"
    #     f"📍 {direccion or 'Sin especificar'}\n"
    #     f"👤 Cliente: +{phone_client}\n"
    #     f"(Responde solo con el monto, ej: 7 o S/7)"
    # )
    # msg_tg = (
    #     f"🛵 *¿Cuál es el costo de delivery?*\n"
    #     f"📍 {direccion or 'Sin especificar'}\n"
    #     f"👤 Cliente: +{phone_client}\n"
    #     f"_Responde por WhatsApp con el monto, ej: 7 o S/7_"
    # )
    # for d in deliveries:
    #     await _notify_delivery(d["phone"], d["name"], d["index"], msg_wa, msg_tg)
    #     db.save_delivery_query(d["phone"], phone_client, subtotal, items, pago, direccion)
    # ── Fin líneas delivery ────────────────────────────────────────────────

    # Notificar al dueño para que gestione el motorizado manualmente
    subtotal_linea = f"\n💰 Subtotal: {subtotal}" if subtotal else ""
    items_linea    = f"\n🛒 {items}" if items else ""
    pago_linea     = f"\n💳 Pago: {pago}" if pago else ""

    msg_owner = (
        f"🛵 *Cliente necesita delivery*\n"
        f"👤 +{phone_client}\n"
        f"📍 {direccion or 'Sin especificar'}"
        f"{items_linea}"
        f"{subtotal_linea}"
        f"{pago_linea}\n"
        f"_(Gestionar motorizado manualmente)_"
    )

    await _send_whatsapp(OWNER_PHONE, msg_owner)
    print(f"[CONSULTAR_COSTO] ✅ Dueño notificado para cliente +{phone_client}")


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
