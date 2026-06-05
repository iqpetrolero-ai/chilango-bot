import os
import html
import json
import time
import httpx
from collections import OrderedDict
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, Query, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bot import (process_message, process_message_with_image, reset_conversation,
                 mensaje_bienvenida, esta_en_horario, mensaje_fuera_horario)
from orders import get_orders_count
from menu import MENU_TEXTO
import db

app = FastAPI(title="Chilango Bot 🌮")


@app.on_event("startup")
async def _start_reminder_task():
    """Tarea en background: recuerda a clientes que tienen un pedido pendiente de confirmar."""
    import asyncio as _asyncio

    async def _reminder_loop():
        await _asyncio.sleep(60)  # Esperar 1 min al arrancar antes del primer chequeo
        while True:
            try:
                if not esta_en_horario():
                    await _asyncio.sleep(300)
                    continue
                pendientes = db.get_pending_reminders(minutos=5, cooldown_min=30)
                for p in pendientes:
                    phone = p["phone"]
                    # Enviar recordatorio por WhatsApp
                    recordatorio = (
                        "¡Hola! 😊 Solo para recordarte que quedamos en confirmar tu pedido.\n\n"
                        "¿Lo confirmamos o quieres hacer algún cambio? 🌮"
                    )
                    await send_whatsapp_message(phone, recordatorio)
                    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                    _ts = _dt.now(_tz(_td(hours=-5))).strftime("%H:%M")
                    db.append_message(phone, "assistant", recordatorio, ts=_ts)
                    db.mark_reminder_sent(phone)
                    print(f"[RECORDATORIO] Enviado a {phone}")
            except Exception as _e:
                print(f"[RECORDATORIO] Error: {_e}")
            await _asyncio.sleep(300)  # Chequear cada 5 minutos

    _asyncio.create_task(_reminder_loop())

    async def _survey_loop():
        await _asyncio.sleep(120)
        while True:
            try:
                pedidos = db.get_orders_for_survey(minutes=60)
                for p in pedidos:
                    encuesta = (
                        "¡Hola! 😊 Esperamos que hayas disfrutado tu pedido de Chilango.\n\n"
                        "¿Cómo estuvo la experiencia? Tu opinión nos ayuda a mejorar 🙏\n\n"
                        "⭐ Del 1 al 5, ¿qué nota le das?"
                    )
                    await send_whatsapp_message(p["phone"], encuesta)
                    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                    _ts = _dt.now(_tz(_td(hours=-5))).strftime("%H:%M")
                    db.append_message(p["phone"], "assistant", encuesta, ts=_ts)
                    db.mark_survey_sent(p["id"])
                    print(f"[ENCUESTA] Enviada a {p['phone']}")
            except Exception as _e:
                print(f"[ENCUESTA] Error: {_e}")
            await _asyncio.sleep(600)  # Chequear cada 10 minutos

    _asyncio.create_task(_survey_loop())

    async def _carta_followup_loop():
        await _asyncio.sleep(90)
        while True:
            try:
                # Solo enviar follow-ups en horario de atención
                if esta_en_horario():
                    pendientes = db.get_pending_carta_followups(minutos=15)
                    for p in pendientes:
                        followup = (
                            "¡Hola! 😊 ¿Pudiste ver nuestra carta? 🌮\n\n"
                            "Si te animaste con algo o tienes alguna duda, aquí estamos. "
                            "¿Le entramos con un pedido?"
                        )
                        await send_whatsapp_message(p["phone"], followup)
                        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                        _ts = _dt.now(_tz(_td(hours=-5))).strftime("%H:%M")
                        db.append_message(p["phone"], "assistant", followup, ts=_ts)
                        db.mark_carta_followup_sent(p["phone"])
                        print(f"[CARTA FOLLOWUP] Enviado a {p['phone']}")
            except Exception as _e:
                print(f"[CARTA FOLLOWUP] Error: {_e}")
            await _asyncio.sleep(300)  # Chequear cada 5 minutos

    _asyncio.create_task(_carta_followup_loop())

import os as _os
if _os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
if not ADMIN_PASSWORD:
    raise RuntimeError("La variable de entorno ADMIN_PASSWORD no está configurada")

security = HTTPBasic()


def _format_contact_time(iso_str: str) -> str:
    """Formatea last_msg_at al estilo WhatsApp: HH:MM hoy, Ayer, día semana, o DD/MM."""
    if not iso_str:
        return ""
    try:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _PERU = _tz(_td(hours=-5))
        ts = _dt.fromisoformat(iso_str).astimezone(_PERU)
        now = _dt.now(_PERU)
        delta = (now.date() - ts.date()).days
        if delta == 0:
            return ts.strftime("%I:%M %p").lstrip("0")
        if delta == 1:
            return "Ayer"
        if delta < 7:
            dias = ["lun.", "mar.", "mié.", "jue.", "vie.", "sáb.", "dom."]
            return dias[ts.weekday()]
        return ts.strftime("%d/%m")
    except Exception:
        return ""


def verificar_admin(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(credentials.username.encode(), b"admin")
    ok_pass = secrets.compare_digest(credentials.password.encode(), ADMIN_PASSWORD.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, detail="Acceso no autorizado",
                            headers={"WWW-Authenticate": "Basic"})


META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "").strip()
META_PHONE_NUMBER_ID = os.environ.get("META_PHONE_NUMBER_ID", "").strip()
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "").strip()
DELIVERY_SERVICE_PHONE = os.environ.get("DELIVERY_SERVICE_PHONE", "525513781963").strip()
OWNER_PHONE = os.environ.get("OWNER_PHONE", "51954713696").strip()
BASE_URL = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
PDF_URL = f"https://{BASE_URL}/static/carta.pdf" if BASE_URL else ""
# ── Servicios de delivery (hasta 4 motorizados) ──────────────
# Variables: DELIVERY_1_PHONE / DELIVERY_1_NAME ... DELIVERY_4_PHONE / DELIVERY_4_NAME
# Backward compat: DELIVERY_PHONE → DELIVERY_1_PHONE
_RAW_DELIVERIES = [
    {
        "phone": (os.environ.get(f"DELIVERY_{i}_PHONE") or (os.environ.get("DELIVERY_PHONE","") if i==1 else "")).strip(),
        "name":  os.environ.get(f"DELIVERY_{i}_NAME", f"Motorizado {i}").strip(),
    }
    for i in range(1, 5)
]
DELIVERIES = [d for d in _RAW_DELIVERIES if d["phone"]]
DELIVERY_PHONE = DELIVERIES[0]["phone"] if DELIVERIES else ""  # backward compat para código existente
# Mapa rápido phone_clean → nombre para mostrar en panel
DELIVERY_NAME_MAP: dict[str, str] = {
    d["phone"].replace("+", "").strip(): d["name"]
    for d in DELIVERIES if d["phone"]
}

PALABRAS_CARTA = ["carta", "menu", "menú", "ver carta", "ver menu", "qué tienen", "que tienen"]

# Saludos genéricos que no necesitan procesarse después de la bienvenida
SALUDOS_GENERICOS = {"hola", "buenas", "buenos días", "buenas tardes", "buenas noches",
                     "hi", "hello", "hey", "ola", "buenas noches", "2"}

# ── Deduplicación de webhooks ─────────────────────────────────
# Meta reenvía el mismo mensaje si no recibe respuesta rápida.
# Usamos OrderedDict como set ordenado: popitem(last=False) elimina el más antiguo.
_processed_msg_ids: OrderedDict = OrderedDict()

# ── Rate limiting por número de teléfono ──────────────────────
# Máx 15 mensajes por minuto por número para proteger la API de Claude.
_rate_limit: dict[str, list] = {}


