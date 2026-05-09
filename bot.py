import os
import base64
from datetime import datetime, timezone, timedelta
from anthropic import AsyncAnthropic
from menu import MENU_TEXTO
from orders import save_order, update_order, cancel_order
import db

db.init_db()

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
    if ahora.weekday() not in (4, 5, 6):
        return False
    return 17 <= ahora.hour < 23


def mensaje_fuera_horario() -> str:
    return (
        "¡Hola! 👋 Gracias por escribirnos.\n\n"
        "En este momento estamos cerrados 😔\n\n"
        "🕒 Atendemos:\n"
        "*Viernes, Sábado y Domingo*\n"
        "de *5:00 pm a 11:00 pm*\n\n"
        "¡Te esperamos pronto para taquear rico! 🌮"
    )


def mensaje_bienvenida() -> str:
    return (
        "¡Qué onda! 👋 Soy *Chilo*, tu asistente de *Chilango*.\n\n"
        "Somos un restaurante mexicano de delivery en Tacna. "
        "Tenemos tacos, quesabirrias, burritos y todo lo que necesitas para taquear rico. 🌮🌯\n\n"
        "🕒 *Horario:* Viernes, Sábado y Domingo de 5:00 pm a 11:00 pm.\n\n"
        "¿Qué te apetece hoy?\n\n"
        "1️⃣ Ver carta\n"
        "2️⃣ Hacer un pedido"
    )


SYSTEM_PROMPT = f"""Eres *Chilo*, el asistente virtual de Chilango, restaurante mexicano de delivery en Tacna, Perú.
Tienes personalidad amigable, con onda mexicana auténtica. Eres entusiasta con la comida pero vas al grano.

━━━ DATOS DEL RESTAURANTE ━━━
- Nombre: Chilango 🌮
- Ciudad: Tacna, Perú
- Modalidad: Delivery y recojo en tienda
- Dirección para recojo: Asoc. Ricardo Odonovan Mz H-5, calle Las Poncianas, atrás del Terminal Flores
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
   - "¿Tienen opciones sin picante?" → Sí, guía al cliente
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
   - Pregunta si es delivery o recojo en tienda
     * Si es delivery: pide la dirección de entrega (calle, número y referencia)
     * Si es recojo: indica la dirección "Asoc. Ricardo Odonovan Mz H-5, calle Las Poncianas, atrás del Terminal Flores" y registra "Recojo" como dirección
   - Confirma el pedido mostrando el resumen final con la modalidad elegida

4. CONFIRMAR PEDIDO: Cuando el cliente confirme (diga "sí", "correcto", "dale", etc.),
   muestra el resumen final y si el pago es por Yape o Plin:
   - Indica: "📲 Puedes yapear/plinear al *{YAPE_PLIN_NUMBER}*"
   - Solicita: "Por favor envíanos la captura del pago para confirmar tu pedido ✅"
   - Solo cuando el cliente envíe la captura de pago, verifica la imagen:
     * Si el monto en la imagen coincide con el total del pedido: confirma y agrega el tag de pedido
     * Si el monto es menor al total: indica la diferencia y pide que complete el pago
     * Si no se puede leer el monto claramente: pide una captura más nítida
   Si el pago es en Efectivo, incluye el tag de pedido directamente al confirmar.

   FORMATO EXACTO DEL TAG NUEVO PEDIDO (4 campos obligatorios):
   [PEDIDO_OK|items: <descripción>|total: S/ XX.XX|pago: <Yape|Plin|Efectivo>|dir: <dirección completa>]
   Ejemplos:
   [PEDIDO_OK|items: 2x Taco Suadero, 1x Agua Jamaica|total: S/ 15.00|pago: Yape|dir: Av. Bolognesi 456, frente al parque]
   [PEDIDO_OK|items: 1x Quesabirria, 1x Esquites|total: S/ 20.00|pago: Efectivo|dir: Calle Lima 123]

5. MODIFICACIONES: Si el cliente ya tiene un pedido confirmado y quiere cambiarlo:
   - Escucha qué quiere modificar (agregar, quitar o cambiar ítems)
   - Muestra el nuevo resumen completo con el total recalculado y la dirección confirmada
   - Pide confirmación ("¿Confirmas el cambio?")
   - Cuando confirme, incluye el tag de modificación al final de tu respuesta:

   FORMATO EXACTO DEL TAG MODIFICACIÓN (4 campos obligatorios):
   [PEDIDO_MOD|items: <pedido completo actualizado>|total: S/ XX.XX|pago: <Yape|Plin|Efectivo>|dir: <dirección>]
   Ejemplo:
   [PEDIDO_MOD|items: 3x Taco Suadero, 1x Agua Jamaica|total: S/ 21.50|pago: Yape|dir: Av. Bolognesi 456, frente al parque]

   REGLA: usa [PEDIDO_OK|...] solo para pedidos nuevos y [PEDIDO_MOD|...] solo para modificaciones.

6. CANCELACIONES: Si el cliente quiere cancelar su pedido:
   - Pregunta si está seguro ("¿Confirmas que deseas cancelar tu pedido?")
   - Si confirma, incluye el tag al final de tu respuesta: [PEDIDO_CANCEL]
   - Responde con un mensaje amable indicando que el pedido fue cancelado

7. ESCALACIÓN: Si el cliente escribe "humano", "agente" o "hablar con alguien",
   dile que el equipo lo atenderá pronto al 954 713 696.

8. ESTADO DEL PEDIDO: SOLO si el cliente ya tiene un pedido confirmado previamente y en un mensaje
   posterior pregunta explícitamente por él (ej: "¿ya salió?", "¿dónde está?", "¿cuánto falta?",
   "¿ya lo mandaron?"), responde de forma breve y tranquilizadora. Ejemplos:
   - "¡Ya casi! Tu pedido está en los últimos detalles 🌮"
   - "¡Lo están preparando con todo el sabor! 🔥"
   NUNCA uses estas frases al confirmar un pedido nuevo. Nunca menciones tiempos exactos ni
   redirijas al WhatsApp del equipo. Máximo 2 líneas.

9. TONO: Español amigable, sin exagerar la jerga. Emojis con moderación. Respuestas cortas y claras.

IMPORTANTE: Nunca inventes precios ni productos que no estén en la carta.
"""


