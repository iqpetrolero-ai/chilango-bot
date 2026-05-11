import os
import html
import json
import httpx
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, Query, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bot import process_message, process_message_with_image, reset_conversation, mensaje_bienvenida
from orders import get_orders_count
from menu import MENU_TEXTO
import db

app = FastAPI(title="Chilango Bot 🌮")

import os as _os
if _os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
if not ADMIN_PASSWORD:
    raise RuntimeError("La variable de entorno ADMIN_PASSWORD no está configurada")

security = HTTPBasic()


def verificar_admin(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(credentials.username.encode(), b"admin")
    ok_pass = secrets.compare_digest(credentials.password.encode(), ADMIN_PASSWORD.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, detail="Acceso no autorizado",
                            headers={"WWW-Authenticate": "Basic"})


META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "").strip()
META_PHONE_NUMBER_ID = os.environ.get("META_PHONE_NUMBER_ID", "").strip()
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "").strip()
BASE_URL = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
PDF_URL = f"https://{BASE_URL}/static/carta.pdf" if BASE_URL else ""

PALABRAS_CARTA = ["carta", "menu", "menú", "ver carta", "ver menu", "qué tienen", "que tienen"]

# Saludos genéricos que no necesitan procesarse después de la bienvenida
SALUDOS_GENERICOS = {"hola", "buenas", "buenos días", "buenas tardes", "buenas noches",
                     "hi", "hello", "hey", "ola", "buenas noches", "2"}

# ── Deduplicación de webhooks ─────────────────────────────────
# Meta reenvía el mismo mensaje si no recibe respuesta rápida.
# Guardamos los últimos 500 message IDs para evitar procesar dos veces.
_processed_msg_ids: set[str] = set()


async def send_whatsapp_message(to: str, text: str, phone_number_id: str = None):
    pid = phone_number_id or META_PHONE_NUMBER_ID
    url = f"https://graph.facebook.com/v19.0/{pid}/messages"
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            print(f"[ERROR META] {resp.status_code} {resp.text}")


async def send_whatsapp_document(to: str, caption: str, doc_url: str, phone_number_id: str = None):
    pid = phone_number_id or META_PHONE_NUMBER_ID
    url = f"https://graph.facebook.com/v19.0/{pid}/messages"
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": {
            "link": doc_url,
            "caption": caption,
            "filename": "Carta_Chilango.pdf",
        },
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            print(f"[ERROR META DOC] {resp.status_code} {resp.text}")


async def download_meta_image(media_id: str) -> tuple:
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"https://graph.facebook.com/v19.0/{media_id}", headers=headers)
        if resp.status_code != 200:
            print(f"[ERROR IMAGEN] No se pudo obtener URL: {resp.text}")
            return None, None
        data = resp.json()
        media_url = data.get("url")
        mime_type = data.get("mime_type", "image/jpeg")
        if not media_url:
            return None, None
        resp2 = await client.get(media_url, headers=headers)
        if resp2.status_code != 200:
            print(f"[ERROR IMAGEN] No se pudo descargar: {resp2.status_code}")
            return None, None
        return resp2.content, mime_type


async def _send_reply(phone: str, reply: str, sending_id: str):
    if len(reply) > 1500:
        mitad = len(reply) // 2
        corte = reply.rfind("\n", mitad - 200, mitad + 200)
        if corte == -1:
            corte = mitad
        await send_whatsapp_message(phone, reply[:corte].strip(), sending_id)
        await send_whatsapp_message(phone, reply[corte:].strip(), sending_id)
    else:
        await send_whatsapp_message(phone, reply, sending_id)


async def handle_message(phone: str, message: str, phone_number_id: str = None):
    msg_lower = message.lower().strip()
    sending_id = phone_number_id or META_PHONE_NUMBER_ID
    print(f"[MENSAJE] {phone}: {message}")

    # Enviar carta como PDF o texto
    if message.strip() == "1" or any(p in msg_lower for p in PALABRAS_CARTA):
        db.append_message(phone, "user", message)
        respuesta_carta = "📄 [Carta enviada como PDF]" if PDF_URL else "[Carta enviada como texto]"
        db.append_message(phone, "assistant", respuesta_carta)
        if PDF_URL:
            await send_whatsapp_document(phone, "¡Aquí está nuestra carta! 🌮", PDF_URL, sending_id)
        else:
            mitad = len(MENU_TEXTO) // 2
            corte = MENU_TEXTO.rfind("\n", mitad - 200, mitad + 200)
            if corte == -1:
                corte = mitad
            await send_whatsapp_message(phone, MENU_TEXTO[:corte].strip(), sending_id)
            await send_whatsapp_message(phone, MENU_TEXTO[corte:].strip(), sending_id)
        return

    if message.lower() in ["/reset", "reiniciar"]:
        reset_conversation(phone)
        await send_whatsapp_message(phone, "¡Listo! Conversación reiniciada. ¿En qué te puedo ayudar? 🌮", sending_id)
        return

    # Nuevo usuario: enviar bienvenida primero
    if not db.is_welcomed(phone):
        db.mark_welcomed(phone)
        bienvenida = mensaje_bienvenida()
        await send_whatsapp_message(phone, bienvenida, sending_id)
        if msg_lower not in SALUDOS_GENERICOS and message.strip():
            # process_message se encarga de guardar el historial
            reply = await process_message(phone, message)
            await _send_reply(phone, reply, sending_id)
        else:
            # Saludo genérico: guardar el intercambio manualmente
            db.append_message(phone, "user", message)
            db.append_message(phone, "assistant", bienvenida)
        db.mark_unread(phone)
        return

    db.mark_unread(phone)
    try:
        reply = await process_message(phone, message)
        await _send_reply(phone, reply, sending_id)
    except Exception as e:
        import traceback
        print(f"[ERROR PROCESO] {phone}: {e}")
        traceback.print_exc()


# ── Webhook Meta ──────────────────────────────────────────────
@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == META_VERIFY_TOKEN:
        print("[WEBHOOK] Verificación exitosa")
        return PlainTextResponse(hub_challenge)
    return PlainTextResponse("Token inválido", status_code=403)


@app.post("/webhook")
async def receive_message(request: Request):
    body = await request.json()

    try:
        entry = body["entry"][0]
        changes = entry["changes"][0]["value"]

        if "messages" not in changes:
            return JSONResponse({"status": "ok"})

        phone_number_id = changes.get("metadata", {}).get("phone_number_id", META_PHONE_NUMBER_ID)
        message_data = changes["messages"][0]

        # Deduplicar: si ya procesamos este mensaje_id, ignorar
        msg_id = message_data.get("id", "")
        if msg_id:
            if msg_id in _processed_msg_ids:
                print(f"[WEBHOOK] Mensaje duplicado ignorado: {msg_id}")
                return JSONResponse({"status": "ok"})
            _processed_msg_ids.add(msg_id)
            if len(_processed_msg_ids) > 500:
                _processed_msg_ids.clear()

        phone = message_data["from"]
        msg_type = message_data.get("type", "")

        if msg_type == "text":
            text = message_data["text"]["body"]
            await handle_message(phone, text, phone_number_id)
        elif msg_type == "image":
            media_id = message_data["image"]["id"]
            image_bytes, mime_type = await download_meta_image(media_id)
            if image_bytes:
                reply = await process_message_with_image(phone, image_bytes, mime_type)
                await send_whatsapp_message(phone, reply, phone_number_id)
            else:
                await send_whatsapp_message(phone, "No pude leer la imagen, ¿puedes enviarla de nuevo? 📸", phone_number_id)
        else:
            await send_whatsapp_message(phone, "Por favor envía un mensaje de texto 😊", phone_number_id)

    except Exception as e:
        print(f"[ERROR WEBHOOK] {e}")

    return JSONResponse({"status": "ok"})


