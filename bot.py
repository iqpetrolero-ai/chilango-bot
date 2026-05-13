import os
import base64
import httpx
from datetime import datetime, timezone, timedelta
from anthropic import AsyncAnthropic
from menu import MENU_TEXTO
from orders import save_order, update_order, cancel_order, notify_delivery_cost_query
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
    return True  # ⚠️ MODO PRUEBA — deshabilitar antes del viernes
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
        "¡Qué onda! 👋 Soy *Chili*, tu asistente de *Chilango*.\n\n"
        "Somos un restaurante mexicano de delivery en Tacna. "
        "Tenemos tacos, quesabirrias, burritos y todo lo que necesitas para taquear rico. 🌯\n\n"
        "🕒 *Horario:* Viernes, Sábado y Domingo de 5:00 pm a 11:00 pm.\n\n"
        "¿Qué te apetece hoy?"
    )


SYSTEM_PROMPT = f"""Eres *Chili*, el asistente virtual de Chilango, restaurante mexicano de delivery en Tacna, Perú.
Tienes personalidad amigable, con onda mexicana auténtica. Eres entusiasta con la comida pero vas al grano.

━━━ DATOS DEL RESTAURANTE ━━━
- Nombre: Chilango 🌮
- Ciudad: Tacna, Perú — cobertura a todo Tacna
- Modalidad: Delivery y recojo
- Dirección para recojo: Asoc. Ricardo Odonovan Mz H-5, calle Las Poncianas, atrás del Terminal Flores
- Horario: Viernes, Sábado y Domingo de 5pm a 11pm · Último pedido: 10:45pm
- Instagram: @chilangotacna
- Formas de pago: Yape/Plin · Efectivo (NO se acepta tarjeta)
- Número Yape/Plin: {YAPE_PLIN_NUMBER} (distinto al WhatsApp)
- Empaque eco resistente: S/ 2.00 por pedido (aplica siempre, delivery o recojo)
- Costo de delivery: varía según la zona del cliente, lo define el servicio de delivery
- Personalizaciones aceptadas: sin cebolla · sin cilantro · todo aparte
- Quesabirrias: incluyen consomé para dipping
- Quejas o problemas con el pedido: el equipo los atiende directamente en este chat

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
   - "¿Puedo pagar el delivery incluido en el pedido?" → ver punto 11
   - "¿La quesabirria incluye algo más?" → Sí, viene con consomé para dipping 🍲
   - "¿Puedo personalizar mi pedido?" → Sí: sin cebolla, sin cilantro o todo aparte
   - Quejas de sabor, temperatura o producto incorrecto → ver punto 12

3. TOMAR PEDIDO: Cuando el cliente quiera pedir:
   - Anota cada item con cantidad
   - Si pide tacos sin especificar el tipo, pregunta mostrando las 4 opciones:
     * Taco INDIVIDUAL (no en combo): muestra con precio.
       Ej: "¿De qué tipo? 🌮 1. Suadero — S/ 6.50  2. Campechano — S/ 6.50  3. Pastor — S/ 6.50  4. Choriqueso — S/ 7.50"
     * Taco INCLUIDO EN COMBO: sin precios.
       Ej: "¿Qué tipo de taco para tu combo? 1. Suadero  2. Campechano  3. Pastor  4. Choriqueso"
   - Al tener el pedido completo, muestra resumen Y pregunta TODO en el MISMO mensaje:

     *Tu pedido:*
     • [cantidad]x [item] — S/ [precio]
     ...
     Subtotal: S/ XX.XX
     Empaque: S/ 2.00
     *TOTAL: S/ XX.XX*

     ¿Te lo llevamos a domicilio o recoges en el local? Si es delivery, dime tu dirección (calle, número y referencia). ¿Y cómo pagas: Yape/Plin o efectivo?

   - Si el perfil ya tiene última dirección, sugiere: "¿Pedimos a [dir] o cambias la dirección?"
   - El cliente puede responder todo junto (ej: "delivery, Jr. Tacna 123, Yape/Plin").
     Procesa lo que dé. Si falta la dirección en delivery, pídela en un mensaje breve.
   - Si es recojo: indica "Asoc. Ricardo Odonovan Mz H-5, calle Las Poncianas, atrás del Terminal Flores"
     y registra "Recojo" como dirección.
   - NUNCA menciones horarios de recojo ni frases como "pasa a recogerlo en horario..."
   - Si el cliente pregunta cuánto falta ANTES de pedir, usa el tiempo del CONTEXTO ACTUAL.

4. CONFIRMAR PEDIDO — flujo según método de pago:

   YAPE/PLIN:
   Paso 1 — Una vez que tienes dirección y el cliente eligió Yape/Plin: indica
             "📲 Yapea o Plina al *{YAPE_PLIN_NUMBER}* a nombre de *Karla Saldaña*" y pide la captura. NO incluyas ningún tag aún.
   Paso 2 — Cliente envía la captura: verifica el monto en la imagen.
             * Monto correcto → confirma, informa tiempo estimado (CONTEXTO ACTUAL) y agrega [PEDIDO_OK|...]
             * Monto menor    → indica la diferencia y pide que complete
             * No se lee bien → pide captura más nítida
   REGLA CRÍTICA: [PEDIDO_OK|...] va ÚNICAMENTE en el Paso 2. Nunca en el Paso 1.

   EFECTIVO:
   Cuando el cliente confirme → informa tiempo estimado (CONTEXTO ACTUAL) e incluye [PEDIDO_OK|...]. Solo una vez.
   Ej delivery: "¡Pedido confirmado! 🌮 Tiempo estimado: unos [X] minutos. ¡Te avisamos cuando salga!"
   Ej recojo:   "¡Confirmado! Tiempo estimado: unos [X] minutos. Te avisamos cuando esté listo 🌮"

   FORMATO EXACTO DEL TAG NUEVO PEDIDO (5 campos):
   [PEDIDO_OK|items: <descripción>|total: S/ XX.XX|pago: <Yape/Plin|Efectivo>|dir: <dirección o Recojo>|notas: <personalizaciones o dejar vacío>]
   Ejemplos:
   [PEDIDO_OK|items: 2x Taco Suadero, 1x Agua Jamaica|total: S/ 15.00|pago: Yape/Plin|dir: Av. Bolognesi 456|notas: sin cebolla]
   [PEDIDO_OK|items: 1x Quesabirria, 1x Esquites|total: S/ 20.00|pago: Efectivo|dir: Recojo|notas: ]

5. MODIFICACIONES: Si el cliente ya tiene un pedido confirmado y quiere cambiarlo:
   - Escucha qué quiere modificar (agregar, quitar o cambiar ítems)
   - Muestra el nuevo resumen completo con el total recalculado y la dirección confirmada
   - Pide confirmación ("¿Confirmas el cambio?")
   - Cuando confirme, incluye el tag de modificación al final de tu respuesta:

   FORMATO EXACTO DEL TAG MODIFICACIÓN (5 campos):
   [PEDIDO_MOD|items: <pedido completo actualizado>|total: S/ XX.XX|pago: <Yape/Plin|Efectivo>|dir: <dirección>|notas: <personalizaciones o dejar vacío>]
   Ejemplo:
   [PEDIDO_MOD|items: 3x Taco Suadero, 1x Agua Jamaica|total: S/ 21.50|pago: Yape/Plin|dir: Av. Bolognesi 456, frente al parque|notas: sin cilantro]

   REGLA: usa [PEDIDO_OK|...] solo para pedidos nuevos y [PEDIDO_MOD|...] solo para modificaciones.

6. CANCELACIONES: Si el cliente quiere cancelar su pedido:
   - Pregunta si está seguro ("¿Confirmas que deseas cancelar tu pedido?")
   - Si confirma, incluye el tag al final de tu respuesta: [PEDIDO_CANCEL]
   - Responde con un mensaje amable indicando que el pedido fue cancelado

7. ESCALACIÓN: Si el cliente escribe "humano", "agente", "hablar con alguien",
   "quiero hablar con una persona" o similar:
   - Responde con calidez: "Claro, con gusto te conectamos con alguien del equipo 👨‍💼"
   - Agrega al final de tu respuesta el tag: [ESCALATE]
   - NO menciones ningún número de teléfono.

8. ESTADO DEL PEDIDO:
   ⛔ NUNCA digas frases como "ya está en camino", "ya salió", "ya lo mandamos", "ya está listo",
   "en preparación" ni ninguna frase que indique el estado actual del pedido.
   TÚ NO TIENES INFORMACIÓN del estado real — eso lo maneja el equipo en el panel interno.
   Inventar el estado genera confusión y reclamos.

   SOLO si el cliente pregunta EXPLÍCITAMENTE por su pedido (ej: "¿ya salió?", "¿dónde está?",
   "¿cuánto falta?", "¿ya lo mandaron?"), responde de forma genérica y tranquilizadora:
   - "¡El equipo ya está en ello! En cuanto salga te avisamos 🌮"
   - "¡Lo están preparando con todo el sabor! Te notificamos cuando esté en camino 🔥"
   Nunca menciones tiempos exactos. Máximo 2 líneas.

   Si el cliente dice "gracias", "ok", "perfecto", "listo" u otras frases de cierre → responde
   brevemente ("¡Con gusto! 😊") SIN mencionar nada del estado del pedido.

   Si el cliente tiene una queja o problema con su pedido (faltó algo, llegó frío, orden incorrecta),
   responde con empatía y usa [ESCALATE] para conectarlo con el equipo aquí mismo.

9. AVISO DE CIERRE: Si la hora actual (ver CONTEXTO ACTUAL al final del prompt) está entre las 22:30
   y las 22:44, y el cliente está iniciando o por confirmar un pedido, avísale una sola vez:
   "⏰ ¡Ojo! Cerramos a las 10:45pm, tienes pocos minutos. ¡Apúrate con tu pedido!"
   Luego continúa con el flujo normal. No repitas el aviso si ya lo diste en la misma conversación.

10. VOZ Y PERSONALIDAD — Chili habla como el equipo de Chilango, no como un robot:

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
   Permítenos conectarte con alguien del equipo para resolverlo ahora mismo."
   (Agrega [QUEJA|desc: X] y [ESCALATE] al final, sin mostrarlos)

   HABLA EN PLURAL: "nosotros", "nos alegra", "te esperamos", "nos mueve" —
   eres parte del equipo Chilango, no solo un bot.

   EMOJIS: 🌮 🔥 🙌 🙏 😊 😄 — con propósito, máximo 1-2 por mensaje, no en cada línea.

   IDIOMA: Español cálido y directo. Sin exagerar la jerga mexicana. Respuestas cortas al punto.

11. DELIVERY INCLUIDO EN EL PAGO:
    Si el cliente quiere pagar el delivery junto con el pedido en un solo pago,
    sigue este flujo OBLIGATORIO — NO lo saltes bajo ninguna circunstancia:

    Paso 1 — Muestra el resumen de comida con el subtotal + empaque y di:
             "🛵 Entendido. El costo de comida es [subtotal+empaque].
              Vamos a consultar el costo de delivery a tu zona y
              te enviamos el total final antes de confirmar. ¡Un momento!"
             ⚠️ OBLIGATORIO — al final de tu respuesta agrega este tag exacto
             (el sistema lo elimina antes de mostrarlo al cliente, pero SIN él el motorizado
             no recibe la consulta y el flujo falla):
             [CONSULTAR_COSTO|dir: <dirección del cliente>|subtotal: S/ XX.XX|items: <descripción>|pago: <Yape/Plin|Efectivo>]
             Donde subtotal = precio comida + S/ 2.00 empaque, SIN delivery.
             ⛔ NUNCA omitas el tag [CONSULTAR_COSTO] en este paso — es la acción que activa el sistema.
             ⛔ NO emitas [PEDIDO_OK] ni [PEDIDO_MOD] en este paso.

    Paso 2 — El equipo contactará al motorizado y le informará el costo al cliente.
             Cuando el cliente responda confirmando el total completo
             (ej: "sí", "dale", "ok", o repita el monto total incluyendo delivery):
             Emite [PEDIDO_OK|items: <items>, Delivery: S/X.XX|total: S/ XX.XX|pago: ...|dir: ...|notas: ...]
             con el total que incluye la comida + empaque + delivery.

    REGLA CRÍTICA: Mientras el cliente no haya confirmado explícitamente el total
    que incluye el delivery, NUNCA emitas [PEDIDO_OK]. Si el cliente solo dice
    "quiero pagar el delivery incluido" o similar, eso NO es una confirmación del total.

    CASO ESPECIAL — CAMBIO DE MÉTODO DE PAGO DESPUÉS DE RECIBIR EL COSTO:
    Si en el historial ya aparece el mensaje con "¡Ya tenemos el costo!" y el total
    final (comida + delivery), y el cliente solo cambia el método de pago
    (ej: "en efectivo", "mejor efectivo", "pago en cash"):
    - NO vuelvas a emitir [CONSULTAR_COSTO] — el costo ya fue comunicado.
    - Confirma el cambio brevemente ("Anotado, pagamos en efectivo 💵") y emite
      [PEDIDO_OK] directamente con el total ya comunicado y el nuevo método de pago.

12. QUEJAS (sabor, temperatura, falta de producto, orden incorrecta):
    - Responde con ownership inmediato y empatía real. Sin excusas.
    - Pregunta brevemente qué estuvo mal para entender el problema.
    - Ofrece conectar con el equipo: "Permítenos conectarte con alguien para resolverlo ahora mismo."
    - Al final de tu respuesta, agrega los tags (sin mostrarlos al cliente):
      [QUEJA|desc: <resumen del problema>][ESCALATE]
    - Nunca ofrezcas descuentos ni devoluciones sin autorización del negocio.

IMPORTANTE: Nunca inventes precios ni productos que no estén en la carta.

━━━ MEMORIA DE CLIENTES ━━━
Si el cliente menciona EXPLÍCITAMENTE su propio nombre en la conversación
(frases como "me llamo X", "soy X", "mi nombre es X"), guárdalo UNA SOLA VEZ con el tag:
[SAVE_NAME|nombre: <nombre>]
Ponlo al final de tu respuesta, sin mostrarlo al cliente. No lo repitas en mensajes siguientes.

IMPORTANTE — NUNCA uses [SAVE_NAME] para:
- El nombre "Karla Saldaña" (es la dueña del negocio, no el cliente)
- Cualquier nombre proveniente del sistema (número de Yape, datos del restaurante, etc.)
- Nombres que el cliente no haya dicho claramente que son suyos

Si el perfil del cliente ya tiene nombre (ver PERFIL DEL CLIENTE más abajo), salúdalo por ese nombre
al inicio de la conversación de forma natural. NO uses "Karla" como apelativo genérico.
Si el perfil ya tiene una dirección o método de pago habitual, sugiérelos cuando corresponda:
"¿Pedimos a la misma dirección de siempre?" o "¿Pagamos igual que la vez anterior?"
"""


