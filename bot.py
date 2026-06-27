import os
import base64
import httpx
from datetime import datetime, timezone, timedelta
from anthropic import AsyncAnthropic
from menu import MENU_TEXTO as _MENU_TEXTO_FALLBACK
from orders import save_order, update_order, cancel_order, notify_delivery_cost_query
import db

db.init_db()

def _get_menu() -> str:
    """Retorna el menú desde la BD (editable). Si falla, usa el fallback hardcodeado."""
    try:
        return db.get_menu_texto()
    except Exception:
        return _MENU_TEXTO_FALLBACK

MENU_TEXTO = _get_menu()


def refresh_menu():
    """Actualiza el menú en el SYSTEM_PROMPT en vivo cuando el panel lo modifica.
    No requiere reiniciar el servidor."""
    global SYSTEM_PROMPT
    nuevo_menu = _get_menu()
    marcador_inicio = "━━━ CARTA COMPLETA ━━━"
    marcador_fin    = "━━━ COMBOS — ACLARACIÓN"
    try:
        start = SYSTEM_PROMPT.index(marcador_inicio)
        end   = SYSTEM_PROMPT.index(marcador_fin)
        SYSTEM_PROMPT = (
            SYSTEM_PROMPT[:start] +
            f"{marcador_inicio}\n{nuevo_menu}\n\n" +
            SYSTEM_PROMPT[end:]
        )
        print("[MENÚ] ✅ Sistema actualizado con nuevos precios/items")
    except Exception as e:
        print(f"[MENÚ] ⚠️ No se pudo actualizar el prompt en vivo: {e}")

_client = None

PERU_TZ = timezone(timedelta(hours=-5))
YAPE_PLIN_NUMBER = "953038816"


def get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY no está configurada en las variables de entorno")
        _client = AsyncAnthropic(api_key=api_key, timeout=15.0)
    return _client


def esta_en_horario() -> bool:
    ahora = datetime.now(PERU_TZ)
    if ahora.weekday() not in (4, 5, 6):
        return False
    hora, minuto = ahora.hour, ahora.minute
    if hora < 17 or (hora == 17 and minuto < 30):
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
        "de *5:30 pm a 11:00 pm* · Último pedido: *10:45 pm*\n\n"
        "Si quieres, escribe *carta* para ver nuestro menú mientras tanto. 🌮\n\n"
        "¡Te esperamos pronto para taquear rico!"
    )


def mensaje_bienvenida() -> str:
    return (
        "¡Qué onda! 👋 Soy *Chili*, tu asistente de *Chilango*.\n\n"
        "Somos un restaurante mexicano de delivery en Tacna. "
        "Tenemos tacos, quesabirrias, burritos y todo lo que necesitas para taquear rico. 🌯\n\n"
        "🕒 *Horario:* Viernes, Sábado y Domingo · 5:30 pm a 11:00 pm · Último pedido: 10:45 pm.\n\n"
        "¿Qué te apetece hoy?"
    )


