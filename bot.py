import os
import base64
from datetime import datetime, timezone, timedelta
from anthropic import AsyncAnthropic
from menu import MENU_TEXTO
from orders import save_order

_client = None

PERU_TZ = timezone(timedelta(hours=-5))

YAPE_PLIN_NUMBER = "954713696"


def get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY no está configurada en las variables de entorno")
        _client = AsyncAnthropic(api_key=api_key)
    return _client


def esta_en_horario() -> bool:
    ahora = datetime.now(PERU_TZ)
    # 4=Viernes, 5=Sábado, 6=Domingo
    if ahora.weekday() not in (4, 5, 6):
        return False
    return 17 <= ahora.hour < 23


def mensaje_fuera_horario() -> str:
    return (
        "¡Hola! 👋 Gracias por escribirnos.\n\n"
        "En este momento estamos descansando 😔\n\n"
        "🕒 Atendemos:\n"
        "*Viernes, Sábado y Domingo*\n"
        "de *5:00 pm a 11:00 pm*\n\n"
        "¡Te esperamos pronto para taquear rico! 🌮"
    )


def mensaje_bienvenida() -> str:
    return (
        "¡Qué onda! 👋 Soy *Chili*, tu asistente de *Chilango*.\n\n"
        "Somos un restaurante mexicano de delivery en Tacna. "
        "Tenemos tacos, quesabirrias, burritos y todo lo que necesitas para taquear rico. 🌮🌯\n\n"
        "🕒 *Horario:* Viernes, Sábado y Domingo de 5:00 pm a 11:00 pm.\n\n"
        "¿Qué se te antoja hoy?\n\n"
        "1️⃣ Ver carta\n"
        "2️⃣ Hacer un pedido"
    )


conversaciones: dict[str, list] = {}
bienvenida_enviada: set[str] = set()

SYSTEM_PROMPT = f"""Eres *Chili*, el asistente virtual de Chilango, restaurante mexicano de delivery en Tacna, Perú.
Tienes personalidad amigable, con onda mexicana auténtica, especificamente chilango (de la CDMX). Eres entusiasta con la comida pero vas al grano.

━━━ DATOS DEL RESTAURANTE ━━━
- Nombre: Chilango 🌮
- Ciudad: Tacna, Perú
- Modalidad: Solo delivery (no hay recojo en tienda)
- Horario: Viernes, Sábado y Domingo de 5pm a 11pm
- WhatsApp: 954 713 696
- Instagram: @chilangotacna
- Formas de pago: Yape · Plin · Efectivo
- Número Yape/Plin: {YAPE_PLIN_NUMBER} (distinto al WhatsApp)
- Empaque eco resistente: S/ 2.00 por pedido (SIEMPRE incluir en el total)

━━━ CARTA COMPLETA ━━━
{MENU_TEXTO}

━━━ COMBOS — ACLARACIÓN IMPORTANTE ━━━
- "Combo Pa' Ti Solito": el agua incluida es SOLO horchata, jamaica o tamarindo. NO incluye chamoyada de mango.
- "De Compas": incluye 2 aguas del chavo a elegir entre horchata, jamaica o tamarindo. NO incluye chamoyada de mango.
- La Chamoyada de Mango (S/ 13.00) es un producto aparte, no está incluida en ningún combo.

━━━ INSTRUCCIONES DE COMPORTAMIENTO ━━━

1. OPCIONES RÁPIDAS: Si el cliente escribe "1", muéstrale que la carta se está enviando.
   Si escribe "2", inicia el flujo de pedido.

2. PREGUNTAS: Responde con detalle y entusiasmo sobre ingredientes, tamaños, sabores.
   - "¿Qué es la birria?" → Carne de res guisada en adobo especiado, jugosa y sabrosa
   - "¿Tienen opciones sin picante?" → Todos nuestros productos son sin picante, ese va aparte
   - "¿Cuánto demora el delivery?" → Aprox 30-45 min según la zona

3. TOMAR PEDIDO: Cuando el cliente quiera pedir:
   - Anota cada item con cantidad
   - Si pide tacos sin especificar, pregunta el tipo mostrando SIEMPRE las 4 opciones:
     "¿De qué tipo? 🌮
     1. Suadero — S/ 6.50
     2. Campechano — S/ 6.50
     3. Pastor — S/ 6.50
     4. Choriqueso — S/ 7.50"
   - Al tener todo el pedido, muestra el resumen así:
     *Tu pedido:*
     • [cantidad]x [item] — S/ [precio]
     ...
     Subtotal: S/ XX.XX
     Empaque: S/ 2.00
     *TOTAL: S/ XX.XX*
   - Pregunta cómo va a pagar (Yape, Plin o Efectivo)
   - Pregunta la dirección de entrega
   - Confirma el pedido mostrando el resumen final

4. CONFIRMAR PEDIDO: Cuando el cliente confirme (diga "sí", "correcto", "dale", etc.),
   muestra el resumen final y si el pago es por Yape o Plin:
   - Indica: "📲 Puedes yapear/plinear al *{YAPE_PLIN_NUMBER}*"
   - Solicita: "Por favor envíanos la captura del pago para confirmar tu pedido ✅"
   - Solo cuando el cliente envíe la captura de pago, verifica la imagen:
     * Si el monto en la imagen coincide con el total del pedido: confirma y agrega [PEDIDO_OK|...]
     * Si el monto es menor al total: indica la diferencia y pide que complete el pago
     * Si no se puede leer el monto claramente: pide una captura más nítida
   Si el pago es en Efectivo, incluye el tag [PEDIDO_OK|...] directamente al confirmar.

5. ESCALACIÓN: Si el cliente escribe "humano", "agente" o "hablar con alguien",
   dile que el equipo lo atenderá pronto al 954 713 696.

6. TONO: Español amigable (jerga chilanga), sin exagerar la jerga. Emojis con moderación. Respuestas cortas y claras.

IMPORTANTE: Nunca inventes precios ni productos que no estén en la carta.
"""


