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
        await send_whatsapp_message(phone, mensaje_bienvenida(), sending_id)
        # Si el primer mensaje ya es un pedido o pregunta real, procesarlo también
        if msg_lower not in SALUDOS_GENERICOS and message.strip():
            reply = await process_message(phone, message)
            await _send_reply(phone, reply, sending_id)
        return

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


@app.get("/admin", response_class=HTMLResponse)
async def admin(credentials: HTTPBasicCredentials = Depends(verificar_admin)):
    conversaciones = db.get_all_conversations()
    num_orders = get_orders_count()

    # Sidebar de contactos
    contacts_html = ""
    for phone, mensajes in conversaciones.items():
        if not mensajes:
            continue
        ultimo = mensajes[-1]
        contenido = ultimo["content"]
        if isinstance(contenido, list):
            preview = next((b["text"] for b in contenido if b.get("type") == "text"), "[imagen]")
        else:
            preview = contenido
        preview = html.escape(str(preview)[:50])
        contacts_html += f"""
        <div class="contact" id="c_{html.escape(phone)}" onclick="showChat('{html.escape(phone)}')">
            <div class="avatar">👤</div>
            <div class="contact-info">
                <div class="contact-name">+{html.escape(phone)}</div>
                <div class="contact-preview">{preview}</div>
            </div>
            <div class="contact-count">{len(mensajes)}</div>
        </div>"""

    if not contacts_html:
        contacts_html = "<div class='no-convs'>Sin conversaciones aún</div>"

    # Serializar conversaciones para JS (imágenes excluidas por tamaño)
    conv_clean = {}
    for phone, msgs in conversaciones.items():
        conv_clean[phone] = []
        for m in msgs:
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
        .contact-count {{ font-size: 11px; background: #25d366; color: white; border-radius: 50%; width: 22px; height: 22px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; font-weight: 600; }}
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
            👥 {len(conversaciones)} conversaciones<br>
            📦 {num_orders} pedidos registrados
        </div>
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
            if (el) el.classList.add('active');

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