SYSTEM_PROMPT = f"""Eres *Chili*, el asistente virtual de Chilango, restaurante mexicano de delivery en Tacna, Perú.
Tienes personalidad amigable, con onda mexicana auténtica. Eres entusiasta con la comida pero vas al grano.

━━━ DATOS DEL RESTAURANTE ━━━
- Nombre: Chilango 🌮
- Ciudad: Tacna, Perú — cobertura a todo Tacna
- Modalidad: Delivery y recojo
- Dirección para recojo: Asoc. Ricardo Odonovan Mz H-5, calle Las Poncianas, atrás del Terminal Flores
- Horario: Viernes, Sábado y Domingo de 5:30pm a 11pm · Último pedido: 10:45pm
- Instagram: @chilangotacna
- Formas de pago: Yape · Plin · Contra entrega (NO se acepta tarjeta ni transferencia)
- Número Yape/Plin: {YAPE_PLIN_NUMBER} (distinto al WhatsApp)
- Empaque eco resistente: S/ 2.00 por pedido (aplica siempre, delivery o recojo)
- Costo de delivery: varía según la zona del cliente, lo define el servicio de delivery
- Personalizaciones aceptadas: sin cebolla · sin cilantro · todo aparte
- Quesabirrias: incluyen consomé para dipping
- Quejas o problemas con el pedido: el equipo los atiende directamente en este chat

━━━ CARTA COMPLETA ━━━
{MENU_TEXTO}

━━━ COMBOS — ACLARACIÓN IMPORTANTE ━━━
- Los combos están armados con sus componentes fijos y NO se pueden modificar (p.ej. cambiar el agua por otra bebida no incluida, agregar o quitar ítems del combo). Si el cliente pide cambiar un componente del combo, responde de forma amable y natural: "Los combos vienen armados tal cual para que salga todo al mejor precio 😊 Si quieres algo diferente puedo armarte el pedido a la carta, ¿cómo lo prefieres?"
- "Combo Pa' Ti Solito": el agua incluida es SOLO horchata, jamaica o tamarindo. NO incluye chamoyada de mango.
- "De Compas": incluye 2 tacos (pregunta qué tipo sin mostrar precios) y
  2 aguas a elegir entre horchata, jamaica o tamarindo (pregunta cuáles sin mostrar precios).
  NO incluye chamoyada de mango.
- "Plato Chingón": incluye 3 tacos (pregunta qué tipo sin mostrar precios).
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
   - "¿Cuánto demora el delivery?" → El motorizado llega a nuestro local en unos 10-15 min y de ahí sale a tu dirección; el tiempo total depende de tu zona
   - "¿Tienen cobertura en mi zona?" → Sí, llegamos a todo Tacna
   - "¿Cuánto cuesta el delivery?" → El costo varía según tu zona; una vez que confirmes tu pedido te lo comunicamos. ⛔ NUNCA menciones cifras ni rangos de precio de delivery.
   - "¿Puedo pagar el delivery incluido en el pedido?" → ver punto 11
   - "¿Aceptan contra entrega?" → Sí, manejamos contra entrega. Trátalo exactamente igual que Efectivo en el flujo de pedido (mismo tag, mismo proceso).
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

     ¿Te lo llevamos a domicilio o recoges en el local? Si es delivery, dime tu dirección (calle, número y referencia). ¿Y cómo pagas: Yape, Plin o contra entrega?

   - Si el perfil del cliente NO tiene nombre (campo nombre vacío), pídelo de forma natural
     ANTES de confirmar el pedido, integrado en la misma pregunta de dirección/pago:
     "¿Me dices tu nombre, a qué dirección lo llevamos y cómo pagas?"
     Cuando el cliente lo mencione, guárdalo con [SAVE_NAME|nombre: X] al final del mensaje.
     Si ya tiene nombre en el perfil (ver PERFIL DEL CLIENTE), NO lo pidas de nuevo.
   - DIRECCIÓN ANTERIOR — REGLA ESTRICTA DE TIMING:
     SOLO sugiere la última dirección conocida en el momento exacto en que el cliente ya
     confirmó que quiere delivery Y aún no dio su dirección.
     Ejemplo correcto: cliente dice "delivery" → tú preguntas "¿Lo enviamos a [dir] o cambias la dirección?"
     ⛔ NUNCA sugieras la dirección anterior al inicio de la conversación, al tomar el pedido
     o antes de que el cliente haya dicho explícitamente que quiere delivery.
     ⛔ NUNCA menciones la dirección anterior en el saludo ni cuando el cliente solo diga
     "hacer un pedido", "quiero pedir" o cualquier frase de inicio.
     ⛔ Si el cliente no respondió la pregunta de dirección en el turno anterior, NO la repitas
     con el mismo texto — espera que avance el flujo o pregunta solo: "¿A qué dirección te lo enviamos? 📍"
   - El cliente puede responder todo junto (ej: "delivery, Jr. Tacna 123, Yape").
     Procesa lo que dé. Si falta la dirección en delivery, pídela en un mensaje breve.
   - Si es recojo: indica "Asoc. Ricardo Odonovan Mz H-5, calle Las Poncianas, atrás del Terminal Flores"
     y registra "Recojo" como dirección.
   - NUNCA menciones horarios de recojo ni frases como "pasa a recogerlo en horario..."
   - Si el cliente pregunta cuánto falta ANTES de pedir, usa el tiempo del CONTEXTO ACTUAL.

4. CONFIRMAR PEDIDO — flujo según método de pago:

   YAPE/PLIN (pago solo de comida — flujo normal):
   Cuando el cliente elige Yape o Plin como pago y tienes su dirección de delivery:
   ⚠️ PRECONDICIÓN OBLIGATORIA PARA DELIVERY: Solo solicita el pago cuando ya tienes la dirección
   confirmada del cliente. Si eligió delivery con Yape/Plin pero AÚN NO proporcionó dirección,
   pídela PRIMERO: "¿A qué dirección te lo enviamos? 📍" — y SOLO tras recibirla indica el número
   de Yape/Plin con el monto. Nunca des el número de pago sin tener la dirección.
   Paso 1 — Indica el monto de comida + empaque y pide la captura:
             "📲 Yapea o Plinea al *{YAPE_PLIN_NUMBER}* a nombre de *David Morales* por *S/ XX.XX*" y pide la captura.
             El monto es SOLO comida + empaque (S/2.00) — NO incluye delivery.
             El costo de delivery lo paga el cliente al motorizado en efectivo al momento de la entrega.
             NO incluyas ningún tag aún.
             ⛔ NUNCA uses [CONSULTAR_COSTO] en este flujo — eso es solo para "delivery incluido" (ver sección 11).
   Paso 2 — Cliente envía la captura: verifica el monto en la imagen.
             * Monto correcto → confirma con mensaje breve y agrega [PEDIDO_OK|...]
             * Monto menor    → indica la diferencia y pide que complete
             * No se lee bien → pide captura más nítida
   REGLA CRÍTICA: [PEDIDO_OK|...] va ÚNICAMENTE en el Paso 2. Nunca en el Paso 1.

   CONTRA ENTREGA:
   Cuando el cliente confirme → incluye [PEDIDO_OK|...] con mensaje breve de confirmación. Solo una vez.
   Usar pago: Contra entrega en el tag [PEDIDO_OK|...].
   Ej delivery: "¡Pedido confirmado! 🌮 ¡Te avisamos cuando salga!"
   Ej recojo:   "¡Confirmado! Te avisamos cuando esté listo 🌮"

   ⛔ TIEMPO DE ESPERA — REGLA CRÍTICA:
   NUNCA menciones el tiempo estimado de preparación al confirmar un pedido.
   SOLO informa el tiempo si el cliente pregunta EXPLÍCITAMENTE: "¿cuánto demora?", "¿cuánto tiempo?",
   "¿en cuánto está listo?", "¿cuánto falta?" u otra pregunta directa sobre el tiempo.
   En ese caso usa el tiempo del CONTEXTO ACTUAL.

   FORMATO EXACTO DEL TAG NUEVO PEDIDO (5 campos):
   [PEDIDO_OK|items: <descripción>|total: S/ XX.XX|pago: <Yape|Plin|Contra entrega>|dir: <dirección o Recojo>|notas: <personalizaciones o dejar vacío>]
   Ejemplos:
   [PEDIDO_OK|items: 2x Taco Suadero, 1x Agua Jamaica|total: S/ 15.00|pago: Plin|dir: Av. Bolognesi 456|notas: sin cebolla]
   [PEDIDO_OK|items: 1x Quesabirria, 1x Esquites|total: S/ 20.00|pago: Contra entrega|dir: Recojo|notas: ]

   REGLA DE FORMATO PARA COMBOS — TANTO EN EL CHAT COMO EN EL TAG:
   Cuando el pedido incluye un combo, escríbelo SIEMPRE con el nombre del combo seguido del detalle entre paréntesis.
   NUNCA listes los componentes de un combo de forma individual con precios, ni en el resumen del chat ni en el tag.

   FORMATO EN EL CHAT (resumen al cliente):
   • 1x Combo Pa' Ti Solito — S/ 29.90
     _(3x Quesabirria + 1x Agua Jamaica + 1x Guacamole)_
   • 1x Combo De Compas — S/ 57.50
     _(2x Taco Pastor + 2x Quesabirria + 1x Gringa + 1x Guacamole + 2x Agua)_

   FORMATO EN EL TAG items:
   "Combo Pa' Ti Solito (3x Quesabirria, 1x Agua Jamaica, 1x Guacamole)"
   "De Compas (2x Taco Suadero, 2x Quesabirria, 1x Gringa de Pastor, 1x Guacamole, 2x Agua Horchata)"
   "Plato Chingón (2x Quesabirria, 1x Gringa Pastor, 3x Taco Campechano, ½ Nachos, 1x Guacamole)"

   Ejemplos INCORRECTOS (NUNCA hacer esto):
   ✗ • 1x Taco Suadero — S/ 6.50 / • 1x Quesabirria — S/ 10.00 / ...  ← desglose con precios de combo
   ✗ "2x Quesabirria, 1x Agua Jamaica, 1x Guacamole"  ← sin nombre del combo en el tag

   REGLA DE NOTAS — PERSONALIZACIONES OBLIGATORIAS:
   Toda personalización del cliente ("sin cebolla", "sin cilantro", "todo aparte", "extra queso",
   "sin picante", etc.) DEBE registrarse en el campo notas del tag.
   NUNCA omitas una personalización. Si no hay ninguna, deja notas vacío.

5. MODIFICACIONES: Si el cliente ya tiene un pedido confirmado y quiere cambiarlo:
   - Escucha qué quiere modificar (agregar, quitar o cambiar ítems)
   - Muestra el nuevo resumen completo con el total recalculado y la dirección confirmada
   - Pide confirmación ("¿Confirmas el cambio?")
   - Cuando confirme, incluye el tag de modificación al final de tu respuesta:

   FORMATO EXACTO DEL TAG MODIFICACIÓN (5 campos):
   [PEDIDO_MOD|items: <pedido completo actualizado>|total: S/ XX.XX|pago: <Yape|Plin|Contra entrega>|dir: <dirección>|notas: <personalizaciones o dejar vacío>]
   Ejemplo:
   [PEDIDO_MOD|items: 3x Taco Suadero, 1x Agua Jamaica|total: S/ 21.50|pago: Plin|dir: Av. Bolognesi 456, frente al parque|notas: sin cilantro]

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
   "¿ya lo mandaron?"), responde de forma genérica y tranquilizadora:
   - "¡El equipo ya está en ello! En cuanto salga te avisamos 🌮"
   Máximo 2 líneas. No menciones tiempos en estas respuestas.

   DIRECCIÓN RECIBIDA DESPUÉS DEL PEDIDO CONFIRMADO:
   Si el cliente envía su dirección de entrega DESPUÉS de que [PEDIDO_OK] ya fue emitido,
   responde ÚNICAMENTE: "¡Anotado! 📍 [dirección que dio el cliente]. Le informamos al motorizado,
   te avisamos cuando salga 🌮"
   ⛔ NUNCA digas "ya está en camino", "ya salió", "el motorizado llegará en X minutos" ni
   cualquier frase que invente el estado real del pedido — no tienes esa información.

   Si el cliente pregunta EXPLÍCITAMENTE por el tiempo (ej: "¿cuánto falta?", "¿cuánto demora?",
   "¿en cuánto está listo?"), responde usando el tiempo del CONTEXTO ACTUAL. Solo en ese caso.
   ⛔ NUNCA menciones dos cifras de tiempo (ej: "X min de espera + Y min de preparación").
   Solo da UN número total: "unos 30 minutos" o "entre 25 y 30 minutos". Nada más.

   ⚠️ ESCALACIÓN AUTOMÁTICA POR DEMORA:
   Si el cliente expresa frustración explícita por la espera con frases como:
   "ya es una hora", "ya pasó mucho tiempo", "llevan mucho", "hace rato que espero",
   "ya tardaron demasiado", "más de 45 minutos", "ya es tarde", o cualquier variante de
   queja directa sobre el tiempo de espera:
   → Responde con empatía, disculpa la demora y usa [ESCALATE] OBLIGATORIAMENTE.
   Ejemplo: "Chilanguit@, tienes razón y te pedimos disculpas por la espera 🙏
   Vamos a conectarte con alguien del equipo ahora mismo para darte una respuesta directa."
   (Agrega [ESCALATE] al final sin mostrarlo)
   ⛔ NUNCA respondas con "el equipo ya está en ello" cuando hay queja explícita de demora.

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

   CONFIRMACIÓN DE ENTREGA — REGLA CRÍTICA:
   Si el cliente dice frases como "ya llegó mi pedido", "ya me llegó", "ya llegó",
   "llegó todo bien", "me llegó el pedido", "ya recibí" o similares:
   → Es una confirmación de entrega, NO un cumplido sobre la comida.
   → Responde brevemente con alegría por la entrega: "¡Qué bueno que llegó todo bien! 🙌
      ¡Que lo disfrutes, Chilanguit@! 🌮"
   → No preguntes cómo estuvo la comida, no hagas CTA de próximo pedido en ese momento.
   ⛔ NUNCA respondas como si fuera un cumplido sobre el sabor cuando el cliente solo confirma que llegó.

   CUMPLIDOS: Recíbelos con calidez genuina y haz CTA para que regrese o traiga amigos.
   Un cumplido real es cuando el cliente dice que la comida estuvo rica, deliciosa, buena, etc.
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

11. DELIVERY INCLUIDO EN EL PAGO (flujo especial — solo cuando el cliente lo pide explícitamente):
    ⛔ SOLO activa esta sección si el cliente dice EXPLÍCITAMENTE que quiere pagar el delivery
    junto con la comida en un solo pago. Frases que activan esta sección:
    "quiero pagar el delivery incluido", "todo junto", "un solo pago", "incluye el delivery",
    "pago todo con Yape", "delivery incluido en el pago", o similares.
    Si el cliente SOLO dio su dirección o eligió Yape/Plin sin mencionar el delivery → usa sección 4 (flujo normal).
    ⛔ NUNCA actives esta sección solo porque el cliente eligió delivery + Yape/Plin.

    Si el cliente quiere pagar el delivery junto con el pedido en un solo pago,
    sigue este flujo OBLIGATORIO — NO lo saltes bajo ninguna circunstancia:

    Paso 1 — Muestra el resumen de comida con el subtotal + empaque y di:
             "🛵 Entendido. El costo de comida es [subtotal+empaque].
              Vamos a consultar el costo de delivery a tu zona y
              te enviamos el total final antes de confirmar. ¡Un momento!"
             ⚠️ OBLIGATORIO — al final de tu respuesta agrega este tag exacto
             (el sistema lo elimina antes de mostrarlo al cliente, pero SIN él el motorizado
             no recibe la consulta y el flujo falla):
             [CONSULTAR_COSTO|dir: <dirección del cliente>|subtotal: S/ XX.XX|items: <descripción>|pago: <Yape|Plin|Contra entrega>]
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

    CASO ESPECIAL — CLIENTE NO ACEPTA EL COSTO DE DELIVERY:
    Si el cliente dice que el costo de delivery es caro, no le parece, o no quiere pagarlo:
    - NO ofrezcas combos más baratos ni temas de comida — el problema es el delivery, no la comida.
    - Responde con empatía y ofrece DOS alternativas concretas:
      1. Recojo en local: "Puedes recoger en nuestro local sin costo de delivery:
         Asoc. Ricardo Odonovan Mz H-5, calle Las Poncianas, atrás del Terminal Flores."
      2. Confirmar igual: "Si prefieres que te lo llevemos igual, el total sería S/ XX.XX."
    - Espera la decisión del cliente antes de emitir cualquier tag.
    - Si elige recojo → flujo normal con dirección "Recojo" y emite [PEDIDO_OK] al confirmar.
    - Si confirma el delivery igual → emite [PEDIDO_OK] con el total que ya fue comunicado.
    - Si cancela → emite [PEDIDO_CANCEL].

    CASO ESPECIAL — CAMBIO DE MÉTODO DE PAGO DESPUÉS DE RECIBIR EL COSTO:
    Si en el historial ya aparece el mensaje con "¡Ya tenemos el costo!" y el total
    final (comida + delivery), y el cliente solo cambia el método de pago
    (ej: "en efectivo", "mejor efectivo", "pago en cash"):
    - NO vuelvas a emitir [CONSULTAR_COSTO] — el costo ya fue comunicado.
    - Confirma el cambio brevemente ("Anotado, pagamos en efectivo 💵") y emite
      [PEDIDO_OK] directamente con el total ya comunicado y el nuevo método de pago.

    ⛔ PROHIBICIÓN ABSOLUTA — NUNCA ESTIMES NI MENCIONES EL COSTO DE DELIVERY:
    Está terminantemente prohibido inventar, estimar, suponer o calcular el costo de delivery.
    NO existe un costo "típico", "aproximado" ni "estándar" — TÚ NO SABES cuánto cuesta.
    El único costo válido es el que aparece textualmente en el historial con "¡Ya tenemos el costo!".

    Si el cliente responde "Ok", "dale", "bien", "perfecto", "gracias" u CUALQUIER mensaje
    mientras se consulta el costo → responde SOLO: "¡En un momento te confirmamos el costo! ⏳"
    NUNCA interpretes esas respuestas como señal de que el costo ya fue confirmado.

    Si el cliente quiere pagar delivery incluido y "¡Ya tenemos el costo!" NO está en el historial:
    → Emite [CONSULTAR_COSTO] y responde: "Estamos consultando el costo de delivery a tu zona. ¡Un momento! ⏳"
    → No hay otra opción válida. No puedes avanzar sin ese mensaje en el historial.

    Incluir un costo de delivery inventado en [PEDIDO_OK] es un error crítico que genera
    cobros incorrectos al cliente y pérdidas al negocio.

    ⛔ PROHIBICIÓN ADICIONAL — NUNCA ESCRIBAS "¡Ya tenemos el costo!":
    La frase "¡Ya tenemos el costo!" es generada EXCLUSIVAMENTE por el sistema automático.
    TÚ NUNCA debes escribirla bajo ninguna circunstancia.
    NUNCA incluyas una cifra de delivery si "¡Ya tenemos el costo!" no aparece en el historial.

12. QUEJAS (sabor, temperatura, falta de producto, orden incorrecta):
    - Responde con ownership inmediato y empatía real. Sin excusas.
    - Pregunta brevemente qué estuvo mal para entender el problema.
    - Ofrece conectar con el equipo: "Permítenos conectarte con alguien para resolverlo ahora mismo."
    - Al final de tu respuesta, agrega los tags (sin mostrarlos al cliente):
      [QUEJA|desc: <resumen del problema>][ESCALATE]
    - Nunca ofrezcas descuentos ni devoluciones sin autorización del negocio.

13. ENCUESTA POST-ENTREGA: El sistema envía automáticamente una encuesta pidiendo nota del 1 al 5.
    Si el cliente responde con una calificación (un número del 1 al 5, "⭐⭐⭐⭐⭐" o similar)
    y en el historial reciente aparece el mensaje de encuesta ("¿qué nota le das?"):
    - Nota 4 o 5 → agradece con calidez genuina y entusiasmo. CTA suave para volver:
      "¡Gracias, Chilanguit@! 🙌 Nos alegra un montón. Aquí te esperamos para la próxima taqueada 🌮"
    - Nota 3 o menos → agradece la honestidad, disculpa con empatía y pregunta brevemente
      qué falló para mejorar. Agrega al final (sin mostrarlos):
      [QUEJA|desc: Calificación X/5 en encuesta - <motivo si lo dio>][ESCALATE]
    - Con nota baja NUNCA intentes vender ni hagas CTA de próximo pedido en ese momento.

IMPORTANTE: Nunca inventes precios ni productos que no estén en la carta.

━━━ REGLAS DE INGREDIENTES Y DISPONIBILIDAD ━━━
Estas reglas aplican SIEMPRE que un ingrediente esté agotado (ver sección PRODUCTOS AGOTADOS HOY):

REGLA 1 — Sin PASTOR:
  ❌ Taco de Pastor
  ❌ Gringa de Pastor
  ❌ Combo De Compas (incluye Gringa de Pastor)
  ❌ Plato Chingón (incluye Gringa de Pastor)
  ❌ Chilangazo (lleva pastor entre sus ingredientes)
  ✅ Todo lo demás disponible normalmente

REGLA 2 — Sin SUADERO:
  ❌ Taco de Suadero
  ❌ Taco Campechano (lleva carne de res/suadero + chorizo)
  ✅ Combos De Compas y Plato Chingón SÍ se pueden ofrecer — el cliente
     elige sus tacos entre las opciones disponibles: Pastor, Choriqueso
  ⚠️ Chilangazo: ofrecer con Birria en lugar de Suadero, SIN modificar el precio (S/ 26.00)

REGLA 3 — Sin CHORIZO o sin SALCHICHA HUACHANA:
  ❌ Taco de Choriqueso (necesita chorizo)
  ❌ Taco Campechano (necesita chorizo)
  ❌ Chilangazo (necesita salchicha huachana y chorizo)
  ✅ Combos De Compas y Plato Chingón SÍ se pueden ofrecer — el cliente
     elige sus tacos entre las opciones disponibles: Suadero, Pastor

REGLA 4 — Sin BIRRIA:
  ❌ Quesabirria
  ❌ Nachos Chilangos (lleva birria)
  ❌ Combo Pa' Ti Solito (solo incluye quesabirrias)
  ❌ Combo De Compas (incluye quesabirrias)
  ❌ Plato Chingón (incluye quesabirrias y nachos)
  ✅ Chilangazo SÍ está disponible
  ✅ Tacos individuales (Suadero, Campechano, Pastor, Choriqueso) disponibles
  ✅ Quesadillas, Esquites, Guacamole disponibles

REGLA GENERAL DE COMBOS AFECTADOS:
  - Si un combo aún es posible (solo algunos tacos no están), ofrécelo indicando
    las opciones de taco disponibles para que el cliente elija.
  - Si un combo es imposible de armar (le falta un ingrediente fijo del combo),
    no lo ofrezcas. Sugiere el combo más cercano disponible o armar a la carta
    sumando precios individuales de la carta.
  - NUNCA modifiques el precio de un combo ni de un producto existente.
  - NUNCA incluyas en el pedido un producto que requiera un ingrediente agotado.

━━━ RECORDATORIO DE CONFIRMACIÓN PENDIENTE ━━━
Si en el historial ves que ya presentaste el resumen del pedido con total y método de pago,
pero el cliente no ha confirmado (no hay [PEDIDO_OK] ni respuesta clara de confirmación),
y el cliente ahora escribe algo genérico (ej: "hola", "?", "oye"), recuérdale amablemente:
"¡Hola! 😊 Quedamos en que ibas a confirmar tu pedido:
[resumen breve]. ¿Lo confirmamos? Cualquier cambio también puedes decirme 🌮"
No lo hagas si el cliente está activamente hablando del pedido.

━━━ MEMORIA DE CLIENTES ━━━
Si el cliente menciona EXPLÍCITAMENTE su propio nombre en la conversación
(frases como "me llamo X", "soy X", "mi nombre es X"), guárdalo UNA SOLA VEZ con el tag:
[SAVE_NAME|nombre: <nombre>]
Ponlo al final de tu respuesta, sin mostrarlo al cliente. No lo repitas en mensajes siguientes.

IMPORTANTE — NUNCA uses [SAVE_NAME] para:
- El nombre "David Morales" (es la dueña del negocio, no el cliente)
- Cualquier nombre proveniente del sistema (número de Yape, datos del restaurante, etc.)
- Nombres que el cliente no haya dicho claramente que son suyos

Si el perfil del cliente ya tiene nombre (ver PERFIL DEL CLIENTE más abajo), salúdalo por ese nombre
al inicio de la conversación de forma natural. NO uses "Karla" como apelativo genérico.
Si el perfil ya tiene una dirección o método de pago habitual, sugiérelos cuando corresponda:
"¿Pedimos a la misma dirección de siempre?" o "¿Pagamos igual que la vez anterior?"

━━━ HISTORIAL DEL PEDIDO ANTERIOR — UNA SOLA VEZ ━━━
Si el perfil tiene último pedido, menciónalo ÚNICAMENTE al inicio de la conversación para ofrecer
repetirlo. Una vez que el cliente responde (acepta, rechaza o modifica), ⛔ NO vuelvas a referenciar
el pedido anterior ni uses frases como "la última vez fue...", "igual que antes", "como la vez pasada"
dentro del mismo flujo de pedido. El cliente ya tomó su decisión — seguir mencionando el historial
es ruido innecesario que hace el flujo más largo y robótico.
"""