def _check_rate_limit(phone: str, max_msgs: int = 15, window_secs: int = 60) -> bool:
    """Retorna True si el teléfono superó el límite de mensajes."""
    now = time.time()
    timestamps = _rate_limit.get(phone, [])
    timestamps = [t for t in timestamps if now - t < window_secs]
    if len(timestamps) >= max_msgs:
        return True
    timestamps.append(now)
    _rate_limit[phone] = timestamps
    return False


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
    token = os.environ.get("META_ACCESS_TOKEN", "").strip() or META_ACCESS_TOKEN
    pid = phone_number_id or os.environ.get("META_PHONE_NUMBER_ID", "").strip() or META_PHONE_NUMBER_ID
    url = f"https://graph.facebook.com/v19.0/{pid}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
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
    async with httpx.AsyncClient(timeout=10) as client:
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
    """Extrae el monto numérico de la respuesta del motorizado.
    Maneja: '7', 'S/7', '7 soles', 'el costo es 8 soles', 'son 10 para esa zona', etc.
    Retorna None si no encuentra un número razonable (1–300 soles).
    """
    import re
    t = text.strip().lower()

    # 1. Patrón S/ XX.XX (más confiable)
    m = re.search(r's\s*/\s*(\d+(?:[.,]\d{1,2})?)', t)
    if m:
        return float(m.group(1).replace(',', '.'))

    # 2. Patrón "XX soles" o "XX sol"
    m = re.search(r'(\d+(?:[.,]\d{1,2})?)\s*sol', t)
    if m:
        return float(m.group(1).replace(',', '.'))

    # 3. Cualquier número entre 1 y 300 (rango razonable de delivery en Tacna)
    numeros = re.findall(r'\b(\d+(?:[.,]\d{1,2})?)\b', t)
    for n in numeros:
        val = float(n.replace(',', '.'))
        if 1 <= val <= 300:
            return val

    return None


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

    # Rate limiting: máx 15 mensajes/minuto por número (excluye números de delivery)
    delivery_phones = {d["phone"].replace("+", "") for d in DELIVERIES}
    if phone_clean not in delivery_phones and _check_rate_limit(phone_clean):
        print(f"[RATE LIMIT] {phone_clean} excedió 15 msg/min — ignorado")
        return

    # ── Mensajes de números de delivery ─────────────────────────────────────────
    if phone_clean in delivery_phones:
        delivery_name = next(
            (d["name"] for d in DELIVERIES if d["phone"].replace("+", "") == phone_clean),
            "Motorizado"
        )
        print(f"[DELIVERY MSG] {delivery_name} ({phone_clean}): {message}")

        # Si hay consulta pendiente Y el mensaje parece un costo → procesar como respuesta de costo
        consulta = db.get_pending_delivery_query(phone_clean)
        if consulta:
            costo_delivery = _parse_delivery_cost(message)
            if costo_delivery is not None:
                subtotal_num = _parse_amount(consulta.get("subtotal", "0"))
                total_num    = subtotal_num + costo_delivery
                items_txt    = consulta.get("items", "")
                pago_txt     = consulta.get("pago", "")
                client_phone = consulta["client_phone"]

                # Verificar que el cliente no tenga ya un pedido confirmado hoy
                # (evita enviar el costo si el cliente ya eligió contra entrega u otro método)
                from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                _hoy_str = _dt.now(_tz(_td(hours=-5))).strftime("%d/%m/%Y")
                _pedidos_confirmados = [
                    p for p in db.get_orders_for_date(_hoy_str)
                    if p.get("phone") == client_phone and p.get("estado") not in ("Cancelado ❌",)
                ]
                if _pedidos_confirmados:
                    db.delete_delivery_query(consulta["id"])
                    print(f"[DELIVERY COST] ⚠️ Pedido ya confirmado para +{client_phone} — costo ignorado")
                    return

                msg_cliente = (
                    f"¡Ya tenemos el costo! 🛵\n\n"
                    f"🛒 {items_txt}\n"
                    f"📦 Empaque incluido\n"
                    f"🛵 Delivery: S/ {costo_delivery:.2f}\n"
                    f"💰 *Total completo: S/ {total_num:.2f}*\n\n"
                    f"¿Confirmamos tu pedido con {pago_txt}? 😊"
                )
                _ts = _dt.now(_tz(_td(hours=-5))).strftime("%H:%M")
                db.append_message(client_phone, "assistant", msg_cliente, ts=_ts)
                db.mark_unread(client_phone)
                await send_whatsapp_message(client_phone, msg_cliente, sending_id)
                db.delete_delivery_query(consulta["id"])
                # Cancelar consultas de otros motorizados para el mismo cliente (broadcast)
                for _d in DELIVERIES:
                    _op = db.get_pending_delivery_query(_d["phone"].replace("+",""))
                    if _op and _op.get("client_phone") == client_phone and _op["id"] != consulta["id"]:
                        db.delete_delivery_query(_op["id"])
                print(f"[DELIVERY COST] S/{costo_delivery} enviado a cliente +{client_phone} — total S/{total_num:.2f}")
                return  # procesado como costo — no continuar

            else:
                # Hay consulta pendiente pero no se pudo extraer un número
                # → avisar al dueño para gestión manual y notificar al cliente
                client_phone = consulta["client_phone"]
                aviso_dueño = (
                    f"⚠️ *{delivery_name}* respondió a la consulta de costo "
                    f"pero no se detectó un monto:\n\n\"{message}\"\n\n"
                    f"Cliente: +{client_phone} — gestiona manualmente."
                )
                await send_whatsapp_message("51954713696", aviso_dueño, sending_id)
                msg_cliente = (
                    f"🙏 Estamos confirmando el costo de delivery con el motorizado, "
                    f"en un momento te avisamos. ¡Gracias por tu paciencia!"
                )
                from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                _ts = _dt.now(_tz(_td(hours=-5))).strftime("%H:%M")
                db.append_message(client_phone, "assistant", msg_cliente, ts=_ts)
                db.mark_unread(client_phone)
                await send_whatsapp_message(client_phone, msg_cliente, sending_id)
                print(f"[DELIVERY COST] No se pudo parsear costo de {delivery_name}: '{message}'")
                return

        # Sin consulta pendiente → detectar si es confirmación de asignación de delivery
        # Palabras y frases que indican que el motorizado aceptó el viaje
        _CONFIRM_WORDS = {
            "ok", "okay", "okey", "dale", "si", "sí", "listo", "ya", "bueno",
            "voy", "entendido", "recibido", "confirmado", "vamos", "claro",
        }
        _CONFIRM_PHRASES = [
            "ya voy", "en camino", "ya salgo", "ahí voy", "para alla",
            "para allá", "saliendo", "voy para", "ya estoy",
            "en ruta", "ya sali", "ya salí",
        ]
        _msg_norm = msg_lower.strip().rstrip("!.¡ ")
        _es_confirmacion = (
            _msg_norm in _CONFIRM_WORDS
            or any(frase in msg_lower for frase in _CONFIRM_PHRASES)
        )
        if _es_confirmacion:
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            _ts = _dt.now(_tz(_td(hours=-5))).strftime("%H:%M")
            # Guardar en panel de conversaciones como no leído (aparece con punto rojo)
            db.append_message(phone_clean, "user", f"✅ {message}", ts=_ts)
            db.mark_unread(phone_clean)
            print(f"[DELIVERY CONFIRM] {delivery_name} confirmó asignación: '{message}' → visible en panel")
            return  # Confirmación procesada — no tratar como cliente

        # Ninguna coincidencia → tratar como cliente normal (puede estar pidiendo comida)
        print(f"[DELIVERY MSG] {delivery_name} tratado como cliente para este mensaje")

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
        if not esta_en_horario():
            # Fuera de horario: no enviamos botones de pedido, solo el aviso
            reply = mensaje_fuera_horario()
            await send_whatsapp_message(phone, reply, sending_id)
            db.append_message(phone, "user", message)
            db.append_message(phone, "assistant", reply)
            db.mark_unread(phone)
            return
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
            if reply:
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
        if reply:
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
            _processed_msg_ids[msg_id] = True
            if len(_processed_msg_ids) > 500:
                _processed_msg_ids.popitem(last=False)  # elimina el más antiguo

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
                if reply:
                    await send_whatsapp_message(phone, reply, phone_number_id)
            else:
                await send_whatsapp_message(phone, "No pude leer la imagen, ¿puedes enviarla de nuevo? 📸", phone_number_id)
        elif msg_type == "interactive":
            # Respuesta a botones interactivos
            btn = message_data.get("interactive", {}).get("button_reply", {})
            btn_id    = btn.get("id", "")
            btn_title = btn.get("title", "")
            phone_clean = phone.replace("whatsapp:", "").replace("+", "")
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
                db.mark_escalated(phone_clean)   # ← bot en silencio hasta que el equipo libere
                await send_whatsapp_message(phone, respuesta, phone_number_id)
                print(f"[ESCALATE] {phone} solicitó hablar con el equipo — bot pausado para esta conv")
            elif btn_id == "equipo_no":
                respuesta = "Entendido. Cualquier cosa, aquí estamos 🌮"
                db.append_message(phone, "user",      "No, gracias", ts=now_ts)
                db.append_message(phone, "assistant", respuesta, ts=now_ts)
                await send_whatsapp_message(phone, respuesta, phone_number_id)
            else:
                # Botón desconocido: tratar como texto normal
                await handle_message(phone, btn_title or btn_id, phone_number_id)
        elif msg_type == "location":
            loc  = message_data.get("location", {})
            lat  = loc.get("latitude", "")
            lng  = loc.get("longitude", "")
            name = loc.get("name", "")
            addr = loc.get("address", "")
            maps_url  = f"https://maps.google.com/?q={lat},{lng}"
            # Texto legible para el panel y para Claude
            loc_parts = ["📍 Ubicación compartida por GPS"]
            if name:
                loc_parts.append(f"Lugar: {name}")
            if addr:
                loc_parts.append(f"Referencia: {addr}")
            loc_parts.append(f"Ver en mapa: {maps_url}")
            loc_text = "\n".join(loc_parts)
            # Procesar como si fuera un mensaje de texto (Claude entiende la dirección)
            await handle_message(phone, loc_text, phone_number_id)
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
        <p><small>Horario: Vie · Sáb · Dom · 5:30pm – 11pm</small></p>
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


async def _notify_order_listo(order: dict):
    """Envía WhatsApp al cliente avisando que el pedido está listo esperando al delivery."""
    if not order:
        return
    phone = order["phone"]
    mensaje = (
        "🎉 *¡Tu pedido está listo!*\n\n"
        f"🛒 {order['items']}\n"
        f"💰 {order['total']}\n\n"
        "🛵 Estamos esperando al motorizado — en breve saldrá a tu dirección.\n"
        "¡Gracias por tu paciencia! 🌮"
    )
    await send_whatsapp_message(phone, mensaje)


async def _briefing_motorista(order: dict):
    """Avisa al equipo (dueño) qué decirle al motorizado de Altoke sobre el cobro al entregar.
    Altoke no acepta datos del pedido — el equipo se los da en persona cuando llega el moto."""
    if not order:
        return
    metodo = order.get("metodo_pago") or "Efectivo"
    es_digital = metodo in ("Yape/Plin", "Yape", "Plin")
    tiene_delivery_pago = "delivery:" in (order.get("items") or "").lower()
    if es_digital and tiene_delivery_pago:
        cobro_txt = "✅ Ya pagó TODO (comida + delivery) en digital — dile al moto que NO cobre nada."
    elif es_digital:
        cobro_txt = "💜 Pagó la comida en digital — dile al moto que cobre SOLO el delivery al cliente."
    else:
        cobro_txt = f"💵 Paga en EFECTIVO — dile al moto que cobre {order['total']} + delivery al cliente."
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    hora = _dt.now(_tz(_td(hours=-5))).strftime("%d/%m · %I:%M %p")
    msg = (
        f"🛵 *Pedido #{order['id']} salió — Chilango*\n"
        f"📍 {order.get('direccion') or 'Sin dirección'}\n"
        f"💳 {cobro_txt}\n"
        f"🕒 {hora}"
    )
    await send_whatsapp_message(OWNER_PHONE, msg)