# ── Páginas ───────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def home():
    count = get_orders_count()
    return f"""
    <html>
    <head>
        <title>Chilango Bot</title>
        <style>
            body {{ font-family: Arial, sans-serif; max-width: 600px; margin: 80px auto; text-align: center; }}
            h1 {{ color: #2D5016; }}
            .badge {{ background: #2D5016; color: white; padding: 8px 20px; border-radius: 20px; }}
        </style>
    </head>
    <body>
        <h1>🌮 Chilango Bot</h1>
        <p>El bot está funcionando correctamente ✅</p>
        <p>Pedidos registrados: <span class="badge">{count}</span></p>
        <p><small>Horario: Vie · Sáb · Dom · 5pm – 11pm</small></p>
    </body>
    </html>
    """


@app.get("/health")
async def health():
    return {"status": "ok", "bot": "Chilango 🌮"}


ESTADOS = ["Nuevo 🆕", "En preparación 👨‍🍳", "En camino 🛵", "Entregado ✅"]
ESTADO_COLORS = {
    "Nuevo 🆕":             "#e3f2fd",
    "En preparación 👨‍🍳":  "#fff8e1",
    "En camino 🛵":         "#fff3e0",
    "Entregado ✅":         "#e8f5e9",
    "Cancelado ❌":         "#fce4ec",
}
ESTADO_BADGE = {
    "Nuevo 🆕":             "#1976d2",
    "En preparación 👨‍🍳":  "#f57f17",
    "En camino 🛵":         "#e65100",
    "Entregado ✅":         "#2e7d32",
    "Cancelado ❌":         "#c62828",
}

# Paso de "En camino" → notificación WhatsApp al cliente
STEP_LABELS = ["Nuevo", "Preparación", "En camino", "Entregado"]
STEP_IDX = {"Nuevo 🆕": 0, "En preparación 👨‍🍳": 1, "En camino 🛵": 2, "Entregado ✅": 3}


async def _notify_order_camino(order: dict):
    """Envía WhatsApp al cliente cuando su pedido pasa a 'En camino'."""
    if not order:
        return
    phone = order["phone"]
    es_recojo = (order.get("direccion") or "").strip().lower() == "recojo"
    if es_recojo:
        mensaje = (
            "✅ *¡Tu pedido está listo para recoger!*\n\n"
            f"🛒 {order['items']}\n"
            f"💰 {order['total']}\n\n"
            "📍 Asoc. Ricardo Odonovan Mz H-5, calle Las Poncianas,\n"
            "atrás del Terminal Flores\n\n"
            "¡Te esperamos! 🌮"
        )
    else:
        mensaje = (
            "🛵 *¡Tu pedido está en camino!*\n\n"
            f"🛒 {order['items']}\n"
            f"💰 {order['total']}\n\n"
            "¡Gracias por elegir Chilango! 🌮"
        )
    await send_whatsapp_message(phone, mensaje)


def _render_card(p: dict) -> str:
    """Renderiza una tarjeta de pedido como HTML."""
    estado = p.get("estado") or "Nuevo 🆕"
    bg = ESTADO_COLORS.get(estado, "#f5f5f5")
    badge_color = ESTADO_BADGE.get(estado, "#666")

    # Progress steps
    step_idx = STEP_IDX.get(estado, -1)
    steps_parts = []
    for i, label in enumerate(STEP_LABELS):
        if i < step_idx:
            cls = "s-done"
        elif i == step_idx:
            cls = "s-active"
        else:
            cls = "s-pending"
        steps_parts.append(f'<div class="step {cls}"><div class="sdot"></div><span>{label}</span></div>')
        if i < len(STEP_LABELS) - 1:
            line_cls = "line-done" if i < step_idx else "line-pending"
            steps_parts.append(f'<div class="sline {line_cls}"></div>')
    steps_html = "".join(steps_parts)

    # Botón siguiente estado
    pid = p["id"]
    es_cancelado = estado == "Cancelado ❌"
    idx = ESTADOS.index(estado) if estado in ESTADOS else 0
    siguiente = ESTADOS[idx + 1] if (idx < len(ESTADOS) - 1 and not es_cancelado) else None
    if siguiente:
        sig_label = html.escape(siguiente)
        sig_js = siguiente.replace("'", "\\'")
        btn_sig = f"<button class='btn-next' onclick=\"cambiarEstado({pid},'{sig_js}')\">→ {sig_label}</button>"
    elif es_cancelado:
        btn_sig = '<span class="lbl-done" style="color:#c62828">Cancelado</span>'
    else:
        btn_sig = '<span class="lbl-done">✅ Completado</span>'

    btn_del = f'<button class="btn-del" onclick="eliminarPedido({pid},this)" title="Eliminar">🗑️</button>'

    # Pago
    metodo = p.get("metodo_pago") or "Efectivo"
    pago_color = {"Yape/Plin": "#6c3d98", "Yape": "#6c3d98", "Plin": "#6c3d98", "Efectivo": "#2D5016"}.get(metodo, "#555")
    pago_emoji = {"Yape/Plin": "💜", "Yape": "💜", "Plin": "💜", "Efectivo": "💵"}.get(metodo, "💳")
    es_digital = metodo in ("Yape/Plin", "Yape", "Plin")
    pago_estado_html = (
        '<span class="pago-estado pagado">✅ Pagado</span>' if es_digital
        else '<span class="pago-estado pendiente">💵 Cobrar al entregar</span>'
    )

    # Dirección / tipo de entrega
    direccion = p.get("direccion") or ""
    es_recojo = direccion.strip().lower() == "recojo"
    entrega_badge = (
        '<span class="entrega-badge recojo">🏪 Recojo</span>' if es_recojo
        else '<span class="entrega-badge delivery">🏍️ Delivery</span>'
    )
    dir_html = (
        f'<div class="card-dir">📍 {html.escape(direccion)}</div>'
        if direccion and not es_recojo else
        ('<div class="card-dir">📍 Recojo en local</div>' if es_recojo else
         '<div class="card-dir sin-dir">📍 Sin dirección</div>')
    )

    mod_badge = '<span class="mod-badge">✏️ Mod</span>' if p.get("modificado") else ""

    # Bullets para múltiples productos
    items_raw = p.get("items") or ""
    items_list = [i.strip() for i in items_raw.split(",") if i.strip()]
    if len(items_list) > 1:
        items_inner = "".join(f'<div class="item-line">• {html.escape(i)}</div>' for i in items_list)
    else:
        items_inner = html.escape(items_raw)

    # Botón siguiente: para recojo en "En preparación" el siguiente es "Listo p/retirar"
    if es_recojo and siguiente and siguiente == "En camino 🛵":
        sig_label_display = "📦 Listo p/retirar"
        sig_js_safe = siguiente.replace("'", "\\'")
        btn_sig = f"<button class='btn-next recojo-next' onclick=\"cambiarEstado({pid},'{sig_js_safe}')\">{sig_label_display}</button>"

    return f"""<div class="card" id="card-{p['id']}" data-estado="{html.escape(estado)}" data-recojo="{1 if es_recojo else 0}"
     style="border-left:4px solid {badge_color};background:{bg}">
  <div class="card-top">
    <span class="card-id">#{p['id']}</span>
    <span class="card-time">🕒 {p['hora']}</span>
    <span class="card-phone">+{html.escape(p['phone'])}</span>
    {entrega_badge}
    {mod_badge}
    <span class="badge" style="background:{badge_color}">{html.escape(estado)}</span>
  </div>
  <div class="progress-row">{steps_html}</div>
  <div class="card-items">{items_inner}</div>
  {dir_html}
  <div class="card-foot">
    <div class="foot-left">
      <span class="card-total">{html.escape(p['total'])}</span>
      <span class="pago-badge" style="background:{pago_color}">{pago_emoji} {html.escape(metodo)}</span>
      {pago_estado_html}
    </div>
    <div class="foot-right">{btn_sig}{btn_del}</div>
  </div>
</div>"""