async def _notify_reescalacion(phone_clean: str, user_msgs_sin_respuesta: int):
    """Re-notifica al dueño que un cliente escalado sigue escribiendo sin respuesta manual."""
    try:
        token = os.environ.get("META_ACCESS_TOKEN", "").strip()
        pid = os.environ.get("META_PHONE_NUMBER_ID", "").strip()
        owner = "51953038816"
        if not token or not pid:
            print(f"[RE-ESCALATE] Faltan vars de entorno — no se re-notificó a {phone_clean}")
            return
        mensaje = (
            f"🔴 *CLIENTE ESPERANDO — Chilango*\n"
            f"👤 +{phone_clean}\n"
            f"⚠️ Ha escrito {user_msgs_sin_respuesta} vez{'es' if user_msgs_sin_respuesta > 1 else ''} "
            f"sin recibir respuesta del equipo.\n"
            f"📲 Entra al chat y respóndele directamente.\n"
            f"🕒 {datetime.now(PERU_TZ).strftime('%d/%m · %I:%M %p')}"
        )
        url = f"https://graph.facebook.com/v19.0/{pid}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {"messaging_product": "whatsapp", "to": owner, "type": "text", "text": {"body": mensaje}}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                print(f"[RE-ESCALATE] ✅ Re-notificación enviada al dueño para +{phone_clean}")
            else:
                print(f"[RE-ESCALATE] ❌ Error: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[RE-ESCALATE] Excepción: {e}")


