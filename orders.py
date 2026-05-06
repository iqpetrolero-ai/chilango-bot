import os
from datetime import datetime, timezone, timedelta

import openpyxl
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

EXCEL_FILE = "pedidos_chilango.xlsx"
PERU_TZ = timezone(timedelta(hours=-5))

OWNER_WHATSAPP = "whatsapp:+51953038816"
TWILIO_FROM = "whatsapp:+14155238886"


def _init_excel():
    if not os.path.exists(EXCEL_FILE):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Pedidos"

        headers = ["Fecha", "Hora", "Teléfono", "Items del Pedido", "Total", "Estado"]
        ws.append(headers)

        header_fill = PatternFill(start_color="2D5016", end_color="2D5016", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF", size=11)
        for col, cell in enumerate(ws[1], 1):
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


def _notify_owner(phone_clean: str, items: str, total: str, now: datetime):
    try:
        from twilio.rest import Client
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
        if not account_sid or not auth_token:
            return
        client = Client(account_sid, auth_token)
        mensaje = (
            f"🆕 *NUEVO PEDIDO — Chilango*\n"
            f"👤 Cliente: +{phone_clean}\n"
            f"🛒 {items}\n"
            f"💰 {total}\n"
            f"🕒 {now.strftime('%d/%m · %I:%M %p')}"
        )
        client.messages.create(
            body=mensaje,
            from_=TWILIO_FROM,
            to=OWNER_WHATSAPP,
        )
        print(f"[NOTIFICACIÓN] Enviada al dueño")
    except Exception as e:
        print(f"[ERROR NOTIFICACIÓN] {e}")


def save_order(phone: str, items: str, total: str):
    _init_excel()
    wb = load_workbook(EXCEL_FILE)
    ws = wb.active

    now = datetime.now(PERU_TZ)
    phone_clean = phone.replace("whatsapp:", "").replace("+", "")

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
    print(f"[PEDIDO GUARDADO] {now.strftime('%d/%m %H:%M')} | {phone_clean} | {total}")

    _notify_owner(phone_clean, items, total, now)


def get_orders_count() -> int:
    if not os.path.exists(EXCEL_FILE):
        return 0
    wb = load_workbook(EXCEL_FILE)
    ws = wb.active
    return ws.max_row - 1
