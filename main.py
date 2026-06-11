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
                     "hi", "hello", "hey", "ola", "2"}

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
        # CTA inmediato después de la carta (solo en horario de atención)
        if esta_en_horario():
            await send_whatsapp_buttons(
                phone,
                "¿Se te antojó algo? 👇",
                [{"id": "hacer_pedido", "title": "🛵 Hacer un pedido"}],
                sending_id,
            )
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


def _enrich_pedidos(pedidos: list) -> list:
    """Normaliza estados antiguos (sin emoji) y agrega campos derivados para el frontend."""
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
        p["siguiente_estado_raw"] = sig
        p["es_recojo"] = (p.get("direccion") or "").strip().lower() == "recojo"
    return pedidos


@app.post("/admin/test-notify")
async def test_notify(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    from orders import _notify_owner
    from datetime import datetime, timezone, timedelta
    PERU_TZ = timezone(timedelta(hours=-5))
    now = datetime.now(PERU_TZ)
    await _notify_owner("TEST", "Mensaje de prueba 🌮", "S/ 0.00", "Efectivo", now)
    return JSONResponse({"status": "ok", "mensaje": "Notificación enviada — revisa los logs de Railway para ver si hubo error"})


# Plantilla del panel — SIN f-string: los tokens __X__ se reemplazan en pedidos_panel().
# Así el CSS/JS no necesita llaves escapadas y es mucho más fácil de mantener.
_PEDIDOS_TEMPLATE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<title>Pedidos — Chilango</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="/static/logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.31.0/dist/tabler-icons.min.css">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --brand:#2D5016; --brand-dark:#22400F; --brand-soft:#EAF3DE;
  --bg:#F6F7F5; --surface:#FFFFFF;
  --border:#E4E6E2; --border2:#D5D8D2;
  --text:#1E221B; --text2:#5A5F56; --text3:#8C9186;
  --blue:#1A5DA8; --blue-bg:#E8F1FB;
  --amber:#8A5A0B; --amber-mid:#B97A10; --amber-bg:#FBF0DC;
  --violet:#5B3E9E; --violet-bg:#EFEAFA;
  --green:#2E6B2E; --green-bg:#E6F2E6;
  --red:#B3362C; --red-bg:#FBEAE8;
  --radius:12px;
}
body{font-family:'Inter',-apple-system,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;font-size:14px}

/* ── Header ── */
.hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 20px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:60}
.hdr img{height:36px;width:36px;border-radius:8px;object-fit:cover}
.hdr-title{margin-right:auto;min-width:0}
.hdr-title h1{font-size:15px;font-weight:600;letter-spacing:-.2px}
.hdr-title small{font-size:12px;color:var(--text3)}
.hdr-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.searchwrap{display:flex;align-items:center;gap:7px;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:0 10px;height:34px;min-width:190px}
.searchwrap i{color:var(--text3);font-size:15px}
.searchwrap input{border:none;outline:none;background:transparent;font:inherit;font-size:13px;flex:1;color:var(--text);min-width:0}
select.ctl{height:34px;border:1px solid var(--border);border-radius:8px;background:var(--surface);padding:0 8px;font:inherit;font-size:13px;color:var(--text2);cursor:pointer;outline:none}
.iconbtn{height:34px;width:34px;display:flex;align-items:center;justify-content:center;border:1px solid var(--border);background:var(--surface);border-radius:8px;cursor:pointer;color:var(--text2);font-size:17px;transition:background .15s}
.iconbtn:hover{background:var(--bg)}
.iconbtn.off{color:var(--text3)}