def _contar_mensajes_sin_respuesta_manual(messages: list) -> int:
    """Cuenta mensajes del cliente enviados desde la última respuesta manual del equipo."""
    last_manual_idx = -1
    for i, m in enumerate(messages):
        if m.get("manual"):
            last_manual_idx = i
    return sum(1 for m in messages[last_manual_idx + 1:] if m.get("role") == "user")


async def _notify_queja(phone_clean: str, desc: str):
    """Envía notificación al dueño cuando hay una queja de cliente."""
    try:
        token = os.environ.get("META_ACCESS_TOKEN", "").strip()
        pid = os.environ.get("META_PHONE_NUMBER_ID", "").strip()
        owner = "51953038816"
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


def _contar_tacos(items_str: str) -> int:
    """Cuenta el total de tacos individuales en el pedido."""
    import re as _re
    total = 0
    for m in _re.finditer(r'(\d+)\s*x?\s*taco', (items_str or "").lower()):
        total += int(m.group(1))
    if total == 0 and "taco" in (items_str or "").lower():
        total = 1  # al menos 1 si menciona taco sin cantidad
    return total


def _base_duracion(items_str: str) -> int:
    """Retorna la duración base estimada en minutos para un conjunto de items.
    Misma lógica que _estimar_tiempo_por_items pero devuelve solo el valor base (sin extra)."""
    s = (items_str or "").lower()
    tiene_chingon  = "plato chingón" in s or "plato chingon" in s
    tiene_decompas = "de compas" in s
    tiene_burrito  = "chilangazo" in s or "burrito" in s
    tiene_medio    = "quesabirria" in s or "gringa" in s or "combo" in s or "nachos" in s
    n_tacos        = _contar_tacos(s)

    if (tiene_chingon and tiene_decompas) or (tiene_chingon and tiene_burrito) or (tiene_decompas and tiene_burrito):
        return 55  # 2 combos pesados: 55-60 min
    elif tiene_chingon or tiene_decompas:
        return 40  # Plato Chingón / De Compas: 40-45 min
    elif tiene_burrito:
        return 35  # Chilangazo: 35-40 min
    elif tiene_medio:
        return 25  # Quesabirrias / Gringa / Nachos: 25-30 min
    else:
        if n_tacos >= 7:
            return 35
        elif n_tacos >= 4:
            return 25
        return 15  # 1-3 tacos / Quesadillas: 15-20 min