def _extract_tag(reply: str, tag_name: str) -> tuple[dict | None, str]:
    """Extrae un tag del reply y devuelve (campos, reply_limpio)."""
    marker = f"[{tag_name}|"
    if marker not in reply:
        return None, reply
    try:
        start = reply.index(marker)
        end = reply.index("]", start)
        tag = reply[start: end + 1]
        parts = tag[len(marker):-1].split("|")
        fields = {
            "items": parts[0].replace("items: ", "").strip(),
            "total": parts[1].replace("total: ", "").strip(),
            "pago":  parts[2].replace("pago: ", "").strip() if len(parts) > 2 else "Efectivo",
            "dir":   parts[3].replace("dir: ", "").strip()  if len(parts) > 3 else "",
        }
        reply_clean = (reply[:start] + reply[end + 1:]).strip()
        return fields, reply_clean
    except Exception as e:
        print(f"[ERROR] No se pudo parsear [{tag_name}]: {e}")
        return None, reply


async def _parse_and_save_order(phone: str, reply: str) -> str:
    # Pedido nuevo
    fields, reply = _extract_tag(reply, "PEDIDO_OK")
    if fields:
        await save_order(phone, fields["items"], fields["total"], fields["pago"], fields["dir"])

    # Modificación de pedido existente
    fields, reply = _extract_tag(reply, "PEDIDO_MOD")
    if fields:
        await update_order(phone, fields["items"], fields["total"], fields["pago"], fields["dir"])

    # Cancelación de pedido
    if "[PEDIDO_CANCEL]" in reply:
        reply = reply.replace("[PEDIDO_CANCEL]", "").strip()
        await cancel_order(phone)

    return reply


async def _call_claude(phone: str, messages: list) -> str:
    history = messages[-30:]
    response = await get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=history,
    )
    return response.content[0].text


async def process_message(phone: str, message: str) -> str:
    if not esta_en_horario():
        return mensaje_fuera_horario()

    messages = db.get_messages(phone)
    messages.append({"role": "user", "content": message})

    reply = await _call_claude(phone, messages)
    reply = await _parse_and_save_order(phone, reply)

    messages.append({"role": "assistant", "content": reply})
    db.save_messages(phone, messages)

    return reply


async def process_message_with_image(phone: str, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    if not esta_en_horario():
        return mensaje_fuera_horario()

    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    messages = db.get_messages(phone)
    messages.append({
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
            {"type": "text", "text": "Te envío la captura del pago."},
        ],
    })

    reply = await _call_claude(phone, messages)
    reply = await _parse_and_save_order(phone, reply)

    messages.append({"role": "assistant", "content": reply})
    db.save_messages(phone, messages)

    return reply


def reset_conversation(phone: str):
    db.reset_conv(phone)
