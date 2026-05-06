import os
from datetime import datetime

import openpyxl
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

EXCEL_FILE = "pedidos_chilango.xlsx"


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


def save_order(phone: str, items: str, total: str):
    _init_excel()
    wb = load_workbook(EXCEL_FILE)
    ws = wb.active

    now = datetime.now()
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


def get_orders_count() -> int:
    if not os.path.exists(EXCEL_FILE):
        return 0
    wb = load_workbook(EXCEL_FILE)
    ws = wb.active
    return ws.max_row - 1