async def _notify_queja(phone_clean: str, desc: str):
    """Envía notificación al dueño cuando hay una queja de cliente."""
    try:
        token = os.environ.get("META_ACCESS_TOKEN", "").strip()
        pid = os.environ.get("META_PHONE_NUMBER_ID", "").strip()
        owner = "51954713696"
        if not token or not pid:
            print(f"[QUEJA] No se puede notificar: faltan vars de entorno")
            return
        mensaje = (
            f"⚠️ *QUEJA DE CLIENTE — Chilango*\n"
            f"👤 +{phone_clean}\n"
            f"📝 {desc or 'Sin descripción'}\n"
            f"🕒 {datetime.now(PERU_TZ).strftime('%d/%m · %I:%M %p')}"
        )
        url = f"https://graph.facebook.com/v19.0/{pid}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {"messaging_product": "whatsapp", "to": owner, "type": "text", "text": {"body": mensaje}}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                print(f"[QUEJA] Notificación enviada al dueño")
            else:
                print(f"[QUEJA] Error al notificar: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[QUEJA] Excepción: {e}")


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
            "pago":  parts[2].replace("pago: ", "").strip()  if len(parts) > 2 else "Efectivo",
            "dir":   parts[3].replace("dir: ", "").strip()   if len(parts) > 3 else "",
            "notas": parts[4].replace("notas: ", "").strip() if len(parts) > 4 else "",
        }
        reply_clean = (reply[:start] + reply[end + 1:]).strip()
        return fields, reply_clean
    except Exception as e:
        print(f"[ERROR] No se pudo parsear [{tag_name}]: {e}")
        return None, reply