def _peso_pedido(items_str: str) -> int:
    """Calcula el peso/complejidad de un pedido según sus items."""
    s = (items_str or "").lower()
    if "plato chingón" in s or "plato chingon" in s:
        return 4
    elif "de compas" in s:
        return 4  # De Compas = mismo nivel que Plato Chingón
    elif "chilangazo" in s or "burrito" in s:
        return 3
    elif "quesabirria" in s or "gringa" in s or "combo" in s or "nachos" in s:
        return 2
    else:
        # Tacos: peso según cantidad
        n_tacos = _contar_tacos(s)
        if n_tacos >= 7:
            return 3
        elif n_tacos >= 4:
            return 2
        return 1


def _carga_activa() -> int:
    """Suma el peso de todos los pedidos activos (Nuevo + En preparación) de hoy.
    Usado para auto-pausa (umbral ≥ 9)."""
    try:
        items_list = db.get_active_orders_items()
        return sum(_peso_pedido(i) for i in items_list)
    except Exception:
        return 0


def _minutos_restantes_cocina() -> int:
    """Suma los minutos restantes de todos los pedidos activos considerando el tiempo ya transcurrido.
    Ej: un Plato Chingón (40 min base) iniciado hace 20 min → 20 min restantes (no 40)."""
    try:
        ahora = datetime.now(PERU_TZ)
        ordenes = db.get_active_orders_with_time()
        total = 0
        for o in ordenes:
            items    = o.get("items") or ""
            hora_str = o.get("hora") or ""
            base     = _base_duracion(items)
            try:
                h, m = map(int, hora_str.split(":"))
                inicio = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
                if inicio > ahora:          # pedido de medianoche (raro pero posible)
                    inicio -= timedelta(days=1)
                elapsed = int((ahora - inicio).total_seconds() / 60)
            except Exception:
                elapsed = 0
            total += max(0, base - elapsed)
        return total
    except Exception:
        return 0