/* ── Nav ── */
.nav{background:var(--surface);border-bottom:1px solid var(--border);display:flex;overflow-x:auto;-webkit-overflow-scrolling:touch;padding:0 12px}
.nav a{display:flex;align-items:center;gap:6px;color:var(--text2);text-decoration:none;padding:10px 14px;font-size:13px;white-space:nowrap;border-bottom:2px solid transparent}
.nav a i{font-size:16px}
.nav a:hover{color:var(--text)}
.nav a.active{color:var(--brand);border-bottom-color:var(--brand);font-weight:600}
.nav-badge{background:var(--red);color:#fff;border-radius:999px;min-width:17px;height:17px;font-size:10px;font-weight:600;display:none;align-items:center;justify-content:center;padding:0 5px}

/* ── Layout ── */
.wrap{max-width:1100px;margin:0 auto;padding:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;margin-bottom:14px}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px}
.kpi .lbl{font-size:12px;color:var(--text2)}
.kpi .val{font-size:20px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.kpi .val small{font-size:12px;font-weight:400;color:var(--text3)}

.controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.seg{display:inline-flex;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:3px;gap:2px;overflow-x:auto;max-width:100%}
.seg button{border:none;background:transparent;color:var(--text2);padding:6px 12px;border-radius:7px;cursor:pointer;font:inherit;font-size:12.5px;white-space:nowrap;transition:background .15s}
.seg button:hover{background:var(--bg)}
.seg button.active{background:var(--brand);color:#fff;font-weight:600}
.btn-ghost{display:flex;align-items:center;gap:6px;height:34px;padding:0 14px;border:1px solid var(--border);background:var(--surface);border-radius:8px;cursor:pointer;font:inherit;font-size:13px;color:var(--text2);margin-left:auto}
.btn-ghost:hover{background:var(--bg)}
.btn-ghost.danger{color:var(--red);border-color:#F0CBC7}

.pausa-banner{background:var(--red-bg);border:1px solid #F0CBC7;color:var(--red);border-radius:var(--radius);padding:9px 14px;font-size:13px;font-weight:600;margin:14px auto 0;max-width:1068px;display:flex;align-items:center;gap:8px}

/* ── Agotados ── */
.agotados{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:12px;overflow:hidden}
.ag-head{display:flex;align-items:center;gap:8px;width:100%;border:none;background:transparent;padding:10px 14px;cursor:pointer;font:inherit;font-size:13px;color:var(--text2);font-weight:600}
.ag-head .warnico{color:var(--amber-mid);font-size:16px}
.ag-head .cnt{background:var(--amber-bg);color:var(--amber);border-radius:999px;padding:1px 8px;font-size:11.5px;font-weight:600;display:none}
.ag-head .chev{margin-left:auto;transition:transform .2s;font-size:16px}
.agotados.open .chev{transform:rotate(180deg)}
.ag-body{display:none;padding:0 14px 12px;align-items:center;gap:8px;flex-wrap:wrap}
.agotados.open .ag-body{display:flex}
.ag-chip{font-size:12px;border:1px solid var(--border2);border-radius:999px;padding:4px 12px;cursor:pointer;background:var(--surface);color:var(--text2);font-family:inherit;transition:all .15s}
.ag-chip.on{background:var(--amber-bg);border-color:var(--amber-mid);color:var(--amber);font-weight:600}
.ag-body input{flex:1;min-width:150px;border:1px solid var(--border);border-radius:8px;padding:7px 10px;font:inherit;font-size:13px;outline:none}
.ag-body input:focus{border-color:var(--brand)}
.ag-save{border:none;background:var(--brand);color:#fff;border-radius:8px;padding:7px 14px;font:inherit;font-size:12.5px;font-weight:600;cursor:pointer}
.ag-save:hover{background:var(--brand-dark)}

/* ── Consultas de costo ── */
.cost-banner{background:var(--blue-bg);border:1px solid #C7DCF2;border-radius:var(--radius);padding:12px 14px;display:none;margin-bottom:12px}
.cost-banner-title{font-size:13px;font-weight:600;color:var(--blue);margin-bottom:10px;display:flex;align-items:center;gap:6px}
.cost-badge{background:var(--blue);color:#fff;border-radius:999px;padding:1px 8px;font-size:11px}
.cost-card{background:var(--surface);border-radius:10px;padding:12px 14px;margin-bottom:8px;border:1px solid #C7DCF2;display:flex;flex-wrap:wrap;align-items:flex-start;gap:10px}
.cost-card:last-child{margin-bottom:0}
.cost-info{flex:1;min-width:200px;font-size:13px;color:var(--text);line-height:1.7}
.cost-info strong{color:var(--blue)}
.cost-sugg{font-size:11px;background:var(--green-bg);color:var(--green);border-radius:6px;padding:3px 8px;display:inline-block;margin-top:2px;font-weight:600}
.cost-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.cost-input{border:1px solid #C7DCF2;border-radius:8px;padding:8px 12px;font:inherit;font-size:14px;width:120px;outline:none;font-weight:600;color:var(--blue)}
.cost-input:focus{border-color:var(--blue)}
.cost-btn{background:var(--blue);color:#fff;border:none;border-radius:8px;padding:9px 16px;font:inherit;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.cost-btn:hover{opacity:.92}

/* ── Grid + tarjetas ── */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:12px;align-items:start}
@keyframes card-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes pulse-demora{0%,100%{opacity:1}50%{opacity:.55}}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);animation:card-in .25s ease-out;display:flex;flex-direction:column;position:relative}
.card.hidden{display:none}

.oc-hdr{padding:12px 14px 8px;display:flex;justify-content:space-between;gap:8px;align-items:flex-start}
.oc-title{font-size:15px;font-weight:600}
.oc-title .num{color:var(--brand)}
.oc-meta{font-size:12px;color:var(--text3);margin-top:3px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.oc-meta i{font-size:13px;vertical-align:-1px}
.oc-meta a{color:var(--brand);text-decoration:none;font-weight:600}
.oc-meta a:hover{text-decoration:underline}
.oc-mod{font-size:10.5px;background:var(--amber-bg);color:var(--amber);padding:1px 7px;border-radius:999px;font-weight:600}
.oc-hdr-right{display:flex;flex-direction:column;align-items:flex-end;gap:5px;flex-shrink:0}
.st{font-size:11.5px;font-weight:600;padding:3px 10px;border-radius:999px;white-space:nowrap}
.st-nuevo{background:var(--blue-bg);color:var(--blue)}
.st-prep{background:var(--amber-bg);color:var(--amber)}
.st-camino{background:var(--violet-bg);color:var(--violet)}
.st-done{background:var(--green-bg);color:var(--green)}
.st-cancel{background:var(--red-bg);color:var(--red)}
.oc-elapsed{font-size:11.5px;color:var(--text3);display:flex;align-items:center;gap:4px}
.oc-elapsed i{font-size:12px}
.oc-elapsed.warn{color:var(--amber-mid);font-weight:600}
.oc-elapsed.late{color:var(--red);font-weight:600;animation:pulse-demora 1.5s ease-in-out infinite}

.oc-progress{display:flex;align-items:center;padding:6px 14px 8px}
.oc-step{display:flex;flex-direction:column;align-items:center;gap:3px;min-width:0}
.oc-step span{font-size:10px;color:var(--text3);white-space:nowrap;max-width:74px;overflow:hidden;text-overflow:ellipsis}
.oc-dot{width:9px;height:9px;border-radius:50%;background:var(--border2);transition:background .3s}
.oc-step.s-done .oc-dot{background:var(--green)}
.oc-step.s-active .oc-dot{background:var(--brand);box-shadow:0 0 0 3px var(--brand-soft)}
.oc-step.s-done span,.oc-step.s-active span{color:var(--text);font-weight:600}
.oc-line{flex:1;height:2px;background:var(--border);margin:0 4px 13px}
.oc-line.done{background:var(--green)}

.oc-body{padding:4px 14px 10px;flex:1}
.oc-items{font-size:13.5px;line-height:1.65;word-break:break-word}
.oc-note{display:inline-block;margin-top:6px;font-size:12px;background:var(--amber-bg);color:var(--amber);padding:2px 9px;border-radius:6px}
.oc-info{border-top:1px solid var(--border);padding:9px 14px;font-size:12.5px;color:var(--text2);display:flex;flex-direction:column;gap:5px}
.oc-info .row{display:flex;align-items:flex-start;gap:7px}
.oc-info i{font-size:14px;color:var(--text3);margin-top:1px}
.oc-info a{color:var(--text2);text-decoration:none;border-bottom:1px dashed var(--border2)}
.oc-info a:hover{color:var(--blue);border-color:var(--blue)}
.sin-dir{color:var(--text3);font-style:italic}

.altoke{margin:10px 14px 0;font-size:12px;font-weight:600;border-radius:8px;padding:7px 10px;display:flex;gap:7px;align-items:flex-start;line-height:1.5}
.altoke i{font-size:14px;margin-top:1px;flex-shrink:0}
.altoke.a-green{background:var(--green-bg);color:var(--green)}
.altoke.a-violet{background:var(--violet-bg);color:var(--violet)}
.altoke.a-amber{background:var(--amber-bg);color:var(--amber)}

.oc-foot{border-top:1px solid var(--border);padding:10px 14px;margin-top:10px;display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.oc-pay{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.oc-total{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums}
.pay{font-size:11px;font-weight:600;padding:2px 9px;border-radius:999px;white-space:nowrap}
.pay.digital{background:var(--violet-bg);color:var(--violet)}
.pay.cobrar{background:var(--amber-bg);color:var(--amber)}
.pay.dinc{background:var(--blue-bg);color:var(--blue)}
.oc-foot-actions{display:flex;gap:6px;align-items:center}
.btn-primary{display:flex;align-items:center;gap:6px;background:var(--brand);color:#fff;border:none;border-radius:8px;height:32px;padding:0 13px;font:inherit;font-size:12.5px;font-weight:600;cursor:pointer;white-space:nowrap}
.btn-primary i{font-size:15px}
.btn-primary:hover{background:var(--brand-dark)}
.btn-primary:disabled{background:var(--border2);cursor:default}
.oc-done-lbl{font-size:12.5px;color:var(--text3);font-weight:600}

.menu-wrap{position:relative}
.dropdown{position:absolute;right:0;top:calc(100% + 4px);background:var(--surface);border:1px solid var(--border);border-radius:10px;box-shadow:0 8px 24px rgba(20,24,16,.12);min-width:190px;padding:4px;display:none;z-index:80}
.dropdown.open{display:block}
.dropdown button{display:flex;align-items:center;gap:8px;width:100%;border:none;background:transparent;padding:8px 10px;border-radius:7px;font:inherit;font-size:12.5px;color:var(--text);cursor:pointer;text-align:left}
.dropdown button i{font-size:15px;color:var(--text3)}
.dropdown button:hover{background:var(--bg)}
.dropdown button.danger,.dropdown button.danger i{color:var(--red)}
.dropdown hr{border:none;border-top:1px solid var(--border);margin:4px 6px}

/* ── Misc ── */
.empty{text-align:center;padding:50px 20px 60px;color:var(--text3);font-size:14px;grid-column:1/-1}
.empty i{display:block;font-size:40px;margin-bottom:10px;color:var(--border2)}
.footer-note{text-align:center;font-size:11.5px;color:var(--text3);padding:14px}
.toast{position:fixed;bottom:24px;right:24px;background:#1E221B;color:#fff;padding:11px 20px;border-radius:10px;font-size:13px;font-weight:600;box-shadow:0 8px 24px rgba(0,0,0,.25);z-index:200;transform:translateY(80px);opacity:0;transition:all .35s cubic-bezier(.34,1.56,.64,1);display:flex;align-items:center;gap:8px}
.toast.show{transform:translateY(0);opacity:1}

/* ── Modal delivery ── */
.dlv-overlay{display:none;position:fixed;inset:0;background:rgba(20,24,16,.45);z-index:300;align-items:center;justify-content:center}
.dlv-box{background:var(--surface);border-radius:14px;padding:20px 24px;min-width:280px;border:1px solid var(--border)}
.dlv-box h3{font-size:14px;font-weight:600;margin-bottom:14px;color:var(--text);display:flex;align-items:center;gap:7px}
.dlv-option{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:10px;cursor:pointer;border:1px solid var(--border);margin-bottom:8px;transition:border-color .15s,background .15s}
.dlv-option:hover{background:var(--bg)}
.dlv-option input[type=radio]{accent-color:var(--brand);width:15px;height:15px;cursor:pointer}
.dlv-option label{font-size:13.5px;font-weight:600;cursor:pointer;flex:1}
.dlv-btns{display:flex;gap:8px;margin-top:14px;justify-content:flex-end}
.dlv-cancel{background:transparent;border:1px solid var(--border2);color:var(--text2);padding:8px 16px;border-radius:8px;cursor:pointer;font:inherit;font-size:13px}
.dlv-confirm{background:var(--brand);color:#fff;border:none;padding:8px 18px;border-radius:8px;cursor:pointer;font:inherit;font-size:13px;font-weight:600}

@media(max-width:640px){
  .hdr{padding:8px 12px}
  .hdr-title small{display:none}
  .searchwrap{min-width:130px}
  .wrap{padding:12px 10px}
  .kpis{grid-template-columns:repeat(2,1fr)}
}
</style>
</head>
<body>

<header class="hdr">
  <img src="/static/logo.png" alt="Chilango">
  <div class="hdr-title"><h1>Chilango</h1><small>Panel de operaciones · __FECHA_LABEL__</small></div>
  <div class="hdr-actions">
    <div class="searchwrap"><i class="ti ti-search"></i><input id="searchBox" type="search" placeholder="Buscar #, teléfono, producto…" oninput="setSearch(this.value)"></div>
    <select id="fechaSelect" class="ctl" onchange="if(this.value)location.href='/pedidos?fecha='+encodeURIComponent(this.value)">__FECHAS_OPTIONS__</select>
    <button class="iconbtn" id="btnSound" onclick="toggleSound()" title="Sonido y notificaciones de pedidos nuevos"><i class="ti ti-bell"></i></button>
    <div class="menu-wrap">
      <button class="iconbtn" onclick="toggleMenu('settingsMenu', event)" title="Más opciones"><i class="ti ti-dots-vertical"></i></button>
      <div class="dropdown" id="settingsMenu">
        <button onclick="probarNotif()"><i class="ti ti-bell-ringing"></i> Probar notificación</button>
      </div>
    </div>
  </div>
</header>

<nav class="nav">
  <a href="/pedidos" class="active"><i class="ti ti-package"></i> Pedidos <span class="nav-badge" id="navBadge">0</span></a>
  <a href="/admin"><i class="ti ti-message-circle"></i> Conversaciones</a>
  <a href="/admin/clientes"><i class="ti ti-users"></i> Clientes</a>
  <a href="/admin/metricas"><i class="ti ti-chart-bar"></i> Métricas</a>
  <a href="/admin/zonas-delivery"><i class="ti ti-motorbike"></i> Zonas</a>
  <a href="/admin/menu"><i class="ti ti-tools-kitchen-2"></i> Menú</a>
</nav>

__PAUSA_BANNER__

<main class="wrap">

  <div class="kpis">
    <div class="kpi"><div class="lbl">Ventas de hoy</div><div class="val" id="chipTotal">S/ __TOTAL_DIA__</div></div>
    <div class="kpi"><div class="lbl">Pedidos</div><div class="val"><span id="totalCount">__N_PEDIDOS__</span> <small>· <span id="activosCount">__N_ACTIVOS__</span> activos</small></div></div>
    <div class="kpi"><div class="lbl">Yape / Plin</div><div class="val" id="cntYapePlin">__CNT_YP__</div></div>
    <div class="kpi"><div class="lbl">Efectivo</div><div class="val" id="cntEfec">__CNT_EF__</div></div>
  </div>

  <div class="controls">
    <div class="seg" id="filterSeg">
      <button class="active" data-estado="all" onclick="filterCards('all',this)">Todos</button>
      <button data-estado="Nuevo 🆕" onclick="filterCards('Nuevo 🆕',this)">Nuevos</button>
      <button data-estado="En preparación 👨‍🍳" onclick="filterCards('En preparación 👨‍🍳',this)">Preparación</button>
      <button data-estado="En camino 🛵" onclick="filterCards('En camino 🛵',this)">En camino</button>
      <button data-estado="Entregado ✅" onclick="filterCards('Entregado ✅',this)">Entregados</button>
      <button data-estado="Cancelado ❌" onclick="filterCards('Cancelado ❌',this)">Cancelados</button>
    </div>
    <button class="btn-ghost __PAUSA_CLS__" id="btnPausa" onclick="togglePausa()" data-pausado="__PAUSA_DATA__">__PAUSA_LABEL__</button>
  </div>

  <div class="agotados __AGOTADOS_OPEN__" id="agotadosBar">
    <button class="ag-head" onclick="document.getElementById('agotadosBar').classList.toggle('open')">
      <i class="ti ti-alert-triangle warnico"></i> Productos agotados
      <span class="cnt" id="agCount"></span>
      <i class="ti ti-chevron-down chev"></i>
    </button>
    <div class="ag-body">
      __AGOTADOS_CHIPS__
      <input id="agotadosInput" type="text" value="__AGOTADOS_VAL__" placeholder="otros… (separados por coma)" onkeydown="if(event.key==='Enter')guardarAgotados()">
      <button class="ag-save" onclick="guardarAgotados()">Guardar</button>
      <span id="agotadosStatus" style="font-size:12px;color:var(--green);display:none"><i class="ti ti-check"></i> Guardado</span>
    </div>
  </div>

  <div class="cost-banner" id="costBanner">
    <div class="cost-banner-title"><i class="ti ti-motorbike"></i> Consultas de costo pendientes <span class="cost-badge" id="costBadge">0</span></div>
    <div id="costList"></div>
  </div>

  <div class="grid" id="ordersGrid"></div>
  <div class="footer-note" id="lastRefresh">Actualización automática cada 10 s</div>
</main>

<div class="toast" id="toast"></div>

<div class="dlv-overlay" id="dlvModal" onclick="if(event.target===this)closeDlvModal()">
  <div class="dlv-box">
    <h3><i class="ti ti-motorbike"></i> <span id="dlvTitle">Llamar delivery</span></h3>
    <input type="hidden" id="dlvOrderId" value="">
    <div id="dlvOpts"></div>
    <div id="dlvManual" style="display:none">
      <p style="font-size:13px;color:var(--text2);margin-bottom:8px">Número WhatsApp del motorizado (sin +):</p>
      <input id="dlvPhoneInput" type="tel" placeholder="ej: 51987654321"
             style="width:100%;border:1px solid var(--border);border-radius:8px;padding:9px 12px;font:inherit;font-size:14px;outline:none"
             onkeydown="if(event.key==='Enter')confirmarDelivery()">
    </div>
    <div class="dlv-btns">
      <button class="dlv-cancel" onclick="closeDlvModal()">Cancelar</button>
      <button class="dlv-confirm" onclick="confirmarDelivery()">Enviar</button>
    </div>
  </div>
</div>

<script>
const INIT_PEDIDOS = __PEDIDOS_JSON__;
const DELIVERIES   = __DELIVERIES_JS__;
const STEP_IDX = {"Nuevo 🆕":0,"En preparación 👨‍🍳":1,"En camino 🛵":2,"Entregado ✅":3};
const STEP_LBL = ["Nuevo","Preparación","En camino","Entregado"];
const ST_META = {
  "Nuevo 🆕":            {label:"Nuevo",          cls:"st-nuevo",  nextLabel:"Empezar preparación", nextIcon:"ti-chef-hat"},
  "En preparación 👨‍🍳": {label:"En preparación", cls:"st-prep",   nextLabel:"Marcar en camino",    nextIcon:"ti-motorbike"},
  "En camino 🛵":        {label:"En camino",      cls:"st-camino", nextLabel:"Marcar entregado",    nextIcon:"ti-check"},
  "Entregado ✅":        {label:"Entregado",      cls:"st-done"},
  "Cancelado ❌":        {label:"Cancelado",      cls:"st-cancel"},
};
const TAB_LABELS = {"all":"Todos","Nuevo 🆕":"Nuevos","En preparación 👨‍🍳":"Preparación","En camino 🛵":"En camino","Entregado ✅":"Entregados","Cancelado ❌":"Cancelados"};

let knownIds  = new Set();
let curFilter = 'all';
let audioCtx  = null;
let searchQ   = '';
let lastPayload  = '';
let lastRenderTs = 0;
let soundOn = localStorage.getItem('ch_sound') !== '0';

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* ── Sonido ── */
function playBeep() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    [880, 1100, 1320].forEach((f, i) => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.connect(g); g.connect(audioCtx.destination);
      o.type = 'sine';
      o.frequency.value = f;
      g.gain.setValueAtTime(0.25, audioCtx.currentTime + i*0.12);
      g.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + i*0.12 + 0.25);
      o.start(audioCtx.currentTime + i*0.12);
      o.stop(audioCtx.currentTime + i*0.12 + 0.3);
    });
  } catch(e) {}
}

function initSoundBtn() {
  const b = document.getElementById('btnSound');
  if (!b) return;
  b.classList.toggle('off', !soundOn);
  b.innerHTML = soundOn ? '<i class="ti ti-bell"></i>' : '<i class="ti ti-bell-off"></i>';
}
function toggleSound() {
  soundOn = !soundOn;
  localStorage.setItem('ch_sound', soundOn ? '1' : '0');
  if (soundOn) {
    playBeep();
    if ('Notification' in window && Notification.permission === 'default') {
      Notification.requestPermission();
    }
  }
  initSoundBtn();
}
initSoundBtn();

function notifyBrowser(msg) {
  if (!('Notification' in window) || Notification.permission !== 'granted' || !document.hidden) return;
  try {
    const n = new Notification('Chilango — Pedidos', { body: msg, tag: 'chilango-new-order' });
    n.onclick = () => { window.focus(); n.close(); };
  } catch(e) {}
}

/* ── Toast ── */
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3500);
}

/* ── Menús desplegables ── */
function toggleMenu(id, ev) {
  if (ev) ev.stopPropagation();
  document.querySelectorAll('.dropdown.open').forEach(d => { if (d.id !== id) d.classList.remove('open'); });
  const el = document.getElementById(id);
  if (el) el.classList.toggle('open');
}
document.addEventListener('click', () => {
  document.querySelectorAll('.dropdown.open').forEach(d => d.classList.remove('open'));
});

/* ── Filtro por estado + búsqueda ── */
function applyFilters() {
  document.querySelectorAll('.card').forEach(c => {
    const matchEstado = curFilter === 'all' || c.dataset.estado === curFilter;
    const matchQ = !searchQ || c.textContent.toLowerCase().includes(searchQ);
    c.classList.toggle('hidden', !(matchEstado && matchQ));
  });
}
function filterCards(estado, btn) {
  curFilter = estado;
  document.querySelectorAll('#filterSeg button').forEach(t => t.classList.remove('active'));
  if (btn) btn.classList.add('active');
  applyFilters();
}
function setSearch(q) {
  searchQ = (q || '').trim().toLowerCase();
  applyFilters();
}

/* ── Acciones AJAX ── */
async function cambiarEstado(id, nuevoEstado) {
  const card = document.getElementById('card-' + id);
  const btn  = card ? card.querySelector('.btn-primary') : null;
  if (btn) { btn.disabled = true; btn.innerHTML = '<i class="ti ti-loader-2"></i> …'; }
  try {
    const r = await fetch('/api/pedidos/estado', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({order_id: id, estado: nuevoEstado})
    });
    if (r.status === 401) { location.reload(); return; }
    if (!r.ok) throw new Error(await r.text());
    await refreshOrders(true);
  } catch(e) {
    alert('Error al actualizar: ' + e.message);
    await refreshOrders(true);
  }
}

async function cancelarPedido(id) {
  if (!confirm('¿Cancelar el pedido #' + id + '? Esta acción no se puede deshacer.')) return;
  try {
    const r = await fetch('/api/pedidos/estado', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({order_id: id, estado: 'Cancelado ❌'})
    });
    if (r.status === 401) { location.reload(); return; }
    if (!r.ok) throw new Error(await r.text());
    await refreshOrders(true);
  } catch(e) {
    alert('Error al cancelar: ' + e.message);
  }
}

async function eliminarPedido(id) {
  if (!confirm('¿Eliminar este pedido? No se puede deshacer.')) return;
  try {
    const r = await fetch('/api/pedidos/eliminar', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({order_id: id})
    });
    if (r.status === 401) { location.reload(); return; }
    const card = document.getElementById('card-' + id);
    if (card) { card.style.opacity='0'; setTimeout(()=>card.remove(),300); }
    knownIds.delete(id);
  } catch(e) { alert('Error al eliminar: ' + e.message); }
}

async function llamarDelivery(orderId) {
  try {
    const r = await fetch('/api/pedidos/llamar-delivery', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({order_id: orderId})
    });
    if (r.status === 401) { location.reload(); return; }
    const data = await r.json();
    if (data.status === 'ok') {
      showToast('Solicitud de delivery en curso');
    } else {
      alert('Error: ' + (data.msg || 'No se pudo enviar'));
    }
  } catch(e) {
    alert('Error: ' + e.message);
  }
}

async function avisarListo(orderId) {
  try {
    const r = await fetch('/api/pedidos/aviso-listo', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({order_id: orderId})
    });
    if (r.status === 401) { location.reload(); return; }
    const data = await r.json();
    if (data.status === 'ok') {
      showToast('Cliente notificado — pedido listo esperando delivery');
    } else {
      alert('Error: ' + (data.msg || 'No se pudo enviar'));
    }
  } catch(e) {
    alert('Error: ' + e.message);
  }
}

/* ── Modal delivery (consulta de costo manual) ── */
function _openDlvModal(orderId, mode) {
  document.getElementById('dlvOrderId').value = orderId;
  const modal = document.getElementById('dlvModal');
  modal.dataset.mode = mode;
  document.getElementById('dlvTitle').textContent = mode === 'cost' ? 'Consultar costo delivery' : 'Llamar delivery';
  const optsEl = document.getElementById('dlvOpts');
  optsEl.innerHTML = '';
  if (DELIVERIES && DELIVERIES.length > 0) {
    document.getElementById('dlvManual').style.display = 'none';
    DELIVERIES.forEach((d, i) => {
      optsEl.innerHTML += `<div class="dlv-option">
        <input type="radio" name="dlvChoice" id="dlv${i}" value="${d.phone}" ${i===0?'checked':''}>
        <label for="dlv${i}">${esc(d.name)}</label>
      </div>`;
    });
  } else {
    document.getElementById('dlvManual').style.display = 'block';
    document.getElementById('dlvPhoneInput').value = '';
  }
  modal.style.display = 'flex';
  setTimeout(() => {
    const inp = document.getElementById('dlvPhoneInput');
    if (inp && inp.offsetParent) inp.focus();
  }, 100);
}

function consultarCostoDelivery(orderId) { _openDlvModal(orderId, 'cost'); }

function closeDlvModal() {
  document.getElementById('dlvModal').style.display = 'none';
}

async function _enviarDelivery(orderId, phone, name, endpoint, toastPrefix) {
  try {
    const r = await fetch(endpoint, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({order_id: orderId, delivery_phone: phone})
    });
    if (r.status === 401) { location.reload(); return; }
    const data = await r.json();
    if (data.status === 'ok') {
      showToast(toastPrefix + (data.delivery || name));
    } else {
      alert('Error: ' + (data.msg || 'No se pudo enviar'));
    }
  } catch(e) {
    alert('Error: ' + e.message);
  }
}

function confirmarDelivery() {
  const orderId  = +document.getElementById('dlvOrderId').value;
  const mode     = document.getElementById('dlvModal').dataset.mode || 'delivery';
  const endpoint = mode === 'cost' ? '/api/pedidos/consultar-delivery' : '/api/pedidos/llamar-delivery';
  const toastPrefix = mode === 'cost' ? 'Consulta enviada a ' : 'Solicitud enviada a ';
  let phone, name;
  const manualDiv = document.getElementById('dlvManual');
  if (manualDiv && manualDiv.style.display !== 'none') {
    phone = (document.getElementById('dlvPhoneInput').value || '').trim().replace(/[^0-9]/g,'');
    if (!phone || phone.length < 8) { alert('Ingresa un número de WhatsApp válido (solo dígitos)'); return; }
    name = 'Delivery';
  } else {
    const sel = document.querySelector('input[name="dlvChoice"]:checked');
    if (!sel) { alert('Selecciona un servicio de delivery'); return; }
    const d = DELIVERIES.find(d => d.phone === sel.value);
    phone = d.phone; name = d.name;
  }
  closeDlvModal();
  _enviarDelivery(orderId, phone, name, endpoint, toastPrefix);
}

/* ── Construir tarjeta ── */
function buildCard(p) {
  const estado = p.estado || 'Nuevo 🆕';
  const meta   = ST_META[estado] || {label: estado, cls: 'st-nuevo'};
  const sIdx   = STEP_IDX[estado] !== undefined ? STEP_IDX[estado] : -1;

  const steps = STEP_LBL.map((lbl, i) => {
    const cls  = i < sIdx ? 'oc-step s-done' : (i === sIdx ? 'oc-step s-active' : 'oc-step');
    const line = i < STEP_LBL.length - 1
      ? `<div class="oc-line${i < sIdx ? ' done' : ''}"></div>`
      : '';
    return `<div class="${cls}"><div class="oc-dot"></div><span>${lbl}</span></div>${line}`;
  }).join('');

  const esRecojo  = (p.es_recojo === true || p.es_recojo === 1);
  const esCancel  = estado === 'Cancelado ❌';
  const esActivo  = !['Entregado ✅','Cancelado ❌'].includes(estado);
  const siguiente = p.siguiente_estado_raw || null;

  const entrega = esRecojo
    ? '<i class="ti ti-building-store"></i> Recojo'
    : '<i class="ti ti-motorbike"></i> Delivery';
  const modBadge = p.modificado ? '<span class="oc-mod">Editado</span>' : '';

  let elapsedChip = '';
  if (esActivo && p.hora) {
    const [eh, em] = p.hora.split(':').map(Number);
    const nowE = new Date();
    let diffE = (nowE.getHours() * 60 + nowE.getMinutes()) - (eh * 60 + em);
    if (diffE < 0) diffE += 1440;
    if (diffE >= 0 && diffE < 600) {
      const eCls = diffE >= 45 ? 'late' : (diffE >= 25 ? 'warn' : '');
      elapsedChip = `<span class="oc-elapsed ${eCls}"><i class="ti ti-hourglass-high"></i>${diffE} min</span>`;
    }
  }

  const itemsList = (p.items || '').split(/,(?![^(]*\))/).map(s => s.trim()).filter(Boolean);
  const itemsHtml = itemsList.length > 0
    ? itemsList.map(i => esc(i)).join('<br>')
    : esc(p.items || '');

  const notaHtml = (p.notas || '').trim()
    ? `<div><span class="oc-note">Nota: ${esc(p.notas)}</span></div>`
    : '';

  let infoRows = '';
  if (esRecojo) {
    infoRows += `<div class="row"><i class="ti ti-building-store"></i><span>El cliente retira en el local</span></div>`;
  } else if (p.direccion) {
    const mapsUrl = 'https://www.google.com/maps/search/?api=1&query=' + encodeURIComponent(p.direccion + ', Tacna, Perú');
    infoRows += `<div class="row"><i class="ti ti-map-pin"></i><a href="${mapsUrl}" target="_blank" title="Ver en Google Maps">${esc(p.direccion)}</a></div>`;
  } else {
    infoRows += `<div class="row"><i class="ti ti-map-pin"></i><span class="sin-dir">Sin dirección</span></div>`;
  }
  infoRows += `<div class="row"><i class="ti ti-phone"></i><a href="https://wa.me/${esc(p.phone)}" target="_blank" title="Abrir chat de WhatsApp">+${esc(p.phone)}</a></div>`;

  const metodo    = p.metodo_pago || 'Efectivo';
  const esDigital = ['Yape/Plin','Yape','Plin'].includes(metodo);
  const tieneDeliveryPago = (p.items || '').toLowerCase().includes('delivery:');
  const payBadge = esDigital
    ? `<span class="pay digital">${esc(metodo)} · pagado</span>`
    : `<span class="pay cobrar">${esc(metodo)}${esActivo ? ' · cobrar al entregar' : ''}</span>`;
  const dincBadge = tieneDeliveryPago ? '<span class="pay dinc">Delivery pagado</span>' : '';

  let altoke = '';
  if (estado === 'En preparación 👨‍🍳' && !esRecojo) {
    if (esDigital && tieneDeliveryPago) {
      altoke = `<div class="altoke a-green"><i class="ti ti-circle-check"></i><span>Dile al moto: ya pagó TODO — NO cobrar nada al cliente</span></div>`;
    } else if (esDigital) {
      altoke = `<div class="altoke a-violet"><i class="ti ti-device-mobile"></i><span>Dile al moto: pagó en digital — cobrar SOLO el delivery</span></div>`;
    } else {
      altoke = `<div class="altoke a-amber"><i class="ti ti-cash"></i><span>Dile al moto: pago en efectivo — cobrar ${esc(p.total)} + delivery</span></div>`;
    }
  }

  let primaryBtn = '';
  if (esActivo && siguiente) {
    let lbl  = meta.nextLabel || ('Pasar a ' + (ST_META[siguiente] ? ST_META[siguiente].label : siguiente));
    let icon = meta.nextIcon || 'ti-arrow-right';
    if (esRecojo && siguiente === 'En camino 🛵') { lbl = 'Listo p/ retirar'; icon = 'ti-package'; }
    primaryBtn = `<button class="btn-primary" data-next="${esc(siguiente)}" onclick="cambiarEstado(${p.id}, this.dataset.next)"><i class="ti ${icon}"></i>${lbl}</button>`;
  } else if (!esActivo) {
    primaryBtn = `<span class="oc-done-lbl">${esCancel ? 'Cancelado' : 'Entregado'}</span>`;
  }

  let menuItems = '';
  if (esActivo && !esRecojo) {
    menuItems += `<button onclick="llamarDelivery(${p.id})"><i class="ti ti-motorbike"></i> Llamar delivery</button>`;
  }
  if (estado === 'En preparación 👨‍🍳' && !esRecojo) {
    menuItems += `<button onclick="avisarListo(${p.id})"><i class="ti ti-package"></i> Avisar pedido listo</button>`;
  }
  menuItems += `<button onclick="window.open('/admin/imprimir/${p.id}','_blank')"><i class="ti ti-printer"></i> Imprimir recibo</button>`;
  if (esActivo) {
    menuItems += `<hr><button class="danger" onclick="cancelarPedido(${p.id})"><i class="ti ti-x"></i> Cancelar pedido</button>`;
  } else {
    menuItems += `<hr>`;
  }
  menuItems += `<button class="danger" onclick="eliminarPedido(${p.id})"><i class="ti ti-trash"></i> Eliminar</button>`;

  return `<div class="card" id="card-${p.id}" data-estado="${esc(estado)}">
  <div class="oc-hdr">
    <div>
      <div class="oc-title">Pedido <span class="num">#${p.id}</span></div>
      <div class="oc-meta"><span><i class="ti ti-clock"></i> ${esc(p.hora)}</span><span>${entrega}</span>${modBadge}</div>
    </div>
    <div class="oc-hdr-right">
      <span class="st ${meta.cls}">${meta.label}</span>
      ${elapsedChip}
    </div>
  </div>
  <div class="oc-progress">${steps}</div>
  <div class="oc-body">
    <div class="oc-items">${itemsHtml}</div>
    ${notaHtml}
  </div>
  <div class="oc-info">${infoRows}</div>
  ${altoke}
  <div class="oc-foot">
    <div class="oc-pay">
      <span class="oc-total">${esc(p.total)}</span>
      ${payBadge}
      ${dincBadge}
    </div>
    <div class="oc-foot-actions">
      ${primaryBtn}
      <div class="menu-wrap">
        <button class="iconbtn" onclick="toggleMenu('cardMenu-${p.id}', event)" title="Más acciones"><i class="ti ti-dots"></i></button>
        <div class="dropdown" id="cardMenu-${p.id}">${menuItems}</div>
      </div>
    </div>
  </div>
</div>`;
}

/* ── Render + refresh ── */
function processPedidos(pedidos, force) {
  const newOnes = pedidos.filter(p => !knownIds.has(p.id));
  if (newOnes.length > 0 && knownIds.size > 0) {
    if (soundOn) playBeep();
    const msg = newOnes.length === 1
      ? `Pedido #${newOnes[0].id} — ${(newOnes[0].items || '').slice(0, 60)}`
      : `${newOnes.length} nuevos pedidos llegaron`;
    showToast(newOnes.length === 1 ? 'Nuevo pedido llegó' : newOnes.length + ' nuevos pedidos');
    notifyBrowser(msg);
    document.title = '(1) Nuevo pedido — Chilango';
    setTimeout(() => { document.title = 'Pedidos — Chilango'; }, 5000);
  }
  knownIds = new Set(pedidos.map(p => p.id));

  const payload = JSON.stringify(pedidos);
  const changed = payload !== lastPayload;
  const menuAbierto = !!document.querySelector('.dropdown.open');
  const mustRender = force || changed || ((Date.now() - lastRenderTs) > 60000 && !menuAbierto);
  if (mustRender) {
    lastPayload = payload;
    lastRenderTs = Date.now();
    const grid = document.getElementById('ordersGrid');
    if (pedidos.length === 0) {
      const fechaSel2 = document.getElementById('fechaSelect')?.value || '';
      grid.innerHTML = `<div class="empty"><i class="ti ti-clipboard-list"></i>${fechaSel2 ? 'Sin pedidos para ' + esc(fechaSel2) : 'No hay pedidos hoy todavía'}</div>`;
    } else {
      grid.innerHTML = pedidos.map(buildCard).join('');
    }
  }

  const nNuevos = pedidos.filter(p => (p.estado||'').startsWith('Nuevo')).length;
  const navBadge = document.getElementById('navBadge');
  if (navBadge) {
    navBadge.textContent = nNuevos;
    navBadge.style.display = nNuevos > 0 ? 'inline-flex' : 'none';
  }

  const tabCounts = {
    'all': pedidos.length,
    'Nuevo 🆕': nNuevos,
    'En preparación 👨‍🍳': pedidos.filter(p => p.estado === 'En preparación 👨‍🍳').length,
    'En camino 🛵': pedidos.filter(p => p.estado === 'En camino 🛵').length,
    'Entregado ✅': pedidos.filter(p => p.estado === 'Entregado ✅').length,
    'Cancelado ❌': pedidos.filter(p => p.estado === 'Cancelado ❌').length,
  };
  document.querySelectorAll('#filterSeg button').forEach(tab => {
    const e = tab.dataset.estado;
    if (e in tabCounts) tab.textContent = `${TAB_LABELS[e]} ${tabCounts[e]}`;
    if (e === curFilter) tab.classList.add('active');
  });

  applyFilters();

  document.getElementById('totalCount').textContent = pedidos.length;
  const activos = pedidos.filter(p => !['Entregado ✅','Cancelado ❌'].includes(p.estado)).length;
  document.getElementById('activosCount').textContent = activos;

  const noCancel = pedidos.filter(p => (p.estado || '') !== 'Cancelado ❌');
  const totalDia = noCancel.reduce((sum, p) => {
    const t = parseFloat((p.total || '0').replace('S/', '').replace(',','.').trim()) || 0;
    return sum + t;
  }, 0);
  const chipTotal = document.getElementById('chipTotal');
  if (chipTotal) chipTotal.textContent = `S/ ${totalDia.toFixed(2)}`;

  const cntYP = noCancel.filter(p => ['Yape/Plin','Yape','Plin'].includes(p.metodo_pago)).length;
  const cntEf = noCancel.filter(p => !['Yape/Plin','Yape','Plin'].includes(p.metodo_pago)).length;
  const chipYP = document.getElementById('cntYapePlin');
  const chipEf = document.getElementById('cntEfec');
  if (chipYP) chipYP.textContent = cntYP;
  if (chipEf) chipEf.textContent = cntEf;
}

async function refreshOrders(force) {
  try {
    const fechaSel = document.getElementById('fechaSelect')?.value || '';
    const url = fechaSel ? `/api/pedidos?fecha=${encodeURIComponent(fechaSel)}` : '/api/pedidos';
    const r = await fetch(url, {credentials:'same-origin'});
    if (r.status === 401) { location.reload(); return; }
    const data = await r.json();
    processPedidos(data.pedidos, force);
    const now = new Date().toLocaleTimeString('es-PE',{hour:'2-digit',minute:'2-digit'});
    document.getElementById('lastRefresh').textContent = `Actualizado ${now}`;
  } catch(e) {
    console.warn('Refresh error:', e);
  }
}

function probarNotif() {
  fetch('/admin/test-notify', {method:'POST',credentials:'same-origin'})
    .then(r=>r.json())
    .then(()=>showToast('Solicitud enviada — revisa logs de Railway'))
    .catch(e=>alert('Error: '+e));
}

/* ── Agotados ── */
function _agUpdateCount() {
  const inp = document.getElementById('agotadosInput');
  const n = inp.value.split(',').map(s => s.trim()).filter(Boolean).length;
  const c = document.getElementById('agCount');
  c.textContent = n;
  c.style.display = n > 0 ? 'inline-block' : 'none';
}
_agUpdateCount();

function toggleAgotado(btn, item) {
  const inp = document.getElementById('agotadosInput');
  const parts = inp.value.split(',').map(s => s.trim()).filter(Boolean);
  const idx = parts.findIndex(p => p.toLowerCase() === item.toLowerCase());
  if (idx === -1) {
    parts.push(item);
    btn.classList.add('on');
  } else {
    parts.splice(idx, 1);
    btn.classList.remove('on');
  }
  inp.value = parts.join(', ');
  guardarAgotados();
}

async function guardarAgotados() {
  const val = document.getElementById('agotadosInput').value.trim();
  try {
    const r = await fetch('/api/config/agotados', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({value: val})
    });
    const d = await r.json();
    if (d.status === 'ok') {
      const st = document.getElementById('agotadosStatus');
      st.style.display = 'inline';
      setTimeout(() => st.style.display = 'none', 2000);
      _agUpdateCount();
    }
  } catch(e) { alert('Error: ' + e.message); }
}