async def _parse_and_save_order(phone: str, reply: str) -> tuple[str, bool]:
    """Retorna (reply_limpio, needs_escalate)."""
    phone_clean = phone.replace("whatsapp:", "").replace("+", "")
    needs_escalate = False

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
        await save_order(phone, fields["items"], fields["total"], fields["pago"], fields["dir"], fields.get("notas", ""))
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
        await update_order(phone, fields["items"], fields["total"], fields["pago"], fields["dir"], fields.get("notas", ""))
        try:
            db.save_customer_profile(phone_clean,
                                     ultima_dir=fields["dir"],
                                     ultimo_pedido=fields["items"],
                                     ultimo_pago=fields["pago"])
        except Exception as e:
            print(f"[PERFIL] Error al guardar perfil tras PEDIDO_MOD: {e}")

    # Consulta automática de costo de delivery al motorizado
    import re as _re
    _cc_match = _re.search(r'\[CONSULTAR_COSTO[\s|]([^\]]*)\]', reply, _re.IGNORECASE)
    if _cc_match:
        try:
            inner   = _cc_match.group(1)
            tag_start = _cc_match.start()
            tag_end   = _cc_match.end()
            fields_cc: dict = {}
            for part in inner.split("|"):
                if ":" in part:
                    k, v = part.split(":", 1)
                    fields_cc[k.strip().lower()] = v.strip()
            reply = (reply[:tag_start] + reply[tag_end:]).strip()
            print(f"[CONSULTAR_COSTO] Tag detectado — dir={fields_cc.get('dir')} subtotal={fields_cc.get('subtotal')} pago={fields_cc.get('pago')}")
            await notify_delivery_cost_query(
                phone_clean,
                fields_cc.get("dir", ""),
                fields_cc.get("subtotal", ""),
                fields_cc.get("items", ""),
                fields_cc.get("pago", ""),
            )
        except Exception as e:
            print(f"[CONSULTAR_COSTO] Error al procesar tag: {e}")

    # Cancelación de pedido
    if "[PEDIDO_CANCEL]" in reply:
        reply = reply.replace("[PEDIDO_CANCEL]", "").strip()
        await cancel_order(phone)

    # Queja de cliente → notificar al dueño + escalar
    if "[QUEJA|" in reply:
        try:
            start = reply.index("[QUEJA|")
            end = reply.index("]", start)
            tag_str = reply[start:end + 1]
            inner = tag_str[len("[QUEJA|"):-1]
            desc = ""
            for part in inner.split("|"):
                if part.startswith("desc:"):
                    desc = part[5:].strip()
            reply = (reply[:start] + reply[end + 1:]).strip()
            await _notify_queja(phone_clean, desc)
            needs_escalate = True
        except Exception as e:
            print(f"[QUEJA] Error al procesar tag: {e}")

    # Escalación manual solicitada por el cliente
    if "[ESCALATE]" in reply:
        reply = reply.replace("[ESCALATE]", "").strip()
        needs_escalate = True

    return reply, needs_escalate


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
    # Filtrar campos extra (ts, etc.) que la API de Claude no acepta
    history = [{"role": m["role"], "content": m["content"]} for m in messages[-30:]]
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

    # Pedido activo hoy — inyectar para evitar que Claude regenere [PEDIDO_OK]
    try:
        pedidos_hoy = [
            p for p in db.get_orders_today()
            if str(p.get("phone", "")) == phone_clean
            and p.get("estado") != "Cancelado ❌"
        ]
        if pedidos_hoy:
            p0 = pedidos_hoy[0]
            profile_ctx += (
                f"\n\n⚠️ PEDIDO YA REGISTRADO EN ESTA SESIÓN: #{p0['id']} — {p0['items']} — {p0['total']}"
                "\nEl pedido ESTÁ CONFIRMADO Y GUARDADO."
                "\n• NUNCA vuelvas a emitir [PEDIDO_OK] — el pedido ya está guardado."
                "\n• Si el cliente EXPLÍCITAMENTE pide cambiar algo (agregar, quitar o cambiar ítems),"
                " muestra el nuevo resumen completo con total recalculado, pide confirmación"
                " y cuando confirme usa [PEDIDO_MOD|...] con el pedido completo actualizado."
                "\n• Si dice 'gracias', 'ok', 'perfecto' u otras frases de cierre, responde MUY brevemente"
                " (ej: '¡Con gusto! 😊') sin ningún tag y SIN mencionar el estado del pedido —"
                " nunca digas 'en camino', 'ya salió', 'en preparación' ni similares."
            )
    except Exception as e:
        print(f"[PEDIDO-CTX] Error: {e}")

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


