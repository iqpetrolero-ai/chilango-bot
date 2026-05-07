import os
import json
import httpx
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, Query, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bot import process_message, process_message_with_image, reset_conversation, conversaciones
from orders import get_orders_count
from menu import MENU_TEXTO

app = FastAPI(title="Chilango Bot 🌮")

import os as _os
if _os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "chilango2026").strip()

security = HTTPBasic()

def verificar_admin(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(credentials.username.encode(), b"admin")
    ok_pass = secrets.compare_digest(credentials.password.encode(), ADMIN_PASSWORD.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, detail="Acceso no autorizado",
                            headers={"WWW-Authenticate": "Basic"})

META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "").strip()
META_PHONE_NUMBER_ID = os.environ.get("META_PHONE_NUMBER_ID", "").strip()
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "chilango2026").strip()
BASE_URL = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
PDF_URL = f"https://{BASE_URL}/static/carta.pdf" if BASE_URL else ""

PALABRAS_CARTA = ["carta", "menu", "menú", "ver carta", "ver menu", "qué tienen", "que tienen"]


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
    """Descarga imagen de Meta y retorna (bytes, mime_type) o (None, None)."""
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

    reply = await process_message(phone, message)

    if len(reply) > 1500:
        mitad = len(reply) // 2
        corte = reply.rfind("\n", mitad - 200, mitad + 200)
        if corte == -1:
            corte = mitad
        await send_whatsapp_message(phone, reply[:corte].strip(), sending_id)
        await send_whatsapp_message(phone, reply[corte:].strip(), sending_id)
    else:
        await send_whatsapp_message(phone, reply, sending_id)


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
    filas = ""
    for phone, mensajes in conversaciones.items():
        if not mensajes:
            continue
        burbujas = ""
        for m in mensajes:
            content = m["content"]
            if isinstance(content, list):
                texto = next((b["text"] for b in content if b.get("type") == "text"), "[imagen]")
            else:
                texto = content
            lado = "cliente" if m["role"] == "user" else "bot"
            burbujas += f'<div class="msg {lado}"><b>{"Cliente" if lado == "cliente" else "🤖 Bot"}:</b> {texto}</div>'
        filas += f"""
        <details>
            <summary>📱 +{phone} ({len(mensajes)} mensajes)</summary>
            <div class="chat">{burbujas}</div>
        </details>"""

    if not filas:
        filas = "<p style='color:#888'>No hay conversaciones activas.</p>"

    return f"""
    <html>
    <head>
        <title>Admin — Chilango Bot</title>
        <meta charset="utf-8">
        <meta http-equiv="refresh" content="15">
        <style>
            body {{ font-family: Arial, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; }}
            h1 {{ color: #2D5016; }}
            details {{ border: 1px solid #ddd; border-radius: 8px; margin: 10px 0; padding: 10px; }}
            summary {{ cursor: pointer; font-weight: bold; }}
            .chat {{ margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }}
            .msg {{ padding: 8px 12px; border-radius: 8px; max-width: 85%; white-space: pre-wrap; font-size: 14px; }}
            .cliente {{ background: #f0f0f0; align-self: flex-start; }}
            .bot {{ background: #dcf8c6; align-self: flex-end; }}
        </style>
    </head>
    <body>
        <h1>🌮 Chilango Bot — Conversaciones</h1>
        <p>Activas: <b>{len(conversaciones)}</b> &nbsp;|&nbsp; Pedidos totales: <b>{get_orders_count()}</b></p>
        {filas}
    </body>
    </html>
    """


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