async function togglePausa() {
  const btn = document.getElementById('btnPausa');
  const pausado = btn.dataset.pausado === '1';
  const nuevo = pausado ? '0' : '1';
  try {
    const r = await fetch('/api/config/pausa', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({value: nuevo})
    });
    const d = await r.json();
    if (d.status === 'ok') location.reload();
  } catch(e) { alert('Error: ' + e.message); }
}

/* ── Consultas de costo pendientes ── */
async function checkPendingCostQueries() {
  try {
    const r = await fetch('/api/delivery/pendientes', {credentials:'same-origin'});
    if (!r.ok) return;
    const data = await r.json();
    renderPendingCostQueries(data.pendientes || []);
  } catch(e) { console.warn('CostQueries error:', e); }
}

function renderPendingCostQueries(queries) {
  const banner = document.getElementById('costBanner');
  const list   = document.getElementById('costList');
  const badge  = document.getElementById('costBadge');
  if (!banner || !list) return;
  if (queries.length === 0) {
    banner.style.display = 'none';
    return;
  }
  banner.style.display = 'block';
  badge.textContent = queries.length;
  list.innerHTML = queries.map(q => {
    const sugg = q.sugerencia
      ? `<span class="cost-sugg">Zona similar: S/ ${q.sugerencia.costo.toFixed(2)} (${q.sugerencia.count} ${q.sugerencia.count === 1 ? 'vez' : 'veces'})</span>`
      : '';
    const inputId = 'costInput_' + q.client_phone;
    return `<div class="cost-card">
      <div class="cost-info">
        <strong>+${esc(q.client_phone)}</strong><br>
        <i class="ti ti-map-pin"></i> ${esc(q.direccion || 'Sin especificar')}<br>
        <i class="ti ti-shopping-cart"></i> ${esc(q.items || '—')}<br>
        Subtotal: <strong>${esc(q.subtotal || '—')}</strong>
        ${sugg}
      </div>
      <div class="cost-actions">
        <input class="cost-input" id="${inputId}" type="number" min="0" step="0.5"
          placeholder="S/ costo…"
          value="${q.sugerencia ? q.sugerencia.costo : ''}"
          onkeydown="if(event.key==='Enter')enviarCostoCliente('${esc(q.client_phone)}','${esc(q.subtotal)}')">
        <button class="cost-btn" onclick="enviarCostoCliente('${esc(q.client_phone)}','${esc(q.subtotal)}')">
          Enviar al cliente
        </button>
      </div>
    </div>`;
  }).join('');
}

