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
# ── Servicios de delivery (1 o 2) ────────────────────────────
# Backward compat: DELIVERY_PHONE se toma como delivery 1 si no hay DELIVERY_1_PHONE
_d1_phone = (os.environ.get("DELIVERY_1_PHONE") or os.environ.get("DELIVERY_PHONE", "")).strip()
_d1_name  = os.environ.get("DELIVERY_1_NAME", "Delivery 1").strip()
_d2_phone = os.environ.get("DELIVERY_2_PHONE", "").strip()
_d2_name  = os.environ.get("DELIVERY_2_NAME", "Delivery 2").strip()
DELIVERIES = [
    {"phone": _d1_phone, "name": _d1_name}
    for _ in [None] if _d1_phone
] + [
    {"phone": _d2_phone, "name": _d2_name}
    for _ in [None] if _d2_phone
]
DELIVERY_PHONE = _d1_phone  # backward compat para código existente

PALABRAS_CARTA = ["carta", "menu", "menú", "ver carta", "ver menu", "qué tienen", "que tienen"]

# Saludos genéricos que no necesitan procesarse después de la bienvenida
SALUDOS_GENERICOS = {"hola", "buenas", "buenos días", "buenas tardes", "buenas noches",
                     "hi", "hello", "hey", "ola", "buenas noches", "2"}

# ── Deduplicación de webhooks ─────────────────────────────────
# Meta reenvía el mismo mensaje si no recibe respuesta rápida.
# Guardamos los últimos 500 message IDs para evitar procesar dos veces.
_processed_msg_ids: set[str] = set()


async def send_whatsapp_message(to: str, text: str, phone_number_id: str = None) -> bool:
    """Envía mensaje WA. Retorna True si fue exitoso, False si hubo error."""
    # Leer siempre en tiempo de ejecución (no al arrancar el módulo)
    token = os.environ.get("META_ACCESS_TOKEN", "").strip() or META_ACCESS_TOKEN
    pid   = phone_number_id or os.environ.get("META_PHONE_NUMBER_ID", "").strip() or META_PHONE_NUMBER_ID
    # Normalizar número: quitar "+" y espacios
    to_clean = to.replace("+", "").replace(" ", "")
    if not pid or not token:
        print(f"[ERROR META] META_ACCESS_TOKEN o META_PHONE_NUMBER_ID no configurados — no se puede enviar a {to_clean}")
        return False
    url = f"https://graph.facebook.com/v19.0/{pid}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    to = to_clean
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                err = resp.json().get("error", {})
                print(f"[ERROR META] {resp.status_code} | código {err.get('code')} | {err.get('message', resp.text)}")
                return False
            return True
    except Exception as e:
        print(f"[ERROR META] Excepción al enviar WA a {to}: {e}")
        return False


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


async def send_whatsapp_buttons(to: str, body: str, buttons: list, phone_number_id: str = None):
    """Envía mensaje con botones interactivos de WhatsApp (máx 3 botones, 20 chars c/u)."""
    pid = phone_number_id or META_PHONE_NUMBER_ID
    url = f"https://graph.facebook.com/v19.0/{pid}/messages"
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                    for b in buttons
                ]
            },
        },
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            print(f"[ERROR BOTONES] {resp.status_code} {resp.text}")


async def send_escalate_button(phone: str, sending_id: str = None):
    """Manda el botón '¿Quieres hablar con el equipo?' al cliente."""
    await send_whatsapp_buttons(
        phone,
        "¿Quieres que alguien del equipo te escriba aquí mismo? 👨‍💼",
        [
            {"id": "equipo_si", "title": "Sí, por favor"},
            {"id": "equipo_no", "title": "No, gracias"},
        ],
        sending_id,
    )


def _parse_delivery_cost(text: str):
    """Extrae el monto numérico de la respuesta del motorizado. Ej: '7', 'S/7', '7 soles' → 7.0"""
    import re
    t = text.strip().lower()
    t = re.sub(r's\s*/?\s*', '', t)          # quitar S/
    t = t.replace("soles", "").replace("sol", "").replace(",", ".")
    m = re.search(r'\b(\d+(?:\.\d{1,2})?)\b', t)
    return float(m.group(1)) if m else None


def _parse_amount(text: str) -> float:
    """Extrae el valor numérico de un string de monto. Ej: 'S/ 31.90' → 31.9"""
    import re
    m = re.search(r'(\d+(?:[.,]\d{1,2})?)', (text or "").replace(",", "."))
    return float(m.group(1)) if m else 0.0


