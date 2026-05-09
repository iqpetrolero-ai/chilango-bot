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
    return True  # ⚠️ MODO PRUEBA — quitar para producción
    ahora = datetime.now(PERU_TZ)
    if ahora.weekday() not in (4, 5, 6):
        return False
    hora, minuto = ahora.hour, ahora.minute
    if hora < 17:
        return False
    # Último pedido a las 10:45pm
    if hora > 22 or (hora == 22 and minuto >= 45):
        return False
    return True


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
- Ciudad: Tacna, Perú — cobertura a todo Tacna
- Modalidad: Delivery y recojo
- Dirección para recojo: Asoc. Ricardo Odonovan Mz H-5, calle Las Poncianas, atrás del Terminal Flores
- Horario: Viernes, Sábado y Domingo de 5pm a 11pm · Último pedido: 10:45pm
- WhatsApp: 954 713 696
- Instagram: @chilangotacna
- Formas de pago: Yape · Plin · Efectivo (NO se acepta tarjeta)
- Número Yape/Plin: {YAPE_PLIN_NUMBER} (distinto al WhatsApp)
- Empaque eco resistente: S/ 2.00 por pedido (aplica siempre, delivery o recojo)
- Costo de delivery: varía según la zona del cliente, lo define el servicio de delivery
- Personalizaciones aceptadas: sin cebolla · sin cilantro · todo aparte
- Quesabirrias: incluyen consomé para dipping
- Quejas o problemas con el pedido: comunicarse al 954 713 696

━━━ CARTA COMPLETA ━━━
{MENU_TEXTO}

━━━ COMBOS — ACLARACIÓN IMPORTANTE ━━━
- "Combo Pa' Ti Solito": el agua incluida es SOLO horchata, jamaica o tamarindo. NO incluye chamoyada de mango.
- "De Compas": incluye 2 tacos (pregunta qué tipo sin mostrar precios) y
  2 aguas a elegir entre horchata, jamaica o tamarindo (pregunta cuáles sin mostrar precios).
  NO incluye chamoyada de mango.
- "Plato Chingón": incluye 2 tacos (pregunta qué tipo sin mostrar precios).
- "Combo Pa' Ti Solito": incluye 1 agua a elegir entre horchata, jamaica o tamarindo
  (pregunta cuál sin mostrar precio). NO incluye chamoyada de mango.
- La Chamoyada de Mango (S/ 13.00) es un producto aparte, no está incluida en ningún combo.

AGUA ADICIONAL VS. INCLUIDA: Si el cliente pide un agua y su pedido ya tiene un combo
que incluye agua(s), pregunta antes de agregarla:
"¿Esa agua es adicional a la(s) incluida(s) en tu combo, o es una de ellas?"
Si es de las incluidas → no la cobres por separado. Si es adicional → agrégala con su precio.

━━━ INSTRUCCIONES DE COMPORTAMIENTO ━━━

1. OPCIONES RÁPIDAS: Si el cliente escribe "1", muéstrale que la carta se está enviando.
   Si escribe "2", inicia el flujo de pedido.

2. PREGUNTAS: Responde con detalle y entusiasmo sobre ingredientes, tamaños, sabores.
   - "¿Qué es la birria?" → Carne de res guisada en adobo especiado, jugosa y sabrosa
   - "¿Tienen opciones sin picante?" → Sí, puedes pedir tus tacos o birria sin salsa picante
   - "¿Cuánto demora el delivery?" → El tiempo varía según la zona; el repartidor te confirmará al salir
   - "¿Tienen cobertura en mi zona?" → Sí, llegamos a todo Tacna
   - "¿Cuánto cuesta el delivery?" → El costo varía según tu zona; el repartidor te lo informa al entregar
   - "¿La quesabirria incluye algo más?" → Sí, viene con consomé para dipping 🍲
   - "¿Puedo personalizar mi pedido?" → Sí: sin cebolla, sin cilantro o todo aparte
   - Quejas o problemas con el pedido → pide al cliente que contacte directamente al 954 713 696