async function enviarCostoCliente(phone, subtotalStr) {
  const inputEl = document.getElementById('costInput_' + phone);
  const monto = parseFloat((inputEl ? inputEl.value : '') || '');
  if (isNaN(monto) || monto <= 0) {
    alert('Ingresa un monto de delivery válido (ej: 7)');
    if (inputEl) inputEl.focus();
    return;
  }
  try {
    const r = await fetch('/api/delivery/enviar-costo', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({phone, monto, subtotal: subtotalStr})
    });
    if (r.status === 401) { location.reload(); return; }
    const data = await r.json();
    if (data.status === 'ok') {
      showToast('Costo enviado al cliente +' + phone);
      checkPendingCostQueries();
    } else {
      alert('Error: ' + (data.msg || 'No se pudo enviar'));
    }
  } catch(e) { alert('Error: ' + e.message); }
}

/* ── Inicio ── */
processPedidos(INIT_PEDIDOS, true);
setInterval(refreshOrders, 10000);
checkPendingCostQueries();
setInterval(checkPendingCostQueries, 15000);
</script>
</body></html>"""


@app.get("/pedidos", response_class=HTMLResponse)
async def pedidos_panel(
    credentials: HTTPBasicCredentials = Depends(verificar_admin),
    fecha: str = Query(None)          # ?fecha=DD/MM/YYYY para ver días anteriores
):
    from datetime import datetime, timezone, timedelta
    PERU_TZ = timezone(timedelta(hours=-5))
    hoy = datetime.now(PERU_TZ).strftime("%d/%m/%Y")

    fecha_sel = fecha if fecha else hoy

    if fecha_sel == hoy:
        pedidos = db.get_orders_today()
    else:
        pedidos = db.get_orders_for_date(fecha_sel)
    pedidos = _enrich_pedidos(pedidos)

    fechas_raw = db.get_available_dates()
    fechas_disponibles = fechas_raw if hoy in fechas_raw else [hoy] + fechas_raw

    count_entregado = sum(1 for p in pedidos if p.get("estado") == "Entregado ✅")
    count_cancel    = sum(1 for p in pedidos if p.get("estado") == "Cancelado ❌")
    total_activos   = len(pedidos) - count_entregado - count_cancel

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

    agotados_actual = db.get_config("productos_agotados", "")
    bot_pausado = db.get_config("bot_pausado", "0") == "1"

    fechas_options = "".join(
        f'<option value="{f}" {"selected" if f == fecha_sel else ""}>{f}{" (hoy)" if f == hoy else ""}</option>'
        for f in fechas_disponibles
    )

    agotados_set = {s.strip().lower() for s in agotados_actual.split(",") if s.strip()}
    agotados_chips = "".join(
        f'<button class="ag-chip{" on" if item.lower() in agotados_set else ""}" onclick="toggleAgotado(this,\'{item}\')">{item}</button>'
        for item in ["Pastor", "Suadero", "Chorizo", "Birria", "Chamoyada"]
    )

    if bot_pausado:
        pausa_banner = ('<div class="pausa-banner"><i class="ti ti-player-pause"></i>'
                        ' Bot pausado — los clientes reciben mensaje de capacidad máxima</div>')
        pausa_label = '<i class="ti ti-player-play"></i> Reanudar bot'
        pausa_cls = "danger"
    else:
        pausa_banner = ""
        pausa_label = '<i class="ti ti-player-pause"></i> Pausar bot'
        pausa_cls = ""

    fecha_label = f"Hoy · {fecha_sel}" if fecha_sel == hoy else fecha_sel

    page = (_PEDIDOS_TEMPLATE
            .replace("__FECHA_LABEL__", html.escape(fecha_label))
            .replace("__FECHAS_OPTIONS__", fechas_options)
            .replace("__PAUSA_BANNER__", pausa_banner)
            .replace("__PAUSA_LABEL__", pausa_label)
            .replace("__PAUSA_CLS__", pausa_cls)
            .replace("__PAUSA_DATA__", "1" if bot_pausado else "0")
            .replace("__AGOTADOS_OPEN__", "open" if agotados_set else "")
            .replace("__AGOTADOS_CHIPS__", agotados_chips)
            .replace("__AGOTADOS_VAL__", html.escape(agotados_actual))
            .replace("__TOTAL_DIA__", f"{total_dia:.2f}")
            .replace("__N_PEDIDOS__", str(len(pedidos)))
            .replace("__N_ACTIVOS__", str(total_activos))
            .replace("__CNT_YP__", str(cnt_yapeplin))
            .replace("__CNT_EF__", str(cnt_efec))
            .replace("__DELIVERIES_JS__", json.dumps(DELIVERIES))
            .replace("__PEDIDOS_JSON__", json.dumps(pedidos, ensure_ascii=False).replace("</", "<\\/"))
            )
    return HTMLResponse(page)


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
    return JSONResponse({"pedidos": _enrich_pedidos(pedidos)})


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
# ── DESIGN SYSTEM COMPARTIDO DEL PANEL ────────────────────────
# ══════════════════════════════════════════════════════════════

_UI_HEAD = """<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="/static/logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.31.0/dist/tabler-icons.min.css">"""

_UI_CSS = """*{box-sizing:border-box;margin:0;padding:0}
:root{--brand:#2D5016;--brand-dark:#22400F;--brand-soft:#EAF3DE;--bg:#F6F7F5;--surface:#FFFFFF;--border:#E4E6E2;--border2:#D5D8D2;--text:#1E221B;--text2:#5A5F56;--text3:#8C9186;--blue:#1A5DA8;--blue-bg:#E8F1FB;--amber:#8A5A0B;--amber-mid:#B97A10;--amber-bg:#FBF0DC;--violet:#5B3E9E;--violet-bg:#EFEAFA;--green:#2E6B2E;--green-bg:#E6F2E6;--red:#B3362C;--red-bg:#FBEAE8;--radius:12px}
body{font-family:'Inter',-apple-system,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;font-size:14px}
.hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 20px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:60}
.hdr img{height:36px;width:36px;border-radius:8px;object-fit:cover}
.hdr-title{margin-right:auto;min-width:0}
.hdr-title h1{font-size:15px;font-weight:600;letter-spacing:-.2px}
.hdr-title small{font-size:12px;color:var(--text3)}
.hdr-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.nav{background:var(--surface);border-bottom:1px solid var(--border);display:flex;overflow-x:auto;-webkit-overflow-scrolling:touch;padding:0 12px;flex-shrink:0}
.nav a{display:flex;align-items:center;gap:6px;color:var(--text2);text-decoration:none;padding:10px 14px;font-size:13px;white-space:nowrap;border-bottom:2px solid transparent}
.nav a i{font-size:16px}
.nav a:hover{color:var(--text)}
.nav a.active{color:var(--brand);border-bottom-color:var(--brand);font-weight:600}
.nav-badge{background:var(--red);color:#fff;border-radius:999px;min-width:17px;height:17px;font-size:10px;font-weight:600;display:none;align-items:center;justify-content:center;padding:0 5px}
.wrap{max-width:1100px;margin:0 auto;padding:16px;width:100%}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;margin-bottom:14px}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px}
.kpi .lbl{font-size:12px;color:var(--text2)}
.kpi .val{font-size:20px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.kpi .val small{font-size:12px;font-weight:400;color:var(--text3)}
.kpi .delta{font-size:11.5px;font-weight:600;margin-top:3px;display:inline-flex;align-items:center;gap:3px}
.kpi .delta i{font-size:13px}
.kpi .delta.up{color:var(--green)}.kpi .delta.down{color:var(--red)}.kpi .delta.flat{color:var(--text3)}
.iconbtn{height:34px;width:34px;display:flex;align-items:center;justify-content:center;border:1px solid var(--border);background:var(--surface);border-radius:8px;cursor:pointer;color:var(--text2);font-size:17px;transition:background .15s}
.iconbtn:hover{background:var(--bg)}
.seg{display:inline-flex;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:3px;gap:2px;overflow-x:auto;max-width:100%}
.seg button{border:none;background:transparent;color:var(--text2);padding:6px 12px;border-radius:7px;cursor:pointer;font:inherit;font-size:12.5px;white-space:nowrap;transition:background .15s}
.seg button:hover{background:var(--bg)}
.seg button.active{background:var(--brand);color:#fff;font-weight:600}
.tbl-wrap{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow-x:auto}
table.tbl{width:100%;border-collapse:collapse}
.tbl thead th{background:var(--bg);color:var(--text2);padding:10px 14px;text-align:left;font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid var(--border);white-space:nowrap}
.tbl tbody tr{border-bottom:1px solid var(--border)}
.tbl tbody tr:last-child{border-bottom:none}
.tbl tbody tr:hover{background:var(--bg)}
.tbl td{padding:10px 14px;font-size:13px}
.num{text-align:right;font-variant-numeric:tabular-nums}
.searchwrap{display:flex;align-items:center;gap:7px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:0 10px;height:34px;min-width:190px}
.searchwrap i{color:var(--text3);font-size:15px}
.searchwrap input{border:none;outline:none;background:transparent;font:inherit;font-size:13px;flex:1;color:var(--text);min-width:0}
select.ctl{height:34px;border:1px solid var(--border);border-radius:8px;background:var(--surface);padding:0 8px;font:inherit;font-size:13px;color:var(--text2);cursor:pointer;outline:none}
.toast{position:fixed;bottom:24px;right:24px;background:#1E221B;color:#fff;padding:11px 20px;border-radius:10px;font-size:13px;font-weight:600;box-shadow:0 8px 24px rgba(0,0,0,.25);z-index:200;transform:translateY(80px);opacity:0;transition:all .35s}
.toast.show{transform:translateY(0);opacity:1}
.chipbadge{font-size:11px;font-weight:600;padding:2px 9px;border-radius:999px;white-space:nowrap}
.btn-mini{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--border2);background:var(--surface);color:var(--text2);border-radius:999px;padding:4px 11px;font-size:11.5px;font-weight:600;cursor:pointer;font-family:inherit}
.btn-mini:hover{background:var(--bg)}
@media(max-width:640px){.hdr{padding:8px 12px}.hdr-title small{display:none}.wrap{padding:12px 10px}.kpis{grid-template-columns:repeat(2,1fr)}}"""

_NAV_ITEMS = [
    ("/pedidos",              "ti-package",         "Pedidos",        "pedidos"),
    ("/admin",                "ti-message-circle",  "Conversaciones", "conversaciones"),
    ("/admin/clientes",       "ti-users",           "Clientes",       "clientes"),
    ("/admin/metricas",       "ti-chart-bar",       "Métricas",       "metricas"),
    ("/admin/zonas-delivery", "ti-motorbike",       "Zonas",          "zonas"),
    ("/admin/menu",           "ti-tools-kitchen-2", "Menú",           "menu"),
]


def _nav_html(active: str) -> str:
    parts = []
    for href, icon, label, key in _NAV_ITEMS:
        cls = ' class="active"' if key == active else ""
        badge = ' <span class="nav-badge" id="navBadge">0</span>' if key == "pedidos" else ""
        parts.append(f'<a href="{href}"{cls}><i class="ti {icon}"></i> {label}{badge}</a>')
    return '<nav class="nav">' + "".join(parts) + "</nav>"


def _ui_header(subtitle: str, actions: str = "") -> str:
    return (
        '<header class="hdr"><img src="/static/logo.png" alt="Chilango">'
        f'<div class="hdr-title"><h1>Chilango</h1><small>{subtitle}</small></div>'
        f'<div class="hdr-actions">{actions}</div></header>'
    )


# Burbuja de pedidos nuevos en el nav (común a todas las páginas excepto /pedidos)
_NAV_BADGE_JS = """async function _chkNuevos(){try{const r=await fetch('/api/pedidos',{credentials:'same-origin'});if(!r.ok)return;const d=await r.json();const n=d.pedidos.filter(p=>(p.estado||'').startsWith('Nuevo')).length;const b=document.getElementById('navBadge');if(b){b.textContent=n;b.style.display=n>0?'inline-flex':'none';}}catch(e){}}
_chkNuevos();setInterval(_chkNuevos,15000);"""


# ══════════════════════════════════════════════════════════════
# ── MENÚ EDITABLE ─────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════

_MENU_TEMPLATE = """<!DOCTYPE html>
<html lang="es"><head><title>Menú — Chilango</title>
__UI_HEAD__
<style>__UI_CSS__
.mi-input{border:1px solid var(--border);border-radius:7px;padding:6px 9px;font:inherit;font-size:13px;outline:none;width:100%;background:var(--surface)}
.mi-input:focus{border-color:var(--brand)}
.cat-row td{background:var(--brand-soft);color:var(--brand);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.4px;padding:8px 14px}
.btn-save{display:inline-flex;align-items:center;gap:5px;background:var(--brand);color:#fff;border:none;border-radius:7px;padding:6px 12px;font:inherit;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap}
.btn-save:hover{background:var(--brand-dark)}
input.mi-disp{width:16px;height:16px;accent-color:var(--brand);cursor:pointer}
.hint{font-size:12.5px;color:var(--text3);margin-bottom:14px}
</style></head><body>
__HEADER__
__NAV__
<main class="wrap">
  <p class="hint">Edita precios, nombres o desactiva productos sin tocar el código. Los cambios se aplican al bot de inmediato.</p>
  <div class="tbl-wrap">
    <table class="tbl">
      <thead><tr>
        <th>Producto</th><th>Descripción</th>
        <th style="width:100px">Precio (S/)</th><th style="width:70px;text-align:center">Activo</th><th style="width:110px"></th>
      </tr></thead>
      <tbody>__FILAS__</tbody>
    </table>
  </div>
</main>
<div class="toast" id="toast">Guardado</div>
<script>
async function guardarItem(btn) {
  const tr = btn.closest('tr');
  const id = tr.dataset.id;
  const nombre      = tr.querySelector('.mi-nombre').value.trim();
  const descripcion = tr.querySelector('.mi-desc').value.trim();
  const precio      = parseFloat(tr.querySelector('.mi-precio').value);
  const disponible  = tr.querySelector('.mi-disp').checked ? 1 : 0;
  btn.disabled = true;
  const prev = btn.innerHTML;
  btn.innerHTML = '…';
  try {
    const r = await fetch('/api/menu/item', {
      method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id, nombre, descripcion, precio, disponible})
    });
    const d = await r.json();
    if (d.status === 'ok') {
      const t = document.getElementById('toast');
      t.classList.add('show');
      setTimeout(()=>t.classList.remove('show'), 2200);
    } else {
      alert('Error: ' + (d.msg || 'No se pudo guardar'));
    }
  } catch(e) { alert('Error: ' + e.message); }
  btn.disabled = false;
  btn.innerHTML = prev;
}
__NAV_BADGE_JS__
</script>
</body></html>"""


@app.get("/admin/menu", response_class=HTMLResponse)
async def admin_menu(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    items = db.get_menu_items()
    grupos: dict = {}
    for it in items:
        grupos.setdefault(it["categoria"], []).append(it)

    filas = ""
    for cat, cat_items in grupos.items():
        filas += f'<tr class="cat-row"><td colspan="5">{html.escape(cat)}</td></tr>'
        for it in cat_items:
            disp_checked = "checked" if it["disponible"] else ""
            filas += f"""
<tr data-id="{it['id']}">
  <td><input class="mi-input mi-nombre" value="{html.escape(it['nombre'])}"></td>
  <td><input class="mi-input mi-desc" value="{html.escape(it['descripcion'] or '')}"></td>
  <td><input class="mi-input mi-precio" type="number" step="0.5" value="{it['precio']}" style="width:80px"></td>
  <td style="text-align:center"><input class="mi-disp" type="checkbox" {disp_checked}></td>
  <td><button class="btn-save" onclick="guardarItem(this)"><i class="ti ti-device-floppy"></i> Guardar</button></td>
</tr>"""

    page = (_MENU_TEMPLATE
            .replace("__UI_HEAD__", _UI_HEAD)
            .replace("__UI_CSS__", _UI_CSS)
            .replace("__HEADER__", _ui_header("Menú editable"))
            .replace("__NAV__", _nav_html("menu"))
            .replace("__NAV_BADGE_JS__", _NAV_BADGE_JS)
            .replace("__FILAS__", filas))
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
# ── DASHBOARD DE MÉTRICAS (interactivo) ───────────────────────
# ══════════════════════════════════════════════════════════════

@app.get("/api/metricas")
async def api_metricas(
    credentials: HTTPBasicCredentials = Depends(verificar_admin),
    dias: int = Query(14)
):
    """Datos agregados para el dashboard. ?dias=7|14|30|90 controla el rango."""
    return JSONResponse(db.get_metricas(dias))


_METRICAS_TEMPLATE = """<!DOCTYPE html>
<html lang="es"><head><title>Métricas — Chilango</title>
__UI_HEAD__
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>__UI_CSS__
.controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.upd{font-size:11.5px;color:var(--text3);margin-left:auto}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.chart-box{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;min-width:0}
.chart-box.full{grid-column:1/-1}
.chart-box h3{font-size:13px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:6px;margin-bottom:10px}
.chart-box h3 i{font-size:15px;color:var(--text3)}
.chart-head{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.chart-head h3{margin-bottom:0}
.chart-hint{font-size:11.5px;color:var(--text3);margin-top:8px;display:flex;align-items:center;gap:5px}
.chart-hint i{font-size:13px}
@media(max-width:700px){.charts{grid-template-columns:1fr}}
</style></head><body>
__HEADER__
__NAV__
<main class="wrap">

  <div class="kpis">
    <div class="kpi"><div class="lbl">Hoy</div><div class="val" id="kHoy">—</div><span class="delta flat" id="kHoyP"></span></div>
    <div class="kpi"><div class="lbl">Últimos 7 días</div><div class="val" id="kSem">—</div><span class="delta flat" id="kSemD"></span></div>
    <div class="kpi"><div class="lbl">Este mes</div><div class="val" id="kMes">—</div><span class="delta flat" id="kMesP"></span></div>
    <div class="kpi"><div class="lbl">Ticket promedio</div><div class="val" id="kTick">—</div><span class="delta flat" id="kTickP"></span></div>
  </div>

  <div class="controls">
    <div class="seg" id="segDias">
      <button data-d="7">7 días</button>
      <button data-d="14" class="active">14 días</button>
      <button data-d="30">30 días</button>
      <button data-d="90">90 días</button>
    </div>
    <button class="iconbtn" onclick="loadMetricas()" title="Actualizar ahora"><i class="ti ti-refresh"></i></button>
    <span class="upd" id="updNote"></span>
  </div>

  <div class="charts">
    <div class="chart-box full">
      <div class="chart-head">
        <h3><i class="ti ti-chart-bar"></i> <span id="mainTitle">Ventas por día (S/)</span></h3>
        <div class="seg" id="segMetric">
          <button data-m="ventas" class="active">Ventas</button>
          <button data-m="pedidos">Pedidos</button>
        </div>
      </div>
      <div style="position:relative;height:240px"><canvas id="cMain"></canvas></div>
      <p class="chart-hint"><i class="ti ti-hand-click"></i> Toca una barra para abrir los pedidos de ese día</p>
    </div>
    <div class="chart-box"><h3><i class="ti ti-trophy"></i> Top productos</h3><div style="position:relative;height:230px"><canvas id="cTop"></canvas></div></div>
    <div class="chart-box"><h3><i class="ti ti-calendar-week"></i> Ventas por día de semana</h3><div style="position:relative;height:230px"><canvas id="cDow"></canvas></div></div>
    <div class="chart-box"><h3><i class="ti ti-clock"></i> Hora pico</h3><div style="position:relative;height:230px"><canvas id="cHora"></canvas></div></div>
    <div class="chart-box"><h3><i class="ti ti-wallet"></i> Método de pago</h3><div style="position:relative;height:230px"><canvas id="cPago"></canvas></div></div>
  </div>
</main>

<script>
let DIAS = 14, METRIC = 'ventas', M = null;
let cMain, cTop, cDow, cHora, cPago;
const BRAND='#2D5016', GREEN='#4C8527', SOFT='#C9E3B2', AMBER='#D99A2B', VIOLET='#5B3E9E', BLUE='#1A5DA8', GRAY='#B4B2A9', GRID='#EEF0EC';
Chart.defaults.font.family = 'Inter';
Chart.defaults.font.size = 11.5;
Chart.defaults.color = '#5A5F56';

function fmtS(n){ return 'S/ ' + (n || 0).toFixed(2); }

function deltaSemana(cur, prev, el){
  if (!prev) { el.className = 'delta flat'; el.textContent = 'sin semana previa'; return; }
  const pct = ((cur - prev) / prev) * 100;
  const up = pct >= 0;
  el.className = 'delta ' + (Math.abs(pct) < 1 ? 'flat' : (up ? 'up' : 'down'));
  el.innerHTML = `<i class="ti ${up ? 'ti-trending-up' : 'ti-trending-down'}"></i>${pct >= 0 ? '+' : ''}${pct.toFixed(0)}% vs semana previa`;
}

async function loadMetricas(){
  try {
    const r = await fetch('/api/metricas?dias=' + DIAS, {credentials:'same-origin'});
    if (!r.ok) return;
    M = await r.json();
    renderKpis();
    renderCharts();
    document.getElementById('updNote').textContent =
      'Actualizado ' + new Date().toLocaleTimeString('es-PE', {hour:'2-digit', minute:'2-digit'});
  } catch(e) { console.warn('metricas:', e); }
}

function renderKpis(){
  document.getElementById('kHoy').textContent  = fmtS(M.total_hoy);
  document.getElementById('kHoyP').textContent = M.pedidos_hoy + (M.pedidos_hoy === 1 ? ' pedido' : ' pedidos');
  document.getElementById('kSem').textContent  = fmtS(M.total_semana);
  deltaSemana(M.total_semana, M.total_semana_prev, document.getElementById('kSemD'));
  document.getElementById('kMes').textContent  = fmtS(M.total_mes);
  document.getElementById('kMesP').textContent = M.pedidos_mes + ' pedidos';
  document.getElementById('kTick').textContent = fmtS(M.ticket_promedio);
  document.getElementById('kTickP').textContent = M.pedidos_periodo + ' pedidos en ' + M.dias + ' días';
}

function renderCharts(){
  [cMain, cTop, cDow, cHora, cPago].forEach(c => { if (c) c.destroy(); });
  const esVentas = METRIC === 'ventas';
  document.getElementById('mainTitle').textContent = esVentas ? 'Ventas por día (S/)' : 'Pedidos por día';

  cMain = new Chart(document.getElementById('cMain'), {
    type: 'bar',
    data: { labels: M.dias_labels, datasets: [{
      data: esVentas ? M.dias_ventas : M.dias_pedidos,
      backgroundColor: SOFT, borderColor: GREEN, hoverBackgroundColor: GREEN,
      borderWidth: 1, borderRadius: 4
    }]},
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: {display:false},
        tooltip: { callbacks: { label: c => esVentas ? fmtS(c.parsed.y) : c.parsed.y + ' pedidos' } } },
      scales: { y: {beginAtZero:true, grid:{color:GRID}}, x: {grid:{display:false}} },
      onClick: (e, els) => {
        if (els.length && M.dias_fechas) {
          const f = M.dias_fechas[els[0].index];
          window.location = '/pedidos?fecha=' + encodeURIComponent(f);
        }
      },
      onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; }
    }
  });

  cTop = new Chart(document.getElementById('cTop'), {
    type: 'bar',
    data: { labels: M.top_productos.map(p => p.nombre), datasets: [{
      data: M.top_productos.map(p => p.qty),
      backgroundColor: BRAND, borderRadius: 4
    }]},
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: {display:false},
        tooltip: { callbacks: { label: c => c.parsed.x + ' unidades' } } },
      scales: { x: {beginAtZero:true, grid:{color:GRID}, ticks:{precision:0}}, y: {grid:{display:false}} }
    }
  });

  cDow = new Chart(document.getElementById('cDow'), {
    type: 'bar',
    data: { labels: M.dow_labels, datasets: [{
      data: M.dow_ventas,
      backgroundColor: ['#A4C97E', '#6B9A3F', BRAND, GRAY], borderRadius: 4
    }]},
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: {display:false},
        tooltip: { callbacks: {
          label: c => fmtS(c.parsed.y),
          afterLabel: c => (M.dow_pedidos[c.dataIndex] || 0) + ' pedidos'
        } } },
      scales: { y: {beginAtZero:true, grid:{color:GRID}}, x: {grid:{display:false}} }
    }
  });

  cHora = new Chart(document.getElementById('cHora'), {
    type: 'bar',
    data: { labels: M.horas_labels, datasets: [{
      data: M.horas_data, backgroundColor: AMBER, borderRadius: 4
    }]},
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: {display:false},
        tooltip: { callbacks: { label: c => c.parsed.y + ' pedidos' } } },
      scales: { y: {beginAtZero:true, grid:{color:GRID}, ticks:{precision:0}}, x: {grid:{display:false}} }
    }
  });

  const pagoLabels = Object.keys(M.pago_conteo);
  const pagoColors = pagoLabels.map(l =>
    ['Yape/Plin','Yape','Plin'].includes(l) ? VIOLET :
    (l === 'Efectivo' ? GREEN : (l === 'Contra entrega' ? AMBER : GRAY)));
  cPago = new Chart(document.getElementById('cPago'), {
    type: 'doughnut',
    data: { labels: pagoLabels, datasets: [{
      data: Object.values(M.pago_conteo), backgroundColor: pagoColors, borderWidth: 2, borderColor: '#fff'
    }]},
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '62%',
      plugins: { legend: {position:'bottom', labels:{boxWidth:12, padding:14}},
        tooltip: { callbacks: { label: c => {
          const total = c.dataset.data.reduce((a,b)=>a+b,0) || 1;
          return ` ${c.label}: ${c.parsed} (${Math.round(c.parsed/total*100)}%)`;
        } } } }
    }
  });
}