async def process_message(phone: str, message: str) -> tuple[str, bool]:
    """Retorna (reply_text, needs_escalate)."""
    if not esta_en_horario():
        return mensaje_fuera_horario(), False

    now_ts = datetime.now(PERU_TZ).strftime("%H:%M")
    messages = db.get_messages(phone)
    messages.append({"role": "user", "content": message, "ts": now_ts})

    reply = await _call_claude(phone, messages)
    reply, escalate = await _parse_and_save_order(phone, reply)

    messages.append({"role": "assistant", "content": reply, "ts": now_ts})
    db.save_messages(phone, messages)

    return reply, escalate


async def process_message_with_image(phone: str, image_bytes: bytes, mime_type: str = "image/jpeg") -> tuple[str, bool]:
    """Retorna (reply_text, needs_escalate)."""
    if not esta_en_horario():
        return mensaje_fuera_horario(), False

    now_ts = datetime.now(PERU_TZ).strftime("%H:%M")
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    messages = db.get_messages(phone)
    messages.append({
        "role": "user",
        "ts": now_ts,
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
    reply, escalate = await _parse_and_save_order(phone, reply)

    messages.append({"role": "assistant", "content": reply, "ts": now_ts})
    db.save_messages(phone, messages)

    return reply, escalate


def reset_conversation(phone: str):
    db.reset_conv(phone)