async def process_message(phone: str, message: str) -> str:
    msg_lower = message.lower().strip()

    # Fuera de horario
    if not esta_en_horario():
        return mensaje_fuera_horario()

    # Primer mensaje — enviar bienvenida
    if phone not in bienvenida_enviada:
        bienvenida_enviada.add(phone)
        conversaciones[phone] = []
        return mensaje_bienvenida()

    if phone not in conversaciones:
        conversaciones[phone] = []

    conversaciones[phone].append({
        "role": "user",
        "content": message,
    })

    history = conversaciones[phone][-30:]

    response = await get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=history,
    )

    reply: str = response.content[0].text

    if "[PEDIDO_OK|" in reply:
        try:
            start = reply.index("[PEDIDO_OK|")
            end = reply.index("]", start)
            tag = reply[start : end + 1]
            parts = tag[len("[PEDIDO_OK|") : -1].split("|")
            items = parts[0].replace("items: ", "").strip()
            total = parts[1].replace("total: ", "").strip()
            save_order(phone, items, total)
            reply = (reply[:start] + reply[end + 1 :]).strip()
        except Exception as e:
            print(f"[ERROR] No se pudo parsear el pedido: {e}")

    conversaciones[phone].append({
        "role": "assistant",
        "content": reply,
    })

    return reply


async def process_message_with_image(phone: str, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    if phone not in conversaciones:
        conversaciones[phone] = []

    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    conversaciones[phone].append({
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": image_b64,
                },
            },
            {
                "type": "text",
                "text": "Te envío la captura del pago.",
            },
        ],
    })

    history = conversaciones[phone][-30:]

    response = await get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=history,
    )

    reply: str = response.content[0].text

    if "[PEDIDO_OK|" in reply:
        try:
            start = reply.index("[PEDIDO_OK|")
            end = reply.index("]", start)
            tag = reply[start : end + 1]
            parts = tag[len("[PEDIDO_OK|") : -1].split("|")
            items = parts[0].replace("items: ", "").strip()
            total = parts[1].replace("total: ", "").strip()
            save_order(phone, items, total)
            reply = (reply[:start] + reply[end + 1 :]).strip()
        except Exception as e:
            print(f"[ERROR] No se pudo parsear el pedido: {e}")

    conversaciones[phone].append({
        "role": "assistant",
        "content": reply,
    })

    return reply


def reset_conversation(phone: str):
    conversaciones.pop(phone, None)
    bienvenida_enviada.discard(phone)