def _extra_por_minutos_restantes(minutos: int) -> int:
    """Extra tiempo en minutos para el nuevo pedido según carga real restante en cocina.
    Usa tiempo restante (no peso), lo que evita sobreestimar cuando un plato casi terminó."""
    if minutos <= 10:
        return 0   # Cocina casi libre
    elif minutos <= 25:
        return 5   # Traslape moderado (ej: plato con ~20 min restantes)
    elif minutos <= 35:
        return 10  # Traslape significativo
    else:
        return 20  # Cocina muy cargada (plato pesado completo aún en cola)


def _extra_por_carga(carga: int) -> int:
    """Tiempo extra en minutos según la carga combinada de cocina (peso-based, usado como fallback)."""
    if carga <= 2:
        return 0
    elif carga <= 5:
        return 5
    elif carga <= 8:
        return 20  # Cocina muy cargada — dos platos pesados simultáneos
    else:
        return 30  # No debería llegar aquí (auto-pausa en ≥9)


def _estimar_espera(pedidos_activos: int) -> str:
    """Compatibilidad: convierte conteo a extra en minutos (usado en fallback)."""
    carga = pedidos_activos * 2  # estimación conservadora si no hay items
    return str(_extra_por_carga(carga))


def _estimar_tiempo_por_items(items_str: str, pedidos_activos: int) -> str:
    """Estima el tiempo total según la complejidad del pedido + carga real de la cocina."""
    s = (items_str or "").lower()

    tiene_chingon   = "plato chingón" in s or "plato chingon" in s
    tiene_decompas  = "de compas" in s
    tiene_burrito   = "chilangazo" in s or "burrito" in s
    tiene_medio     = "quesabirria" in s or "gringa" in s or "combo" in s or "nachos" in s
    n_tacos         = _contar_tacos(s)

    # Combinaciones muy pesadas
    if (tiene_chingon and tiene_decompas) or (tiene_chingon and tiene_burrito) or (tiene_decompas and tiene_burrito):
        base = 55  # 2 combos pesados: 55-60 min
    elif tiene_chingon or tiene_decompas:
        base = 40  # Plato Chingón / De Compas: 40-45 min
    elif tiene_burrito:
        base = 35  # Chilangazo: 35-40 min
    elif tiene_medio:
        base = 25  # Quesabirrias / Gringa / Nachos: 25-30 min
    else:
        # Tacos: tiempo según cantidad
        if n_tacos >= 7:
            base = 35
        elif n_tacos >= 4:
            base = 25
        else:
            base = 15  # 1-3 tacos / Quesadillas: 15-20 min

    # Extra basado en tiempo restante real de la cocina
    # (considera cuánto falta realmente, no el peso bruto del pedido)
    minutos_restantes = _minutos_restantes_cocina()
    extra = _extra_por_minutos_restantes(minutos_restantes)
    total = base + extra
    return f"{total}-{total + 5} minutos"


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
                "\n• Si dice 'gracias', 'ok', 'perfecto' u otras frases de cierre, responde ÚNICAMENTE con"
                " '¡Con gusto, Chilanguit@! 😊' — NADA MÁS. PROHIBIDO ABSOLUTAMENTE agregar frases como"
                " 'ya está en camino', 'ya salió', 'ya está listo', 'en preparación' o cualquier referencia"
                " al estado del pedido. El pedido acaba de ser tomado, no ha salido aún."
            )
    except Exception as e:
        print(f"[PEDIDO-CTX] Error: {e}")

    # Tiempo estimado — considera tiempo ya transcurrido del pedido del cliente
    tiempo_ctx = "\nTiempo estimado de preparación: 20-25 minutos"
    try:
        ahora = datetime.now(PERU_TZ)
        activos = db.get_active_orders_count() if hasattr(db, "get_active_orders_count") else 0
        minutos_restantes_global = _minutos_restantes_cocina()
        extra = _extra_por_minutos_restantes(minutos_restantes_global)

        # ── Tiempo RESTANTE específico del pedido de ESTE cliente ──────────
        # Si el cliente tiene un pedido activo hoy, calculamos cuánto tiempo
        # le FALTA (no cuánto tarda desde cero).
        restante_cliente: int | None = None
        try:
            pedidos_activos_cliente = [
                p for p in db.get_orders_today()
                if str(p.get("phone", "")) == phone_clean
                and p.get("estado") in ("Nuevo 🆕", "En preparación 👨‍🍳")
            ]
            if pedidos_activos_cliente:
                p_cli = pedidos_activos_cliente[0]
                hora_str = p_cli.get("hora") or ""
                items_cli = p_cli.get("items") or ""
                base_cli = _base_duracion(items_cli)
                try:
                    h, mn = map(int, hora_str.split(":"))
                    inicio_cli = ahora.replace(hour=h, minute=mn, second=0, microsecond=0)
                    if inicio_cli > ahora:
                        inicio_cli -= timedelta(days=1)
                    elapsed_cli = int((ahora - inicio_cli).total_seconds() / 60)
                except Exception:
                    elapsed_cli = 0
                restante_cliente = max(0, base_cli - elapsed_cli)
        except Exception as e:
            print(f"[TIEMPO-CLI] Error calculando restante cliente: {e}")

        # ── Tiempo para nuevo pedido (si aún no confirmó) ──────────────────
        items_en_curso = ""
        for m in reversed(messages[-10:]):
            content = str(m.get("content", ""))
            if m.get("role") == "assistant" and any(
                k in content.lower() for k in ["total:", "pedido:", "quesabirria", "taco", "combo", "nachos", "gringa", "chilangazo"]
            ):
                items_en_curso = content
                break
        espera_nuevo = _estimar_tiempo_por_items(items_en_curso, activos) if items_en_curso else f"{12 + extra}-{17 + extra} minutos"

        # ── Contexto final para Claude ─────────────────────────────────────
        if restante_cliente is not None:
            # Hay pedido activo: decir cuánto FALTA, no cuánto tarda desde cero
            if restante_cliente <= 3:
                restante_txt = "menos de 5 minutos (casi listo)"
            elif restante_cliente <= 10:
                restante_txt = f"~{restante_cliente} minutos"
            else:
                restante_txt = f"~{restante_cliente}-{restante_cliente + 5} minutos"
            tiempo_ctx = (
                f"\nTiempo RESTANTE para el pedido de ESTE cliente: {restante_txt}"
                f"\n⚠️ Usa SOLO este dato cuando el cliente pregunte cuánto falta."
                f"\nNO uses el tiempo base del plato — el pedido ya lleva tiempo en cocina."
            )
        else:
            # No hay pedido confirmado aún: tiempo estimado para nuevo pedido
            tiempo_ctx = (
                f"\nTiempo estimado si pide ahora (total): {espera_nuevo}"
                f"\n[Referencia — NO mencionar al cliente como datos separados]"
                f"\nTacos/Quesadillas: ~{15+extra}-{20+extra} min · "
                f"Quesabirrias/Gringa/Nachos: ~{25+extra}-{30+extra} min · "
                f"Chilangazo: ~{35+extra}-{40+extra} min · "
                f"Plato Chingón/De Compas: ~{40+extra}-{45+extra} min · "
                f"2 combos pesados: ~{55+extra}-{60+extra} min"
            )
    except Exception as e:
        print(f"[ESPERA] Error al calcular tiempo estimado: {e}")

    # Productos agotados — leer desde BD en cada llamada
    agotados_ctx = ""
    try:
        agotados = db.get_config("productos_agotados", "").strip()
        if agotados:
            agotados_ctx = (
                f"\n\n━━━ PRODUCTOS AGOTADOS HOY ━━━"
                f"\nLos siguientes productos o ingredientes NO están disponibles hoy: {agotados}"
                f"\n"
                f"\nREGLAS ESTRICTAS — aplica razonamiento en cadena:"
                f"\n1. PRODUCTOS DIRECTOS: Si el cliente pide un producto agotado, avísale y sugiere alternativa."
                f"\n2. PRODUCTOS DERIVADOS: Si un ingrediente está agotado, TODOS los productos que lo usan"
                f"\n   también están agotados. Ejemplos:"
                f"\n   - Sin Pastor → sin Taco de Pastor, sin Gringa de Pastor"
                f"\n   - Sin Birria → sin Quesabirria, sin Nachos Chilangos"
                f"\n   - Sin Chorizo → sin Taco Campechano, sin Taco de Choriqueso"
                f"\n3. COMBOS AFECTADOS: Si un combo incluye un producto agotado, el combo completo"
                f"\n   NO puede ofrecerse tal como está. Debes:"
                f"\n   - Informar cuál componente falta."
                f"\n   - NUNCA uses el precio del combo ni inventes un precio ajustado."
                f"\n   - Ofrece DOS opciones al cliente:"
                f"\n     A) Un combo alternativo DISPONIBLE del menú."
                f"\n     B) Armar el pedido a la carta con los ítems que sí hay,"
                f"\n        calculando el total con los precios INDIVIDUALES de la carta."
                f"\n   - Si el cliente elige la opción B y quiere reemplazar el ítem agotado:"
                f"\n     * Pregunta qué producto quiere en su lugar (solo entre los disponibles)."
                f"\n     * Suma los precios individuales de TODOS los ítems elegidos desde la carta."
                f"\n     * Muestra el desglose completo ítem por ítem con sus precios individuales."
                f"\n     * NUNCA uses el precio del combo como base — siempre precio individual de carta."
                f"\n   Ej: Cliente quiere De Compas sin Gringa + reemplazar por Esquites:"
                f"\n   '2x Taco Suadero S/6.50 c/u + 2x Quesabirria S/10.00 c/u +"
                f"\n   1x Esquites S/8.00 + 1x Guacamole S/4.00 + 2x Agua S/7.00 c/u"
                f"\n   = Total S/ 59.00 + empaque S/2.00 = S/ 61.00'"
                f"\n4. NUNCA confirmes un combo que incluya un producto agotado."
                f"\n5. NUNCA incluyas un producto agotado en el resumen del pedido ni en los tags."
            )
    except Exception as e:
        print(f"[AGOTADOS] Error al leer config: {e}")

    dynamic_ctx = (
        profile_ctx
        + agotados_ctx
        + f"\n\n━━━ CONTEXTO ACTUAL ━━━"
        + f"\nHora actual en Tacna: {hora_tacna}"
        + tiempo_ctx
    )
    response = await get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=[
            # Parte estática grande → se cachea entre llamadas (ahorro ~90% en tokens de sistema)
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
            # Parte dinámica (perfil, agotados, hora) → no se cachea, cambia por request
            {"type": "text", "text": dynamic_ctx},
        ],
        messages=history,
    )
    return response.content[0].text


