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


async def _notify_owner(phone_clean: str, items: str, total: str, now: datetime):
    try:
        token = os.environ.get("META_ACCESS_TOKEN", "").strip()
        phone_number_id = os.environ.get("META_PHONE_NUMBER_ID", "").strip()
        if not token or not phone_number_id:
            print("[NOTIFICACIÓN] META_ACCESS_TOKEN o META_PHONE_NUMBER_ID no configurados")
            return
        mensaje = (
            f"🆕 *NUEVO PEDIDO — Chilango*\n"
            f"👤 Cliente: +{phone_clean}\n"
            f"🛒 {items}\n"
            f"💰 {total}\n"
            f"🕒 {now.strftime('%d/%m · %I:%M %p')}"
        )
        url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": OWNER_PHONE,
            "type": "text",
            "text": {"body": mensaje},
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                print("[NOTIFICACIÓN] Enviada al dueño")
            else:
                print(f"[ERROR NOTIFICACIÓN] {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[ERROR NOTIFICACIÓN] {e}")


async def save_order(phone: str, items: str, total: str):
    now = datetime.now(PERU_TZ)
    phone_clean = phone.replace("whatsapp:", "").replace("+", "")

    # Persistencia confiable en SQLite
    db.save_order_db(phone_clean, items, total)
    print(f"[PEDIDO GUARDADO] {now.strftime('%d/%m %H:%M')} | {phone_clean} | {total}")

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

    await _notify_owner(phone_clean, items, total, now)


def get_orders_count() -> int:
    return db.get_orders_count()