@app.post("/admin/test-notify")
async def test_notify(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    from orders import _notify_owner
    from datetime import datetime, timezone, timedelta
    PERU_TZ = timezone(timedelta(hours=-5))
    now = datetime.now(PERU_TZ)
    await _notify_owner("TEST", "Mensaje de prueba 🌮", "S/ 0.00", "Efectivo", now)
    return JSONResponse({"status": "ok", "mensaje": "Notificación enviada — revisa los logs de Railway para ver si hubo error"})


@app.get("/pedidos", response_class=HTMLResponse)
async def pedidos_panel(
    credentials: HTTPBasicCredentials = Depends(verificar_admin),
    fecha: str = Query(None)          # ?fecha=DD/MM/YYYY para ver días anteriores
):
    from datetime import datetime, timezone, timedelta
    PERU_TZ = timezone(timedelta(hours=-5))
    hoy = datetime.now(PERU_TZ).strftime("%d/%m/%Y")
    fecha_sel = fecha if fecha else hoy
    pedidos = db.get_orders_for_date(fecha_sel) if hasattr(db, "get_orders_for_date") else db.get_orders_today()
    fechas_disponibles = db.get_available_dates() if hasattr(db, "get_available_dates") else [hoy]

    def _cnt(e): return sum(1 for p in pedidos if (p.get("estado") or "Nuevo 🆕") == e)
    count_nuevos   = _cnt("Nuevo 🆕")
    count_prep     = _cnt("En preparación 👨‍🍳")
    count_camino   = _cnt("En camino 🛵")
    count_entregado = _cnt("Entregado ✅")
    count_cancel   = _cnt("Cancelado ❌")
    total_activos  = len(pedidos) - count_entregado - count_cancel

    # Total acumulado del día: todo menos cancelados (incluye entregados)
    total_dia = sum(
        float(p["total"].replace("S/", "").replace(",", ".").strip())
        for p in pedidos
        if p.get("estado") != "Cancelado ❌" and p.get("total")
    ) if pedidos else 0

    cnt_yapeplin = sum(1 for p in pedidos if p.get("metodo_pago") in ("Yape/Plin", "Yape", "Plin") and p.get("estado") != "Cancelado ❌")
    cnt_efec = sum(1 for p in pedidos if p.get("metodo_pago") not in ("Yape/Plin", "Yape", "Plin") and p.get("estado") != "Cancelado ❌")

    cards = "".join(_render_card(p) for p in pedidos) if pedidos else '<div class="empty">No hay pedidos hoy todavía 🌮</div>'

    # Inject Python data as JS constants
    estados_js   = json.dumps(ESTADOS)
    badge_js     = json.dumps(ESTADO_BADGE)
    bg_js        = json.dumps(ESTADO_COLORS)
    step_idx_js  = json.dumps(STEP_IDX)
    step_lbl_js  = json.dumps(STEP_LABELS)

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<title>Pedidos — Chilango</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;min-height:100vh}}