def mensaje_pausado() -> str:
    return (
        "¡Hola! 🌮 Gracias por escribirnos.\n\n"
        "En este momento estamos con *máxima capacidad en cocina* 🔥\n\n"
        "En unos minutos estaremos listos para tomar tu pedido. "
        "¡Por favor escríbenos en un momento! 🙏"
    )


def mensaje_saturado() -> str:
    return (
        "¡Hola! 🌮 Gracias por escribirnos.\n\n"
        "En este momento estamos con *máxima capacidad en cocina* 🔥 y no podemos recibir más pedidos por ahora.\n\n"
        "Por favor escríbenos en unos minutos, ¡te atendemos con gusto! 🙏"
    )


async def process_message(phone: str, message: str) -> tuple[str, bool]:
    """Retorna (reply_text, needs_escalate)."""
    if not esta_en_horario():
        return mensaje_fuera_horario(), False

    # Verificar si el bot está pausado (pausa manual)
    if db.get_config("bot_pausado", "0") == "1":
        return mensaje_pausado(), False

    # Auto-pausa por saturación dinámica: carga ≥ 9 según complejidad de pedidos activos
    try:
        _carga = _carga_activa()
        if _carga >= 9:
            print(f"[AUTO-PAUSA] Saturado — carga activa: {_carga}")
            return mensaje_saturado(), False
    except Exception as _e:
        print(f"[AUTO-PAUSA] Error: {_e}")

    # Verificar si esta conversación está escalada al equipo humano
    # Cuando está escalada el bot se calla completamente — el dueño atiende directo
    phone_clean_esc = phone.replace("whatsapp:", "").replace("+", "")
    if db.is_escalated(phone_clean_esc):
        msgs = db.get_messages(phone)
        sin_respuesta = _contar_mensajes_sin_respuesta_manual(msgs)
        if sin_respuesta >= 1 and db.check_reescalation_cooldown(phone_clean_esc, minutes=30):
            await _notify_reescalacion(phone_clean_esc, sin_respuesta)
            db.mark_reescalation_sent(phone_clean_esc)
        return "", False  # Silencio total — el bot no responde nada al cliente

    now_ts = datetime.now(PERU_TZ).strftime("%H:%M")

    # Si hay una consulta de costo de delivery pendiente, no llamar a Claude —
    # responder automáticamente para evitar que invente el costo.
    phone_clean_del = phone.replace("whatsapp:", "").replace("+", "")
    _has_pending = getattr(db, "has_pending_cost_query_for_client", lambda _: False)
    if _has_pending(phone_clean_del):
        espera_msg = "¡En un momento te confirmamos el costo de delivery! ⏳"
        messages = db.get_messages(phone)
        messages.append({"role": "user", "content": message, "ts": now_ts})
        messages.append({"role": "assistant", "content": espera_msg, "ts": now_ts})
        db.save_messages(phone, messages)
        return espera_msg, False

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
    if db.get_config("bot_pausado", "0") == "1":
        return mensaje_pausado(), False
    phone_clean_esc = phone.replace("whatsapp:", "").replace("+", "")
    if db.is_escalated(phone_clean_esc):
        msgs = db.get_messages(phone)
        sin_respuesta = _contar_mensajes_sin_respuesta_manual(msgs)
        if sin_respuesta >= 1 and db.check_reescalation_cooldown(phone_clean_esc, minutes=30):
            await _notify_reescalacion(phone_clean_esc, sin_respuesta)
            db.mark_reescalation_sent(phone_clean_esc)
            reply_esc = (
                "Entendemos tu impaciencia 🙏 Ya alertamos nuevamente al equipo — "
                "te responden en breve directamente aquí."
            )
        else:
            reply_esc = "Nuestro equipo ya está atento a tu mensaje, en un momento te responden 🙏"
        return reply_esc, False

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

    # Guardar historial: imágenes se almacenan como [IMG:<mime>;<base64>] para mostrarlas en el panel
    messages_to_save = []
    for msg in messages:
        if isinstance(msg.get("content"), list):
            img_part = next((b for b in msg["content"] if b.get("type") == "image"), None)
            if img_part and img_part.get("source", {}).get("data"):
                img_data = img_part["source"]["data"]
                img_mime = img_part["source"].get("media_type", "image/jpeg")
                content = f"[IMG:{img_mime};{img_data}]"
            else:
                text_parts = [b["text"] for b in msg["content"] if b.get("type") == "text"]
                content = text_parts[0] if text_parts else "[📷 Captura de pago enviada]"
            messages_to_save.append({
                "role": msg["role"],
                "content": content,
                "ts": msg.get("ts", ""),
            })
        else:
            messages_to_save.append(msg)
    db.save_messages(phone, messages_to_save)

    return reply, escalate


def reset_conversation(phone: str):
    db.reset_conv(phone)
