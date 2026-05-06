import os
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse

from bot import process_message, reset_conversation
from orders import get_orders_count

app = FastAPI(title="Chilango Bot 🌮")


@app.post("/webhook")
async def webhook(
    From: str = Form(...),
    Body: str = Form(...),
):
    phone = From
    message = Body.strip()

    print(f"[MENSAJE] {phone}: {message}")

    # Comando especial para resetear conversación (útil para pruebas)
    if message.lower() in ["/reset", "reiniciar"]:
        reset_conversation(phone)
        reply = "¡Listo! Conversación reiniciada. ¿En qué te puedo ayudar? 🌮"
    else:
        reply = await process_message(phone, message)

    print(f"[RESPUESTA] {reply[:80]}...")

    resp = MessagingResponse()
    resp.message(reply)
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
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return {
        "encontrada": bool(key),
        "longitud": len(key),
        "inicio": key[:12] if key else "vacía",
        "tiene_espacios": " " in key,
        "tiene_saltos": "\n" in key or "\r" in key,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