/* ── Header ── */
.hdr{{background:#2D5016;color:white;padding:10px 18px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:50;box-shadow:0 2px 8px rgba(0,0,0,.25)}}
.hdr img{{height:38px;border-radius:8px}}
.hdr-title{{flex:1}}
.hdr-title h1{{font-size:16px;font-weight:700}}
.hdr-title small{{font-size:11px;opacity:.7}}
.hdr-right{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.chip{{background:rgba(255,255,255,.18);border-radius:20px;padding:5px 13px;font-size:13px;font-weight:700;white-space:nowrap}}
.chip.yape{{background:#6c3d98}}
.chip.plin{{background:#0066cc}}
.chip.efec{{background:#2D5016;border:1px solid rgba(255,255,255,.3)}}

/* ── Nav ── */
.nav{{background:#1b3a0e;display:flex}}
.nav a{{color:rgba(255,255,255,.7);text-decoration:none;padding:9px 18px;font-size:13px;transition:background .15s}}
.nav a:hover,.nav a.active{{color:#fff;background:rgba(255,255,255,.12)}}

/* ── Toolbar ── */
.toolbar{{background:#fff;padding:10px 16px;display:flex;align-items:center;gap:10px;border-bottom:1px solid #e0e0e0;flex-wrap:wrap}}
.toolbar-info{{font-size:13px;color:#555;flex:1}}
.toolbar-info strong{{color:#2D5016}}
.btn-test{{background:#6c3d98;color:#fff;border:none;padding:7px 16px;border-radius:20px;cursor:pointer;font-size:12px;font-weight:600}}
.btn-test:hover{{background:#5a3180}}

/* ── Filtros ── */
.filters{{background:#fff;padding:8px 16px;border-bottom:1px solid #e0e0e0;display:flex;gap:6px;overflow-x:auto;-webkit-overflow-scrolling:touch}}
.tab{{border:none;background:#f0f2f5;color:#555;padding:6px 14px;border-radius:20px;cursor:pointer;font-size:12px;font-weight:600;white-space:nowrap;transition:all .15s}}
.tab:hover{{background:#e0e0e0}}
.tab.active{{background:#2D5016;color:#fff}}

/* ── Grid de tarjetas ── */
.grid{{padding:14px;display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:12px;max-width:1100px;margin:0 auto}}

/* ── Tarjeta ── */
.card{{border-radius:12px;padding:14px;box-shadow:0 2px 8px rgba(0,0,0,.08);transition:box-shadow .2s,opacity .3s}}
.card:hover{{box-shadow:0 4px 16px rgba(0,0,0,.13)}}
.card.hidden{{display:none}}
.card-top{{display:flex;align-items:center;gap:6px;margin-bottom:10px;flex-wrap:wrap}}
.card-id{{font-size:11px;color:#aaa;font-weight:600}}
.card-time{{font-size:12px;color:#666}}
.card-phone{{font-size:13px;color:#111;font-weight:700}}
.badge{{font-size:11px;color:#fff;padding:3px 10px;border-radius:20px;font-weight:700;margin-left:auto;white-space:nowrap}}
.mod-badge{{font-size:10px;background:#e65100;color:#fff;padding:2px 7px;border-radius:20px;font-weight:700}}
.entrega-badge{{font-size:10px;padding:2px 8px;border-radius:20px;font-weight:700;white-space:nowrap}}
.entrega-badge.delivery{{background:#0277bd;color:#fff}}
.entrega-badge.recojo{{background:#6a1b9a;color:#fff}}
.pago-estado{{font-size:11px;padding:2px 8px;border-radius:20px;font-weight:700}}
.pago-estado.pagado{{background:#e8f5e9;color:#2e7d32;border:1px solid #a5d6a7}}
.pago-estado.pendiente{{background:#fff3e0;color:#e65100;border:1px solid #ffcc80}}
.btn-next.recojo-next{{background:#6a1b9a}}
.nav-badge{{background:#e53935;color:#fff;border-radius:10px;min-width:18px;height:18px;font-size:10px;font-weight:700;display:none;align-items:center;justify-content:center;padding:0 5px;margin-left:5px;vertical-align:middle;line-height:18px}}

/* ── Progress ── */
.progress-row{{display:flex;align-items:center;margin-bottom:10px}}
.step{{display:flex;flex-direction:column;align-items:center;font-size:9px;color:#bbb;gap:3px;min-width:0}}
.step span{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:52px}}
.sdot{{width:10px;height:10px;border-radius:50%;background:#ddd;transition:background .3s}}
.s-done .sdot{{background:#2D5016}}
.s-active .sdot{{background:#f57f17;box-shadow:0 0 0 3px rgba(245,127,23,.25)}}
.s-done span,.s-active span{{color:#333;font-weight:600}}
.sline{{flex:1;height:2px;background:#ddd;margin:0 2px;margin-bottom:12px}}
.line-done{{background:#2D5016}}

/* ── Items / Dir ── */
.card-items{{font-size:13px;color:#333;background:rgba(0,0,0,.04);padding:8px 10px;border-radius:8px;margin-bottom:8px;word-break:break-word}}
.item-line{{padding:2px 0;line-height:1.4}}
.card-dir{{font-size:12px;color:#555;background:rgba(0,0,0,.04);padding:6px 10px;border-radius:8px;margin-bottom:10px}}
.card-dir.sin-dir{{color:#bbb;font-style:italic}}

/* ── Footer ── */
.card-foot{{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}}
.foot-left{{display:flex;align-items:center;gap:8px}}
.card-total{{font-size:16px;font-weight:800;color:#2D5016}}
.pago-badge{{font-size:11px;color:#fff;padding:3px 10px;border-radius:20px;font-weight:700}}
.foot-right{{display:flex;align-items:center;gap:6px}}
.btn-next{{background:#2D5016;color:#fff;border:none;padding:8px 16px;border-radius:20px;cursor:pointer;font-size:12px;font-weight:700;transition:background .15s,transform .1s}}
.btn-next:hover{{background:#3a6b1e}}
.btn-next:active{{transform:scale(.96)}}
.btn-next:disabled{{background:#aaa;cursor:not-allowed}}
.btn-del{{background:transparent;border:1px solid #e0e0e0;padding:6px 9px;border-radius:8px;cursor:pointer;font-size:14px;color:#c62828;transition:background .15s}}
.btn-del:hover{{background:#fce4ec;border-color:#c62828}}
.lbl-done{{font-size:12px;color:#666;font-weight:600}}

/* ── Misc ── */
.empty{{text-align:center;padding:60px 20px;color:#aaa;font-size:15px;grid-column:1/-1}}
.footer-note{{text-align:center;font-size:11px;color:#bbb;padding:12px}}

/* ── Toast ── */
.toast{{position:fixed;bottom:24px;right:24px;background:#2D5016;color:#fff;padding:12px 22px;border-radius:30px;font-size:14px;font-weight:700;box-shadow:0 4px 20px rgba(0,0,0,.3);z-index:200;transform:translateY(80px);opacity:0;transition:all .35s cubic-bezier(.34,1.56,.64,1)}}
.toast.show{{transform:translateY(0);opacity:1}}
</style>
</head>
<body>

<div class="hdr">
  <img src="/static/logo.png" alt="Chilango">
  <div class="hdr-title"><h1>Chilango</h1><small>Panel de operaciones</small></div>
  <div class="hdr-right">
    <span class="chip" id="chipTotal">💰 S/ {total_dia:.2f}</span>
    <span class="chip yape" id="cntYapePlin">💜 {cnt_yapeplin} Yape/Plin</span>
    <span class="chip efec" id="cntEfec">💵 {cnt_efec} Efectivo</span>
  </div>
</div>

<nav class="nav">
  <a href="/pedidos" class="active">📦 Pedidos <span class="nav-badge" id="navBadge" style="display:none">0</span></a>
  <a href="/admin">💬 Conversaciones</a>
</nav>

<div class="toolbar">
  <span class="toolbar-info">📅 <strong id="totalCount">{len(pedidos)}</strong> pedidos &nbsp;·&nbsp; <span id="activosCount">{total_activos}</span> activos</span>
  <select id="fechaSelect" onchange="if(this.value)location.href='/pedidos?fecha='+encodeURIComponent(this.value)" style="border:1px solid #ccc;border-radius:8px;padding:5px 10px;font-size:13px;cursor:pointer">
    {"".join(f'<option value="{f}" {"selected" if f == fecha_sel else ""}>{f}{" (hoy)" if f == hoy else ""}</option>' for f in fechas_disponibles)}
  </select>
  <button class="btn-test" onclick="probarNotif()">🔔 Probar notificación</button>
</div>

<div class="filters">
  <button class="tab active" onclick="filterCards('all',this)">Todos ({len(pedidos)})</button>
  <button class="tab" onclick="filterCards('Nuevo 🆕',this)" id="tabNuevo">🆕 Nuevos ({count_nuevos})</button>
  <button class="tab" onclick="filterCards('En preparación 👨‍🍳',this)">👨‍🍳 Preparación ({count_prep})</button>
  <button class="tab" onclick="filterCards('En camino 🛵',this)">🛵 En camino ({count_camino})</button>
  <button class="tab" onclick="filterCards('Entregado ✅',this)">✅ Entregados ({count_entregado})</button>
  <button class="tab" onclick="filterCards('Cancelado ❌',this)">❌ Cancelados ({count_cancel})</button>
</div>

<div class="grid" id="ordersGrid">{cards}</div>
<div class="footer-note" id="lastRefresh">🔄 Actualización automática cada 10 s</div>
<div class="toast" id="toast">🔔 Nuevo pedido llegó</div>

<script>
const ESTADOS   = {estados_js};
const BADGE_CLR = {badge_js};
const BG_CLR    = {bg_js};
const STEP_IDX  = {step_idx_js};
const STEP_LBL  = {step_lbl_js};

let knownIds  = new Set(Array.from(document.querySelectorAll('.card')).map(c => +c.id.replace('card-','')));
let curFilter = 'all';
let audioCtx  = null;

/* ── Sonido ── */
function playBeep() {{
  try {{
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    [880, 1100, 1320].forEach((f, i) => {{
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.connect(g); g.connect(audioCtx.destination);
      o.type = 'sine';
      o.frequency.value = f;
      g.gain.setValueAtTime(0.25, audioCtx.currentTime + i*0.12);
      g.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + i*0.12 + 0.25);
      o.start(audioCtx.currentTime + i*0.12);
      o.stop(audioCtx.currentTime + i*0.12 + 0.3);
    }});
  }} catch(e) {{}}
}}

/* ── Toast ── */
function showToast(msg) {{
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3500);
}}

/* ── Filtro ── */
function filterCards(estado, btn) {{
  curFilter = estado;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  if (btn) btn.classList.add('active');
  document.querySelectorAll('.card').forEach(c => {{
    const e = c.dataset.estado;
    c.classList.toggle('hidden', estado !== 'all' && e !== estado);
  }});
}}

/* ── Cambiar estado (AJAX) ── */
async function cambiarEstado(id, nuevoEstado) {{
  const card = document.getElementById('card-' + id);
  const btn  = card ? card.querySelector('.btn-next') : null;
  if (btn) {{ btn.disabled = true; btn.textContent = '⏳'; }}
  try {{
    const r = await fetch('/api/pedidos/estado', {{
      method: 'POST',
      credentials: 'same-origin',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{order_id: id, estado: nuevoEstado}})
    }});
    if (r.status === 401) {{ location.reload(); return; }}
    if (!r.ok) throw new Error(await r.text());
    await refreshOrders();
  }} catch(e) {{
    alert('Error al actualizar: ' + e.message);
    if (btn) {{ btn.disabled = false; btn.textContent = '→ ' + nuevoEstado; }}
  }}
}}

/* ── Eliminar (AJAX) ── */
async function eliminarPedido(id, btnEl) {{
  if (!confirm('¿Eliminar este pedido? No se puede deshacer.')) return;
  try {{
    const r = await fetch('/api/pedidos/eliminar', {{
      method: 'POST',
      credentials: 'same-origin',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{order_id: id}})
    }});
    if (r.status === 401) {{ location.reload(); return; }}
    const card = document.getElementById('card-' + id);
    if (card) {{ card.style.opacity='0'; card.style.transform='scale(.95)'; setTimeout(()=>card.remove(),300); }}
    knownIds.delete(id);
  }} catch(e) {{ alert('Error al eliminar: ' + e.message); }}
}}

/* ── Construir tarjeta desde JSON ── */
function buildCard(p) {{
  const estado   = p.estado || 'Nuevo 🆕';
  const bg       = BG_CLR[estado]    || '#f5f5f5';
  const badgeClr = BADGE_CLR[estado] || '#666';
  const sIdx     = STEP_IDX[estado] !== undefined ? STEP_IDX[estado] : -1;

  const steps = STEP_LBL.map((lbl, i) => {{
    const cls = i < sIdx ? 's-done' : (i === sIdx ? 's-active' : 's-pending');
    const line = i < STEP_LBL.length-1
      ? `<div class="sline ${{i < sIdx ? 'line-done' : 'line-pending'}}"></div>`
      : '';
    return `<div class="step ${{cls}}"><div class="sdot"></div><span>${{lbl}}</span></div>${{line}}`;
  }}).join('');

  // siguiente_estado viene calculado por el servidor (evita bug de emoji encoding en JS)
  const siguiente  = p.siguiente_estado || null;
  const es_cancel  = estado === 'Cancelado ❌';
  const esRecojo   = (p.es_recojo === true || p.es_recojo === 1);

  // Botón siguiente: para recojo en preparación el botón dice "Listo p/retirar"
  let btnSig;
  if (siguiente) {{
    const esSiguienteCamino = p.siguiente_estado_raw === 'En camino 🛵';
    const lblBtn = (esRecojo && esSiguienteCamino) ? '📦 Listo p/retirar' : `→ ${{esc(siguiente)}}`;
    const clsBtn = (esRecojo && esSiguienteCamino) ? 'btn-next recojo-next' : 'btn-next';
    btnSig = `<button class="${{clsBtn}}" data-next="${{esc(p.siguiente_estado_raw || siguiente)}}" onclick="cambiarEstado(${{p.id}},this.dataset.next)">${{lblBtn}}</button>`;
  }} else {{
    btnSig = es_cancel ? `<span class="lbl-done" style="color:#c62828">Cancelado</span>` : `<span class="lbl-done">✅ Completado</span>`;
  }}

  const metodo    = p.metodo_pago || 'Efectivo';
  const pagoClr   = {{'Yape/Plin':'#6c3d98',Yape:'#6c3d98',Plin:'#6c3d98',Efectivo:'#2D5016'}}[metodo] || '#555';
  const pagoEmoji = {{'Yape/Plin':'💜',Yape:'💜',Plin:'💜',Efectivo:'💵'}}[metodo] || '💳';
  const esDigital = ['Yape/Plin','Yape','Plin'].includes(metodo);
  const pagoEstadoHtml = esDigital
    ? `<span class="pago-estado pagado">✅ Pagado</span>`
    : `<span class="pago-estado pendiente">💵 Cobrar al entregar</span>`;

  const entregaBadge = esRecojo
    ? `<span class="entrega-badge recojo">🏪 Recojo</span>`
    : `<span class="entrega-badge delivery">🏍️ Delivery</span>`;

  const dirTexto = esRecojo ? '📍 Recojo en local' : (p.direccion ? `📍 ${{esc(p.direccion)}}` : null);
  const dirHtml  = dirTexto
    ? `<div class="card-dir">${{dirTexto}}</div>`
    : `<div class="card-dir sin-dir">📍 Sin dirección</div>`;

  const modBadge  = p.modificado ? `<span class="mod-badge">✏️ Mod</span>` : '';

  // Bullets para múltiples productos
  const itemsList = (p.items || '').split(',').map(s => s.trim()).filter(Boolean);
  const itemsHtml = itemsList.length > 1
    ? itemsList.map(i => `<div class="item-line">• ${{esc(i)}}</div>`).join('')
    : esc(p.items || '');

  return `<div class="card" id="card-${{p.id}}" data-estado="${{esc(estado)}}" data-recojo="${{esRecojo?1:0}}"
    style="border-left:4px solid ${{badgeClr}};background:${{bg}}">
  <div class="card-top">
    <span class="card-id">#${{p.id}}</span>
    <span class="card-time">🕒 ${{esc(p.hora)}}</span>
    <span class="card-phone">+${{esc(p.phone)}}</span>
    ${{entregaBadge}}
    ${{modBadge}}
    <span class="badge" style="background:${{badgeClr}}">${{esc(estado)}}</span>
  </div>
  <div class="progress-row">${{steps}}</div>
  <div class="card-items">${{itemsHtml}}</div>
  ${{dirHtml}}
  <div class="card-foot">
    <div class="foot-left">
      <span class="card-total">${{esc(p.total)}}</span>
      <span class="pago-badge" style="background:${{pagoClr}}">${{pagoEmoji}} ${{esc(metodo)}}</span>
      ${{pagoEstadoHtml}}
    </div>
    <div class="foot-right">
      ${{btnSig}}
      <button class="btn-del" onclick="eliminarPedido(${{p.id}},this)" title="Eliminar">🗑️</button>
    </div>
  </div>
</div>`;
}}

function esc(s) {{
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

/* ── Refresh automático ── */
async function refreshOrders() {{
  try {{
    const r = await fetch('/api/pedidos', {{credentials:'same-origin'}});
    if (r.status === 401) {{ location.reload(); return; }}
    const data = await r.json();
    const pedidos = data.pedidos;

    // Detectar pedidos nuevos
    const newOnes = pedidos.filter(p => !knownIds.has(p.id));
    if (newOnes.length > 0 && knownIds.size > 0) {{
      playBeep();
      showToast(`🔔 ${{newOnes.length === 1 ? 'Nuevo pedido llegó' : newOnes.length + ' nuevos pedidos'}}`);
      document.title = '🔔 Nuevo pedido — Chilango';
      setTimeout(() => {{ document.title = 'Pedidos — Chilango'; }}, 5000);
    }}
    knownIds = new Set(pedidos.map(p => p.id));

    // Re-renderizar tarjetas
    const grid = document.getElementById('ordersGrid');
    if (pedidos.length === 0) {{
      grid.innerHTML = '<div class="empty">No hay pedidos hoy todavía 🌮</div>';
    }} else {{
      grid.innerHTML = pedidos.map(buildCard).join('');
    }}

    // Mientras estamos en la página de Pedidos, todos los "Nuevo" se marcan como vistos
    const nuevoIds = pedidos.filter(p => (p.estado||'').startsWith('Nuevo')).map(p => p.id);
    const seen = new Set(JSON.parse(localStorage.getItem('seenNuevoIds') || '[]'));
    nuevoIds.forEach(id => seen.add(id));
    localStorage.setItem('seenNuevoIds', JSON.stringify([...seen]));

    // Ocultar burbuja mientras estamos en esta página
    const navBadge = document.getElementById('navBadge');
    if (navBadge) navBadge.style.display = 'none';

    // Re-aplicar filtro activo
    filterCards(curFilter, document.querySelector('.tab.active'));

    // Actualizar contadores
    document.getElementById('totalCount').textContent = pedidos.length;
    const activos = pedidos.filter(p => !['Entregado ✅','Cancelado ❌'].includes(p.estado)).length;
    document.getElementById('activosCount').textContent = activos;

    // Actualizar total acumulado (todos menos cancelados)
    const noCancel = pedidos.filter(p => (p.estado || '') !== 'Cancelado ❌');
    const totalDia = noCancel.reduce((sum, p) => {{
      const t = parseFloat((p.total || '0').replace('S/', '').replace(',','.').trim()) || 0;
      return sum + t;
    }}, 0);
    const chipTotal = document.getElementById('chipTotal');
    if (chipTotal) chipTotal.textContent = `💰 S/ ${{totalDia.toFixed(2)}}`;

    // Actualizar chips de método de pago
    const cntYP = noCancel.filter(p => ['Yape/Plin','Yape','Plin'].includes(p.metodo_pago)).length;
    const cntEf = noCancel.filter(p => !['Yape/Plin','Yape','Plin'].includes(p.metodo_pago)).length;
    const chipYP = document.getElementById('cntYapePlin');
    const chipEf = document.getElementById('cntEfec');
    if (chipYP) chipYP.textContent = `💜 ${{cntYP}} Yape/Plin`;
    if (chipEf) chipEf.textContent = `💵 ${{cntEf}} Efectivo`;

    const now = new Date().toLocaleTimeString('es-PE',{{hour:'2-digit',minute:'2-digit'}});
    document.getElementById('lastRefresh').textContent = `🔄 Actualizado ${{now}}`;
  }} catch(e) {{
    console.warn('Refresh error:', e);
  }}
}}

function probarNotif() {{
  fetch('/admin/test-notify', {{method:'POST',credentials:'same-origin'}})
    .then(r=>r.json())
    .then(()=>alert('✅ Solicitud enviada — revisa logs de Railway'))
    .catch(e=>alert('Error: '+e));
}}

// Iniciar polling cada 10 segundos
setInterval(refreshOrders, 10000);

// Al abrir la página marcar todos los "Nuevo" actuales como vistos
(function markNuevosAsSeen() {{
  const cards = document.querySelectorAll('.card[data-estado]');
  const ids = Array.from(cards)
    .filter(c => (c.dataset.estado||'').startsWith('Nuevo'))
    .map(c => parseInt(c.id.replace('card-', '')))
    .filter(id => !isNaN(id));
  const seen = new Set(JSON.parse(localStorage.getItem('seenNuevoIds') || '[]'));
  ids.forEach(id => seen.add(id));
  localStorage.setItem('seenNuevoIds', JSON.stringify([...seen]));
  const nb = document.getElementById('navBadge');
  if (nb) nb.style.display = 'none';
}})();
</script>
</body></html>"""


@app.get("/api/pedidos")
async def api_pedidos_json(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    """Endpoint JSON para polling del frontend.
    Incluye 'siguiente_estado' calculado server-side para evitar comparaciones
    de emojis en JS (que pueden fallar por diferencias de encoding).
    """
    pedidos = db.get_orders_today()
    # Normalizar estados sin emoji (DEFAULT antiguo de BD) antes de procesar
    _norm = {
        "Nuevo": "Nuevo 🆕",
        "En preparación": "En preparación 👨‍🍳",
        "En camino": "En camino 🛵",
        "Entregado": "Entregado ✅",
    }
    for p in pedidos:
        estado = p.get("estado") or "Nuevo 🆕"
        estado = _norm.get(estado, estado)
        p["estado"] = estado
        es_cancel = estado == "Cancelado ❌"
        idx = ESTADOS.index(estado) if estado in ESTADOS else -1
        sig = ESTADOS[idx + 1] if (not es_cancel and 0 <= idx < len(ESTADOS) - 1) else None
        p["siguiente_estado"] = sig
        p["siguiente_estado_raw"] = sig  # alias explícito para el JS
        p["es_recojo"] = (p.get("direccion") or "").strip().lower() == "recojo"
    return JSONResponse({"pedidos": pedidos})


@app.post("/api/pedidos/estado")
async def api_actualizar_estado(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """Cambia el estado de un pedido — responde JSON para AJAX."""
    data = await request.json()
    order_id = int(data.get("order_id", 0))
    estado   = data.get("estado", "")
    if not order_id or estado not in ESTADOS + ["Cancelado ❌"]:
        return JSONResponse({"status": "error", "msg": "Datos inválidos"}, status_code=400)
    order = db.get_order_by_id(order_id)
    db.update_order_estado(order_id, estado)
    if estado == "En camino 🛵":
        await _notify_order_camino(order)
    return JSONResponse({"status": "ok", "estado": estado})


@app.post("/api/pedidos/eliminar")
async def api_eliminar_pedido(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """Elimina un pedido — responde JSON para AJAX."""
    data = await request.json()
    order_id = int(data.get("order_id", 0))
    if order_id:
        db.delete_order(order_id)
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "error"}, status_code=400)


@app.post("/pedidos/estado")
async def actualizar_estado(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """Fallback form-POST (por si acaso)."""
    from fastapi.responses import RedirectResponse
    form = await request.form()
    order_id = int(form.get("order_id", 0))
    estado   = form.get("estado", "")
    if order_id and estado in ESTADOS + ["Cancelado ❌"]:
        order = db.get_order_by_id(order_id)
        db.update_order_estado(order_id, estado)
        if estado == "En camino 🛵":
            await _notify_order_camino(order)
    return RedirectResponse(url="/pedidos", status_code=303)


@app.post("/pedidos/eliminar")
async def eliminar_pedido(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """Fallback form-POST (por si acaso)."""
    from fastapi.responses import RedirectResponse
    form = await request.form()
    order_id = int(form.get("order_id", 0))
    if order_id:
        db.delete_order(order_id)
    return RedirectResponse(url="/pedidos", status_code=303)


@app.post("/admin/mark-read/{phone}")
async def mark_read(phone: str, credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    db.mark_read(phone)
    return JSONResponse({"status": "ok"})


@app.post("/admin/send-message")
async def send_manual_message(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """El equipo envía un mensaje manual a un cliente desde el panel de conversaciones."""
    from datetime import datetime, timezone, timedelta
    PERU_TZ = timezone(timedelta(hours=-5))
    data = await request.json()
    phone   = data.get("phone", "").strip()
    message = data.get("message", "").strip()
    if not phone or not message:
        return JSONResponse({"status": "error", "msg": "Faltan datos"}, status_code=400)
    # Enviar por WhatsApp
    await send_whatsapp_message(phone, message)
    # Guardar en historial marcado como manual (no lo procesa Claude como tag)
    now_ts = datetime.now(PERU_TZ).strftime("%H:%M")
    db.append_message(phone, "assistant", message, ts=now_ts, manual=True)
    db.mark_unread(phone)
    print(f"[MANUAL] Mensaje enviado a {phone}: {message[:60]}")
    return JSONResponse({"status": "ok"})


@app.get("/api/conversations")
async def api_conversations(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    """Endpoint JSON para polling del panel admin sin recargar la página."""
    conversaciones_raw = db.get_conversations_with_status()
    # Sidebar HTML (reproducir la misma lógica de /admin)
    contacts_html = ""
    for phone, data in conversaciones_raw.items():
        mensajes = data["messages"]
        leida = data["leida"]
        if not mensajes:
            continue
        ultimo = mensajes[-1]
        contenido = ultimo["content"]
        if isinstance(contenido, list):
            preview = next((b["text"] for b in contenido if b.get("type") == "text"), "[imagen]")
        else:
            preview = contenido
        preview = html.escape(str(preview)[:50])
        badge = "" if leida else f'<div class="contact-unread">{sum(1 for m in mensajes if m["role"] == "user")}</div>'
        unread_class = "" if leida else " unread"
        contacts_html += (
            f'<div class="contact{unread_class}" id="c_{html.escape(phone)}" onclick="showChat(\'{html.escape(phone)}\')">'
            f'<div class="avatar">👤</div>'
            f'<div class="contact-info"><div class="contact-name">+{html.escape(phone)}</div>'
            f'<div class="contact-preview">{preview}</div></div>{badge}</div>'
        )
    # Mensajes limpios (sin imágenes) + timestamp si existe
    conv_clean = {}
    for phone, data in conversaciones_raw.items():
        conv_clean[phone] = []
        for m in data["messages"]:
            c = m["content"]
            if isinstance(c, list):
                texto = next((b["text"] for b in c if b.get("type") == "text"), "[imagen 📷]")
            else:
                texto = c
            conv_clean[phone].append({
                "role": m["role"],
                "content": texto,
                "ts": m.get("ts", ""),
                "manual": m.get("manual", False),
            })
    return JSONResponse({"contacts_html": contacts_html, "convs": conv_clean})


@app.get("/admin", response_class=HTMLResponse)
async def admin(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    conversaciones_raw = db.get_conversations_with_status()
    num_orders = get_orders_count()

    # Sidebar de contactos
    contacts_html = ""
    for phone, data in conversaciones_raw.items():
        mensajes = data["messages"]
        leida = data["leida"]
        if not mensajes:
            continue
        ultimo = mensajes[-1]
        contenido = ultimo["content"]
        if isinstance(contenido, list):
            preview = next((b["text"] for b in contenido if b.get("type") == "text"), "[imagen]")
        else:
            preview = contenido
        preview = html.escape(str(preview)[:50])
        badge = "" if leida else f'<div class="contact-unread">{sum(1 for m in mensajes if m["role"] == "user")}</div>'
        unread_class = "" if leida else " unread"
        contacts_html += f"""
        <div class="contact{unread_class}" id="c_{html.escape(phone)}" onclick="showChat('{html.escape(phone)}')">
            <div class="avatar">👤</div>
            <div class="contact-info">
                <div class="contact-name">+{html.escape(phone)}</div>
                <div class="contact-preview">{preview}</div>
            </div>
            {badge}
        </div>"""

    if not contacts_html:
        contacts_html = "<div class='no-convs'>Sin conversaciones aún</div>"

    # Serializar conversaciones para JS (imágenes excluidas por tamaño, timestamps incluidos)
    conv_clean = {}
    for phone, data in conversaciones_raw.items():
        conv_clean[phone] = []
        for m in data["messages"]:
            c = m["content"]
            if isinstance(c, list):
                texto = next((b["text"] for b in c if b.get("type") == "text"), "[imagen 📷]")
            else:
                texto = c
            conv_clean[phone].append({
                "role": m["role"],
                "content": texto,
                "ts": m.get("ts", ""),
                "manual": m.get("manual", False),
            })
    conv_json = json.dumps(conv_clean, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Admin — Chilango Bot</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f0f2f5; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }}
        .header {{ background: #2D5016; color: white; padding: 10px 20px; display: flex; align-items: center; gap: 12px; flex-shrink: 0; }}
        .header img {{ height: 44px; border-radius: 8px; object-fit: cover; }}
        .header h1 {{ font-size: 17px; }}
        .header .sub {{ font-size: 12px; opacity: 0.7; }}
        .header .stats {{ margin-left: auto; font-size: 13px; opacity: 0.85; text-align: right; }}
        .container {{ display: flex; flex: 1; overflow: hidden; }}
        .sidebar {{ width: 320px; background: white; border-right: 1px solid #e0e0e0; display: flex; flex-direction: column; overflow: hidden; flex-shrink: 0; }}
        .sidebar-title {{ padding: 12px 16px; font-size: 12px; color: #667781; background: #f0f2f5; border-bottom: 1px solid #e9edef; font-weight: 600; letter-spacing: .5px; }}
        .sidebar-list {{ overflow-y: auto; flex: 1; }}
        .contact {{ padding: 12px 16px; border-bottom: 1px solid #e9edef; cursor: pointer; display: flex; align-items: center; gap: 12px; transition: background .1s; }}
        .contact:hover {{ background: #f5f5f5; }}
        .contact.active {{ background: #d9fdd3; }}
        .avatar {{ width: 46px; height: 46px; border-radius: 50%; background: #25d366; display: flex; align-items: center; justify-content: center; font-size: 20px; flex-shrink: 0; }}
        .contact-info {{ flex: 1; min-width: 0; }}
        .contact-name {{ font-weight: 600; font-size: 14px; color: #111; }}
        .contact-preview {{ font-size: 13px; color: #667781; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }}
        .contact-unread {{ font-size: 11px; background: #25d366; color: white; border-radius: 50%; width: 22px; height: 22px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; font-weight: 600; }}
        .contact.unread {{ background: #f0fdf4; }}
        .contact.unread .contact-name {{ color: #111; font-weight: 700; }}
        .chat-panel {{ flex: 1; display: flex; flex-direction: column; background: #efeae2; overflow: hidden; }}
        .chat-header {{ background: #f0f2f5; padding: 10px 16px; display: flex; align-items: center; gap: 12px; border-bottom: 1px solid #e0e0e0; flex-shrink: 0; }}
        .chat-header .avatar {{ width: 38px; height: 38px; font-size: 16px; }}
        .chat-header-name {{ font-weight: 600; font-size: 15px; }}
        .chat-messages {{ flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 4px; }}
        .empty-state {{ flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #667781; gap: 10px; }}
        .empty-state img {{ height: 80px; opacity: 0.4; border-radius: 8px; }}
        .bubble {{ max-width: 65%; padding: 7px 12px 7px 12px; border-radius: 8px; font-size: 14px; line-height: 1.5; white-space: pre-wrap; word-wrap: break-word; }}
        .bubble.cliente {{ background: white; align-self: flex-start; border-radius: 0 8px 8px 8px; box-shadow: 0 1px 1px rgba(0,0,0,.08); }}
        .bubble.bot {{ background: #d9fdd3; align-self: flex-end; border-radius: 8px 0 8px 8px; box-shadow: 0 1px 1px rgba(0,0,0,.08); }}
        .sender {{ font-size: 11px; font-weight: 700; margin-bottom: 3px; color: #25d366; }}
        .bubble.bot .sender {{ color: #128c7e; }}
        .no-convs {{ padding: 24px; color: #667781; text-align: center; font-size: 14px; }}
        .refresh-note {{ font-size: 11px; color: #667781; text-align: center; padding: 6px; background: #f0f2f5; flex-shrink: 0; }}
        .msg-ts {{ font-size: 10px; color: #aaa; font-weight: 400; margin-left: 6px; }}
        .bubble.manual {{ background: #fff8e1; align-self: flex-end; border-radius: 8px 0 8px 8px; box-shadow: 0 1px 1px rgba(0,0,0,.08); }}
        .bubble.manual .sender {{ color: #e65100; }}
        .chat-input-area {{ padding: 10px 12px; background: #f0f2f5; border-top: 1px solid #e0e0e0; display: flex; gap: 8px; align-items: center; flex-shrink: 0; }}
        .chat-input {{ flex: 1; border: 1px solid #ccc; border-radius: 20px; padding: 8px 14px; font-size: 14px; outline: none; font-family: inherit; }}
        .chat-input:focus {{ border-color: #2D5016; }}
        .chat-send-btn {{ background: #2D5016; color: white; border: none; border-radius: 50%; width: 40px; height: 40px; cursor: pointer; font-size: 18px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: background .15s; }}
        .chat-send-btn:hover {{ background: #3a6b1e; }}
        .chat-send-btn:disabled {{ background: #aaa; cursor: default; }}
    </style>
</head>
<body>
    <div class="header">
        <img src="/static/logo.png" alt="Chilango">
        <div>
            <h1>Chilango Bot</h1>
            <div class="sub">Panel de conversaciones</div>
        </div>
        <div class="stats">
            👥 {len(conversaciones_raw)} conversaciones<br>
            📦 {num_orders} pedidos registrados
        </div>
    </div>
    <div style="background:#1b3a0e;display:flex;">
        <a href="/pedidos" style="color:rgba(255,255,255,.7);text-decoration:none;padding:10px 20px;font-size:14px;">📦 Pedidos <span id="adminNavBadge" style="background:#e53935;color:#fff;border-radius:10px;min-width:18px;height:18px;font-size:10px;font-weight:700;display:none;align-items:center;justify-content:center;padding:0 5px;margin-left:4px;vertical-align:middle;line-height:18px">0</span></a>
        <a href="/admin" style="color:white;text-decoration:none;padding:10px 20px;font-size:14px;background:rgba(255,255,255,.1);">💬 Conversaciones</a>
    </div>
    <div class="container">
        <div class="sidebar">
            <div class="sidebar-title">CONVERSACIONES</div>
            <div class="sidebar-list">{contacts_html}</div>
        </div>
        <div class="chat-panel" id="chatPanel">
            <div class="empty-state">
                <img src="/static/logo.png" alt="">
                <span>Selecciona una conversación</span>
            </div>
        </div>
    </div>
    <div class="refresh-note">🔄 Actualización automática cada 20 segundos</div>

    <script>
        const convs = {conv_json};

        function esc(s) {{
            return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\\n/g,'<br>');
        }}

        function buildBubble(m) {{
            const isManual = !!m.manual;
            const lado  = isManual ? 'manual' : (m.role === 'user' ? 'cliente' : 'bot');
            const label = isManual ? '👨‍💼 Equipo' : (m.role === 'user' ? 'Cliente' : '🤖 Chilo');
            const tsHtml = m.ts ? `<span class="msg-ts">${{m.ts}}</span>` : '';
            return `<div class="bubble ${{lado}}"><div class="sender">${{label}}${{tsHtml}}</div>${{esc(m.content)}}</div>`;
        }}

        function showChat(phone) {{
            document.querySelectorAll('.contact').forEach(c => c.classList.remove('active'));
            const el = document.getElementById('c_' + phone);
            if (el) {{
                el.classList.add('active');
                el.classList.remove('unread');
                const badge = el.querySelector('.contact-unread');
                if (badge) badge.remove();
            }}

            fetch(`/admin/mark-read/${{encodeURIComponent(phone)}}`, {{
                method: 'POST', credentials: 'same-origin'
            }});

            const msgs = convs[phone] || [];
            const bubbles = msgs.map(buildBubble).join('');

            document.getElementById('chatPanel').innerHTML = `
                <div class="chat-header">
                    <div class="avatar">👤</div>
                    <div class="chat-header-name">+${{esc(phone)}}</div>
                </div>
                <div class="chat-messages" id="msgs">${{bubbles}}</div>
                <div class="chat-input-area">
                    <input type="text" id="manualInput" class="chat-input"
                           placeholder="Escribe un mensaje al cliente (ej: el delivery a tu zona es S/ 5.00)..."
                           onkeydown="if(event.key==='Enter' && !event.shiftKey){{ event.preventDefault(); sendManual('${{esc(phone)}}'); }}">
                    <button id="sendBtn" class="chat-send-btn" onclick="sendManual('${{esc(phone)}}')" title="Enviar">➤</button>
                </div>`;

            const msgsEl = document.getElementById('msgs');
            if (msgsEl) msgsEl.scrollTop = msgsEl.scrollHeight;
            sessionStorage.setItem('activePhone', phone);
        }}

        async function sendManual(phone) {{
            const input = document.getElementById('manualInput');
            const btn   = document.getElementById('sendBtn');
            const msg   = (input.value || '').trim();
            if (!msg) return;
            btn.disabled = true;
            input.value  = '';
            try {{
                const r = await fetch('/admin/send-message', {{
                    method: 'POST', credentials: 'same-origin',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{phone, message: msg}})
                }});
                if (!r.ok) {{ input.value = msg; alert('Error al enviar'); return; }}
                // Mostrar inmediatamente en el chat
                const msgsEl = document.getElementById('msgs');
                if (msgsEl) {{
                    const now = new Date().toLocaleTimeString('es-PE', {{hour:'2-digit', minute:'2-digit'}});
                    msgsEl.innerHTML += buildBubble({{role:'assistant', content: msg, ts: now, manual: true}});
                    msgsEl.scrollTop = msgsEl.scrollHeight;
                }}
            }} catch(e) {{
                input.value = msg;
                alert('Error: ' + e.message);
            }}
            btn.disabled = false;
            input.focus();
        }}

        // Restaurar conversación activa al cargar
        const saved = sessionStorage.getItem('activePhone');
        if (saved && convs[saved]) showChat(saved);

        // ── Polling AJAX: actualizar sidebar sin recargar página ──
        async function pollConversaciones() {{
            try {{
                const r = await fetch('/api/conversations', {{credentials:'same-origin'}});
                if (!r.ok) return;
                const data = await r.json();
                // Actualizar sidebar
                const lista = document.querySelector('.sidebar-list');
                if (!lista) return;
                lista.innerHTML = data.contacts_html || '';
                // Actualizar mensajes del chat abierto (si hay uno)
                const activePhone = sessionStorage.getItem('activePhone');
                if (activePhone && data.convs[activePhone]) {{
                    const msgsEl = document.getElementById('msgs');
                    if (msgsEl) {{
                        const atBottom = msgsEl.scrollHeight - msgsEl.scrollTop - msgsEl.clientHeight < 60;
                        const newBubbles = data.convs[activePhone].map(buildBubble).join('');
                        if (msgsEl.innerHTML !== newBubbles) {{
                            msgsEl.innerHTML = newBubbles;
                            if (atBottom) msgsEl.scrollTop = msgsEl.scrollHeight;
                        }}
                    }}
                }}
            }} catch(e) {{}}
        }}
        setInterval(pollConversaciones, 5000);

        // Burbuja de nuevos pedidos en el nav
        async function checkPedidosNuevos() {{
            try {{
                const r = await fetch('/api/pedidos', {{credentials:'same-origin'}});
                if (!r.ok) return;
                const data = await r.json();
                const seenIds = new Set(JSON.parse(localStorage.getItem('seenNuevoIds') || '[]'));
                const n = data.pedidos.filter(p => (p.estado||'').startsWith('Nuevo') && !seenIds.has(p.id)).length;
                const badge = document.getElementById('adminNavBadge');
                if (badge) {{
                    badge.textContent = n;
                    badge.style.display = n > 0 ? 'inline-flex' : 'none';
                }}
            }} catch(e) {{}}
        }}
        checkPedidosNuevos();
        setInterval(checkPedidosNuevos, 15000);
    </script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