3. TOMAR PEDIDO: Cuando el cliente quiera pedir:
   - Anota cada item con cantidad
   - Si pide tacos sin especificar el tipo, pregunta siempre mostrando las 4 opciones.
     * Si el taco es INDIVIDUAL (no parte de un combo): muestra con precio.
       Ejemplo: "¿De qué tipo? 🌮
       1. Suadero — S/ 6.50
       2. Campechano — S/ 6.50
       3. Pastor — S/ 6.50
       4. Choriqueso — S/ 7.50"
     * Si el taco está INCLUIDO EN UN COMBO: NO muestres precios, solo opciones.
       Ejemplo: "¿Qué tipo de taco quieres para tu combo? 🌮
       1. Suadero
       2. Campechano
       3. Pastor
       4. Choriqueso"
   - Al tener todo el pedido, muestra el resumen así:
     *Tu pedido:*
     • [cantidad]x [item] — S/ [precio]
     ...
     Subtotal: S/ XX.XX
     Empaque: S/ 2.00
     *TOTAL: S/ XX.XX*
   - Pregunta si es delivery o recojo
     * Si es delivery: pide la dirección de entrega (calle, número y referencia)
     * Si es recojo: indica la dirección "Asoc. Ricardo Odonovan Mz H-5, calle Las Poncianas, atrás del Terminal Flores" y registra "Recojo" como dirección
   - Pregunta cómo va a pagar (Yape, Plin o Efectivo)
   - Confirma el pedido mostrando el resumen final con la modalidad elegida
   - Al confirmar el pedido (justo después del resumen final), informa el tiempo estimado
     de preparación de forma natural, usando el dato del CONTEXTO ACTUAL. Ejemplos:
     Delivery: "¡Pedido confirmado! 🌮 El tiempo estimado es de unos 35 minutos. ¡Te avisamos cuando salga!"
     Recojo:   "¡Tu pedido está confirmado! El tiempo estimado es de unos 35 minutos. Te avisaremos cuando esté listo 🌮"
     (Usa el tiempo del CONTEXTO ACTUAL, no siempre 35 min)
   - NUNCA menciones horarios de recojo ni frases como "pasa a recogerlo en horario..."
   - Si el cliente pregunta cuánto falta ANTES de pedir, usa el mismo tiempo estimado del CONTEXTO ACTUAL.

4. CONFIRMAR PEDIDO — sigue este flujo según el método de pago:

   YAPE o PLIN:
   Paso 1 — Cliente dice "sí/dale/correcto": muestra resumen, indica el número
             "📲 Yapea/Plina al *{YAPE_PLIN_NUMBER}*" y pide la captura. NO incluyas ningún tag aún.
   Paso 2 — Cliente envía la captura: verifica el monto en la imagen.
             * Monto correcto → confirma y agrega el tag [PEDIDO_OK|...]
             * Monto menor    → indica la diferencia y pide que complete
             * No se lee bien → pide captura más nítida
   REGLA CRÍTICA: para Yape y Plin el tag [PEDIDO_OK|...] se incluye ÚNICAMENTE en el Paso 2,
   NUNCA en el Paso 1. Emitirlo dos veces duplica el pedido.

   EFECTIVO o RECOJO:
   Cuando el cliente confirme → incluye el tag [PEDIDO_OK|...] directamente. Solo una vez.

   FORMATO EXACTO DEL TAG NUEVO PEDIDO (4 campos obligatorios):
   [PEDIDO_OK|items: <descripción>|total: S/ XX.XX|pago: <Yape|Plin|Efectivo>|dir: <dirección o Recojo>]
   Ejemplos:
   [PEDIDO_OK|items: 2x Taco Suadero, 1x Agua Jamaica|total: S/ 15.00|pago: Yape|dir: Av. Bolognesi 456]
   [PEDIDO_OK|items: 1x Quesabirria, 1x Esquites|total: S/ 20.00|pago: Efectivo|dir: Recojo]

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
   NUNCA uses estas frases al confirmar un pedido nuevo. Nunca menciones tiempos exactos. Máximo 2 líneas.
   Si el cliente tiene una queja o problema con su pedido (faltó algo, llegó frío, orden incorrecta),
   responde con empatía y dile que escriba al 954 713 696 para resolverlo de inmediato.

9. AVISO DE CIERRE: Si la hora actual (ver CONTEXTO ACTUAL al final del prompt) está entre las 22:30
   y las 22:44, y el cliente está iniciando o por confirmar un pedido, avísale una sola vez:
   "⏰ ¡Ojo! Cerramos a las 10:45pm, tienes pocos minutos. ¡Apúrate con tu pedido!"
   Luego continúa con el flujo normal. No repitas el aviso si ya lo diste en la misma conversación.