async def handle_message(phone: str, message: str, phone_number_id: str = None):
    msg_lower = message.lower().strip()
    sending_id = phone_number_id or META_PHONE_NUMBER_ID
    phone_clean = phone.replace("whatsapp:", "").replace("+", "")
    print(f"[MENSAJE] {phone}: {message}")

    # ── Respuesta del motorizado: no procesar como cliente ──────────────────────
    delivery_phones = {d["phone"].replace("+", "") for d in DELIVERIES}
    if phone_clean in delivery_phones:
        delivery_name = next(
            (d["name"] for d in DELIVERIES if d["phone"].replace("+", "") == phone_clean),
            "Motorizado"
        )
        print(f"[DELIVERY REPLY] {delivery_name} ({phone_clean}): {message}")

        # Intentar auto-responder al cliente con el costo total
        consulta = db.get_pending_delivery_query(phone_clean)
        if consulta:
            costo_delivery = _parse_delivery_cost(message)
            if costo_delivery is not None:
                subtotal_num = _parse_amount(consulta.get("subtotal", "0"))
                total_num    = subtotal_num + costo_delivery
                items_txt    = consulta.get("items", "")
                pago_txt     = consulta.get("pago", "")
                client_phone = consulta["client_phone"]

                msg_cliente = (
                    f"¡Ya tenemos el costo! 🛵\n\n"
                    f"🛒 {items_txt}\n"
                    f"📦 Empaque incluido\n"
                    f"🛵 Delivery: S/ {costo_delivery:.2f}\n"
                    f"💰 *Total completo: S/ {total_num:.2f}*\n\n"
                    f"¿Confirmamos tu pedido con {pago_txt}? 😊"
                )
                # Inyectar en historial para que Claude sepa el total al confirmar
                # client_phone está en formato "521234567890" (igual que llega del webhook)
                from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                _ts = _dt.now(_tz(_td(hours=-5))).strftime("%H:%M")
                db.append_message(client_phone, "assistant", msg_cliente, ts=_ts)
                db.mark_unread(client_phone)
                await send_whatsapp_message(client_phone, msg_cliente, sending_id)
                db.delete_delivery_query(consulta["id"])
                print(f"[DELIVERY COST] S/{costo_delivery} enviado a cliente +{client_phone} — total S/{total_num:.2f}")
            else:
                # No se pudo parsear el número — reenviar al dueño para que lo gestione manualmente
                aviso = f"🛵 *{delivery_name}* (respuesta sin monto reconocible):\n{message}"
                await send_whatsapp_message("51954713696", aviso)
        else:
            # Sin consulta pendiente — reenviar al dueño
            aviso = f"🛵 *Respuesta de {delivery_name}:*\n{message}"
            await send_whatsapp_message("51954713696", aviso)
        return

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
        await send_whatsapp_buttons(
            phone,
            "¿Qué hacemos? 👇",
            [
                {"id": "ver_carta",    "title": "🌮 Ver carta"},
                {"id": "hacer_pedido", "title": "🛵 Hacer un pedido"},
            ],
            sending_id,
        )
        if msg_lower not in SALUDOS_GENERICOS and message.strip():
            reply, escalate = await process_message(phone, message)
            await _send_reply(phone, reply, sending_id)
            if escalate:
                await send_escalate_button(phone, sending_id)
        else:
            db.append_message(phone, "user", message)
            db.append_message(phone, "assistant", bienvenida)
        db.mark_unread(phone)
        return

    db.mark_unread(phone)
    try:
        reply, escalate = await process_message(phone, message)
        await _send_reply(phone, reply, sending_id)
        if escalate:
            await send_escalate_button(phone, sending_id)
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
                reply, _ = await process_message_with_image(phone, image_bytes, mime_type)
                await send_whatsapp_message(phone, reply, phone_number_id)
            else:
                await send_whatsapp_message(phone, "No pude leer la imagen, ¿puedes enviarla de nuevo? 📸", phone_number_id)
        elif msg_type == "interactive":
            # Respuesta a botones interactivos
            btn = message_data.get("interactive", {}).get("button_reply", {})
            btn_id    = btn.get("id", "")
            btn_title = btn.get("title", "")
            from datetime import datetime, timezone, timedelta
            _PERU_TZ = timezone(timedelta(hours=-5))
            now_ts = datetime.now(_PERU_TZ).strftime("%H:%M")
            if btn_id == "ver_carta":
                db.append_message(phone, "user", "Ver carta", ts=now_ts)
                await handle_message(phone, "1", phone_number_id)
            elif btn_id == "hacer_pedido":
                db.append_message(phone, "user", "Hacer un pedido", ts=now_ts)
                await handle_message(phone, "2", phone_number_id)
            elif btn_id == "equipo_si":
                respuesta = "¡Perfecto! En breve alguien del equipo te escribirá aquí mismo 👨‍💼\nGracias por tu paciencia, Chilanguit@ 🌮"
                db.append_message(phone, "user",      "Sí, quiero hablar con el equipo", ts=now_ts)
                db.append_message(phone, "assistant", respuesta, ts=now_ts)
                db.mark_unread(phone)
                await send_whatsapp_message(phone, respuesta, phone_number_id)
                print(f"[ESCALATE] {phone} solicitó hablar con el equipo")
            elif btn_id == "equipo_no":
                respuesta = "Entendido. Cualquier cosa, aquí estamos 🌮"
                db.append_message(phone, "user",      "No, gracias", ts=now_ts)
                db.append_message(phone, "assistant", respuesta, ts=now_ts)
                await send_whatsapp_message(phone, respuesta, phone_number_id)
            else:
                # Botón desconocido: tratar como texto normal
                await handle_message(phone, btn_title or btn_id, phone_number_id)
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
    badge_color = ESTADO_BADGE.get(estado, "#666")
    pid = p["id"]

    # ── Progress steps ──────────────────────────────────────────
    step_idx = STEP_IDX.get(estado, -1)
    steps_parts = []
    for i, label in enumerate(STEP_LABELS):
        cls = "s-done" if i < step_idx else ("s-active" if i == step_idx else "")
        line_cls = "done" if i < step_idx else ""
        steps_parts.append(
            f'<div class="oc-step {cls}"><div class="oc-dot"></div><span>{label}</span></div>'
        )
        if i < len(STEP_LABELS) - 1:
            steps_parts.append(f'<div class="oc-line {line_cls}"></div>')
    steps_html = "".join(steps_parts)

    # ── Datos del pedido ────────────────────────────────────────
    es_cancelado = estado == "Cancelado ❌"
    activo = estado not in ("Entregado ✅", "Cancelado ❌")
    idx = ESTADOS.index(estado) if estado in ESTADOS else 0
    siguiente = ESTADOS[idx + 1] if (idx < len(ESTADOS) - 1 and not es_cancelado) else None

    metodo = p.get("metodo_pago") or "Efectivo"
    es_digital = metodo in ("Yape/Plin", "Yape", "Plin")
    metodo_cls = "metodo-yape" if es_digital else "metodo-efectivo"
    metodo_icon = "💜" if es_digital else "💵"
    cobro_badge = (
        '<span class="oc-pbadge pagado">✅ Pagado</span>' if es_digital
        else '<span class="oc-pbadge cobrar">💳 Cobrar al entregar</span>'
    )

    direccion = p.get("direccion") or ""
    es_recojo = direccion.strip().lower() == "recojo"
    entrega_cls = "recojo" if es_recojo else "delivery"
    entrega_txt = "🏪 Recojo" if es_recojo else "🏍️ Delivery"
    dir_label = "📦 Entrega" if es_recojo else "📍 Dirección:"
    dir_value = "El cliente retira" if es_recojo else (html.escape(direccion) if direccion else "Sin especificar")
    dir_cls = " sin-dir" if (not es_recojo and not direccion) else ""

    notas = (p.get("notas") or "").strip()
    notas_html = (
        f'<hr class="oc-sep"><div class="oc-section"><div class="oc-sec-title">📝 Notas:</div>'
        f'<div class="oc-notas-val">{html.escape(notas)}</div></div>'
    ) if notas else ""

    mod_badge = '<span class="oc-mod">✏️ Mod</span>' if p.get("modificado") else ""

    items_raw = p.get("items") or ""
    items_list = [i.strip() for i in items_raw.split(",") if i.strip()]
    items_html = "".join(f'<div class="oc-item-line">• {html.escape(i)}</div>' for i in items_list) if len(items_list) > 1 else html.escape(items_raw)

    # ── Botones de acción ───────────────────────────────────────
    if activo:
        btn_cancel = f'<button class="oa oa-cancel" onclick="cancelarPedido({pid})">❌ Cancelar</button>'
        btn_delivery = f'<button class="oa oa-delivery" onclick="llamarDelivery({pid})">🛵 Delivery</button>' if not es_recojo else ""
        btn_cost = ""  # eliminado — la consulta de costo se dispara automáticamente desde el bot
        if es_recojo and siguiente and siguiente == "En camino 🛵":
            sig_js = siguiente.replace("'", "\\'")
            btn_next = f'<button class="oa oa-next recojo-next" onclick="cambiarEstado({pid},\'{sig_js}\')">📦 Listo p/retirar</button>'
        elif siguiente:
            sig_js = siguiente.replace("'", "\\'")
            btn_next = f'<button class="oa oa-next" onclick="cambiarEstado({pid},\'{sig_js}\')">→ {html.escape(siguiente)}</button>'
        else:
            btn_next = ""
    else:
        btn_cancel = btn_delivery = btn_cost = ""
        btn_next = f'<span class="oa-done">{"❌ Cancelado" if es_cancelado else "✅ Entregado"}</span>'

    btn_del = f'<button class="oa oa-del" onclick="eliminarPedido({pid},this)" title="Eliminar">🗑️</button>'

    return f"""<div class="card" id="card-{pid}" data-estado="{html.escape(estado)}" data-recojo="{1 if es_recojo else 0}">
  <div class="oc-hdr">
    <div class="oc-hdr-left">
      <div class="oc-title">Pedido <span class="oc-num">#{pid}</span></div>
      <div class="oc-meta">🕒 {p['hora']} · +{html.escape(p['phone'])} <span class="oc-entrega {entrega_cls}">{entrega_txt}</span>{mod_badge}</div>
    </div>
    <span class="oc-status" style="background:{badge_color}">{html.escape(estado)}</span>
  </div>
  <div class="oc-progress">{steps_html}</div>
  <hr class="oc-sep">
  <div class="oc-section">
    <div class="oc-sec-title">🌶️ Artículos:</div>
    <div class="oc-items">{items_html}</div>
  </div>
  <hr class="oc-sep">
  <div class="oc-section">
    <div class="oc-sec-title">{dir_label}</div>
    <div class="oc-addr{dir_cls}">{dir_value}</div>
  </div>
  {notas_html}
  <hr class="oc-sep">
  <div class="oc-payment">
    <span class="oc-total">{html.escape(p['total'])}</span>
    <div class="oc-pay-badges">
      <span class="oc-pbadge {metodo_cls}">{metodo_icon} {html.escape(metodo)}</span>
      {cobro_badge}
    </div>
  </div>
  <div class="oc-actions">{btn_cancel}{btn_delivery}{btn_cost}{btn_next}{btn_del}</div>
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
    estados_js    = json.dumps(ESTADOS)
    badge_js      = json.dumps(ESTADO_BADGE)
    bg_js         = json.dumps(ESTADO_COLORS)
    step_idx_js   = json.dumps(STEP_IDX)
    step_lbl_js   = json.dumps(STEP_LABELS)
    deliveries_js = json.dumps(DELIVERIES)

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

/* ══ Variables de marca Chilango ══════════════════════════════ */
:root{{
  --ch-red:#D32F2F;       /* rojo vibrante */
  --ch-red-bg:#FFEBEE;
  --ch-yellow:#F9A825;    /* amarillo dorado */
  --ch-yellow-bg:#FFFDE7;
  --ch-green:#2E7D32;     /* verde menta */
  --ch-green-bg:#E8F5E9;
  --ch-purple:#6A1B9A;    /* morado oscuro */
  --ch-purple-bg:#F3E5F5;
  --ch-blue:#1565C0;      /* azul vibrante */
  --ch-blue-bg:#E3F2FD;
  --ch-orange:#E65100;
  --ch-orange-bg:#FFF3E0;
  --ch-text:#333;
  --ch-text2:#555;
  --ch-text3:#888;
  --ch-border:#EEEEEE;
  --ch-shadow:0 3px 14px rgba(0,0,0,.10);
}}

/* ── Tarjeta ── */
.card{{border-radius:16px;background:#fff;box-shadow:var(--ch-shadow);transition:box-shadow .2s,opacity .3s;overflow:hidden;font-family:'Segoe UI',system-ui,sans-serif}}
.card:hover{{box-shadow:0 6px 24px rgba(0,0,0,.14)}}
.card.hidden{{display:none}}

/* Header */
.oc-hdr{{padding:14px 16px 10px;border-bottom:1px solid var(--ch-border);display:flex;align-items:flex-start;justify-content:space-between;gap:10px}}
.oc-hdr-left{{min-width:0}}
.oc-title{{font-size:16px;font-weight:800;color:var(--ch-text);letter-spacing:-.2px;line-height:1.2}}
.oc-num{{color:var(--ch-red)}}
.oc-meta{{font-size:11px;color:var(--ch-text3);margin-top:4px;display:flex;align-items:center;gap:5px;flex-wrap:wrap}}
.oc-status{{font-size:11px;font-weight:800;color:#fff;padding:5px 13px;border-radius:20px;white-space:nowrap;letter-spacing:.3px;flex-shrink:0}}
.oc-entrega{{font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px;white-space:nowrap}}
.oc-entrega.delivery{{background:#E3F2FD;color:var(--ch-blue)}}
.oc-entrega.recojo{{background:var(--ch-purple-bg);color:var(--ch-purple)}}
.oc-mod{{font-size:10px;background:var(--ch-orange-bg);color:var(--ch-orange);padding:2px 7px;border-radius:20px;font-weight:700}}
.nav-badge{{background:#e53935;color:#fff;border-radius:10px;min-width:18px;height:18px;font-size:10px;font-weight:700;display:none;align-items:center;justify-content:center;padding:0 5px;margin-left:5px;vertical-align:middle;line-height:18px}}

/* Progress */
.oc-progress{{display:flex;align-items:center;padding:10px 16px 8px}}
.oc-step{{display:flex;flex-direction:column;align-items:center;gap:4px;min-width:0}}
.oc-step span{{font-size:9px;color:#ccc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:54px;text-align:center}}
.oc-dot{{width:12px;height:12px;border-radius:50%;background:#E0E0E0;transition:background .3s}}
.oc-step.s-done .oc-dot{{background:var(--ch-green)}}
.oc-step.s-active .oc-dot{{background:var(--ch-red);box-shadow:0 0 0 3px rgba(211,47,47,.2)}}
.oc-step.s-done span,.oc-step.s-active span{{color:var(--ch-text);font-weight:700}}
.oc-line{{flex:1;height:2px;background:#E0E0E0;margin:0 3px;margin-bottom:14px}}
.oc-line.done{{background:var(--ch-green)}}

/* Separador punteado */
.oc-sep{{border:none;border-top:1px dashed #E8E8E8;margin:0 16px}}

/* Secciones */
.oc-section{{padding:10px 16px}}
.oc-sec-title{{font-size:11px;font-weight:700;color:var(--ch-text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}}
.oc-items{{font-size:13px;color:var(--ch-text);line-height:1.6;word-break:break-word}}
.oc-item-line{{padding:1px 0}}
.oc-addr{{font-size:13px;color:var(--ch-text)}}
.oc-addr.sin-dir{{color:#ccc;font-style:italic}}
.oc-notas-val{{font-size:12px;color:var(--ch-text2);font-style:italic}}

/* Resumen pago */
.oc-payment{{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;gap:8px;flex-wrap:wrap}}
.oc-total{{font-size:21px;font-weight:900;color:var(--ch-purple);letter-spacing:-.5px}}
.oc-pay-badges{{display:flex;gap:6px;flex-wrap:wrap;align-items:center}}
.oc-pbadge{{font-size:11px;font-weight:700;padding:4px 11px;border-radius:20px;display:inline-flex;align-items:center;gap:4px}}
.oc-pbadge.metodo-efectivo{{background:var(--ch-green-bg);color:var(--ch-green);border:1px solid #C8E6C9}}
.oc-pbadge.metodo-yape{{background:var(--ch-purple-bg);color:var(--ch-purple);border:1px solid #CE93D8}}
.oc-pbadge.cobrar{{background:var(--ch-yellow-bg);color:#F57F17;border:1px solid #FFE082}}
.oc-pbadge.pagado{{background:var(--ch-green-bg);color:var(--ch-green);border:1px solid #C8E6C9}}

/* Barra de acciones */
.oc-actions{{display:flex;border-top:1px solid var(--ch-border)}}
.oa{{flex:1;border:none;padding:12px 6px;font-size:12px;font-weight:700;cursor:pointer;background:#fff;color:var(--ch-text);display:flex;align-items:center;justify-content:center;gap:4px;transition:background .15s,opacity .1s}}
.oa:active{{opacity:.7}}
.oa+.oa{{border-left:1px solid var(--ch-border)}}
.oa-cancel{{color:var(--ch-red)}}
.oa-cancel:hover{{background:var(--ch-red-bg)}}
.oa-delivery{{color:var(--ch-orange)}}
.oa-delivery:hover{{background:var(--ch-orange-bg)}}
.oa-cost{{color:var(--ch-green)}}
.oa-cost:hover{{background:var(--ch-green-bg)}}
.oa-next{{background:var(--ch-blue);color:#fff;border-radius:0 0 16px 0}}
.oa-next:hover{{background:#1976D2}}
.oa-next:disabled{{background:#bbb;cursor:not-allowed}}
.oa-next.recojo-next{{background:var(--ch-purple)}}
.oa-next.recojo-next:hover{{background:#7B1FA2}}
.oa-del{{flex:0 0 44px;color:#ccc}}
.oa-del:hover{{background:var(--ch-red-bg);color:var(--ch-red)}}
.oa-done{{padding:12px 16px;font-size:12px;color:#bbb;font-weight:600}}

/* ── Misc ── */
.empty{{text-align:center;padding:60px 20px;color:#aaa;font-size:15px;grid-column:1/-1}}
.footer-note{{text-align:center;font-size:11px;color:#bbb;padding:12px}}

/* ── Toast ── */
.toast{{position:fixed;bottom:24px;right:24px;background:#2D5016;color:#fff;padding:12px 22px;border-radius:30px;font-size:14px;font-weight:700;box-shadow:0 4px 20px rgba(0,0,0,.3);z-index:200;transform:translateY(80px);opacity:0;transition:all .35s cubic-bezier(.34,1.56,.64,1)}}
.toast.show{{transform:translateY(0);opacity:1}}

/* ── Modal selección delivery ── */
.dlv-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:300;align-items:center;justify-content:center}}
.dlv-box{{background:#fff;border-radius:16px;padding:22px 26px;min-width:280px;box-shadow:0 8px 32px rgba(0,0,0,.22)}}
.dlv-box h3{{font-size:15px;font-weight:700;margin-bottom:14px;color:#2D5016}}
.dlv-option{{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:10px;cursor:pointer;border:2px solid #e0e0e0;margin-bottom:8px;transition:border-color .15s,background .15s}}
.dlv-option:hover{{background:#f5f5f5;border-color:#aaa}}
.dlv-option input[type=radio]{{accent-color:#e65100;width:16px;height:16px;cursor:pointer}}
.dlv-option label{{font-size:14px;font-weight:600;cursor:pointer;flex:1}}
.dlv-btns{{display:flex;gap:8px;margin-top:14px;justify-content:flex-end}}
.dlv-cancel{{background:transparent;border:1px solid #ccc;color:#555;padding:8px 16px;border-radius:20px;cursor:pointer;font-size:13px}}
.dlv-confirm{{background:#e65100;color:#fff;border:none;padding:8px 18px;border-radius:20px;cursor:pointer;font-size:13px;font-weight:700}}
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
  <a href="/admin/clientes">👥 Clientes</a>
</nav>

<div class="toolbar">
  <span class="toolbar-info">📅 <strong id="totalCount">{len(pedidos)}</strong> pedidos &nbsp;·&nbsp; <span id="activosCount">{total_activos}</span> activos</span>
  <select id="fechaSelect" onchange="if(this.value)location.href='/pedidos?fecha='+encodeURIComponent(this.value)" style="border:1px solid #ccc;border-radius:8px;padding:5px 10px;font-size:13px;cursor:pointer">
    {"".join(f'<option value="{f}" {"selected" if f == fecha_sel else ""}>{f}{" (hoy)" if f == hoy else ""}</option>' for f in fechas_disponibles)}
  </select>
  <button class="btn-test" onclick="probarNotif()">🔔 Probar notificación</button>
</div>

<div class="filters">
  <button class="tab active" data-estado="all" onclick="filterCards('all',this)">Todos ({len(pedidos)})</button>
  <button class="tab" data-estado="Nuevo 🆕" onclick="filterCards('Nuevo 🆕',this)" id="tabNuevo">🆕 Nuevos ({count_nuevos})</button>
  <button class="tab" data-estado="En preparación 👨‍🍳" onclick="filterCards('En preparación 👨‍🍳',this)">👨‍🍳 Preparación ({count_prep})</button>
  <button class="tab" data-estado="En camino 🛵" onclick="filterCards('En camino 🛵',this)">🛵 En camino ({count_camino})</button>
  <button class="tab" data-estado="Entregado ✅" onclick="filterCards('Entregado ✅',this)">✅ Entregados ({count_entregado})</button>
  <button class="tab" data-estado="Cancelado ❌" onclick="filterCards('Cancelado ❌',this)">❌ Cancelados ({count_cancel})</button>
</div>

<div class="grid" id="ordersGrid">{cards}</div>
<div class="footer-note" id="lastRefresh">🔄 Actualización automática cada 10 s</div>
<div class="toast" id="toast">🔔 Nuevo pedido llegó</div>

<!-- Modal selección/envío delivery -->
<div class="dlv-overlay" id="dlvModal" onclick="if(event.target===this)closeDlvModal()">
  <div class="dlv-box">
    <h3>🛵 Llamar delivery</h3>
    <input type="hidden" id="dlvOrderId" value="">
    <div id="dlvOpts">
      {"".join(
          f'<div class="dlv-option"><input type="radio" name="dlvChoice" id="dlv{i}" value="{d["phone"]}" {"checked" if i==0 else ""}>'
          f'<label for="dlv{i}">{html.escape(d["name"])}</label></div>'
          for i, d in enumerate(DELIVERIES)
      )}
    </div>
    <div id="dlvManual" style="{'display:none' if DELIVERIES else ''}">
      <p style="font-size:13px;color:#777;margin-bottom:8px">Número WhatsApp del motorizado (sin +):</p>
      <input id="dlvPhoneInput" type="tel" placeholder="ej: 51987654321"
             style="width:100%;border:1px solid #ccc;border-radius:8px;padding:9px 12px;font-size:14px;outline:none"
             onkeydown="if(event.key==='Enter')confirmarDelivery()">
    </div>
    <div class="dlv-btns">
      <button class="dlv-cancel" onclick="closeDlvModal()">Cancelar</button>
      <button class="dlv-confirm" onclick="confirmarDelivery()">📤 Enviar</button>
    </div>
  </div>
</div>

<script>
const ESTADOS    = {estados_js};
const BADGE_CLR  = {badge_js};
const BG_CLR     = {bg_js};
const DELIVERIES = {deliveries_js};
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

/* ── Cancelar pedido (AJAX) ── */
async function cancelarPedido(id) {{
  if (!confirm('¿Cancelar el pedido #' + id + '? Esta acción no se puede deshacer.')) return;
  try {{
    const r = await fetch('/api/pedidos/estado', {{
      method: 'POST',
      credentials: 'same-origin',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{order_id: id, estado: 'Cancelado ❌'}})
    }});
    if (r.status === 401) {{ location.reload(); return; }}
    if (!r.ok) throw new Error(await r.text());
    await refreshOrders();
  }} catch(e) {{
    alert('Error al cancelar: ' + e.message);
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

/* ── Modal delivery (compartido para llamar y consultar costo) ── */
function _openDlvModal(orderId, mode) {{
  document.getElementById('dlvOrderId').value = orderId;
  const modal = document.getElementById('dlvModal');
  modal.dataset.mode = mode;
  modal.querySelector('h3').textContent = mode === 'cost' ? '💰 Consultar costo delivery' : '🛵 Llamar delivery';
  const optsEl = document.getElementById('dlvOpts');
  optsEl.innerHTML = '';
  if (DELIVERIES && DELIVERIES.length > 0) {{
    document.getElementById('dlvManual').style.display = 'none';
    DELIVERIES.forEach((d, i) => {{
      optsEl.innerHTML += `<div class="dlv-option">
        <input type="radio" name="dlvChoice" id="dlv${{i}}" value="${{d.phone}}" ${{i===0?'checked':''}}>
        <label for="dlv${{i}}">${{d.name}}</label>
      </div>`;
    }});
  }} else {{
    document.getElementById('dlvManual').style.display = 'block';
    document.getElementById('dlvPhoneInput').value = '';
  }}
  modal.style.display = 'flex';
  setTimeout(() => {{
    const inp = document.getElementById('dlvPhoneInput');
    if (inp && inp.offsetParent) inp.focus();
  }}, 100);
}}

function llamarDelivery(orderId) {{ _openDlvModal(orderId, 'delivery'); }}
function consultarCostoDelivery(orderId) {{ _openDlvModal(orderId, 'cost'); }}

async function _enviarDelivery(orderId, phone, name, endpoint, toastPrefix) {{
  try {{
    const r = await fetch(endpoint, {{
      method: 'POST',
      credentials: 'same-origin',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{order_id: orderId, delivery_phone: phone}})
    }});
    if (r.status === 401) {{ location.reload(); return; }}
    const data = await r.json();
    if (data.status === 'ok') {{
      showToast(toastPrefix + (data.delivery || name));
    }} else {{
      alert('Error: ' + (data.msg || 'No se pudo enviar'));
    }}
  }} catch(e) {{
    alert('Error: ' + e.message);
  }}
}}

function closeDlvModal() {{
  document.getElementById('dlvModal').style.display = 'none';
}}

function confirmarDelivery() {{
  const orderId  = +document.getElementById('dlvOrderId').value;
  const mode     = document.getElementById('dlvModal').dataset.mode || 'delivery';
  const endpoint = mode === 'cost' ? '/api/pedidos/consultar-delivery' : '/api/pedidos/llamar-delivery';
  const toastPrefix = mode === 'cost' ? '💰 Consulta enviada a ' : '🛵 Solicitud enviada a ';
  let phone, name;
  const manualDiv = document.getElementById('dlvManual');
  if (manualDiv && manualDiv.style.display !== 'none') {{
    phone = (document.getElementById('dlvPhoneInput').value || '').trim().replace(/[^0-9]/g,'');
    if (!phone || phone.length < 8) {{ alert('Ingresa un número de WhatsApp válido (solo dígitos)'); return; }}
    name = 'Delivery';
  }} else {{
    const sel = document.querySelector('input[name="dlvChoice"]:checked');
    if (!sel) {{ alert('Selecciona un servicio de delivery'); return; }}
    const d = DELIVERIES.find(d => d.phone === sel.value);
    phone = d.phone; name = d.name;
  }}
  closeDlvModal();
  _enviarDelivery(orderId, phone, name, endpoint, toastPrefix);
}}

/* ── Construir tarjeta desde JSON ── */
function buildCard(p) {{
  const estado   = p.estado || 'Nuevo 🆕';
  const badgeClr = BADGE_CLR[estado] || '#666';
  const sIdx     = STEP_IDX[estado] !== undefined ? STEP_IDX[estado] : -1;

  // Progress steps
  const steps = STEP_LBL.map((lbl, i) => {{
    const cls  = i < sIdx ? 'oc-step s-done' : (i === sIdx ? 'oc-step s-active' : 'oc-step');
    const line = i < STEP_LBL.length - 1
      ? `<div class="oc-line${{i < sIdx ? ' done' : ''}}"></div>`
      : '';
    return `<div class="${{cls}}"><div class="oc-dot"></div><span>${{lbl}}</span></div>${{line}}`;
  }}).join('');

  const esRecojo  = (p.es_recojo === true || p.es_recojo === 1);
  const es_cancel = estado === 'Cancelado ❌';
  const esActivo  = !['Entregado ✅','Cancelado ❌'].includes(estado);
  const siguiente = p.siguiente_estado || null;

  // Header badges
  const entregaCls = esRecojo ? 'recojo' : 'delivery';
  const entregaTxt = esRecojo ? '🏪 Recojo' : '🏍️ Delivery';
  const modBadge   = p.modificado ? `<span class="oc-mod">✏️ Mod</span>` : '';

  // Items con bullets
  const itemsList = (p.items || '').split(',').map(s => s.trim()).filter(Boolean);
  const itemsHtml = itemsList.length > 1
    ? itemsList.map(i => `<div class="oc-item-line">• ${{esc(i)}}</div>`).join('')
    : esc(p.items || '');

  // Dirección
  let dirValue, dirCls = '';
  if (esRecojo) {{ dirValue = 'El cliente retira'; }}
  else if (p.direccion) {{ dirValue = esc(p.direccion); }}
  else {{ dirValue = 'Sin especificar'; dirCls = ' sin-dir'; }}
  const dirLabel = esRecojo ? '📦 Entrega' : '📍 Dirección:';

  // Notas
  const notasHtml = (p.notas || '').trim()
    ? `<hr class="oc-sep"><div class="oc-section"><div class="oc-sec-title">📝 Notas:</div><div class="oc-notas-val">${{esc(p.notas)}}</div></div>`
    : '';

  // Pago
  const metodo     = p.metodo_pago || 'Efectivo';
  const esDigital  = ['Yape/Plin','Yape','Plin'].includes(metodo);
  const metodoCls  = esDigital ? 'metodo-yape' : 'metodo-efectivo';
  const pagoEmoji  = esDigital ? '💜' : '💵';
  const cobroBadge = esDigital
    ? `<span class="oc-pbadge pagado">✅ Pagado</span>`
    : `<span class="oc-pbadge cobrar">💳 Cobrar al entregar</span>`;

  // Botones de acción
  let btnCancelHtml = '', btnDeliveryHtml = '', btnCostHtml = '', btnSigHtml = '';
  if (esActivo) {{
    btnCancelHtml   = `<button class="oa oa-cancel" onclick="cancelarPedido(${{p.id}})">❌ Cancelar</button>`;
    btnDeliveryHtml = !esRecojo
      ? `<button class="oa oa-delivery" onclick="llamarDelivery(${{p.id}})">🛵 Delivery</button>`
      : '';
    btnCostHtml = ''; // eliminado — la consulta se dispara automáticamente desde el bot
    if (siguiente) {{
      const esSiguienteCamino = p.siguiente_estado_raw === 'En camino 🛵';
      const lblBtn  = (esRecojo && esSiguienteCamino) ? '📦 Listo p/retirar' : `→ ${{esc(siguiente)}}`;
      const clsNext = (esRecojo && esSiguienteCamino) ? 'oa oa-next recojo-next' : 'oa oa-next';
      btnSigHtml = `<button class="${{clsNext}}" data-next="${{esc(p.siguiente_estado_raw || siguiente)}}" onclick="cambiarEstado(${{p.id}},this.dataset.next)">${{lblBtn}}</button>`;
    }}
  }} else {{
    btnSigHtml = es_cancel
      ? `<span class="oa-done">❌ Cancelado</span>`
      : `<span class="oa-done">✅ Entregado</span>`;
  }}
  const btnDelHtml = `<button class="oa oa-del" onclick="eliminarPedido(${{p.id}},this)" title="Eliminar">🗑️</button>`;

  return `<div class="card" id="card-${{p.id}}" data-estado="${{esc(estado)}}" data-recojo="${{esRecojo?1:0}}">
  <div class="oc-hdr">
    <div class="oc-hdr-left">
      <div class="oc-title">Pedido <span class="oc-num">#${{p.id}}</span></div>
      <div class="oc-meta">🕒 ${{esc(p.hora)}} · +${{esc(p.phone)}} <span class="oc-entrega ${{entregaCls}}">${{entregaTxt}}</span>${{modBadge}}</div>
    </div>
    <span class="oc-status" style="background:${{badgeClr}}">${{esc(estado)}}</span>
  </div>
  <div class="oc-progress">${{steps}}</div>
  <hr class="oc-sep">
  <div class="oc-section">
    <div class="oc-sec-title">🌶️ Artículos:</div>
    <div class="oc-items">${{itemsHtml}}</div>
  </div>
  <hr class="oc-sep">
  <div class="oc-section">
    <div class="oc-sec-title">${{dirLabel}}</div>
    <div class="oc-addr${{dirCls}}">${{dirValue}}</div>
  </div>
  ${{notasHtml}}
  <hr class="oc-sep">
  <div class="oc-payment">
    <span class="oc-total">${{esc(p.total)}}</span>
    <div class="oc-pay-badges">
      <span class="oc-pbadge ${{metodoCls}}">${{pagoEmoji}} ${{esc(metodo)}}</span>
      ${{cobroBadge}}
    </div>
  </div>
  <div class="oc-actions">${{btnCancelHtml}}${{btnDeliveryHtml}}${{btnCostHtml}}${{btnSigHtml}}${{btnDelHtml}}</div>
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

    // Actualizar burbuja de pedidos nuevos en el nav
    const nNuevos = pedidos.filter(p => (p.estado||'').startsWith('Nuevo')).length;
    const navBadge = document.getElementById('navBadge');
    if (navBadge) {{
      navBadge.textContent = nNuevos;
      navBadge.style.display = nNuevos > 0 ? 'inline-flex' : 'none';
    }}

    // Actualizar conteos en las pestañas de filtro
    const _tabCounts = {{
      'all': pedidos.length,
      'Nuevo 🆕': nNuevos,
      'En preparación 👨‍🍳': pedidos.filter(p => p.estado === 'En preparación 👨‍🍳').length,
      'En camino 🛵': pedidos.filter(p => p.estado === 'En camino 🛵').length,
      'Entregado ✅': pedidos.filter(p => p.estado === 'Entregado ✅').length,
      'Cancelado ❌': pedidos.filter(p => p.estado === 'Cancelado ❌').length,
    }};
    const _tabLabels = {{
      'all':'Todos', 'Nuevo 🆕':'🆕 Nuevos', 'En preparación 👨‍🍳':'👨‍🍳 Preparación',
      'En camino 🛵':'🛵 En camino', 'Entregado ✅':'✅ Entregados', 'Cancelado ❌':'❌ Cancelados',
    }};
    document.querySelectorAll('.tab[data-estado]').forEach(tab => {{
      const e = tab.dataset.estado;
      if (e in _tabCounts) tab.textContent = `${{_tabLabels[e]}} (${{_tabCounts[e]}})`;
      if (e === curFilter) tab.classList.add('active');
    }});

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

// Inicializar burbuja nav al cargar la página
(function initNavBadge() {{
  const nNuevos = document.querySelectorAll('.card[data-estado^="Nuevo"]').length;
  const nb = document.getElementById('navBadge');
  if (nb) {{
    nb.textContent = nNuevos;
    nb.style.display = nNuevos > 0 ? 'inline-flex' : 'none';
  }}
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


@app.post("/api/pedidos/llamar-delivery")
async def api_llamar_delivery(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """Envía un mensaje WhatsApp al servicio de delivery elegido con los datos del pedido."""
    data = await request.json()
    order_id      = int(data.get("order_id", 0))
    delivery_phone = data.get("delivery_phone", "").strip()

    if not order_id:
        return JSONResponse({"status": "error", "msg": "order_id requerido"}, status_code=400)

    order = db.get_order_by_id(order_id)
    if not order:
        return JSONResponse({"status": "error", "msg": "Pedido no encontrado"}, status_code=404)

    if not DELIVERIES:
        return JSONResponse({"status": "error", "msg": "No hay delivery configurado en Railway (DELIVERY_1_PHONE)"}, status_code=500)

    # Si el frontend envió un número específico, úsalo (y valida que esté en la lista)
    valid_phones = {d["phone"] for d in DELIVERIES}
    target_phone = delivery_phone if delivery_phone in valid_phones else DELIVERIES[0]["phone"]
    target_name  = next((d["name"] for d in DELIVERIES if d["phone"] == target_phone), "Delivery")

    from datetime import datetime, timezone, timedelta
    _PERU_TZ = timezone(timedelta(hours=-5))
    hora = datetime.now(_PERU_TZ).strftime("%d/%m · %I:%M %p")

    mensaje = (
        f"Un motorizado porfavor - Chilango 🛵\n"
        f"Cliente: +{order['phone']}\n"
        f"📍 {order.get('direccion') or 'Sin dirección'}\n"
        f"🕒 {hora}"
    )
    if order.get("notas"):
        mensaje += f"\n📝 {order['notas']}"

    # Normalizar número: quitar "+" para la API Meta
    target_phone_clean = target_phone.replace("+", "").strip()
    token_ok = bool(os.environ.get("META_ACCESS_TOKEN", "").strip() or META_ACCESS_TOKEN)
    pid_ok   = bool(os.environ.get("META_PHONE_NUMBER_ID", "").strip() or META_PHONE_NUMBER_ID)
    print(f"[DELIVERY] Enviando a {target_name} ({target_phone_clean}) | token={token_ok} | pid={pid_ok}")
    ok = await send_whatsapp_message(target_phone_clean, mensaje)
    if not ok:
        return JSONResponse({"status": "error", "msg": f"No se pudo enviar WA a {target_name} ({target_phone_clean}) — revisa logs de Railway"}, status_code=500)
    print(f"[DELIVERY] ✅ Solicitud enviada para pedido #{order_id} → {target_name} ({target_phone_clean})")
    return JSONResponse({"status": "ok", "delivery": target_name})


@app.post("/api/pedidos/consultar-delivery")
async def api_consultar_delivery(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """Envía consulta de costo de delivery al motorizado."""
    data = await request.json()
    order_id       = int(data.get("order_id", 0))
    delivery_phone = data.get("delivery_phone", "").strip()

    if not order_id:
        return JSONResponse({"status": "error", "msg": "order_id requerido"}, status_code=400)

    order = db.get_order_by_id(order_id)
    if not order:
        return JSONResponse({"status": "error", "msg": "Pedido no encontrado"}, status_code=404)

    if not DELIVERIES:
        return JSONResponse({"status": "error", "msg": "No hay delivery configurado en Railway"}, status_code=500)

    valid_phones = {d["phone"] for d in DELIVERIES}
    target_phone = delivery_phone if delivery_phone in valid_phones else DELIVERIES[0]["phone"]
    target_name  = next((d["name"] for d in DELIVERIES if d["phone"] == target_phone), "Delivery")

    from datetime import datetime, timezone, timedelta
    _PERU_TZ = timezone(timedelta(hours=-5))
    hora = datetime.now(_PERU_TZ).strftime("%d/%m · %I:%M %p")

    consulta = (
        f"¿Cual es el costo a la siguiente dirección?\n"
        f"Dirección: {order.get('direccion') or 'Sin dirección'}"
    )

    await send_whatsapp_message(target_phone, consulta)
    print(f"[COSTO DELIVERY] Consulta enviada para pedido #{order_id} → {target_name} ({target_phone})")
    return JSONResponse({"status": "ok", "delivery": target_name})


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


@app.get("/admin/clientes", response_class=HTMLResponse)
async def admin_clientes(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    clientes = db.get_customers_with_stats()

    filas = ""
    for i, c in enumerate(clientes, 1):
        nombre    = html.escape(c.get("nombre") or "—")
        phone     = html.escape(c.get("phone") or "")
        ultima_dir= html.escape(c.get("ultima_dir") or "—")
        puntos    = int(c.get("puntos") or 0)
        pedidos   = int(c.get("total_pedidos") or 0)
        gastado   = float(c.get("total_gastado") or 0)
        updated   = (c.get("updated_at") or "—")[:16]
        medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else f"{i}"))
        filas += f"""<tr>
          <td class="cl-rank">{medal}</td>
          <td class="cl-phone"><a href="https://wa.me/{phone}" target="_blank">+{phone}</a></td>
          <td>{nombre}</td>
          <td class="cl-num">{pedidos}</td>
          <td class="cl-num cl-total">S/ {gastado:.2f}</td>
          <td class="cl-pts">
            <input class="pts-input" type="number" min="0" value="{puntos}"
              onchange="guardarPuntos('{phone}', this)"
              onkeydown="if(event.key==='Enter')this.blur()">
          </td>
          <td class="cl-date">{updated}</td>
        </tr>"""

    total_clientes = len(clientes)
    total_gastado_global = sum(c.get("total_gastado") or 0 for c in clientes)

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<title>Clientes — Chilango</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;min-height:100vh}}
.hdr{{background:#2D5016;color:white;padding:10px 18px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:50;box-shadow:0 2px 8px rgba(0,0,0,.25)}}
.hdr-title h1{{font-size:16px;font-weight:700}}
.hdr-title small{{font-size:11px;opacity:.7}}
.nav{{background:#1b3a0e;display:flex}}
.nav a{{color:rgba(255,255,255,.7);text-decoration:none;padding:9px 18px;font-size:13px;transition:background .15s}}
.nav a:hover,.nav a.active{{color:#fff;background:rgba(255,255,255,.12)}}
.wrap{{max-width:1100px;margin:0 auto;padding:20px 16px}}
.stats-bar{{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}}
.stat-chip{{background:white;border-radius:12px;padding:12px 20px;box-shadow:0 1px 4px rgba(0,0,0,.08);text-align:center}}
.stat-chip .val{{font-size:22px;font-weight:700;color:#2D5016}}
.stat-chip .lbl{{font-size:11px;color:#888;margin-top:2px}}
.search-bar{{margin-bottom:14px}}
.search-bar input{{width:100%;max-width:360px;border:1px solid #ddd;border-radius:10px;padding:9px 14px;font-size:14px;outline:none}}
.search-bar input:focus{{border-color:#2D5016}}
.tbl-wrap{{background:white;border-radius:14px;box-shadow:0 1px 4px rgba(0,0,0,.08);overflow:hidden}}
table{{width:100%;border-collapse:collapse}}
thead th{{background:#2D5016;color:white;padding:11px 14px;text-align:left;font-size:12px;font-weight:600;white-space:nowrap}}
tbody tr{{border-bottom:1px solid #f0f0f0;transition:background .1s}}
tbody tr:hover{{background:#f8fdf5}}
tbody tr:last-child{{border-bottom:none}}
td{{padding:10px 14px;font-size:13px;color:#333}}
.cl-rank{{font-size:16px;text-align:center;width:40px}}
.cl-phone a{{color:#2D5016;text-decoration:none;font-weight:600}}
.cl-phone a:hover{{text-decoration:underline}}
.cl-num{{text-align:right;font-variant-numeric:tabular-nums}}
.cl-total{{color:#2D5016;font-weight:700}}
.cl-date{{color:#999;font-size:12px}}
.pts-input{{width:72px;border:1px solid #ddd;border-radius:8px;padding:5px 8px;font-size:13px;text-align:center;outline:none;transition:border-color .15s}}
.pts-input:focus{{border-color:#2D5016}}
.pts-input.saved{{border-color:#4caf50;background:#f0fff4}}
.pts-input.saving{{border-color:#ff9800}}
.empty{{text-align:center;padding:48px;color:#aaa;font-size:15px}}
.toast{{position:fixed;bottom:24px;right:24px;background:#2D5016;color:white;padding:10px 20px;border-radius:10px;font-size:13px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:999}}
.toast.show{{opacity:1}}
</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-title">
    <h1>🌮 Chilango Bot</h1>
    <small>Panel de administración</small>
  </div>
</div>
<nav class="nav">
  <a href="/pedidos">📦 Pedidos</a>
  <a href="/admin">💬 Conversaciones</a>
  <a href="/admin/clientes" class="active">👥 Clientes</a>
</nav>
<div class="wrap">
  <div class="stats-bar">
    <div class="stat-chip"><div class="val">{total_clientes}</div><div class="lbl">Clientes registrados</div></div>
    <div class="stat-chip"><div class="val">S/ {total_gastado_global:.2f}</div><div class="lbl">Facturación total</div></div>
    <div class="stat-chip"><div class="val">S/ {(total_gastado_global/total_clientes if total_clientes else 0):.2f}</div><div class="lbl">Ticket promedio</div></div>
  </div>
  <div class="search-bar">
    <input type="text" id="buscar" placeholder="🔍 Buscar por teléfono o nombre..." oninput="filtrar(this.value)">
  </div>
  <div class="tbl-wrap">
    {"<div class='empty'>Aún no hay clientes registrados 🌮</div>" if not clientes else f"""
    <table id="tablaClientes">
      <thead>
        <tr>
          <th>#</th>
          <th>Teléfono</th>
          <th>Nombre</th>
          <th style="text-align:right">Pedidos</th>
          <th style="text-align:right">Total gastado</th>
          <th style="text-align:center">Puntos 🌟</th>
          <th>Última actividad</th>
        </tr>
      </thead>
      <tbody id="tbody">{filas}</tbody>
    </table>"""}
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
function showToast(msg) {{
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2200);
}}

async function guardarPuntos(phone, input) {{
  input.classList.add('saving');
  try {{
    const r = await fetch('/api/clientes/puntos', {{
      method: 'POST',
      credentials: 'same-origin',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{phone, puntos: parseInt(input.value) || 0}})
    }});
    const d = await r.json();
    if (d.status === 'ok') {{
      input.classList.remove('saving');
      input.classList.add('saved');
      setTimeout(() => input.classList.remove('saved'), 1500);
      showToast('✅ Puntos actualizados');
    }} else {{
      alert('Error: ' + (d.msg || 'No se pudo guardar'));
    }}
  }} catch(e) {{ alert('Error: ' + e.message); }}
}}

function filtrar(q) {{
  q = q.toLowerCase();
  document.querySelectorAll('#tbody tr').forEach(tr => {{
    const txt = tr.textContent.toLowerCase();
    tr.style.display = txt.includes(q) ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""


@app.post("/api/clientes/puntos")
async def api_actualizar_puntos(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    data  = await request.json()
    phone = data.get("phone", "").strip()
    puntos = int(data.get("puntos", 0))
    if not phone:
        return JSONResponse({"status": "error", "msg": "phone requerido"}, status_code=400)
    db.update_customer_points(phone, puntos)
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
        <a href="/admin/clientes" style="color:rgba(255,255,255,.7);text-decoration:none;padding:10px 20px;font-size:14px;">👥 Clientes</a>
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
            const label = isManual ? '👨‍💼 Equipo' : (m.role === 'user' ? 'Cliente' : '🤖 Chili');
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

        // Burbuja de nuevos pedidos en el nav (sin depender de localStorage)
        async function checkPedidosNuevos() {{
            try {{
                const r = await fetch('/api/pedidos', {{credentials:'same-origin'}});
                if (!r.ok) return;
                const data = await r.json();
                const n = data.pedidos.filter(p => (p.estado||'').startsWith('Nuevo')).length;
                const badge = document.getElementById('adminNavBadge');
                if (badge) {{
                    badge.textContent = n;
                    badge.style.display = n > 0 ? 'inline-flex' : 'none';
                }}
            }} catch(e) {{}}
        }}
        checkPedidosNuevos();
        setInterval(checkPedidosNuevos, 10000);
    </script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