async def _notify_order_camino(order: dict):
    """Envía WhatsApp al cliente cuando su pedido pasa a 'En camino'."""
    if not order:
        return
    phone = order["phone"]
    # Marcar timestamp para encuesta post-entrega
    if order.get("id"):
        db.mark_order_camino(order["id"])
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
        metodo = order.get("metodo_pago") or "Efectivo"
        es_digital = metodo in ("Yape/Plin", "Yape", "Plin")
        tiene_delivery_pago = "delivery:" in (order.get("items") or "").lower()
        if es_digital and tiene_delivery_pago:
            aviso_pago = (
                "\n\n💜 *Ya pagaste en digital (incluyendo el delivery).*\n"
                "El motorizado *no cobrará nada adicional* al entregar. ✅"
            )
        elif es_digital:
            aviso_pago = (
                "\n\n💜 *Pago confirmado por digital.*\n"
                "Solo el delivery se cobra al entregar si no lo incluiste antes."
            )
        else:
            aviso_pago = ""
        mensaje = (
            "🛵 *¡Tu pedido está en camino!*\n\n"
            f"🛒 {order['items']}\n"
            f"💰 {order['total']}"
            f"{aviso_pago}\n\n"
            "¡Gracias por elegir Chilango! 🌮"
        )
    await send_whatsapp_message(phone, mensaje)
    # Briefing al motorizado (si es delivery)
    if not es_recojo:
        await _briefing_motorista(order)


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

    # Badge cuando el cliente pagó el delivery incluido en el pedido
    tiene_delivery_pago = "delivery:" in items_raw.lower()
    delivery_badge = '<span class="oc-pbadge delivery-inc">🛵 Delivery pagado</span>' if tiene_delivery_pago else ""

    # Protocolo de cobro para el motorizado de Altoke (visible en "En preparación")
    if estado == "En preparación 👨‍🍳" and not es_recojo:
        if es_digital and tiene_delivery_pago:
            cobro_moto = "✅ Ya pagó TODO — dile al moto que NO cobre nada al cliente"
            cobro_color = "#e8f5e9"; cobro_border = "#a5d6a7"; cobro_txt_color = "#2e7d32"
        elif es_digital:
            cobro_moto = "💜 Pagó en digital — el moto cobra SOLO el delivery al cliente"
            cobro_color = "#f3e5f5"; cobro_border = "#ce93d8"; cobro_txt_color = "#6a1b9a"
        else:
            cobro_moto = f"💵 Pago en efectivo — el moto cobra {html.escape(p['total'])} + delivery"
            cobro_color = "#fff8e1"; cobro_border = "#ffe082"; cobro_txt_color = "#e65100"
        altoke_banner = (
            f'<div style="background:{cobro_color};border:1px solid {cobro_border};border-radius:8px;'
            f'padding:7px 12px;margin:8px 12px 0;font-size:12px;font-weight:700;color:{cobro_txt_color}">'
            f'⚡ Dile al moto: {cobro_moto}</div>'
        )
    else:
        altoke_banner = ""

    # ── Botones de acción ───────────────────────────────────────
    if activo:
        btn_cancel = f'<button class="oa oa-cancel" onclick="cancelarPedido({pid})">❌ Cancelar</button>'
        btn_delivery = f'<button class="oa oa-delivery" onclick="llamarDelivery({pid})">🛵 Delivery</button>' if not es_recojo else ""
        btn_cost = ""  # eliminado — la consulta de costo se dispara automáticamente desde el bot
        btn_listo = (
            f'<button class="oa oa-listo" onclick="avisarListo({pid})">📦 Avisar listo</button>'
            if estado == "En preparación 👨‍🍳" and not es_recojo else ""
        )
        if es_recojo and siguiente and siguiente == "En camino 🛵":
            sig_js = siguiente.replace("'", "\\'")
            btn_next = f'<button class="oa oa-next recojo-next" onclick="cambiarEstado({pid},\'{sig_js}\')">📦 Listo p/retirar</button>'
        elif siguiente:
            sig_js = siguiente.replace("'", "\\'")
            btn_next = f'<button class="oa oa-next" onclick="cambiarEstado({pid},\'{sig_js}\')">→ {html.escape(siguiente)}</button>'
        else:
            btn_next = ""
    else:
        btn_cancel = btn_delivery = btn_cost = btn_listo = ""
        btn_next = f'<span class="oa-done">{"❌ Cancelado" if es_cancelado else "✅ Entregado"}</span>'

    btn_del   = f'<button class="oa oa-del" onclick="eliminarPedido({pid},this)" title="Eliminar">🗑️</button>'
    btn_print = f'<button class="oa oa-print" onclick="window.open(\'/admin/imprimir/{pid}\',\'_blank\')" title="Imprimir recibo">🖨️</button>'

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
      {delivery_badge}
    </div>
  </div>
  {altoke_banner}
  <div class="oc-actions">{btn_cancel}{btn_delivery}{btn_listo}{btn_cost}{btn_next}{btn_print}{btn_del}</div>
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

    # Determinar qué fecha mostrar
    fecha_sel = fecha if fecha else hoy

    # Cargar pedidos según la fecha seleccionada
    if fecha_sel == hoy:
        pedidos = db.get_orders_today()
    else:
        pedidos = db.get_orders_for_date(fecha_sel)

    # Fechas disponibles para el selector (siempre incluye hoy aunque no tenga pedidos)
    fechas_raw = db.get_available_dates()
    fechas_disponibles = fechas_raw if hoy in fechas_raw else [hoy] + fechas_raw

    def _cnt(e): return sum(1 for p in pedidos if (p.get("estado") or "Nuevo 🆕") == e)
    count_nuevos   = _cnt("Nuevo 🆕")
    count_prep     = _cnt("En preparación 👨‍🍳")
    count_camino   = _cnt("En camino 🛵")
    count_entregado = _cnt("Entregado ✅")
    count_cancel   = _cnt("Cancelado ❌")
    total_activos  = len(pedidos) - count_entregado - count_cancel

    # Total acumulado del día: todo menos cancelados (incluye entregados)
    import re as _re_total
    def _safe_total(val: str) -> float:
        try:
            m = _re_total.search(r'(\d+(?:[.,]\d{1,2})?)', (val or "").replace(",", "."))
            return float(m.group(1)) if m else 0.0
        except Exception:
            return 0.0
    total_dia = sum(
        _safe_total(p["total"])
        for p in pedidos
        if p.get("estado") != "Cancelado ❌" and p.get("total")
    ) if pedidos else 0

    cnt_yapeplin = sum(1 for p in pedidos if p.get("metodo_pago") in ("Yape/Plin", "Yape", "Plin") and p.get("estado") != "Cancelado ❌")
    cnt_efec = sum(1 for p in pedidos if p.get("metodo_pago") not in ("Yape/Plin", "Yape", "Plin") and p.get("estado") != "Cancelado ❌")

    if pedidos:
        cards = "".join(_render_card(p) for p in pedidos)
    elif fecha_sel == hoy:
        cards = '<div class="empty">No hay pedidos hoy todavía 🌮</div>'
    else:
        cards = f'<div class="empty">Sin pedidos para {html.escape(fecha_sel)} 🌮</div>'

    agotados_actual = db.get_config("productos_agotados", "")
    bot_pausado = db.get_config("bot_pausado", "0") == "1"

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
.oa-print{{color:#555;flex:0 0 44px}}
.oa-print:hover{{background:#eee}}
.oa-del{{flex:0 0 44px;color:#ccc}}
.oa-del:hover{{background:var(--ch-red-bg);color:var(--ch-red)}}
.oa-listo{{color:var(--ch-purple)}}
.oa-listo:hover{{background:var(--ch-purple-bg)}}
.oa-done{{padding:12px 16px;font-size:12px;color:#bbb;font-weight:600}}
.oc-pbadge.delivery-inc{{background:#E3F2FD;color:var(--ch-blue);border:1px solid #90CAF9}}
.oc-pbadge.demora{{background:#FFEBEE;color:#C62828;border:1px solid #FFCDD2;animation:pulse-demora 1.5s ease-in-out infinite}}
@keyframes pulse-demora{{0%,100%{{opacity:1}}50%{{opacity:.6}}}}

/* ── Misc ── */
.empty{{text-align:center;padding:60px 20px;color:#aaa;font-size:15px;grid-column:1/-1}}
.footer-note{{text-align:center;font-size:11px;color:#bbb;padding:12px}}

/* ── Banner de consultas de costo pendientes ── */
.cost-banner{{background:#E3F2FD;border-bottom:2px solid #1565C0;padding:12px 18px;display:none}}
.cost-banner-title{{font-size:13px;font-weight:800;color:#1565C0;margin-bottom:10px;display:flex;align-items:center;gap:6px}}
.cost-badge{{background:#1565C0;color:#fff;border-radius:20px;padding:2px 9px;font-size:11px}}
.cost-card{{background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:8px;border:1px solid #BBDEFB;display:flex;flex-wrap:wrap;align-items:flex-start;gap:10px}}
.cost-card:last-child{{margin-bottom:0}}
.cost-info{{flex:1;min-width:200px;font-size:13px;color:#333;line-height:1.7}}
.cost-info strong{{color:#1565C0}}
.cost-sugg{{font-size:11px;background:#E8F5E9;color:#2E7D32;border-radius:6px;padding:3px 8px;display:inline-block;margin-top:2px;font-weight:700}}
.cost-actions{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.cost-input{{border:1px solid #90CAF9;border-radius:8px;padding:8px 12px;font-size:14px;width:130px;outline:none;font-weight:700;color:#1565C0}}
.cost-input:focus{{border-color:#1565C0;box-shadow:0 0 0 2px rgba(21,101,192,.15)}}
.cost-btn{{background:#1565C0;color:#fff;border:none;border-radius:8px;padding:9px 18px;font-size:13px;font-weight:700;cursor:pointer;white-space:nowrap}}
.cost-btn:hover{{background:#1976D2}}
.cost-btn:active{{opacity:.8}}

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
  <a href="/admin/metricas">📊 Métricas</a>
  <a href="/admin/zonas-delivery">🛵 Zonas</a>
  <a href="/admin/menu">🍽️ Menú</a>
</nav>

<div class="toolbar">
  <span class="toolbar-info">📅 <strong id="totalCount">{len(pedidos)}</strong> pedidos &nbsp;·&nbsp; <span id="activosCount">{total_activos}</span> activos</span>
  <select id="fechaSelect" onchange="if(this.value)location.href='/pedidos?fecha='+encodeURIComponent(this.value)" style="border:1px solid #ccc;border-radius:8px;padding:5px 10px;font-size:13px;cursor:pointer">
    {"".join(f'<option value="{f}" {"selected" if f == fecha_sel else ""}>{f}{" (hoy)" if f == hoy else ""}</option>' for f in fechas_disponibles)}
  </select>
  <button class="btn-test" onclick="probarNotif()">🔔 Probar notificación</button>
  <button id="btnPausa" onclick="togglePausa()"
    style="border:none;padding:7px 18px;border-radius:20px;cursor:pointer;font-size:13px;font-weight:700;
           background:{'#e53935' if bot_pausado else '#2D5016'};color:#fff"
    data-pausado="{'1' if bot_pausado else '0'}">
    {'▶️ Reanudar bot' if bot_pausado else '⏸️ Pausar bot'}
  </button>
</div>
{'<div style="background:#e53935;color:#fff;text-align:center;padding:8px;font-weight:700;font-size:13px;letter-spacing:.3px">⏸️ BOT PAUSADO — Los clientes reciben mensaje de capacidad máxima</div>' if bot_pausado else ''}

<div id="agotadosBar" style="background:#fff8e1;border-bottom:1px solid #ffe082;padding:8px 18px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
  <span style="font-size:13px;font-weight:600;color:#e65100">⚠️ Agotados:</span>
  {''.join(
      f'<button onclick="toggleAgotado(this,\'{item}\')" '
      f'style="font-size:12px;border:1px solid #e65100;border-radius:20px;padding:4px 12px;cursor:pointer;transition:.15s;background:{"#e65100" if item in agotados_actual else "transparent"};color:{"#fff" if item in agotados_actual else "#e65100"}"'
      f'>{item}</button>'
      for item in ["Pastor","Suadero","Chorizo","Birria","Chamoyada"]
  )}
  <input id="agotadosInput" type="text" value="{html.escape(agotados_actual)}"
    placeholder="otros… (separados por coma)"
    style="flex:1;min-width:160px;border:1px solid #ffcc02;border-radius:8px;padding:5px 10px;font-size:13px;outline:none;background:#fffde7"
    onkeydown="if(event.key==='Enter')guardarAgotados()">
  <button onclick="guardarAgotados()" style="background:#e65100;color:white;border:none;border-radius:8px;padding:6px 14px;font-size:13px;font-weight:600;cursor:pointer">Guardar</button>
  <span id="agotadosStatus" style="font-size:12px;color:#4caf50;display:none">✅ Guardado</span>
</div>

<div class="cost-banner" id="costBanner">
  <div class="cost-banner-title">
    🛵 Consultas de costo pendientes <span class="cost-badge" id="costBadge">0</span>
  </div>
  <div id="costList"></div>
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

async function llamarDelivery(orderId) {{
  // Notifica directamente al dueño — él gestionará el motorizado manualmente
  try {{
    const r = await fetch('/api/pedidos/llamar-delivery', {{
      method: 'POST',
      credentials: 'same-origin',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{order_id: orderId}})
    }});
    if (r.status === 401) {{ location.reload(); return; }}
    const data = await r.json();
    if (data.status === 'ok') {{
      showToast('Solicitud de delivery en curso');
    }} else {{
      alert('Error: ' + (data.msg || 'No se pudo enviar'));
    }}
  }} catch(e) {{
    alert('Error: ' + e.message);
  }}
}}
async function avisarListo(orderId) {{
  try {{
    const r = await fetch('/api/pedidos/aviso-listo', {{
      method: 'POST',
      credentials: 'same-origin',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{order_id: orderId}})
    }});
    if (r.status === 401) {{ location.reload(); return; }}
    const data = await r.json();
    if (data.status === 'ok') {{
      showToast('📦 Cliente notificado — pedido listo esperando delivery');
    }} else {{
      alert('Error: ' + (data.msg || 'No se pudo enviar'));
    }}
  }} catch(e) {{
    alert('Error: ' + e.message);
  }}
}}

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

  // Badge de delivery pagado (cuando el cliente pagó el delivery junto con el pedido)
  const tieneDeliveryPago = (p.items || '').toLowerCase().includes('delivery:');
  const deliveryBadge = tieneDeliveryPago
    ? `<span class="oc-pbadge delivery-inc">🛵 Delivery pagado</span>`
    : '';

  // Badge de demora: pedidos en preparación > 50 min
  let demoraBadge = '';
  if (estado === 'En preparación 👨‍🍳' && p.hora) {{
    const [hh, mm] = p.hora.split(':').map(Number);
    const now = new Date();
    const orderMin = hh * 60 + mm;
    const nowMin   = now.getHours() * 60 + now.getMinutes();
    const diffMin  = nowMin - orderMin;
    if (diffMin >= 50 && diffMin < 600) {{
      demoraBadge = `<span class="oc-pbadge demora">⏰ ${{diffMin}} min en prep.</span>`;
    }}
  }}

  // Botones de acción
  let btnCancelHtml = '', btnDeliveryHtml = '', btnCostHtml = '', btnListoHtml = '', btnSigHtml = '';
  if (esActivo) {{
    btnCancelHtml   = `<button class="oa oa-cancel" onclick="cancelarPedido(${{p.id}})">❌ Cancelar</button>`;
    btnDeliveryHtml = !esRecojo
      ? `<button class="oa oa-delivery" onclick="llamarDelivery(${{p.id}})">🛵 Delivery</button>`
      : '';
    btnCostHtml = ''; // eliminado — la consulta se dispara automáticamente desde el bot
    // Botón "Avisar listo": solo para pedidos en preparación de delivery (no recojo)
    btnListoHtml = (estado === 'En preparación 👨‍🍳' && !esRecojo)
      ? `<button class="oa oa-listo" onclick="avisarListo(${{p.id}})">📦 Avisar listo</button>`
      : '';
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
  const btnDelHtml   = `<button class="oa oa-del" onclick="eliminarPedido(${{p.id}},this)" title="Eliminar">🗑️</button>`;
  const btnPrintHtml = `<button class="oa oa-print" onclick="window.open('/admin/imprimir/${{p.id}}','_blank')" title="Imprimir recibo">🖨️</button>`;

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
      ${{deliveryBadge}}
      ${{demoraBadge}}
    </div>
  </div>
  ${{(estado === 'En preparación 👨‍🍳' && !esRecojo) ? (() => {{
    let cobro, bg, border, color;
    if (esDigital && tieneDeliveryPago) {{
      cobro  = '✅ Ya pagó TODO — dile al moto que NO cobre nada al cliente';
      bg='#e8f5e9'; border='#a5d6a7'; color='#2e7d32';
    }} else if (esDigital) {{
      cobro  = '💜 Pagó en digital — el moto cobra SOLO el delivery al cliente';
      bg='#f3e5f5'; border='#ce93d8'; color='#6a1b9a';
    }} else {{
      cobro  = `💵 Pago en efectivo — el moto cobra ${{esc(p.total)}} + delivery`;
      bg='#fff8e1'; border='#ffe082'; color='#e65100';
    }}
    return `<div style="background:${{bg}};border:1px solid ${{border}};border-radius:8px;padding:7px 12px;margin:8px 12px 0;font-size:12px;font-weight:700;color:${{color}}">⚡ Dile al moto: ${{cobro}}</div>`;
  }})() : ''}}
  <div class="oc-actions">${{btnCancelHtml}}${{btnDeliveryHtml}}${{btnListoHtml}}${{btnCostHtml}}${{btnSigHtml}}${{btnPrintHtml}}${{btnDelHtml}}</div>
</div>`;
}}

function esc(s) {{
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

/* ── Refresh automático ── */
async function refreshOrders() {{
  try {{
    const fechaSel = document.getElementById('fechaSelect')?.value || '';
    const url = fechaSel ? `/api/pedidos?fecha=${{encodeURIComponent(fechaSel)}}` : '/api/pedidos';
    const r = await fetch(url, {{credentials:'same-origin'}});
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
      const fechaSel2 = document.getElementById('fechaSelect')?.value || '';
      grid.innerHTML = `<div class="empty">${{fechaSel2 ? 'Sin pedidos para ' + fechaSel2 : 'No hay pedidos hoy todavía'}} 🌮</div>`;
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

function toggleAgotado(btn, item) {{
  const inp = document.getElementById('agotadosInput');
  const parts = inp.value.split(',').map(s => s.trim()).filter(Boolean);
  const idx = parts.findIndex(p => p.toLowerCase() === item.toLowerCase());
  if (idx === -1) {{
    parts.push(item);
    btn.style.background = '#e65100'; btn.style.color = '#fff';
  }} else {{
    parts.splice(idx, 1);
    btn.style.background = 'transparent'; btn.style.color = '#e65100';
  }}
  inp.value = parts.join(', ');
  guardarAgotados();
}}

async function guardarAgotados() {{
  const val = document.getElementById('agotadosInput').value.trim();
  try {{
    const r = await fetch('/api/config/agotados', {{
      method: 'POST',
      credentials: 'same-origin',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{value: val}})
    }});
    const d = await r.json();
    if (d.status === 'ok') {{
      const st = document.getElementById('agotadosStatus');
      st.style.display = 'inline';
      setTimeout(() => st.style.display = 'none', 2000);
    }}
  }} catch(e) {{ alert('Error: ' + e.message); }}
}}

async function togglePausa() {{
  const btn = document.getElementById('btnPausa');
  const pausado = btn.dataset.pausado === '1';
  const nuevo = pausado ? '0' : '1';
  try {{
    const r = await fetch('/api/config/pausa', {{
      method: 'POST',
      credentials: 'same-origin',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{value: nuevo}})
    }});
    const d = await r.json();
    if (d.status === 'ok') location.reload();
  }} catch(e) {{ alert('Error: ' + e.message); }}
}}

// Iniciar polling cada 10 segundos
setInterval(refreshOrders, 10000);

/* ── Consultas de costo pendientes ── */
async function checkPendingCostQueries() {{
  try {{
    const r = await fetch('/api/delivery/pendientes', {{credentials:'same-origin'}});
    if (!r.ok) return;
    const data = await r.json();
    renderPendingCostQueries(data.pendientes || []);
  }} catch(e) {{ console.warn('CostQueries error:', e); }}
}}

function renderPendingCostQueries(queries) {{
  const banner = document.getElementById('costBanner');
  const list   = document.getElementById('costList');
  const badge  = document.getElementById('costBadge');
  if (!banner || !list) return;
  if (queries.length === 0) {{
    banner.style.display = 'none';
    return;
  }}
  banner.style.display = 'block';
  badge.textContent = queries.length;
  list.innerHTML = queries.map(q => {{
    const sugg = q.sugerencia
      ? `<span class="cost-sugg">💡 Zona similar: S/ ${{q.sugerencia.costo.toFixed(2)}} (${{q.sugerencia.count}} ${{q.sugerencia.count === 1 ? 'vez' : 'veces'}})</span>`
      : '';
    const inputId = 'costInput_' + q.client_phone;
    return `<div class="cost-card">
      <div class="cost-info">
        <strong>👤 +${{q.client_phone}}</strong><br>
        📍 ${{q.direccion || 'Sin especificar'}}<br>
        🛒 ${{q.items || '—'}}<br>
        💰 Subtotal: <strong>${{q.subtotal || '—'}}</strong>
        ${{sugg}}
      </div>
      <div class="cost-actions">
        <input class="cost-input" id="${{inputId}}" type="number" min="0" step="0.5"
          placeholder="S/ costo..."
          value="${{q.sugerencia ? q.sugerencia.costo : ''}}"
          onkeydown="if(event.key==='Enter')enviarCostoCliente('${{q.client_phone}}','${{q.subtotal}}')">
        <button class="cost-btn" onclick="enviarCostoCliente('${{q.client_phone}}','${{q.subtotal}}')">
          ✅ Enviar al cliente
        </button>
      </div>
    </div>`;
  }}).join('');
}}

async function enviarCostoCliente(phone, subtotalStr) {{
  const inputEl = document.getElementById('costInput_' + phone);
  const monto = parseFloat((inputEl ? inputEl.value : '') || '');
  if (isNaN(monto) || monto <= 0) {{
    alert('Ingresa un monto de delivery válido (ej: 7)');
    if (inputEl) inputEl.focus();
    return;
  }}
  try {{
    const r = await fetch('/api/delivery/enviar-costo', {{
      method: 'POST',
      credentials: 'same-origin',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{phone, monto, subtotal: subtotalStr}})
    }});
    if (r.status === 401) {{ location.reload(); return; }}
    const data = await r.json();
    if (data.status === 'ok') {{
      showToast('✅ Costo enviado al cliente +' + phone);
      checkPendingCostQueries();
    }} else {{
      alert('Error: ' + (data.msg || 'No se pudo enviar'));
    }}
  }} catch(e) {{ alert('Error: ' + e.message); }}
}}

// Polling de consultas pendientes cada 15 segundos
checkPendingCostQueries();
setInterval(checkPendingCostQueries, 15000);

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
async def api_pedidos_json(
    credentials: HTTPBasicCredentials = Depends(verificar_admin),
    fecha: str = Query(None)
):
    """Endpoint JSON para polling del frontend.
    Acepta ?fecha=DD/MM/YYYY para respetar la fecha seleccionada en el panel.
    """
    if fecha:
        pedidos = db.get_orders_for_date(fecha)
    else:
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
    """Notifica al dueño que debe gestionar el motorizado manualmente para este pedido."""
    data = await request.json()
    order_id = int(data.get("order_id", 0))
    # delivery_phone mantenida por compatibilidad (ya no se usa en este flujo)
    # delivery_phone = data.get("delivery_phone", "").strip()

    if not order_id:
        return JSONResponse({"status": "error", "msg": "order_id requerido"}, status_code=400)

    order = db.get_order_by_id(order_id)
    if not order:
        return JSONResponse({"status": "error", "msg": "Pedido no encontrado"}, status_code=404)

    from datetime import datetime, timezone, timedelta
    _PERU_TZ = timezone(timedelta(hours=-5))
    hora = datetime.now(_PERU_TZ).strftime("%d/%m · %I:%M %p")

    # ── Líneas de notificación a motorizado (mantenidas, no activas en este flujo) ──
    # valid_phones = {d["phone"] for d in DELIVERIES}
    # target_phone = delivery_phone if delivery_phone in valid_phones else (DELIVERIES[0]["phone"] if DELIVERIES else "")
    # target_name  = next((d["name"] for d in DELIVERIES if d["phone"] == target_phone), "Delivery")
    # msg_tg = f"🛵 *Pedido #{order_id} — Chilango*\n👤 +{order['phone']}\n📍 {order.get('direccion') or 'Sin dirección'}\n🕒 {hora}"
    # target_index = next((i+1 for i, d in enumerate(DELIVERIES) if d["phone"] == target_phone), 1)
    # tg_id = os.environ.get(f"DELIVERY_{target_index}_TELEGRAM_ID", "").strip()
    # ── Fin líneas motorizado ─────────────────────────────────────────────

    # Solicitar motorizado a Altoke — solo avisamos que hay pedido, los datos se dan en persona
    msg_delivery = f"🛵 Necesitamos un motorizado — Chilango\n🕒 {hora}"
    ok = await send_whatsapp_message(DELIVERY_SERVICE_PHONE, msg_delivery)
    if not ok:
        return JSONResponse({"status": "error", "msg": "No se pudo contactar a Altoke"}, status_code=500)

    print(f"[DELIVERY] ✅ Altoke notificado para pedido #{order_id}")
    return JSONResponse({"status": "ok", "delivery": "Altoke"})


@app.post("/api/pedidos/aviso-listo")
async def api_aviso_listo(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """Notifica al cliente que su pedido está listo esperando al delivery."""
    data = await request.json()
    order_id = int(data.get("order_id", 0))
    if not order_id:
        return JSONResponse({"status": "error", "msg": "order_id requerido"}, status_code=400)
    order = db.get_order_by_id(order_id)
    if not order:
        return JSONResponse({"status": "error", "msg": "Pedido no encontrado"}, status_code=404)
    await _notify_order_listo(order)
    print(f"[AVISO LISTO] WhatsApp enviado al cliente para pedido #{order_id}")
    return JSONResponse({"status": "ok"})


@app.post("/api/pedidos/consultar-delivery")
async def api_consultar_delivery(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """Notifica al dueño para que consulte el costo de delivery manualmente."""
    data = await request.json()
    order_id       = int(data.get("order_id", 0))
    # delivery_phone mantenida por compatibilidad (ya no se usa en este flujo)
    # delivery_phone = data.get("delivery_phone", "").strip()

    if not order_id:
        return JSONResponse({"status": "error", "msg": "order_id requerido"}, status_code=400)

    order = db.get_order_by_id(order_id)
    if not order:
        return JSONResponse({"status": "error", "msg": "Pedido no encontrado"}, status_code=404)

    from datetime import datetime, timezone, timedelta
    _PERU_TZ = timezone(timedelta(hours=-5))
    hora = datetime.now(_PERU_TZ).strftime("%d/%m · %I:%M %p")

    # ── Líneas de consulta a motorizado (mantenidas, no activas en este flujo) ──
    # valid_phones = {d["phone"] for d in DELIVERIES}
    # target_phone = delivery_phone if delivery_phone in valid_phones else (DELIVERIES[0]["phone"] if DELIVERIES else "")
    # consulta = f"¿Cual es el costo a la siguiente dirección?\nDirección: {order.get('direccion') or 'Sin dirección'}"
    # await send_whatsapp_message(target_phone, consulta)
    # ── Fin líneas motorizado ─────────────────────────────────────────────

    # Notificar al servicio de delivery para consultar costo
    msg_delivery = (
        f"💰 Consulta de costo delivery\n"
        f"👤 Cliente: +{order['phone']}\n"
        f"📍 {order.get('direccion') or 'Sin dirección'}"
    )
    await send_whatsapp_message(DELIVERY_SERVICE_PHONE, msg_delivery)
    print(f"[COSTO DELIVERY] Dueño notificado para pedido #{order_id}")
    return JSONResponse({"status": "ok", "delivery": "Dueño"})


@app.get("/api/delivery/pendientes")
async def api_delivery_pendientes(
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """Retorna todas las consultas de costo de delivery pendientes con sugerencia de zona."""
    queries = db.get_all_pending_cost_queries()
    result = []
    for q in queries:
        sugg = db.get_delivery_cost_suggestion(q.get("direccion", ""))
        result.append({
            "client_phone": q["client_phone"],
            "direccion":    q.get("direccion", ""),
            "subtotal":     q.get("subtotal", ""),
            "items":        q.get("items", ""),
            "pago":         q.get("pago", ""),
            "created_at":   q.get("created_at", ""),
            "sugerencia":   sugg,
        })
    return JSONResponse({"pendientes": result})


@app.post("/api/delivery/enviar-costo")
async def api_enviar_costo_delivery(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """El dueño ingresa el costo desde el panel — el bot lo envía al cliente automáticamente."""
    data = await request.json()
    phone    = (data.get("phone") or "").strip().replace("+", "")
    monto    = float(data.get("monto") or 0)
    subtotal_str = (data.get("subtotal") or "").strip()

    if not phone or monto <= 0:
        return JSONResponse({"status": "error", "msg": "phone y monto requeridos"}, status_code=400)

    # Verificar que el cliente no tenga ya un pedido confirmado hoy
    # (evita enviar el costo de delivery si el cliente ya eligió contra entrega u otro método)
    from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
    _hoy = _dt2.now(_tz2(_td2(hours=-5))).strftime("%d/%m/%Y")
    _pedidos_hoy = [
        p for p in db.get_orders_for_date(_hoy)
        if p.get("phone") == phone and p.get("estado") not in ("Cancelado ❌",)
    ]
    if _pedidos_hoy:
        db.delete_pending_cost_query(phone)  # limpiar consulta obsoleta
        print(f"[COSTO DELIVERY] ⚠️ Pedido ya confirmado para +{phone} — costo ignorado")
        return JSONResponse({"status": "ignorado", "msg": "El cliente ya tiene un pedido confirmado"})

    # Calcular total: extraer número del subtotal + monto delivery
    import re as _re
    subtotal_num = 0.0
    m = _re.search(r"(\d+(?:[.,]\d{1,2})?)", subtotal_str)
    if m:
        subtotal_num = float(m.group(1).replace(",", "."))
    total = round(subtotal_num + monto, 2)

    # Mensaje al cliente
    monto_fmt   = f"{monto:.2f}".rstrip("0").rstrip(".")
    total_fmt   = f"{total:.2f}".rstrip("0").rstrip(".")
    mensaje = (
        f"¡Ya tenemos el costo! 🛵\n"
        f"El delivery a tu zona es *S/ {monto_fmt}*.\n"
        f"El total de tu pedido sería *S/ {total_fmt}*.\n\n"
        f"¿Confirmamos? 🌮"
    )

    ok = await send_whatsapp_message(phone, mensaje)
    if not ok:
        return JSONResponse({"status": "error", "msg": "No se pudo enviar el mensaje al cliente"}, status_code=500)

    # Agregar al historial de conversación para que el bot procese la confirmación
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    _ts = _dt.now(_tz(_td(hours=-5))).strftime("%H:%M")
    db.append_message(phone, "assistant", mensaje, ts=_ts)

    # Guardar en historial de costos (aprendizaje de zonas)
    query = next((q for q in db.get_all_pending_cost_queries() if q["client_phone"] == phone), None)
    if query:
        db.save_delivery_cost(
            phone,
            query.get("direccion", ""),
            monto,
            query.get("subtotal", ""),
            query.get("items", ""),
        )

    # Eliminar la consulta pendiente
    db.delete_pending_cost_query(phone)

    print(f"[COSTO DELIVERY] ✅ S/{monto_fmt} enviado a +{phone} — total S/{total_fmt}")
    return JSONResponse({"status": "ok"})


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


# ══════════════════════════════════════════════════════════════
# ── MENÚ EDITABLE ─────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════

@app.get("/admin/menu", response_class=HTMLResponse)
async def admin_menu(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    items = db.get_menu_items()
    from menu import EMPAQUE
    # Agrupar por categoría
    grupos: dict = {}
    for it in items:
        grupos.setdefault(it["categoria"], []).append(it)

    filas = ""
    for cat, cat_items in grupos.items():
        filas += f'<tr class="cat-row"><td colspan="5"><strong>{cat}</strong></td></tr>'
        for it in cat_items:
            disp_checked = "checked" if it["disponible"] else ""
            filas += f"""
<tr data-id="{it['id']}">
  <td><input class="mi-nombre" value="{html.escape(it['nombre'])}" style="width:100%"></td>
  <td><input class="mi-desc" value="{html.escape(it['descripcion'] or '')}" style="width:100%"></td>
  <td style="width:80px"><input class="mi-precio" type="number" step="0.5" value="{it['precio']}" style="width:70px"></td>
  <td style="width:60px;text-align:center"><input class="mi-disp" type="checkbox" {disp_checked}></td>
  <td style="width:70px"><button onclick="guardarItem(this)" style="background:#2d6a2d;color:#fff;border:none;padding:4px 10px;border-radius:6px;cursor:pointer">💾 Guardar</button></td>
</tr>"""

    page = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<title>Menú — Chilango</title>
<style>
  body{{font-family:sans-serif;background:#f5f5f5;margin:0;padding:20px}}
  h1{{color:#2d6a2d;margin-bottom:4px}}
  p.sub{{color:#666;margin-top:0;margin-bottom:20px}}
  table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
  th{{background:#2d6a2d;color:#fff;padding:10px 12px;text-align:left}}
  td{{padding:8px 12px;border-bottom:1px solid #eee;vertical-align:middle}}
  tr.cat-row td{{background:#e8f5e9;font-weight:700;font-size:.95em;color:#1b5e20}}
  input[type=text],input.mi-nombre,input.mi-desc{{border:1px solid #ccc;border-radius:4px;padding:4px 6px;font-size:.9em}}
  input[type=number]{{border:1px solid #ccc;border-radius:4px;padding:4px 6px;font-size:.9em}}
  .back{{display:inline-block;margin-bottom:16px;color:#2d6a2d;text-decoration:none;font-weight:600}}
  .back:hover{{text-decoration:underline}}
  #toast{{position:fixed;bottom:24px;right:24px;background:#2d6a2d;color:#fff;padding:10px 20px;border-radius:8px;display:none;font-size:.95em;box-shadow:0 4px 12px rgba(0,0,0,.2)}}
</style>
</head><body>
<a class="back" href="/admin">← Volver al panel</a>
<h1>🍽️ Menú editable</h1>
<p class="sub">Edita precios, nombres o desactiva items sin tocar el código. Los cambios se reflejan en nuevas conversaciones de forma inmediata.</p>
<table>
  <thead><tr>
    <th>Producto</th><th>Descripción</th>
    <th>Precio (S/)</th><th>Activo</th><th></th>
  </tr></thead>
  <tbody>{filas}</tbody>
</table>
<div id="toast">✅ Guardado</div>
<script>
async function guardarItem(btn) {{
  const tr = btn.closest('tr');
  const id = tr.dataset.id;
  const nombre    = tr.querySelector('.mi-nombre').value.trim();
  const descripcion = tr.querySelector('.mi-desc').value.trim();
  const precio    = parseFloat(tr.querySelector('.mi-precio').value);
  const disponible = tr.querySelector('.mi-disp').checked ? 1 : 0;
  btn.textContent = '⏳';
  const r = await fetch('/api/menu/item', {{
    method:'POST', credentials:'same-origin',
    headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{id, nombre, descripcion, precio, disponible}})
  }});
  const d = await r.json();
  btn.textContent = '💾 Guardar';
  if(d.status==='ok') {{
    const t = document.getElementById('toast');
    t.style.display='block';
    setTimeout(()=>t.style.display='none', 2500);
  }}
}}
</script>
</body></html>"""
    return HTMLResponse(page)


@app.post("/api/menu/item")
async def api_update_menu_item(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """Actualiza un item del menú desde el panel y recarga el prompt del bot."""
    from bot import refresh_menu
    data = await request.json()
    item_id    = int(data.get("id", 0))
    nombre     = (data.get("nombre") or "").strip()
    descripcion = (data.get("descripcion") or "").strip()
    precio     = float(data.get("precio", 0))
    disponible = int(data.get("disponible", 1))
    if not item_id or not nombre or precio < 0:
        return JSONResponse({"status": "error", "msg": "Datos inválidos"}, status_code=400)
    db.update_menu_item(item_id, nombre=nombre, descripcion=descripcion,
                        precio=precio, disponible=disponible)
    # Recargar el menú en el sistema prompt sin reiniciar
    try:
        refresh_menu()
    except Exception as e:
        print(f"[MENÚ] refresh_menu error: {e}")
    return JSONResponse({"status": "ok"})


# ══════════════════════════════════════════════════════════════
# ── DASHBOARD DE MÉTRICAS ─────────────────────────────────────
# ══════════════════════════════════════════════════════════════

@app.get("/admin/metricas", response_class=HTMLResponse)
async def admin_metricas(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    m = db.get_metricas()
    import json as _json
    dias_labels   = _json.dumps(m["dias_labels"])
    dias_ventas   = _json.dumps(m["dias_ventas"])
    dias_pedidos  = _json.dumps(m["dias_pedidos"])
    horas_labels  = _json.dumps(m["horas_labels"])
    horas_data    = _json.dumps(m["horas_data"])
    top_nombres   = _json.dumps([p["nombre"] for p in m["top_productos"]])
    top_qtys      = _json.dumps([p["qty"]    for p in m["top_productos"]])
    pago_labels   = _json.dumps(list(m["pago_conteo"].keys()))
    pago_data     = _json.dumps(list(m["pago_conteo"].values()))

    page = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<title>Métricas — Chilango</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:sans-serif;background:#f0f4f0;padding:20px;color:#222}}
  h1{{color:#2d6a2d;margin-bottom:4px}}
  .sub{{color:#666;margin-bottom:20px;font-size:.9em}}
  .back{{display:inline-block;margin-bottom:16px;color:#2d6a2d;text-decoration:none;font-weight:600}}
  .back:hover{{text-decoration:underline}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:24px}}
  .card{{background:#fff;border-radius:12px;padding:16px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.07)}}
  .card .val{{font-size:1.8em;font-weight:700;color:#2d6a2d}}
  .card .lbl{{font-size:.8em;color:#666;margin-top:4px}}
  .charts{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
  .chart-box{{background:#fff;border-radius:12px;padding:16px;box-shadow:0 2px 8px rgba(0,0,0,.07)}}
  .chart-box h3{{font-size:.95em;color:#2d6a2d;margin-bottom:12px}}
  @media(max-width:700px){{.charts{{grid-template-columns:1fr}}}}
</style>
</head><body>
<a class="back" href="/admin">← Volver al panel</a>
<h1>📊 Métricas de ventas</h1>
<p class="sub">Actualizado al abrir esta página</p>

<div class="cards">
  <div class="card"><div class="val">S/ {m['total_hoy']:.2f}</div><div class="lbl">💰 Ventas hoy</div></div>
  <div class="card"><div class="val">{m['pedidos_hoy']}</div><div class="lbl">📦 Pedidos hoy</div></div>
  <div class="card"><div class="val">S/ {m['total_semana']:.2f}</div><div class="lbl">📅 Esta semana</div></div>
  <div class="card"><div class="val">{m['pedidos_semana']}</div><div class="lbl">📦 Pedidos semana</div></div>
  <div class="card"><div class="val">S/ {m['total_mes']:.2f}</div><div class="lbl">🗓️ Este mes</div></div>
  <div class="card"><div class="val">{m['pedidos_mes']}</div><div class="lbl">📦 Pedidos mes</div></div>
</div>

<div class="charts">
  <div class="chart-box" style="grid-column:1/-1">
    <h3>📈 Ventas últimos 14 días (S/)</h3>
    <canvas id="cVentas" height="90"></canvas>
  </div>
  <div class="chart-box">
    <h3>🌮 Top productos más pedidos</h3>
    <canvas id="cTop"></canvas>
  </div>
  <div class="chart-box">
    <h3>🕐 Hora pico</h3>
    <canvas id="cHora"></canvas>
  </div>
  <div class="chart-box">
    <h3>💳 Método de pago</h3>
    <canvas id="cPago"></canvas>
  </div>
  <div class="chart-box">
    <h3>📦 Pedidos por día</h3>
    <canvas id="cPedidos" height="90"></canvas>
  </div>
</div>

<script>
const green = '#2d6a2d', lightGreen = '#81c784', lime = '#c8e6c9';
Chart.defaults.font.family = 'sans-serif';
Chart.defaults.font.size   = 12;

new Chart(document.getElementById('cVentas'), {{
  type:'bar',
  data:{{ labels:{dias_labels}, datasets:[{{
    label:'S/', data:{dias_ventas},
    backgroundColor: lightGreen, borderColor: green, borderWidth:1, borderRadius:4
  }}]}},
  options:{{ plugins:{{legend:{{display:false}}}}, scales:{{ y:{{beginAtZero:true}} }} }}
}});

new Chart(document.getElementById('cTop'), {{
  type:'bar',
  data:{{ labels:{top_nombres}, datasets:[{{
    label:'Unidades', data:{top_qtys},
    backgroundColor: green, borderRadius:4
  }}]}},
  options:{{ indexAxis:'y', plugins:{{legend:{{display:false}}}} }}
}});

new Chart(document.getElementById('cHora'), {{
  type:'bar',
  data:{{ labels:{horas_labels}, datasets:[{{
    label:'Pedidos', data:{horas_data},
    backgroundColor: '#ff8f00', borderRadius:4
  }}]}},
  options:{{ plugins:{{legend:{{display:false}}}}, scales:{{ y:{{beginAtZero:true}} }} }}
}});

new Chart(document.getElementById('cPago'), {{
  type:'doughnut',
  data:{{ labels:{pago_labels}, datasets:[{{
    data:{pago_data},
    backgroundColor:[green, '#ff8f00', '#1565c0', '#6a1b9a']
  }}]}}
}});

new Chart(document.getElementById('cPedidos'), {{
  type:'line',
  data:{{ labels:{dias_labels}, datasets:[{{
    label:'Pedidos', data:{dias_pedidos},
    borderColor: green, backgroundColor: lime,
    fill:true, tension:.3, pointRadius:3
  }}]}},
  options:{{ plugins:{{legend:{{display:false}}}}, scales:{{ y:{{beginAtZero:true}} }} }}
}});
</script>
</body></html>"""
    return HTMLResponse(page)


# ══════════════════════════════════════════════════════════════
# ── HISTORIAL DE COSTOS POR ZONA ──────────────────────────────
# ══════════════════════════════════════════════════════════════

@app.get("/admin/zonas-delivery", response_class=HTMLResponse)
async def admin_zonas_delivery(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    zonas = db.get_delivery_zones_summary()

    if zonas:
        filas = ""
        for z in zonas:
            filas += f"""
<tr>
  <td>{html.escape(z['zona'])}</td>
  <td style="text-align:center"><strong>S/ {z['costo_promedio']:.1f}</strong></td>
  <td style="text-align:center">S/ {z['ultimo_costo']:.1f}</td>
  <td style="text-align:center">S/ {z['costo_min']:.1f} – S/ {z['costo_max']:.1f}</td>
  <td style="text-align:center">{z['frecuencia']}x</td>
  <td style="color:#888;font-size:.85em">{z['ultima_vez']}</td>
  <td style="font-size:.8em;color:#555;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
      title="{html.escape(z['ultima_dir'])}">{html.escape(z['ultima_dir'])}</td>
</tr>"""
        tabla = f"""<table>
<thead><tr>
  <th>Zona / Referencia</th><th>Promedio</th><th>Último</th>
  <th>Rango</th><th>Veces</th><th>Última vez</th><th>Dirección ejemplo</th>
</tr></thead>
<tbody>{filas}</tbody>
</table>"""
    else:
        tabla = '<p style="color:#888;margin-top:20px">Aún no hay datos de costos de delivery registrados.</p>'

    page = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<title>Zonas Delivery — Chilango</title>
<style>
  body{{font-family:sans-serif;background:#f5f5f5;margin:0;padding:20px}}
  h1{{color:#2d6a2d;margin-bottom:4px}}
  p.sub{{color:#666;margin-bottom:20px;font-size:.9em}}
  .back{{display:inline-block;margin-bottom:16px;color:#2d6a2d;text-decoration:none;font-weight:600}}
  .back:hover{{text-decoration:underline}}
  table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
  th{{background:#2d6a2d;color:#fff;padding:10px 12px;text-align:left;font-size:.9em}}
  td{{padding:9px 12px;border-bottom:1px solid #eee;font-size:.9em}}
  tr:hover td{{background:#f1f8f1}}
</style>
</head><body>
<a class="back" href="/admin">← Volver al panel</a>
<h1>🛵 Historial de costos por zona</h1>
<p class="sub">Costos aprendidos automáticamente de cada pedido con delivery. Úsalos como referencia para responder rápido.</p>
{tabla}
</body></html>"""
    return HTMLResponse(page)


# ══════════════════════════════════════════════════════════════
# ── IMPRESIÓN DE PEDIDOS ───────────────────────────────────────
# ══════════════════════════════════════════════════════════════

@app.get("/admin/imprimir/{order_id}", response_class=HTMLResponse)
async def imprimir_pedido(
    order_id: int,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """Genera un recibo imprimible para un pedido."""
    order = db.get_order_by_id(order_id)
    if not order:
        return HTMLResponse("<p>Pedido no encontrado</p>", status_code=404)
    # Obtener fecha/hora del pedido desde la tabla
    with db._conn() as c:
        row = c.execute("SELECT fecha, hora FROM orders WHERE id=?", (order_id,)).fetchone()
    fecha = row["fecha"] if row else "—"
    hora  = row["hora"]  if row else "—"

    page = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<title>Recibo #{order_id}</title>
<style>
  @media print {{
    body{{ margin:0 }} .no-print{{display:none}}
  }}
  body{{font-family:'Courier New',monospace;max-width:320px;margin:0 auto;padding:16px;font-size:13px}}
  h2{{text-align:center;margin:0;font-size:1.1em}}
  .sep{{border:none;border-top:1px dashed #000;margin:8px 0}}
  .row{{display:flex;justify-content:space-between}}
  .label{{font-weight:bold;color:#555}}
  .print-btn{{display:block;margin:16px auto;padding:10px 24px;background:#2d6a2d;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:1em}}
</style>
</head><body>
<h2>🌮 CHILANGO</h2>
<p style="text-align:center;font-size:.85em;margin:2px 0">Pedido #{order_id}</p>
<p style="text-align:center;font-size:.85em;margin:2px 0">{fecha} · {hora}</p>
<hr class="sep">
<p><span class="label">Cliente:</span> +{order['phone']}</p>
<p><span class="label">Dirección:</span> {html.escape(order.get('direccion') or 'Recojo')}</p>
<p><span class="label">Pago:</span> {html.escape(order.get('metodo_pago') or '—')}</p>
<hr class="sep">
<p><span class="label">Pedido:</span></p>
<p style="white-space:pre-wrap">{html.escape(order['items'])}</p>
<hr class="sep">
<p class="row"><span class="label">TOTAL:</span><strong>{html.escape(order['total'])}</strong></p>
{'<p><span class="label">Notas:</span> ' + html.escape(order.get("notas") or "") + '</p>' if order.get("notas") else ""}
<hr class="sep">
<p style="text-align:center;font-size:.8em">¡Gracias por elegir Chilango! 🌮</p>
<button class="print-btn no-print" onclick="window.print()">🖨️ Imprimir</button>
<script>window.onload = () => window.print();</script>
</body></html>"""
    return HTMLResponse(page)


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
    # NO se escala automáticamente — el bot sigue activo para esa conversación
    # Si se quiere silenciar el bot, usar el botón de Escalar en el panel
    print(f"[MANUAL] Mensaje enviado a {phone}: {message[:60]}")
    return JSONResponse({"status": "ok"})


@app.post("/api/conversations/delete")
async def delete_conversation(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """Elimina el historial de chat de un contacto."""
    data = await request.json()
    phone = data.get("phone", "").strip()
    if not phone:
        return JSONResponse({"status": "error"}, status_code=400)
    db.delete_conversation(phone)
    print(f"[ADMIN] Chat eliminado: {phone}")
    return JSONResponse({"status": "ok"})


@app.post("/api/conversations/delete-bulk")
async def delete_conversations_bulk(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """Elimina el historial de múltiples contactos."""
    data = await request.json()
    phones = data.get("phones", [])
    if not phones:
        return JSONResponse({"status": "error", "msg": "No phones provided"}, status_code=400)
    for phone in phones:
        phone = str(phone).strip()
        if phone:
            db.delete_conversation(phone)
            print(f"[ADMIN] Chat eliminado (bulk): {phone}")
    return JSONResponse({"status": "ok", "deleted": len(phones)})


@app.post("/api/conversations/pausar")
async def pausar_bot_conv(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """El equipo pausa el bot para atender manualmente a un cliente."""
    data = await request.json()
    phone = data.get("phone", "").strip()
    if not phone:
        return JSONResponse({"status": "error"}, status_code=400)
    db.mark_escalated(phone)
    print(f"[ESCALATE] Bot pausado para {phone}")
    return JSONResponse({"status": "ok"})


@app.post("/api/conversations/reactivar")
async def reactivar_bot_conv(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    """El equipo libera la conversación para que el bot vuelva a responder."""
    data = await request.json()
    phone = data.get("phone", "").strip()
    if not phone:
        return JSONResponse({"status": "error"}, status_code=400)
    db.reset_escalation(phone)
    print(f"[ESCALATE] Bot reactivado para {phone}")
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
        es_delivery = phone in DELIVERY_NAME_MAP
        display_name = f"🛵 {DELIVERY_NAME_MAP[phone]}" if es_delivery else f"+{phone}"
        avatar = "🛵" if es_delivery else "👤"
        tiempo = _format_contact_time(data.get("last_msg_at", ""))
        contacts_html += (
            f'<div class="contact{unread_class}" id="c_{html.escape(phone)}" onclick="contactClick(event,\'{html.escape(phone)}\')" data-phone="{html.escape(phone)}">'
            f'<input type="checkbox" class="conv-chk" data-phone="{html.escape(phone)}"'
            f' onclick="event.stopPropagation()" onchange="onChkChange()"'
            f' style="display:none;width:14px;height:14px;flex-shrink:0;cursor:pointer;accent-color:#25d366;margin-right:4px">'
            f'<div class="avatar">{avatar}</div>'
            f'<div class="contact-info">'
            f'<div class="contact-row1"><div class="contact-name">{html.escape(display_name)}</div>'
            f'<div class="contact-time">{tiempo}</div></div>'
            f'<div class="contact-row2"><div class="contact-preview">{preview}</div>{badge}</div>'
            f'</div></div>'
        )
    # Mensajes limpios (sin imágenes) + timestamp si existe
    conv_clean = {}
    conv_escalado = {}
    for phone, data in conversaciones_raw.items():
        conv_clean[phone] = []
        conv_escalado[phone] = data.get("escalado", False)
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
    return JSONResponse({"contacts_html": contacts_html, "convs": conv_clean, "escalado": conv_escalado})


@app.get("/admin/clientes", response_class=HTMLResponse)
async def admin_clientes(
    credentials: HTTPBasicCredentials = Depends(verificar_admin),
    fecha: str = Query(None)
):
    from datetime import datetime, timezone, timedelta
    PERU_TZ = timezone(timedelta(hours=-5))
    hoy = datetime.now(PERU_TZ).strftime("%d/%m/%Y")
    fecha_sel = fecha if fecha else hoy

    # Obtener fechas disponibles para el selector
    fechas_raw = db.get_available_dates()
    fechas_disponibles = fechas_raw if hoy in fechas_raw else [hoy] + fechas_raw

    # Clientes del día seleccionado
    clientes = db.get_customers_with_stats_for_date(fecha_sel)

    filas = ""
    for i, c in enumerate(clientes, 1):
        nombre    = html.escape(c.get("nombre") or "—")
        phone          = html.escape(c.get("phone") or "")
        ultima_dir     = html.escape(c.get("ultima_dir") or "—")
        puntos         = int(c.get("puntos") or 0)
        pedidos_dia    = int(c.get("total_pedidos") or 0)
        gastado_dia    = float(c.get("total_gastado") or 0)
        pedidos_hist   = int(c.get("total_pedidos_hist") or pedidos_dia)
        gastado_hist   = float(c.get("total_gastado_hist") or gastado_dia)
        es_recurrente  = pedidos_hist > pedidos_dia
        updated        = (c.get("updated_at") or "—")[:16]
        medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else f"{i}"))
        badge_rec = ' <span style="font-size:10px;background:#e8f5e9;color:#2e7d32;border-radius:10px;padding:1px 6px;font-weight:600">recurrente</span>' if es_recurrente else ''
        filas += f"""<tr>
          <td class="cl-rank">{medal}</td>
          <td class="cl-phone"><a href="https://wa.me/{phone}" target="_blank">+{phone}</a></td>
          <td>{nombre}{badge_rec}</td>
          <td class="cl-num">{pedidos_dia}</td>
          <td class="cl-num cl-total">S/ {gastado_dia:.2f}</td>
          <td class="cl-num cl-hist">{pedidos_hist}</td>
          <td class="cl-num cl-hist">S/ {gastado_hist:.2f}</td>
          <td class="cl-pts">
            <input class="pts-input" type="number" min="0" value="{puntos}"
              onchange="guardarPuntos('{phone}', this)"
              onkeydown="if(event.key==='Enter')this.blur()">
          </td>
          <td class="cl-date">{updated}</td>
        </tr>"""

    total_clientes = len(clientes)
    total_gastado_global = sum(c.get("total_gastado") or 0 for c in clientes)
    total_pedidos_dia = sum(c.get("total_pedidos") or 0 for c in clientes)
    label_fecha = f"{'Hoy ' if fecha_sel == hoy else ''}{fecha_sel}"

    selector_fechas = "".join(
        f'<option value="{f}" {"selected" if f == fecha_sel else ""}>{f}{" (hoy)" if f == hoy else ""}</option>'
        for f in fechas_disponibles
    )

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
.toolbar-cl{{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}}
.toolbar-cl select{{border:1px solid #ccc;border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer;outline:none}}
.toolbar-cl input{{border:1px solid #ddd;border-radius:10px;padding:8px 14px;font-size:14px;outline:none;min-width:240px}}
.toolbar-cl input:focus{{border-color:#2D5016}}
.stats-bar{{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}}
.stat-chip{{background:white;border-radius:12px;padding:12px 20px;box-shadow:0 1px 4px rgba(0,0,0,.08);text-align:center}}
.stat-chip .val{{font-size:22px;font-weight:700;color:#2D5016}}
.stat-chip .lbl{{font-size:11px;color:#888;margin-top:2px}}
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
.cl-hist{{color:#555;background:#f8fdf5}}
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
  <a href="/admin/metricas">📊 Métricas</a>
  <a href="/admin/zonas-delivery">🛵 Zonas</a>
  <a href="/admin/menu">🍽️ Menú</a>
</nav>
<div class="wrap">
  <div class="toolbar-cl">
    <select onchange="if(this.value)location.href='/admin/clientes?fecha='+encodeURIComponent(this.value)">
      {selector_fechas}
    </select>
    <input type="text" id="buscar" placeholder="🔍 Buscar por teléfono o nombre..." oninput="filtrar(this.value)">
  </div>
  <div class="stats-bar">
    <div class="stat-chip"><div class="val">{total_clientes}</div><div class="lbl">Clientes — {label_fecha}</div></div>
    <div class="stat-chip"><div class="val">{total_pedidos_dia}</div><div class="lbl">Pedidos ese día</div></div>
    <div class="stat-chip"><div class="val">S/ {total_gastado_global:.2f}</div><div class="lbl">Facturación del día</div></div>
    <div class="stat-chip"><div class="val">S/ {(total_gastado_global/total_clientes if total_clientes else 0):.2f}</div><div class="lbl">Ticket promedio</div></div>
  </div>
  <div class="tbl-wrap">
    {"<div class='empty'>Sin pedidos para " + html.escape(fecha_sel) + " 🌮</div>" if not clientes else f"""
    <table id="tablaClientes">
      <thead>
        <tr>
          <th>#</th>
          <th>Teléfono</th>
          <th>Nombre</th>
          <th style="text-align:right">Pedidos día</th>
          <th style="text-align:right">Total día</th>
          <th style="text-align:right;background:#1b3a0e">Pedidos hist.</th>
          <th style="text-align:right;background:#1b3a0e">Total hist.</th>
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


@app.post("/api/config/agotados")
async def api_guardar_agotados(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    data = await request.json()
    value = data.get("value", "").strip()
    db.set_config("productos_agotados", value)
    print(f"[CONFIG] Productos agotados actualizados: '{value}'")
    return JSONResponse({"status": "ok"})


@app.post("/api/config/pausa")
async def api_toggle_pausa(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verificar_admin)
):
    data = await request.json()
    value = "1" if data.get("value") == "1" else "0"
    db.set_config("bot_pausado", value)
    estado = "PAUSADO" if value == "1" else "ACTIVO"
    print(f"[CONFIG] Bot {estado}")
    return JSONResponse({"status": "ok", "bot_pausado": value == "1"})


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
        # Mostrar nombre del motorizado si es delivery, si no mostrar número
        es_delivery = phone in DELIVERY_NAME_MAP
        display_name = f"🛵 {DELIVERY_NAME_MAP[phone]}" if es_delivery else f"+{phone}"
        avatar = "🛵" if es_delivery else "👤"
        tiempo = _format_contact_time(data.get("last_msg_at", ""))
        contacts_html += f"""
        <div class="contact{unread_class}" id="c_{html.escape(phone)}" onclick="contactClick(event, '{html.escape(phone)}')" data-phone="{html.escape(phone)}">
            <input type="checkbox" class="conv-chk" data-phone="{html.escape(phone)}"
                onclick="event.stopPropagation()" onchange="onChkChange()"
                style="display:none;width:14px;height:14px;flex-shrink:0;cursor:pointer;accent-color:#25d366;margin-right:4px">
            <div class="avatar">{avatar}</div>
            <div class="contact-info">
                <div class="contact-row1">
                    <div class="contact-name">{html.escape(display_name)}</div>
                    <div class="contact-time">{tiempo}</div>
                </div>
                <div class="contact-row2">
                    <div class="contact-preview">{preview}</div>
                    {badge}
                </div>
            </div>
        </div>"""

    if not contacts_html:
        contacts_html = "<div class='no-convs'>Sin conversaciones aún</div>"

    # Serializar conversaciones para JS (imágenes excluidas por tamaño, timestamps incluidos)
    conv_clean = {}
    conv_escalado = {}
    for phone, data in conversaciones_raw.items():
        conv_clean[phone] = []
        conv_escalado[phone] = data.get("escalado", False)
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
    escalado_json = json.dumps(conv_escalado, ensure_ascii=False)

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
        .sidebar-title {{ padding: 10px 16px; font-size: 12px; color: #667781; background: #f0f2f5; border-bottom: 1px solid #e9edef; font-weight: 600; letter-spacing: .5px; display:flex; align-items:center; justify-content:space-between; }}
        .sidebar-list {{ overflow-y: auto; flex: 1; }}
        .contact {{ padding: 12px 16px; border-bottom: 1px solid #e9edef; cursor: pointer; display: flex; align-items: center; gap: 12px; transition: background .1s; }}
        .contact:hover {{ background: #f5f5f5; }}
        .contact.active {{ background: #d9fdd3; }}
        .avatar {{ width: 46px; height: 46px; border-radius: 50%; background: #25d366; display: flex; align-items: center; justify-content: center; font-size: 20px; flex-shrink: 0; }}
        .contact-info {{ flex: 1; min-width: 0; }}
        .contact-row1 {{ display: flex; align-items: baseline; justify-content: space-between; gap: 6px; }}
        .contact-row2 {{ display: flex; align-items: center; gap: 6px; margin-top: 3px; }}
        .contact-name {{ font-weight: 600; font-size: 14px; color: #111; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; min-width: 0; }}
        .contact-time {{ font-size: 11px; color: #667781; flex-shrink: 0; white-space: nowrap; }}
        .contact-preview {{ font-size: 13px; color: #667781; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; min-width: 0; }}
        .contact-unread {{ font-size: 11px; background: #25d366; color: white; border-radius: 50%; min-width: 20px; height: 20px; padding: 0 4px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; font-weight: 700; }}
        .contact.unread {{ background: #f0fdf4; }}
        .contact.unread .contact-name {{ color: #111; font-weight: 700; }}
        .contact.unread .contact-time {{ color: #25d366; font-weight: 700; }}
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
            <div class="sidebar-title">
                <span>CONVERSACIONES</span>
                <label style="display:flex;align-items:center;gap:5px;cursor:pointer;font-size:12px;font-weight:400;color:#aaa" title="Seleccionar todas">
                    <input type="checkbox" id="chkSelectAll" onchange="toggleSelectAll(this.checked)"
                        style="width:14px;height:14px;cursor:pointer;accent-color:#25d366">
                    Todas
                </label>
            </div>
            <div id="bulkBar" style="display:none;padding:6px 12px;background:#fff3e0;border-bottom:1px solid #ffe082;align-items:center;gap:8px;flex-shrink:0">
                <span id="bulkCount" style="font-size:12px;color:#555;flex:1">0 seleccionadas</span>
                <button onclick="eliminarSeleccionadas()"
                    style="background:#e53935;color:#fff;border:none;border-radius:16px;padding:4px 14px;font-size:12px;font-weight:700;cursor:pointer">
                    🗑️ Eliminar
                </button>
                <button onclick="cancelarSeleccion()"
                    style="background:none;border:1px solid #aaa;border-radius:16px;padding:4px 12px;font-size:12px;cursor:pointer;color:#555">
                    Cancelar
                </button>
            </div>
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
        let escaladoMap = {escalado_json};

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

            const isEscalado = escaladoMap[phone] || false;
            const escaladoBadge = isEscalado
                ? `<span style="background:#e53935;color:#fff;font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;margin-left:8px">🤝 Equipo activo</span>
                   <button onclick="reactivarBot('${{esc(phone)}}')" style="background:#2D5016;color:#fff;border:none;border-radius:20px;padding:4px 12px;font-size:11px;font-weight:700;cursor:pointer;margin-left:6px">🤖 Reactivar bot</button>`
                : `<button onclick="pausarBot('${{esc(phone)}}')" style="background:#f57c00;color:#fff;border:none;border-radius:20px;padding:4px 12px;font-size:11px;font-weight:700;cursor:pointer;margin-left:6px">⏸️ Pausar bot</button>`;
            const avatarIcon = (window.deliveryPhones && window.deliveryPhones[phone]) ? '🛵' : '👤';
            const displayName = (window.deliveryNames && window.deliveryNames[phone]) ? '🛵 ' + window.deliveryNames[phone] : '+' + esc(phone);
            document.getElementById('chatPanel').innerHTML = `
                <div class="chat-header" style="flex-wrap:wrap;gap:6px">
                    <div class="avatar">${{avatarIcon}}</div>
                    <div class="chat-header-name" style="flex:1">${{displayName}}</div>
                    ${{escaladoBadge}}
                    <button onclick="eliminarChat('${{esc(phone)}}')" title="Eliminar chat"
                        style="background:none;border:none;cursor:pointer;font-size:16px;color:#aaa;padding:4px 8px"
                        onmouseover="this.style.color='#e53935'" onmouseout="this.style.color='#aaa'">🗑️</button>
                </div>
                <div class="chat-messages" id="msgs">${{bubbles}}</div>
                ${{isEscalado ? `<div style="padding:8px 12px;background:#fff8e1;border-top:1px solid #ffe082;display:flex;gap:6px;flex-wrap:wrap;align-items:center">
                    <span style="font-size:11px;font-weight:700;color:#e65100">⚡ Respuesta rápida:</span>
                    <button onclick="usarPlantilla(this)" data-txt="Disculpa la demora, Chilanguit@ 🙏 Ya estamos en ello y te avisamos en cuanto tu pedido salga." style="font-size:11px;border:1px solid #e65100;border-radius:16px;padding:3px 10px;background:transparent;color:#e65100;cursor:pointer">⏰ Disculpa demora</button>
                    <button onclick="usarPlantilla(this)" data-txt="¡Nos disculpamos! 🙏 Vamos a compensarte con un guacamole gratis en tu próximo pedido. ¿Te parece bien?" style="font-size:11px;border:1px solid #2D5016;border-radius:16px;padding:3px 10px;background:transparent;color:#2D5016;cursor:pointer">🥑 Guacamole gratis</button>
                    <button onclick="usarPlantilla(this)" data-txt="Chilanguit@, para compensar el inconveniente te regalamos el delivery gratis en tu próximo pedido. Disculpa las molestias 🙏" style="font-size:11px;border:1px solid #2D5016;border-radius:16px;padding:3px 10px;background:transparent;color:#2D5016;cursor:pointer">🛵 Delivery gratis</button>
                    <button onclick="usarPlantilla(this)" data-txt="¡Acá estamos! Cuéntame qué pasó para poder ayudarte mejor 🌮" style="font-size:11px;border:1px solid #555;border-radius:16px;padding:3px 10px;background:transparent;color:#555;cursor:pointer">💬 Pedir detalle</button>
                </div>` : ''}}
                <div class="chat-input-area">
                    <input type="text" id="manualInput" class="chat-input"
                           placeholder="Escribe un mensaje al cliente..."
                           onkeydown="if(event.key==='Enter' && !event.shiftKey){{ event.preventDefault(); sendManual('${{esc(phone)}}'); }}">
                    <button id="sendBtn" class="chat-send-btn" onclick="sendManual('${{esc(phone)}}')" title="Enviar">➤</button>
                </div>`;

            const msgsEl = document.getElementById('msgs');
            if (msgsEl) msgsEl.scrollTop = msgsEl.scrollHeight;
            sessionStorage.setItem('activePhone', phone);
        }}

        async function eliminarChat(phone) {{
            if (!confirm(`¿Eliminar el historial de chat de +${{phone}}? Esta acción no se puede deshacer.`)) return;
            const r = await fetch('/api/conversations/delete', {{
                method: 'POST', credentials: 'same-origin',
                headers: {{'Content-Type':'application/json'}},
                body: JSON.stringify({{phone}})
            }});
            if ((await r.json()).status === 'ok') {{
                document.getElementById('chatPanel').innerHTML = '<div style="padding:40px;text-align:center;color:#aaa">Selecciona una conversación</div>';
                sessionStorage.removeItem('activePhone');
                pollConversaciones();
            }}
        }}

        async function reactivarBot(phone) {{
            if (!confirm('¿Reactivar el bot para este cliente? Volverá a responder automáticamente.')) return;
            await fetch('/api/conversations/reactivar', {{
                method: 'POST', credentials: 'same-origin',
                headers: {{'Content-Type':'application/json'}},
                body: JSON.stringify({{phone}})
            }});
            escaladoMap[phone] = false;
            showChat(phone);
        }}

        async function pausarBot(phone) {{
            await fetch('/api/conversations/pausar', {{
                method: 'POST', credentials: 'same-origin',
                headers: {{'Content-Type':'application/json'}},
                body: JSON.stringify({{phone}})
            }});
            escaladoMap[phone] = true;
            showChat(phone);
        }}

        function usarPlantilla(btn) {{
            const inp = document.getElementById('manualInput');
            if (inp) {{ inp.value = btn.dataset.txt; inp.focus(); }}
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
                // Sincronizar mapa de escalados
                if (data.escalado) Object.assign(escaladoMap, data.escalado);
                // Actualizar sidebar
                const lista = document.querySelector('.sidebar-list');
                if (!lista) return;
                lista.innerHTML = data.contacts_html || '';
                // Restaurar visibilidad de checkboxes si estamos en modo selección
                if (modoSeleccion) {{
                    document.querySelectorAll('.conv-chk').forEach(chk => chk.style.display = 'block');
                }}
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

        /* ── Selección múltiple ── */
        let modoSeleccion = false;

        function toggleSelectAll(checked) {{
            modoSeleccion = true;
            document.querySelectorAll('.conv-chk').forEach(chk => {{
                chk.style.display = 'block';
                chk.checked = checked;
            }});
            actualizarBulkBar();
        }}

        function contactClick(e, phone) {{
            if (modoSeleccion) {{
                const chk = document.querySelector(`.conv-chk[data-phone="${{phone}}"]`);
                if (chk) {{ chk.checked = !chk.checked; onChkChange(); }}
            }} else {{
                showChat(phone);
            }}
        }}

        function onChkChange() {{
            modoSeleccion = true;
            document.querySelectorAll('.conv-chk').forEach(chk => chk.style.display = 'block');
            actualizarBulkBar();
        }}

        function actualizarBulkBar() {{
            const seleccionadas = document.querySelectorAll('.conv-chk:checked');
            const bar = document.getElementById('bulkBar');
            const count = document.getElementById('bulkCount');
            bar.style.cssText = seleccionadas.length > 0
                ? 'display:flex;padding:6px 12px;background:#fff3e0;border-bottom:1px solid #ffe082;align-items:center;gap:8px;flex-shrink:0'
                : 'display:none';
            count.textContent = seleccionadas.length + ' seleccionada' + (seleccionadas.length > 1 ? 's' : '');
            // Sync checkbox "Todas"
            const total = document.querySelectorAll('.conv-chk').length;
            document.getElementById('chkSelectAll').checked = seleccionadas.length === total && total > 0;
            document.getElementById('chkSelectAll').indeterminate = seleccionadas.length > 0 && seleccionadas.length < total;
        }}

        function cancelarSeleccion() {{
            modoSeleccion = false;
            document.querySelectorAll('.conv-chk').forEach(chk => {{
                chk.checked = false;
                chk.style.display = 'none';
            }});
            document.getElementById('bulkBar').style.display = 'none';
            document.getElementById('chkSelectAll').checked = false;
            document.getElementById('chkSelectAll').indeterminate = false;
        }}

        async function eliminarSeleccionadas() {{
            const seleccionadas = [...document.querySelectorAll('.conv-chk:checked')];
            if (seleccionadas.length === 0) return;
            if (!confirm(`¿Eliminar ${{seleccionadas.length}} conversación${{seleccionadas.length > 1 ? 'es' : ''}}? Esta acción no se puede deshacer.`)) return;
            const phones = seleccionadas.map(chk => chk.dataset.phone);
            const r = await fetch('/api/conversations/delete-bulk', {{
                method: 'POST', credentials: 'same-origin',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{phones}})
            }});
            if ((await r.json()).status === 'ok') {{
                cancelarSeleccion();
                document.getElementById('chatPanel').innerHTML = '<div class="empty-state"><img src="/static/logo.png" alt=""><span>Selecciona una conversación</span></div>';
                sessionStorage.removeItem('activePhone');
                pollConversaciones();
            }}
        }}
    </script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
