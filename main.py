import os
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse

from bot import process_message, reset_conversation
from orders import get_orders_count
from menu import MENU_TEXTO

app = FastAPI(title="Chilango Bot 🌮")


@app.post("/webhook")
async def webhook(
    From: str = Form(...),
    Body: str = Form(...),
):
    phone = From
    message = Body.strip()

    print(f"[MENSAJE] {phone}: {message}")

    palabras_carta = ["carta", "menu", "menú", "carta completa", "ver carta", "ver menu", "qué tienen", "que tienen"]
    if message.lower() in ["/reset", "reiniciar"]:
        reset_conversation(phone)
        replies = ["¡Listo! Conversación reiniciada. ¿En qué te puedo ayudar? 🌮"]
    elif any(p in message.lower() for p in palabras_carta):
        mitad = len(MENU_TEXTO) // 2
        corte = MENU_TEXTO.rfind("\n", mitad - 200, mitad + 200)
        if corte == -1:
            corte = mitad
        replies = [MENU_TEXTO[:corte].strip(), MENU_TEXTO[corte:].strip()]
    else:
        reply = await process_message(phone, message)
        if len(reply) > 1500:
            mitad = len(reply) // 2
            corte = reply.rfind("\n", mitad - 200, mitad + 200)
            if corte == -1:
                corte = mitad
            replies = [reply[:corte].strip(), reply[corte:].strip()]
        else:
            replies = [reply]

    print(f"[RESPUESTA] {replies[0][:80]}...")

    resp = MessagingResponse()
    for r in replies:
        resp.message(r)
    return PlainTextResponse(str(resp), media_type="text/xml")


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
        <p>Pedidos registrados hoy: <span class="badge">{count}</span></p>
        <p><small>Horario de atención: Vie · Sáb · Dom · 5pm – 11pm</small></p>
    </body>
    </html>
    """


@app.get("/health")
async def health():
    return {"status": "ok", "bot": "Chilango 🌮"}


@app.get("/debug-key")
async def debug_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return {
        "encontrada": bool(key),
        "longitud": len(key),
        "inicio": key[:12] if key else "vacía",
        "valida": key.startswith("sk-ant-"),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