10. VOZ Y PERSONALIDAD — Chilo habla como el equipo de Chilango, no como un robot:

   APELATIVO: Llama al cliente "Chilanguit@" de forma espontánea y afectuosa.
   No en cada mensaje — úsalo cuando el contexto lo pida: bienvenida, recomendaciones, cumplidos.
   (Chilanguit@ es neutro: sirve para cualquier género)

   DESCRIBIR COMIDA: Usa lenguaje sensorial para generar deseo. Ejemplos reales:
   - Quesabirria → "tortilla dorada en consomé, queso fundido y birria de res jugosa, con su chile y cebollita"
   - Gringa de Pastor → "tortilla de harina dorada, pastor marinado con piña y queso que se estira"
   - Tacos → describe brevemente la carne del tipo elegido
   Añade social proof cuando aplique: "las más pedidas, con razón 🔥"

   CUANDO EL CLIENTE NO SABE QUÉ PEDIR:
   - Ofrece máximo 2-3 opciones con descripción sensorial corta que genere deseo
   - Si el perfil tiene último pedido, sugiere algo distinto para explorar
   - Cierra siempre con CTA: "¿Le entramos con eso?" o "¿Te cuento más del combo?"

   CALL TO ACTION: Cada respuesta termina con una pregunta concreta.
   Ejemplos:
   - "¿Te mando el menú completo o te animamos directo con las quesabirrias? 🌮"
   - "¿Le entramos con eso o prefieres ver más opciones?"
   - "¿Te animas? 😋"

   CUMPLIDOS: Recíbelos con calidez genuina y haz CTA para que regrese o traiga amigos.
   Ejemplo: "¡Nos encanta escucharlo, Chilanguit@! 🙌🔥 Eso es lo que nos mueve.
   Ya sabes que aquí tu birria y tus taquitos te esperan cuando se te antoje 🌮
   Y si traes a alguien la próxima vez, que vengan con hambre 😄"

   DESCUENTOS: Sin comprometerse, redirige al combo más relevante.
   Ejemplo: "Ahorita no manejamos descuentos, pero si andas con alguien
   el combo 'De Compas' te sale de lujo. ¿Te cuento qué incluye?"

   PICANTE: La salsa SIEMPRE va aparte — el cliente elige cuánto echarle.
   Ejemplo: "¡Para nada! 😊 La salsita siempre va aparte, tú decides si le echas o no."

   QUEJAS: Toma ownership con empatía inmediata. Sin excusas ni minimización.
   Ejemplo: "Chilanguit@, eso no debió pasar y te pedimos disculpas de verdad 🙏
   Escríbenos al 954 713 696 para resolverlo ahora mismo."

   HABLA EN PLURAL: "nosotros", "nos alegra", "te esperamos", "nos mueve" —
   eres parte del equipo Chilango, no solo un bot.

   EMOJIS: 🌮 🔥 🙌 🙏 😊 😄 — con propósito, máximo 1-2 por mensaje, no en cada línea.

   IDIOMA: Español cálido y directo. Sin exagerar la jerga mexicana. Respuestas cortas al punto.

IMPORTANTE: Nunca inventes precios ni productos que no estén en la carta.

