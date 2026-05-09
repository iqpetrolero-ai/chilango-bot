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
    reply = await process_message(phone, message)
    await _send_reply(phone, reply, sending_id)


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


@app.post("/admin/test-notify")
async def test_notify(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    from orders import _notify_owner
    from datetime import datetime, timezone, timedelta
    PERU_TZ = timezone(timedelta(hours=-5))
    now = datetime.now(PERU_TZ)
    await _notify_owner("TEST", "Mensaje de prueba 🌮", "S/ 0.00", "Efectivo", now)
    return JSONResponse({"status": "ok", "mensaje": "Notificación enviada — revisa los logs de Railway para ver si hubo error"})


@app.get("/pedidos", response_class=HTMLResponse)
async def pedidos_panel(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    pedidos = db.get_orders_today()
    from datetime import datetime, timezone, timedelta
    PERU_TZ = timezone(timedelta(hours=-5))
    hoy = datetime.now(PERU_TZ).strftime("%d/%m/%Y")

    total_dia = sum(
        float(p["total"].replace("S/", "").replace(",", ".").strip())
        for p in pedidos
        if p["estado"] not in ("Entregado ✅", "Cancelado ❌") and p["total"]
    ) if pedidos else 0

    cards = ""
    for p in pedidos:
        estado = p["estado"] or "Nuevo 🆕"
        color = ESTADO_COLORS.get(estado, "#f5f5f5")
        badge_color = ESTADO_BADGE.get(estado, "#666")
        es_cancelado = estado == "Cancelado ❌"
        idx = ESTADOS.index(estado) if estado in ESTADOS else 0
        siguiente = ESTADOS[idx + 1] if (idx < len(ESTADOS) - 1 and not es_cancelado) else None
        btn_siguiente = f"""
            <form method="post" action="/pedidos/estado" style="display:inline">
                <input type="hidden" name="order_id" value="{p['id']}">
                <input type="hidden" name="estado" value="{siguiente}">
                <button type="submit" class="btn-next">→ {siguiente}</button>
            </form>""" if siguiente else ('<span class="done">Cancelado</span>' if es_cancelado else '<span class="done">Completado ✅</span>')

        btn_eliminar = f"""
            <form method="post" action="/pedidos/eliminar" style="display:inline"
                  onsubmit="return confirm('¿Eliminar este pedido? Esta acción no se puede deshacer.')">
                <input type="hidden" name="order_id" value="{p['id']}">
                <button type="submit" class="btn-del" title="Eliminar pedido">🗑️</button>
            </form>"""

        metodo = p.get("metodo_pago") or "Efectivo"
        pago_color = {"Yape": "#6c3d98", "Plin": "#0066cc", "Efectivo": "#2D5016"}.get(metodo, "#555")
        pago_emoji = {"Yape": "💜", "Plin": "💙", "Efectivo": "💵"}.get(metodo, "💳")
        direccion = p.get("direccion") or ""
        dir_html = f'<div class="card-dir">📍 {html.escape(direccion)}</div>' if direccion else '<div class="card-dir sin-dir">📍 Sin dirección registrada</div>'
        mod_badge = '<span class="mod-badge">✏️ Modificado</span>' if p.get("modificado") else ""

        cards += f"""
        <div class="card" style="background:{color}">
            <div class="card-header">
                <span class="card-time">🕒 {p['hora']}</span>
                <span class="card-phone">📱 +{html.escape(p['phone'])}</span>
                {mod_badge}
                <span class="card-badge" style="background:{badge_color}">{html.escape(estado)}</span>
            </div>
            <div class="card-items">{html.escape(p['items'])}</div>
            {dir_html}
            <div class="card-footer">
                <span class="card-total">{html.escape(p['total'])}</span>
                <span class="pago-badge" style="background:{pago_color}">{pago_emoji} {html.escape(metodo)}</span>
                {btn_siguiente}
                {btn_eliminar}
            </div>
        </div>"""

    if not cards:
        cards = "<div class='empty'>No hay pedidos hoy todavía 🌮</div>"

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Pedidos del día — Chilango</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f0f2f5; min-height: 100vh; }}
        .header {{ background: #2D5016; color: white; padding: 12px 20px; display: flex; align-items: center; gap: 12px; position: sticky; top: 0; z-index: 10; }}
        .header img {{ height: 40px; border-radius: 8px; }}
        .header h1 {{ font-size: 17px; }}
        .header .sub {{ font-size: 12px; opacity: 0.7; }}
        .header .total-dia {{ margin-left: auto; font-size: 15px; font-weight: 700; background: rgba(255,255,255,.15); padding: 6px 14px; border-radius: 20px; }}
        .nav {{ background: #1b3a0e; display: flex; gap: 0; }}
        .nav a {{ color: rgba(255,255,255,.7); text-decoration: none; padding: 10px 20px; font-size: 14px; }}
        .nav a:hover, .nav a.active {{ color: white; background: rgba(255,255,255,.1); }}
        .toolbar {{ padding: 12px 16px; display: flex; align-items: center; gap: 10px; background: white; border-bottom: 1px solid #e0e0e0; }}
        .toolbar span {{ font-size: 14px; color: #555; }}
        .toolbar strong {{ color: #2D5016; }}
        .content {{ padding: 16px; max-width: 700px; margin: 0 auto; }}
        .card {{ border-radius: 12px; padding: 14px; margin-bottom: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
        .card-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }}
        .card-time {{ font-size: 13px; color: #555; }}
        .card-phone {{ font-size: 13px; color: #333; font-weight: 600; }}
        .card-badge {{ font-size: 11px; color: white; padding: 3px 10px; border-radius: 20px; margin-left: auto; font-weight: 600; }}
        .card-items {{ font-size: 13px; color: #333; background: rgba(0,0,0,.04); padding: 8px 10px; border-radius: 8px; margin-bottom: 10px; white-space: pre-wrap; }}
        .card-footer {{ display: flex; align-items: center; justify-content: space-between; }}
        .card-total {{ font-size: 16px; font-weight: 700; color: #2D5016; }}
        .pago-badge {{ font-size: 12px; color: white; padding: 4px 10px; border-radius: 20px; font-weight: 600; }}
        .card-dir {{ font-size: 13px; color: #444; background: rgba(0,0,0,.04); padding: 6px 10px; border-radius: 8px; margin-bottom: 10px; }}
        .card-dir.sin-dir {{ color: #aaa; font-style: italic; }}
        .mod-badge {{ font-size: 11px; background: #e65100; color: white; padding: 2px 8px; border-radius: 20px; font-weight: 600; }}
        .btn-next {{ background: #2D5016; color: white; border: none; padding: 8px 16px; border-radius: 20px; cursor: pointer; font-size: 13px; font-weight: 600; }}
        .btn-next:hover {{ background: #3a6b1e; }}
        .btn-del {{ background: transparent; border: 1px solid #e0e0e0; padding: 6px 10px; border-radius: 8px; cursor: pointer; font-size: 15px; color: #c62828; }}
        .btn-del:hover {{ background: #fce4ec; border-color: #c62828; }}
        .done {{ font-size: 13px; color: #2e7d32; font-weight: 600; }}
        .empty {{ text-align: center; padding: 60px 20px; color: #888; font-size: 16px; }}
        .refresh-note {{ text-align: center; font-size: 11px; color: #aaa; padding: 12px; }}
    </style>
</head>
<body>
    <div class="header">
        <img src="/static/logo.png" alt="Chilango">
        <div>
            <h1>Chilango Bot</h1>
            <div class="sub">Panel de pedidos</div>
        </div>
        <div class="total-dia">💰 S/ {total_dia:.2f} hoy</div>
    </div>
    <div class="nav">
        <a href="/pedidos" class="active">📦 Pedidos del día</a>
        <a href="/admin">💬 Conversaciones</a>
    </div>
    <div class="toolbar">
        <span>📅 {hoy} &nbsp;|&nbsp; <strong>{len(pedidos)}</strong> pedidos</span>
        <button onclick="probarNotif()" style="margin-left:auto;background:#6c3d98;color:white;border:none;padding:6px 14px;border-radius:20px;cursor:pointer;font-size:13px;">🔔 Probar notificación</button>
    </div>
    <div class="content">
        {cards}
    </div>
    <div class="refresh-note">🔄 Actualización automática cada 20 segundos</div>
    <script>
        setTimeout(() => location.reload(), 20000);
        function probarNotif() {{
            fetch('/admin/test-notify', {{method:'POST', credentials:'same-origin'}})
                .then(r => r.json())
                .then(d => alert('✅ Solicitud enviada. Revisa los logs de Railway para ver si llegó o hubo error.'))
                .catch(e => alert('Error: ' + e));
        }}
    </script>
</body>
</html>"""


@app.post("/pedidos/estado")
async def actualizar_estado(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    from fastapi.responses import RedirectResponse
    form = await request.form()
    order_id = int(form.get("order_id", 0))
    estado = form.get("estado", "")
    estados_validos = ESTADOS + ["Cancelado ❌"]

    if order_id and estado in estados_validos:
        order = db.get_order_by_id(order_id)
        db.update_order_estado(order_id, estado)

        # Notificar al cliente cuando su pedido está en camino o listo para recojo
        if estado == "En camino 🛵" and order:
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

    return RedirectResponse(url="/pedidos", status_code=303)


@app.post("/pedidos/eliminar")
async def eliminar_pedido(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
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

    # Serializar conversaciones para JS (imágenes excluidas por tamaño)
    conv_clean = {}
    for phone, data in conversaciones_raw.items():
        conv_clean[phone] = []
        for m in data["messages"]:
            c = m["content"]
            if isinstance(c, list):
                texto = next((b["text"] for b in c if b.get("type") == "text"), "[imagen 📷]")
            else:
                texto = c
            conv_clean[phone].append({"role": m["role"], "content": texto})
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
        <a href="/pedidos" style="color:rgba(255,255,255,.7);text-decoration:none;padding:10px 20px;font-size:14px;">📦 Pedidos del día</a>
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

        function showChat(phone) {{
            document.querySelectorAll('.contact').forEach(c => c.classList.remove('active'));
            const el = document.getElementById('c_' + phone);
            if (el) {{
                el.classList.add('active');
                // Marcar como leída: quitar bolita verde y fondo destacado
                el.classList.remove('unread');
                const badge = el.querySelector('.contact-unread');
                if (badge) badge.remove();
            }}

            // Notificar al servidor (credentials: 'same-origin' reutiliza las credenciales ya autenticadas)
            fetch(`/admin/mark-read/${{encodeURIComponent(phone)}}`, {{
                method: 'POST',
                credentials: 'same-origin'
            }});

            const msgs = convs[phone] || [];
            const bubbles = msgs.map(m => {{
                const lado = m.role === 'user' ? 'cliente' : 'bot';
                const label = m.role === 'user' ? 'Cliente' : '🤖 Chilo';
                return `<div class="bubble ${{lado}}"><div class="sender">${{label}}</div>${{esc(m.content)}}</div>`;
            }}).join('');

            document.getElementById('chatPanel').innerHTML = `
                <div class="chat-header">
                    <div class="avatar">👤</div>
                    <div class="chat-header-name">+${{esc(phone)}}</div>
                </div>
                <div class="chat-messages" id="msgs">${{bubbles}}</div>`;

            const msgsEl = document.getElementById('msgs');
            if (msgsEl) msgsEl.scrollTop = msgsEl.scrollHeight;
            sessionStorage.setItem('activePhone', phone);
        }}

        // Restaurar conversación activa después del auto-refresh
        const saved = sessionStorage.getItem('activePhone');
        if (saved && convs[saved]) showChat(saved);

        setTimeout(() => location.reload(), 20000);
    </script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
