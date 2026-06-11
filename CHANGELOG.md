[CHANGELOG.md](https://github.com/user-attachments/files/28819405/CHANGELOG.md)

# CHANGELOG — Chilango Bot

Formato: `[vX.Y] YYYY-MM-DD — Descripción`
- **X** = versión mayor (cambio arquitectural o de flujo)
- **Y** = versión menor (mejora, fix o nueva función)

---

## [v3.5] 2026-06-10 — Panel más interactivo: búsqueda, notificaciones, tiempos y links rápidos

### 🆕 Panel de pedidos (`/pedidos`)
- **Buscador de pedidos**: filtra en vivo por número de pedido, teléfono, producto o dirección. Se combina con las pestañas de estado.
- **Toggle de sonido 🔔/🔕**: botón persistente (localStorage) para activar/silenciar el beep de pedidos nuevos. Al activarlo se desbloquea el audio del navegador (antes el beep podía no sonar por falta de interacción del usuario).
- **Notificaciones del navegador**: si la pestaña está en segundo plano y llega un pedido nuevo, aparece notificación del sistema con el número y los items. Se pide permiso al activar el sonido.
- **Chip "⏱️ hace X min"** en cada pedido activo: verde (<25 min), ámbar (25-44 min), rojo pulsante (≥45 min). Ayuda a priorizar la cocina de un vistazo.
- **Teléfono clickeable**: abre el chat de WhatsApp del cliente (wa.me) en un clic.
- **Dirección clickeable**: abre Google Maps con la dirección + "Tacna, Perú" para ubicar la zona del delivery.
- **Refresh sin parpadeo**: el grid solo se re-renderiza si los datos cambiaron (o cada 60 s para refrescar los chips de tiempo). Antes se reconstruía todo el HTML cada 10 s.
- **Responsive móvil**: nav con scroll horizontal, header compacto y grid ajustado en pantallas chicas. Animación sutil de entrada en las tarjetas y empty state mejorado.

### 🔧 Mejoras al bot
- **CTA después de la carta**: al enviar el PDF de la carta, el bot manda el botón "🛵 Hacer un pedido" (solo en horario de atención) para cerrar la venta sin esperar el follow-up de 15 min.
- **Encuesta post-entrega con cierre de loop**: nueva sección 13 del prompt. Nota 4-5 → agradecimiento con CTA suave; nota ≤3 → disculpa, pregunta qué falló y escala al equipo con [QUEJA] + [ESCALATE]. Antes el bot no tenía instrucciones para procesar la respuesta de la encuesta.

### 🐛 Bugs corregidos
- **Combos partidos en las tarjetas**: los items se dividían por TODAS las comas, rompiendo el detalle de combos en viñetas separadas ("1x Combo Pa' Ti Solito (3x Quesabirria" / "1x Agua Horchata" / "1x Guacamole)"). Ahora las comas dentro de paréntesis no separan.
- **Spinner de "cambiar estado" nunca aparecía**: el selector buscaba `.btn-next` pero la clase real es `.oa-next`.
- **Duplicado "buenas noches"** en SALUDOS_GENERICOS.

### 📁 Archivos actualizados
| Archivo | Cambios |
|---|---|
| `main.py` | Buscador, toggle sonido, Web Notifications, chips de tiempo, links wa.me/Maps, refresh inteligente, responsive, CTA post-carta, fix combos y .oa-next |
| `bot.py` | Sección 13 del prompt: manejo de respuestas a la encuesta post-entrega |

---

## [v3.4] 2026-06-01 — Tiempos restantes, menú editable, métricas y fixes de producción

### 🆕 Nuevas funcionalidades
- **Menú editable desde el panel** (`/admin/menu`): el dueño puede editar precios, nombres y desactivar items sin tocar código. Los cambios se reflejan en el bot en vivo sin reiniciar.
- **Dashboard de métricas** (`/admin/metricas`): ventas por día (14 días), hora pico, top 7 productos, método de pago, totales del día/semana/mes. Usa Chart.js.
- **Historial de costos por zona** (`/admin/zonas-delivery`): tabla con costos de delivery aprendidos automáticamente por zona, promedio, rango y frecuencia.
- **Impresión de pedidos** (`/admin/imprimir/{id}`): recibo imprimible por pedido. Botón 🖨️ en cada tarjeta del panel. Se imprime automáticamente al abrir.
- **Navegación ampliada**: las tres pestañas (Pedidos, Conversaciones, Clientes) ahora incluyen accesos directos a Métricas, Zonas y Menú.
- **GPS del cliente**: cuando el cliente comparte su ubicación por GPS en WhatsApp, el bot la convierte a link de Google Maps clickeable en el panel y la procesa como dirección.

### 🔧 Mejoras al bot
- **Tiempo restante real**: cuando un cliente pregunta "¿cuánto falta?", el bot calcula cuánto tiempo lleva su pedido en cocina y responde con el tiempo RESTANTE (no el tiempo base completo). Ej: Chilangazo pedido hace 20 min → "~15 min", no "35-40 min".
- **Un solo número de tiempo**: eliminado el bug donde Claude decía dos cifras ("X min de espera + Y min de preparación"). Ahora solo da un total.
- **Regla explícita**: `⛔ NUNCA menciones dos cifras de tiempo`.
- **Tiempos base restaurados** a los valores acordados:
  - 1-3 tacos / Quesadillas: 15-20 min
  - Quesabirrias / Gringa / Nachos: 25-30 min
  - Chilangazo: 35-40 min
  - Plato Chingón / De Compas: 40-45 min
  - 2 combos pesados: 55-60 min

### 🐛 Bugs corregidos
- **Costo de delivery enviado tarde**: si el motorizado o el dueño ingresaba el costo cuando el cliente ya tenía un pedido confirmado (p.ej., eligió contra entrega), el sistema enviaba el costo igual. Ahora verifica si hay pedido activo antes de enviar.
- **Bot respondía en conversaciones escaladas**: cuando una conversación estaba escalada al equipo, el bot seguía enviando "Nuestro equipo ya está atento". Ahora el bot es completamente silencioso en conversaciones escaladas.
- **Mensaje manual auto-escalaba**: enviar un mensaje desde el panel escalaba automáticamente la conversación silenciando el bot. Eliminado — el bot sigue activo salvo escalación manual explícita.
- **GPS sin procesar**: el tipo de mensaje `location` caía en el `else` y respondía "Por favor envía un mensaje de texto". Ahora se procesa correctamente.

### 📁 Archivos actualizados
| Archivo | Cambios |
|---|---|
| `bot.py` | Menú desde BD, refresh_menu(), tiempos restantes por cliente, contexto de tiempo simplificado, reglas de tiempo mejoradas |
| `db.py` | Tabla menu_items, CRUD menú, get_menu_texto(), get_metricas(), get_delivery_zones_summary(), get_active_orders_with_time() |
| `main.py` | Páginas /admin/menu, /admin/metricas, /admin/zonas-delivery, /admin/imprimir/{id}, botón 🖨️, nav ampliada, fixes de delivery y escalación |

---

## [v3.3] 2026-05-XX — Carga dinámica de cocina con tiempo restante real

### 🆕 Nuevas funcionalidades
- **Carga restante de cocina**: reemplaza el sistema de peso fijo por cálculo de minutos restantes reales. Un Plato Chingón que lleva 20 min solo aporta 20 min de carga, no 40.
- **`get_active_orders_with_time()`**: nueva función en db.py que retorna items + hora de inicio de pedidos activos.

### 🔧 Mejoras
- Threshold de extra ajustado: `≤10→0, ≤25→5, ≤35→10, >35→20`
- 2do Plato Chingón simultáneo → +20 min (antes +10)

---

## [v3.2] 2026-05-XX — Pagos, horario y panel de conversaciones

### 🆕 Nuevas funcionalidades
- **Solo Plin**: eliminado Yape/Efectivo. Solo "Plin" y "Contra entrega".
- **Seleccionar todas las conversaciones** para borrado bulk.
- **Tabla de clientes**: stats del día + histórico acumulado + badge "recurrente".
- **Notificación automática "En camino"**: al cambiar estado, WhatsApp al cliente.
- **Re-escalación automática**: si el cliente sigue escribiendo sin respuesta del equipo, re-notifica al dueño con cooldown de 30 min.

### 🔧 Mejoras
- Horario: 5:00pm → **5:30pm**
- Tiempos por complejidad: Chilangazo separado de Quesabirria, De Compas = mismo nivel que Plato Chingón.

---

## [v3.1] 2026-05-XX — Estimación de tiempos por plato

### 🆕 Nuevas funcionalidades
- Tiempos de preparación diferenciados por tipo de plato y cantidad de tacos.
- Auto-pausa con sistema de peso (peso ≥ 9 → pausa).
- Encuesta post-entrega automática (60 min después de "En camino").
- Follow-up de carta (15 min sin pedido).

---

## [v3.0] 2026-05-XX — Panel de administración completo

### 🆕 Nuevas funcionalidades
- Panel de pedidos con filtro por fecha histórica.
- Panel de conversaciones con polling en tiempo real.
- Panel de clientes.
- Gestión de estados de pedido.
- Solicitud de motorizado (Altoke).
- Delivery incluido: flujo completo con consulta de costo.
- Ingredientes agotados configurables desde el panel.

---

## [v2.0] 2026-04-XX — Bot con memoria y escalación

### 🆕 Nuevas funcionalidades
- Memoria de clientes (nombre, dirección, método de pago).
- Escalación manual y automática al equipo.
- Notificación de quejas al dueño.
- Cancelación y modificación de pedidos.
- Carta en PDF.

---

## [v1.0] 2026-03-XX — Bot inicial

- Chatbot WhatsApp básico con Claude.
- Toma de pedidos, confirmación Plin, registro en SQLite.
- Notificación al dueño por WhatsApp.