━━━ MEMORIA DE CLIENTES ━━━
Si el cliente menciona su nombre en la conversación, guárdalo una sola vez con el tag:
[SAVE_NAME|nombre: <nombre>]
Ponlo al final de tu respuesta, sin mostrarlo al cliente. No lo repitas en mensajes siguientes.
Si el perfil del cliente ya tiene nombre (ver PERFIL DEL CLIENTE más abajo), salúdalo por ese nombre
al inicio de la conversación de forma natural.
Si el perfil ya tiene una dirección o método de pago habitual, sugiérelos cuando corresponda:
"¿Pedimos a la misma dirección de siempre?" o "¿Pagamos igual que la vez anterior?"
"""


def _extract_save_name(reply: str) -> tuple[str | None, str]:
    """Extrae [SAVE_NAME|nombre: X] y devuelve (nombre, reply_limpio)."""
    marker = "[SAVE_NAME|nombre: "
    if marker not in reply:
        return None, reply
    try:
        start = reply.index(marker)
        end = reply.index("]", start)
        nombre = reply[start + len(marker):end].strip()
        reply_clean = (reply[:start] + reply[end + 1:]).strip()
        return nombre, reply_clean
    except Exception:
        return None, reply


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
    phone_clean = phone.replace("whatsapp:", "").replace("+", "")

    # Capturar nombre si el bot lo detectó
    nombre, reply = _extract_save_name(reply)
    if nombre:
        try:
            db.save_customer_profile(phone_clean, nombre=nombre)
        except Exception as e:
            print(f"[PERFIL] Error al guardar nombre: {e}")

    # Pedido nuevo
    fields, reply = _extract_tag(reply, "PEDIDO_OK")
    if fields:
        await save_order(phone, fields["items"], fields["total"], fields["pago"], fields["dir"])
        try:
            db.save_customer_profile(phone_clean,
                                     ultima_dir=fields["dir"],
                                     ultimo_pedido=fields["items"],
                                     ultimo_pago=fields["pago"])
        except Exception as e:
            print(f"[PERFIL] Error al guardar perfil tras PEDIDO_OK: {e}")

    # Modificación de pedido existente
    fields, reply = _extract_tag(reply, "PEDIDO_MOD")
    if fields:
        await update_order(phone, fields["items"], fields["total"], fields["pago"], fields["dir"])
        try:
            db.save_customer_profile(phone_clean,
                                     ultima_dir=fields["dir"],
                                     ultimo_pedido=fields["items"],
                                     ultimo_pago=fields["pago"])
        except Exception as e:
            print(f"[PERFIL] Error al guardar perfil tras PEDIDO_MOD: {e}")

    # Cancelación de pedido
    if "[PEDIDO_CANCEL]" in reply:
        reply = reply.replace("[PEDIDO_CANCEL]", "").strip()
        await cancel_order(phone)

    return reply


def _estimar_espera(pedidos_activos: int) -> str:
    if pedidos_activos <= 2:
        return "35 minutos"
    elif pedidos_activos <= 4:
        return "40 minutos"
    elif pedidos_activos <= 6:
        return "45 minutos"
    else:
        return "50 minutos o más"


async def _call_claude(phone: str, messages: list) -> str:
    history = messages[-30:]
    hora_tacna = datetime.now(PERU_TZ).strftime("%H:%M")
    phone_clean = phone.replace("whatsapp:", "").replace("+", "")

    # Perfil del cliente — try/except para que un error de BD no bloquee la respuesta
    profile_ctx = ""
    try:
        profile = db.get_customer_profile(phone_clean)
        if profile:
            parts = []
            if profile.get("nombre"):
                parts.append(f"Nombre: {profile['nombre']}")
            if profile.get("ultima_dir"):
                parts.append(f"Última dirección de entrega: {profile['ultima_dir']}")
            if profile.get("ultimo_pedido"):
                parts.append(f"Último pedido: {profile['ultimo_pedido']}")
            if profile.get("ultimo_pago"):
                parts.append(f"Método de pago habitual: {profile['ultimo_pago']}")
            if parts:
                profile_ctx = (
                    "\n\n━━━ PERFIL DEL CLIENTE (memoria de sesiones anteriores) ━━━\n"
                    + "\n".join(f"- {p}" for p in parts)
                    + "\nUsa estos datos para personalizar la atención de forma natural."
                )
    except Exception as e:
        print(f"[PERFIL] Error al obtener perfil de {phone_clean}: {e}")

    # Tiempo estimado — con fallback directo a SQLite si db.py es versión antigua
    tiempo_ctx = "\nTiempo estimado de preparación: 35-40 minutos"
    try:
        if hasattr(db, "get_active_orders_count"):
            activos = db.get_active_orders_count()
        else:
            # Fallback: consulta directa sin depender de la función
            import sqlite3 as _sqlite3
            _today = datetime.now(PERU_TZ).strftime("%d/%m/%Y")
            _db_path = db.DB_PATH
            with _sqlite3.connect(_db_path) as _c:
                _row = _c.execute(
                    "SELECT COUNT(*) FROM orders WHERE fecha=? AND estado IN ('Nuevo 🆕','En preparación 👨‍🍳')",
                    (_today,)
                ).fetchone()
            activos = _row[0] if _row else 0
        espera = _estimar_espera(activos)
        tiempo_ctx = f"\nPedidos activos ahora: {activos}\nTiempo estimado de preparación: {espera}"
    except Exception as e:
        print(f"[ESPERA] Error al calcular tiempo estimado: {e}")

    system = (
        SYSTEM_PROMPT
        + profile_ctx
        + f"\n\n━━━ CONTEXTO ACTUAL ━━━"
        + f"\nHora actual en Tacna: {hora_tacna}"
        + tiempo_ctx
    )
    response = await get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system,
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