document.querySelectorAll('#segDias button').forEach(b => b.onclick = () => {
  DIAS = +b.dataset.d;
  document.querySelectorAll('#segDias button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  loadMetricas();
});
document.querySelectorAll('#segMetric button').forEach(b => b.onclick = () => {
  METRIC = b.dataset.m;
  document.querySelectorAll('#segMetric button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  if (M) renderCharts();
});

loadMetricas();
setInterval(loadMetricas, 60000);
__NAV_BADGE_JS__
</script>
</body></html>"""


@app.get("/admin/metricas", response_class=HTMLResponse)
async def admin_metricas(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    page = (_METRICAS_TEMPLATE
            .replace("__UI_HEAD__", _UI_HEAD)
            .replace("__UI_CSS__", _UI_CSS)
            .replace("__HEADER__", _ui_header("Métricas de ventas"))
            .replace("__NAV__", _nav_html("metricas"))
            .replace("__NAV_BADGE_JS__", _NAV_BADGE_JS))
    return HTMLResponse(page)


# ══════════════════════════════════════════════════════════════
# ── HISTORIAL DE COSTOS POR ZONA ──────────────────────────────
# ══════════════════════════════════════════════════════════════

_ZONAS_TEMPLATE = """<!DOCTYPE html>
<html lang="es"><head><title>Zonas delivery — Chilango</title>
__UI_HEAD__
<style>__UI_CSS__
.hint{font-size:12.5px;color:var(--text3);margin-bottom:14px}
.z-dir{max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text3);font-size:12px}
.z-prom{font-weight:600;color:var(--brand)}
.controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.empty{text-align:center;padding:50px 20px;color:var(--text3)}
.empty i{display:block;font-size:40px;margin-bottom:10px;color:var(--border2)}
</style></head><body>
__HEADER__
__NAV__
<main class="wrap">
  <p class="hint">Costos de delivery aprendidos automáticamente de cada pedido. Úsalos como referencia para responder rápido las consultas de costo.</p>
  <div class="controls">
    <div class="searchwrap"><i class="ti ti-search"></i><input id="zSearch" type="search" placeholder="Buscar zona o dirección…" oninput="zFiltrar(this.value)"></div>
  </div>
  __TABLA__
</main>
<script>
function zFiltrar(q) {
  q = (q || '').trim().toLowerCase();
  document.querySelectorAll('#zBody tr').forEach(tr => {
    tr.style.display = !q || tr.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}
__NAV_BADGE_JS__
</script>
</body></html>"""


@app.get("/admin/zonas-delivery", response_class=HTMLResponse)
async def admin_zonas_delivery(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    zonas = db.get_delivery_zones_summary()

    if zonas:
        filas = ""
        for z in zonas:
            filas += f"""
<tr>
  <td style="font-weight:600">{html.escape(z['zona'])}</td>
  <td class="num z-prom">S/ {z['costo_promedio']:.1f}</td>
  <td class="num">S/ {z['ultimo_costo']:.1f}</td>
  <td class="num">S/ {z['costo_min']:.1f} – S/ {z['costo_max']:.1f}</td>
  <td class="num">{z['frecuencia']}×</td>
  <td style="color:var(--text3);font-size:12px;white-space:nowrap">{html.escape(z['ultima_vez'])}</td>
  <td class="z-dir" title="{html.escape(z['ultima_dir'])}">{html.escape(z['ultima_dir'])}</td>
</tr>"""
        tabla = f"""<div class="tbl-wrap"><table class="tbl">
<thead><tr>
  <th>Zona / referencia</th><th style="text-align:right">Promedio</th><th style="text-align:right">Último</th>
  <th style="text-align:right">Rango</th><th style="text-align:right">Veces</th><th>Última vez</th><th>Dirección ejemplo</th>
</tr></thead>
<tbody id="zBody">{filas}</tbody>
</table></div>"""
    else:
        tabla = ('<div class="tbl-wrap"><div class="empty"><i class="ti ti-map-pin-off"></i>'
                 'Aún no hay costos de delivery registrados</div></div>')

    page = (_ZONAS_TEMPLATE
            .replace("__UI_HEAD__", _UI_HEAD)
            .replace("__UI_CSS__", _UI_CSS)
            .replace("__HEADER__", _ui_header("Costos de delivery por zona"))
            .replace("__NAV__", _nav_html("zonas"))
            .replace("__NAV_BADGE_JS__", _NAV_BADGE_JS)
            .replace("__TABLA__", tabla))
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


def _contact_item_html(phone: str, data: dict) -> str:
    """Item de contacto del sidebar de conversaciones (usado por /admin y /api/conversations)."""
    mensajes = data["messages"]
    if not mensajes:
        return ""
    leida = data["leida"]
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
    display_name = DELIVERY_NAME_MAP[phone] if es_delivery else f"+{phone}"
    avatar_icon = "ti-motorbike" if es_delivery else "ti-user"
    tiempo = _format_contact_time(data.get("last_msg_at", ""))
    p = html.escape(phone)
    return (
        f'<div class="contact{unread_class}" id="c_{p}" onclick="contactClick(event,\'{p}\')" data-phone="{p}">'
        f'<input type="checkbox" class="conv-chk" data-phone="{p}"'
        f' onclick="event.stopPropagation()" onchange="onChkChange()"'
        f' style="display:none;width:14px;height:14px;flex-shrink:0;cursor:pointer;accent-color:#2D5016;margin-right:4px">'
        f'<div class="avatar"><i class="ti {avatar_icon}"></i></div>'
        f'<div class="contact-info">'
        f'<div class="contact-row1"><div class="contact-name">{html.escape(display_name)}</div>'
        f'<div class="contact-time">{tiempo}</div></div>'
        f'<div class="contact-row2"><div class="contact-preview">{preview}</div>{badge}</div>'
        f'</div></div>'
    )


def _conv_clean_for_js(conversaciones_raw: dict) -> tuple[dict, dict]:
    """Serializa conversaciones para el frontend (sin imágenes base64)."""
    conv_clean: dict = {}
    conv_escalado: dict = {}
    for phone, data in conversaciones_raw.items():
        conv_clean[phone] = []
        conv_escalado[phone] = data.get("escalado", False)
        for m in data["messages"]:
            c = m["content"]
            if isinstance(c, list):
                texto = next((b["text"] for b in c if b.get("type") == "text"), "[imagen]")
            else:
                texto = c
            conv_clean[phone].append({
                "role": m["role"],
                "content": texto,
                "ts": m.get("ts", ""),
                "manual": m.get("manual", False),
            })
    return conv_clean, conv_escalado


@app.get("/api/conversations")
async def api_conversations(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    """Endpoint JSON para polling del panel admin sin recargar la página."""
    conversaciones_raw = db.get_conversations_with_status()
    contacts_html = "".join(
        _contact_item_html(phone, data) for phone, data in conversaciones_raw.items()
    )
    conv_clean, conv_escalado = _conv_clean_for_js(conversaciones_raw)
    return JSONResponse({"contacts_html": contacts_html, "convs": conv_clean, "escalado": conv_escalado})


_CLIENTES_TEMPLATE = """<!DOCTYPE html>
<html lang="es"><head><title>Clientes — Chilango</title>
__UI_HEAD__
<style>__UI_CSS__
.controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.cl-rank{text-align:center;width:44px}
.rank-chip{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:50%;font-size:11.5px;font-weight:600;background:var(--bg);color:var(--text2)}
.rank-chip.r1{background:var(--amber-bg);color:var(--amber)}
.rank-chip.r2{background:var(--bg);color:var(--text2);border:1px solid var(--border2)}
.rank-chip.r3{background:#F8EDE6;color:#9A5B2E}
.cl-phone a{color:var(--brand);text-decoration:none;font-weight:600;display:inline-flex;align-items:center;gap:5px}
.cl-phone a:hover{text-decoration:underline}
.cl-phone a i{font-size:14px}
.cl-total{color:var(--brand);font-weight:600}
.cl-hist{color:var(--text2);background:var(--bg)}
.cl-date{color:var(--text3);font-size:12px;white-space:nowrap}
.pts-input{width:70px;border:1px solid var(--border);border-radius:7px;padding:5px 8px;font:inherit;font-size:13px;text-align:center;outline:none;transition:border-color .15s;font-variant-numeric:tabular-nums}
.pts-input:focus{border-color:var(--brand)}
.pts-input.saved{border-color:var(--green);background:var(--green-bg)}
.pts-input.saving{border-color:var(--amber-mid)}
.rec-badge{font-size:10.5px;background:var(--green-bg);color:var(--green);border-radius:999px;padding:1px 7px;font-weight:600;margin-left:5px}
.empty{text-align:center;padding:50px 20px;color:var(--text3)}
.empty i{display:block;font-size:40px;margin-bottom:10px;color:var(--border2)}
</style></head><body>
__HEADER__
__NAV__
<main class="wrap">
  <div class="kpis">
    <div class="kpi"><div class="lbl">Clientes — __LABEL_FECHA__</div><div class="val">__N_CLIENTES__</div></div>
    <div class="kpi"><div class="lbl">Pedidos ese día</div><div class="val">__N_PEDIDOS__</div></div>
    <div class="kpi"><div class="lbl">Facturación del día</div><div class="val">S/ __FACTURACION__</div></div>
    <div class="kpi"><div class="lbl">Ticket promedio</div><div class="val">S/ __TICKET__</div></div>
  </div>
  <div class="controls">
    <select class="ctl" onchange="if(this.value)location.href='/admin/clientes?fecha='+encodeURIComponent(this.value)">__FECHAS_OPTIONS__</select>
    <div class="searchwrap"><i class="ti ti-search"></i><input id="buscar" type="search" placeholder="Buscar por teléfono o nombre…" oninput="filtrar(this.value)"></div>
  </div>
  __TABLA__
</main>
<div class="toast" id="toast"></div>
<script>
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2200);
}

async function guardarPuntos(phone, input) {
  input.classList.add('saving');
  try {
    const r = await fetch('/api/clientes/puntos', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({phone, puntos: parseInt(input.value) || 0})
    });
    const d = await r.json();
    if (d.status === 'ok') {
      input.classList.remove('saving');
      input.classList.add('saved');
      setTimeout(() => input.classList.remove('saved'), 1500);
      showToast('Puntos actualizados');
    } else {
      alert('Error: ' + (d.msg || 'No se pudo guardar'));
    }
  } catch(e) { alert('Error: ' + e.message); }
}

function filtrar(q) {
  q = (q || '').toLowerCase();
  document.querySelectorAll('#tbody tr').forEach(tr => {
    tr.style.display = tr.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}
__NAV_BADGE_JS__
</script>
</body></html>"""


@app.get("/admin/clientes", response_class=HTMLResponse)
async def admin_clientes(
    credentials: HTTPBasicCredentials = Depends(verificar_admin),
    fecha: str = Query(None)
):
    from datetime import datetime, timezone, timedelta
    PERU_TZ = timezone(timedelta(hours=-5))
    hoy = datetime.now(PERU_TZ).strftime("%d/%m/%Y")
    fecha_sel = fecha if fecha else hoy

    fechas_raw = db.get_available_dates()
    fechas_disponibles = fechas_raw if hoy in fechas_raw else [hoy] + fechas_raw

    clientes = db.get_customers_with_stats_for_date(fecha_sel)

    filas = ""
    for i, c in enumerate(clientes, 1):
        nombre       = html.escape(c.get("nombre") or "—")
        phone        = html.escape(c.get("phone") or "")
        puntos       = int(c.get("puntos") or 0)
        pedidos_dia  = int(c.get("total_pedidos") or 0)
        gastado_dia  = float(c.get("total_gastado") or 0)
        pedidos_hist = int(c.get("total_pedidos_hist") or pedidos_dia)
        gastado_hist = float(c.get("total_gastado_hist") or gastado_dia)
        es_recurrente = pedidos_hist > pedidos_dia
        updated      = (c.get("updated_at") or "—")[:16]
        rank_cls = "r1" if i == 1 else ("r2" if i == 2 else ("r3" if i == 3 else ""))
        badge_rec = '<span class="rec-badge">recurrente</span>' if es_recurrente else ''
        filas += f"""<tr>
          <td class="cl-rank"><span class="rank-chip {rank_cls}">{i}</span></td>
          <td class="cl-phone"><a href="https://wa.me/{phone}" target="_blank" title="Abrir chat de WhatsApp"><i class="ti ti-brand-whatsapp"></i>+{phone}</a></td>
          <td>{nombre}{badge_rec}</td>
          <td class="num">{pedidos_dia}</td>
          <td class="num cl-total">S/ {gastado_dia:.2f}</td>
          <td class="num cl-hist">{pedidos_hist}</td>
          <td class="num cl-hist">S/ {gastado_hist:.2f}</td>
          <td style="text-align:center">
            <input class="pts-input" type="number" min="0" value="{puntos}"
              onchange="guardarPuntos('{phone}', this)"
              onkeydown="if(event.key==='Enter')this.blur()">
          </td>
          <td class="cl-date">{updated}</td>
        </tr>"""

    total_clientes = len(clientes)
    total_gastado_global = sum(c.get("total_gastado") or 0 for c in clientes)
    total_pedidos_dia = sum(c.get("total_pedidos") or 0 for c in clientes)
    label_fecha = "hoy" if fecha_sel == hoy else fecha_sel

    fechas_options = "".join(
        f'<option value="{f}" {"selected" if f == fecha_sel else ""}>{f}{" (hoy)" if f == hoy else ""}</option>'
        for f in fechas_disponibles
    )

    if clientes:
        tabla = f"""<div class="tbl-wrap">
    <table class="tbl">
      <thead>
        <tr>
          <th style="text-align:center">#</th>
          <th>Teléfono</th>
          <th>Nombre</th>
          <th style="text-align:right">Pedidos día</th>
          <th style="text-align:right">Total día</th>
          <th style="text-align:right">Pedidos hist.</th>
          <th style="text-align:right">Total hist.</th>
          <th style="text-align:center">Puntos</th>
          <th>Última actividad</th>
        </tr>
      </thead>
      <tbody id="tbody">{filas}</tbody>
    </table></div>"""
    else:
        tabla = (f'<div class="tbl-wrap"><div class="empty"><i class="ti ti-users"></i>'
                 f'Sin pedidos para {html.escape(fecha_sel)}</div></div>')

    page = (_CLIENTES_TEMPLATE
            .replace("__UI_HEAD__", _UI_HEAD)
            .replace("__UI_CSS__", _UI_CSS)
            .replace("__HEADER__", _ui_header("Clientes y puntos"))
            .replace("__NAV__", _nav_html("clientes"))
            .replace("__NAV_BADGE_JS__", _NAV_BADGE_JS)
            .replace("__LABEL_FECHA__", html.escape(label_fecha))
            .replace("__N_CLIENTES__", str(total_clientes))
            .replace("__N_PEDIDOS__", str(total_pedidos_dia))
            .replace("__FACTURACION__", f"{total_gastado_global:.2f}")
            .replace("__TICKET__", f"{(total_gastado_global/total_clientes if total_clientes else 0):.2f}")
            .replace("__FECHAS_OPTIONS__", fechas_options)
            .replace("__TABLA__", tabla))
    return HTMLResponse(page)


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


# ══════════════════════════════════════════════════════════════
# ── PANEL DE CONVERSACIONES ───────────────────────────────────
# ══════════════════════════════════════════════════════════════

_ADMIN_TEMPLATE = """<!DOCTYPE html>
<html lang="es"><head><title>Conversaciones — Chilango</title>
__UI_HEAD__
<style>__UI_CSS__
html,body{height:100%}
body{display:flex;flex-direction:column;overflow:hidden}
.hdr{position:static}
.container{display:flex;flex:1;overflow:hidden;position:relative}
.sidebar{width:320px;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0}
.sidebar-title{padding:10px 14px;font-size:11px;color:var(--text3);border-bottom:1px solid var(--border);font-weight:600;letter-spacing:.5px;display:flex;align-items:center;justify-content:space-between;text-transform:uppercase}
.sidebar-title label{display:flex;align-items:center;gap:5px;cursor:pointer;font-size:11.5px;font-weight:400;color:var(--text3);text-transform:none}
.sidebar-title input{width:14px;height:14px;cursor:pointer;accent-color:var(--brand)}
.sidebar-list{overflow-y:auto;flex:1}
.contact{padding:11px 14px;border-bottom:1px solid var(--border);cursor:pointer;display:flex;align-items:center;gap:11px;transition:background .1s}
.contact:hover{background:var(--bg)}
.contact.active{background:var(--brand-soft)}
.contact.unread{background:#F2F7EC}
.avatar{width:40px;height:40px;border-radius:50%;background:var(--bg);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:18px;color:var(--text2);flex-shrink:0}
.contact-info{flex:1;min-width:0}
.contact-row1{display:flex;align-items:baseline;justify-content:space-between;gap:6px}
.contact-row2{display:flex;align-items:center;gap:6px;margin-top:2px}
.contact-name{font-weight:600;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
.contact-time{font-size:11px;color:var(--text3);flex-shrink:0;white-space:nowrap}
.contact.unread .contact-time{color:var(--green);font-weight:600}
.contact-preview{font-size:12.5px;color:var(--text3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
.contact-unread{font-size:11px;background:var(--brand);color:#fff;border-radius:999px;min-width:19px;height:19px;padding:0 5px;display:flex;align-items:center;justify-content:center;font-weight:600;flex-shrink:0}
.no-convs{padding:24px;color:var(--text3);text-align:center;font-size:13px}
.bulk-bar{display:none;padding:6px 12px;background:var(--amber-bg);border-bottom:1px solid #EAD9B0;align-items:center;gap:8px;flex-shrink:0}
.bulk-bar .cnt{font-size:12px;color:var(--amber);flex:1;font-weight:600}
.bulk-del{display:inline-flex;align-items:center;gap:5px;background:var(--red);color:#fff;border:none;border-radius:999px;padding:4px 13px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit}
.bulk-cancel{background:none;border:1px solid var(--border2);border-radius:999px;padding:4px 11px;font-size:12px;cursor:pointer;color:var(--text2);font-family:inherit}
.chat-panel{flex:1;display:flex;flex-direction:column;background:#EFEDE7;overflow:hidden}
.chat-header{background:var(--surface);padding:9px 14px;display:flex;align-items:center;gap:9px;border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap}
.chat-header .avatar{width:34px;height:34px;font-size:15px}
.chat-header-name{font-weight:600;font-size:14px;flex:1;min-width:0}
.btn-back{display:none}
.chat-messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:4px}
.bubble{max-width:68%;padding:7px 12px;border-radius:10px;font-size:13.5px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word}
.bubble.cliente{background:var(--surface);border:1px solid var(--border);align-self:flex-start;border-top-left-radius:2px}
.bubble.bot{background:#DDEBCF;align-self:flex-end;border-top-right-radius:2px}
.bubble.manual{background:var(--amber-bg);align-self:flex-end;border-top-right-radius:2px}
.sender{font-size:11px;font-weight:600;margin-bottom:2px;color:var(--text2);display:flex;align-items:center;gap:4px}
.sender i{font-size:12px}
.bubble.bot .sender{color:var(--green)}
.bubble.manual .sender{color:var(--amber)}
.msg-ts{font-size:10px;color:var(--text3);font-weight:400;margin-left:6px}
.empty-state{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;color:var(--text3);gap:10px;font-size:13.5px}
.empty-state i{font-size:46px;color:var(--border2)}
.tpl-bar{padding:8px 12px;background:var(--surface);border-top:1px solid var(--border);display:flex;gap:6px;flex-wrap:wrap;align-items:center;flex-shrink:0}
.tpl-bar .tpl-lbl{font-size:11px;font-weight:600;color:var(--amber);display:flex;align-items:center;gap:4px}
.tpl{font-size:11.5px;border:1px solid var(--border2);border-radius:999px;padding:4px 11px;background:var(--surface);color:var(--text2);cursor:pointer;font-family:inherit;transition:background .15s}
.tpl:hover{background:var(--bg)}
.chat-input-area{padding:10px 12px;background:var(--surface);border-top:1px solid var(--border);display:flex;gap:8px;align-items:center;flex-shrink:0}
.chat-input{flex:1;border:1px solid var(--border);border-radius:999px;padding:9px 14px;font:inherit;font-size:13.5px;outline:none;min-width:0}
.chat-input:focus{border-color:var(--brand)}
.chat-send-btn{background:var(--brand);color:#fff;border:none;border-radius:50%;width:38px;height:38px;cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:background .15s}
.chat-send-btn:hover{background:var(--brand-dark)}
.chat-send-btn:disabled{background:var(--border2);cursor:default}
.refresh-note{font-size:11px;color:var(--text3);text-align:center;padding:5px;background:var(--surface);border-top:1px solid var(--border);flex-shrink:0}
@media(max-width:700px){
  .sidebar{width:100%;border-right:none}
  .chat-panel{position:absolute;inset:0;transform:translateX(100%);transition:transform .25s ease;z-index:30}
  body.chat-open .chat-panel{transform:none}
  .btn-back{display:flex;height:30px;width:30px;font-size:16px}
}
</style></head><body>
__HEADER__
__NAV__
<div class="container">
  <div class="sidebar">
    <div class="sidebar-title">
      <span>Conversaciones</span>
      <label title="Seleccionar todas"><input type="checkbox" id="chkSelectAll" onchange="toggleSelectAll(this.checked)"> Todas</label>
    </div>
    <div id="bulkBar" class="bulk-bar">
      <span id="bulkCount" class="cnt">0 seleccionadas</span>
      <button class="bulk-del" onclick="eliminarSeleccionadas()"><i class="ti ti-trash"></i> Eliminar</button>
      <button class="bulk-cancel" onclick="cancelarSeleccion()">Cancelar</button>
    </div>
    <div class="sidebar-list">__CONTACTS__</div>
  </div>
  <div class="chat-panel" id="chatPanel">
    <div class="empty-state"><i class="ti ti-message-circle"></i><span>Selecciona una conversación</span></div>
  </div>
</div>
<div class="refresh-note">Actualización automática cada 5 s</div>

<script>
const convs = __CONV_JSON__;
let escaladoMap = __ESC_JSON__;
const DELIVERY_NAMES = __DELIVERY_NAMES__;

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\\n/g,'<br>');
}

function buildBubble(m) {
  const isManual = !!m.manual;
  const lado  = isManual ? 'manual' : (m.role === 'user' ? 'cliente' : 'bot');
  const label = isManual
    ? '<i class="ti ti-headset"></i> Equipo'
    : (m.role === 'user' ? 'Cliente' : '<i class="ti ti-robot"></i> Chili');
  const tsHtml = m.ts ? `<span class="msg-ts">${m.ts}</span>` : '';
  return `<div class="bubble ${lado}"><div class="sender">${label}${tsHtml}</div>${esc(m.content)}</div>`;
}

function closeChat() {
  document.body.classList.remove('chat-open');
  document.querySelectorAll('.contact').forEach(c => c.classList.remove('active'));
  sessionStorage.removeItem('activePhone');
}

function showChat(phone) {
  document.querySelectorAll('.contact').forEach(c => c.classList.remove('active'));
  const el = document.getElementById('c_' + phone);
  if (el) {
    el.classList.add('active');
    el.classList.remove('unread');
    const badge = el.querySelector('.contact-unread');
    if (badge) badge.remove();
  }

  fetch(`/admin/mark-read/${encodeURIComponent(phone)}`, {
    method: 'POST', credentials: 'same-origin'
  });

  const msgs = convs[phone] || [];
  const bubbles = msgs.map(buildBubble).join('');

  const isEscalado = escaladoMap[phone] || false;
  const escaladoCtl = isEscalado
    ? `<span class="chipbadge" style="background:var(--red-bg);color:var(--red)">Equipo activo</span>
       <button class="btn-mini" onclick="reactivarBot('${esc(phone)}')"><i class="ti ti-robot"></i> Reactivar bot</button>`
    : `<button class="btn-mini" onclick="pausarBot('${esc(phone)}')"><i class="ti ti-player-pause"></i> Pausar bot</button>`;
  const esDelivery = !!DELIVERY_NAMES[phone];
  const avatarIcon = esDelivery ? 'ti-motorbike' : 'ti-user';
  const displayName = esDelivery ? DELIVERY_NAMES[phone] : '+' + esc(phone);
  const tplBar = isEscalado ? `<div class="tpl-bar">
      <span class="tpl-lbl"><i class="ti ti-bolt"></i> Respuesta rápida:</span>
      <button class="tpl" onclick="usarPlantilla(this)" data-txt="Disculpa la demora, Chilanguit@ 🙏 Ya estamos en ello y te avisamos en cuanto tu pedido salga.">Disculpa demora</button>
      <button class="tpl" onclick="usarPlantilla(this)" data-txt="¡Nos disculpamos! 🙏 Vamos a compensarte con un guacamole gratis en tu próximo pedido. ¿Te parece bien?">Guacamole gratis</button>
      <button class="tpl" onclick="usarPlantilla(this)" data-txt="Chilanguit@, para compensar el inconveniente te regalamos el delivery gratis en tu próximo pedido. Disculpa las molestias 🙏">Delivery gratis</button>
      <button class="tpl" onclick="usarPlantilla(this)" data-txt="¡Acá estamos! Cuéntame qué pasó para poder ayudarte mejor 🌮">Pedir detalle</button>
    </div>` : '';

  document.getElementById('chatPanel').innerHTML = `
    <div class="chat-header">
      <button class="iconbtn btn-back" onclick="closeChat()" title="Volver"><i class="ti ti-arrow-left"></i></button>
      <div class="avatar"><i class="ti ${avatarIcon}"></i></div>
      <div class="chat-header-name">${displayName}</div>
      ${escaladoCtl}
      <a class="iconbtn" style="text-decoration:none" href="https://wa.me/${esc(phone)}" target="_blank" title="Abrir en WhatsApp"><i class="ti ti-brand-whatsapp"></i></a>
      <button class="iconbtn" onclick="eliminarChat('${esc(phone)}')" title="Eliminar chat"><i class="ti ti-trash"></i></button>
    </div>
    <div class="chat-messages" id="msgs">${bubbles}</div>
    ${tplBar}
    <div class="chat-input-area">
      <input type="text" id="manualInput" class="chat-input"
             placeholder="Escribe un mensaje al cliente…"
             onkeydown="if(event.key==='Enter' && !event.shiftKey){ event.preventDefault(); sendManual('${esc(phone)}'); }">
      <button id="sendBtn" class="chat-send-btn" onclick="sendManual('${esc(phone)}')" title="Enviar"><i class="ti ti-send"></i></button>
    </div>`;

  const msgsEl = document.getElementById('msgs');
  if (msgsEl) msgsEl.scrollTop = msgsEl.scrollHeight;
  sessionStorage.setItem('activePhone', phone);
  document.body.classList.add('chat-open');
}

async function eliminarChat(phone) {
  if (!confirm(`¿Eliminar el historial de chat de +${phone}? Esta acción no se puede deshacer.`)) return;
  const r = await fetch('/api/conversations/delete', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({phone})
  });
  if ((await r.json()).status === 'ok') {
    document.getElementById('chatPanel').innerHTML = '<div class="empty-state"><i class="ti ti-message-circle"></i><span>Selecciona una conversación</span></div>';
    closeChat();
    pollConversaciones();
  }
}

async function reactivarBot(phone) {
  if (!confirm('¿Reactivar el bot para este cliente? Volverá a responder automáticamente.')) return;
  await fetch('/api/conversations/reactivar', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({phone})
  });
  escaladoMap[phone] = false;
  showChat(phone);
}

async function pausarBot(phone) {
  await fetch('/api/conversations/pausar', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({phone})
  });
  escaladoMap[phone] = true;
  showChat(phone);
}

function usarPlantilla(btn) {
  const inp = document.getElementById('manualInput');
  if (inp) { inp.value = btn.dataset.txt; inp.focus(); }
}

async function sendManual(phone) {
  const input = document.getElementById('manualInput');
  const btn   = document.getElementById('sendBtn');
  const msg   = (input.value || '').trim();
  if (!msg) return;
  btn.disabled = true;
  input.value  = '';
  try {
    const r = await fetch('/admin/send-message', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({phone, message: msg})
    });
    if (!r.ok) { input.value = msg; alert('Error al enviar'); return; }
    const msgsEl = document.getElementById('msgs');
    if (msgsEl) {
      const now = new Date().toLocaleTimeString('es-PE', {hour:'2-digit', minute:'2-digit'});
      msgsEl.innerHTML += buildBubble({role:'assistant', content: msg, ts: now, manual: true});
      msgsEl.scrollTop = msgsEl.scrollHeight;
    }
  } catch(e) {
    input.value = msg;
    alert('Error: ' + e.message);
  }
  btn.disabled = false;
  input.focus();
}

// Restaurar conversación activa al cargar
const saved = sessionStorage.getItem('activePhone');
if (saved && convs[saved]) showChat(saved);

// ── Polling AJAX: actualizar sidebar sin recargar página ──
async function pollConversaciones() {
  try {
    const r = await fetch('/api/conversations', {credentials:'same-origin'});
    if (!r.ok) return;
    const data = await r.json();
    if (data.escalado) Object.assign(escaladoMap, data.escalado);
    Object.assign(convs, data.convs);
    const lista = document.querySelector('.sidebar-list');
    if (!lista) return;
    const prevChecked = new Set(
      [...document.querySelectorAll('.conv-chk:checked')].map(c => c.dataset.phone)
    );
    lista.innerHTML = data.contacts_html || '<div class="no-convs">Sin conversaciones aún</div>';
    if (modoSeleccion) {
      document.querySelectorAll('.conv-chk').forEach(chk => {
        chk.style.display = 'block';
        if (prevChecked.has(chk.dataset.phone)) chk.checked = true;
      });
      actualizarBulkBar();
    }
    const activePhone = sessionStorage.getItem('activePhone');
    if (activePhone) {
      const act = document.getElementById('c_' + activePhone);
      if (act) act.classList.add('active');
      if (data.convs[activePhone]) {
        const msgsEl = document.getElementById('msgs');
        if (msgsEl) {
          const atBottom = msgsEl.scrollHeight - msgsEl.scrollTop - msgsEl.clientHeight < 60;
          const newBubbles = data.convs[activePhone].map(buildBubble).join('');
          if (msgsEl.innerHTML !== newBubbles) {
            msgsEl.innerHTML = newBubbles;
            if (atBottom) msgsEl.scrollTop = msgsEl.scrollHeight;
          }
        }
      }
    }
  } catch(e) {}
}
setInterval(pollConversaciones, 5000);

/* ── Selección múltiple ── */
let modoSeleccion = false;

function toggleSelectAll(checked) {
  modoSeleccion = true;
  document.querySelectorAll('.conv-chk').forEach(chk => {
    chk.style.display = 'block';
    chk.checked = checked;
  });
  actualizarBulkBar();
}

function contactClick(e, phone) {
  if (modoSeleccion) {
    const chk = document.querySelector(`.conv-chk[data-phone="${phone}"]`);
    if (chk) { chk.checked = !chk.checked; onChkChange(); }
  } else {
    showChat(phone);
  }
}

function onChkChange() {
  modoSeleccion = true;
  document.querySelectorAll('.conv-chk').forEach(chk => chk.style.display = 'block');
  actualizarBulkBar();
}

function actualizarBulkBar() {
  const seleccionadas = document.querySelectorAll('.conv-chk:checked');
  const bar = document.getElementById('bulkBar');
  const count = document.getElementById('bulkCount');
  bar.style.display = seleccionadas.length > 0 ? 'flex' : 'none';
  count.textContent = seleccionadas.length + ' seleccionada' + (seleccionadas.length > 1 ? 's' : '');
  const total = document.querySelectorAll('.conv-chk').length;
  document.getElementById('chkSelectAll').checked = seleccionadas.length === total && total > 0;
  document.getElementById('chkSelectAll').indeterminate = seleccionadas.length > 0 && seleccionadas.length < total;
}

function cancelarSeleccion() {
  modoSeleccion = false;
  document.querySelectorAll('.conv-chk').forEach(chk => {
    chk.checked = false;
    chk.style.display = 'none';
  });
  document.getElementById('bulkBar').style.display = 'none';
  document.getElementById('chkSelectAll').checked = false;
  document.getElementById('chkSelectAll').indeterminate = false;
}

async function eliminarSeleccionadas() {
  const seleccionadas = [...document.querySelectorAll('.conv-chk:checked')];
  if (seleccionadas.length === 0) return;
  if (!confirm(`¿Eliminar ${seleccionadas.length} conversación${seleccionadas.length > 1 ? 'es' : ''}? Esta acción no se puede deshacer.`)) return;
  const phones = seleccionadas.map(chk => chk.dataset.phone);
  const r = await fetch('/api/conversations/delete-bulk', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({phones})
  });
  if ((await r.json()).status === 'ok') {
    cancelarSeleccion();
    document.getElementById('chatPanel').innerHTML = '<div class="empty-state"><i class="ti ti-message-circle"></i><span>Selecciona una conversación</span></div>';
    closeChat();
    pollConversaciones();
  }
}
__NAV_BADGE_JS__
</script>
</body></html>"""


@app.get("/admin", response_class=HTMLResponse)
async def admin(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    conversaciones_raw = db.get_conversations_with_status()
    num_orders = get_orders_count()

    contacts_html = "".join(
        _contact_item_html(phone, data) for phone, data in conversaciones_raw.items()
    )
    if not contacts_html:
        contacts_html = "<div class='no-convs'>Sin conversaciones aún</div>"

    conv_clean, conv_escalado = _conv_clean_for_js(conversaciones_raw)
    conv_json     = json.dumps(conv_clean, ensure_ascii=False).replace("</", "<\\/")
    escalado_json = json.dumps(conv_escalado, ensure_ascii=False)
    delivery_names_json = json.dumps(DELIVERY_NAME_MAP, ensure_ascii=False)

    subtitle = f"Conversaciones · {len(conversaciones_raw)} chats · {num_orders} pedidos históricos"

    page = (_ADMIN_TEMPLATE
            .replace("__UI_HEAD__", _UI_HEAD)
            .replace("__UI_CSS__", _UI_CSS)
            .replace("__HEADER__", _ui_header(subtitle))
            .replace("__NAV__", _nav_html("conversaciones"))
            .replace("__NAV_BADGE_JS__", _NAV_BADGE_JS)
            .replace("__CONTACTS__", contacts_html)
            .replace("__CONV_JSON__", conv_json)
            .replace("__ESC_JSON__", escalado_json)
            .replace("__DELIVERY_NAMES__", delivery_names_json))
    return HTMLResponse(page)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
