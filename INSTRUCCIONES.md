# 🌮 Guía de Instalación — Chilango Bot

Sigue estos pasos **en orden**. No necesitas saber programar.
Tiempo estimado: **45 minutos** la primera vez.

---

## PASO 1 — Obtener tu clave de Claude (IA)

1. Ve a **https://console.anthropic.com**
2. Crea una cuenta (es gratis registrarse)
3. Una vez dentro, haz clic en **"API Keys"** en el menú izquierdo
4. Clic en **"Create Key"**
5. Ponle un nombre: `chilango-bot`
6. Copia la clave (empieza con `sk-ant-...`) y **guárdala en un bloc de notas**

> ⚠️ Solo se muestra una vez. Si la pierdes, tendrás que crear otra.

---

## PASO 2 — Crear cuenta en Twilio (WhatsApp)

1. Ve a **https://www.twilio.com/try-twilio**
2. Regístrate con tu email
3. Verifica tu número de teléfono
4. Una vez dentro del dashboard, anota estos dos datos:
   - **Account SID** (empieza con `AC...`)
   - **Auth Token** (haz clic en "show" para verlo)

### Activar el Sandbox de WhatsApp (para pruebas gratis)

1. En el menú izquierdo busca: **Messaging → Try it out → Send a WhatsApp message**
2. Verás un número de Twilio y un código tipo `join palabra-palabra`
3. Desde tu WhatsApp personal, envía ese mensaje al número de Twilio
4. Listo — ya puedes recibir y enviar mensajes de prueba

---

## PASO 3 — Subir el bot a Railway (hosting gratuito)

### 3.1 Crear cuenta en GitHub
1. Ve a **https://github.com** y crea una cuenta si no tienes

### 3.2 Subir los archivos
1. Ve a **https://github.com/new**
2. Nombre del repositorio: `chilango-bot`
3. Selecciona **Private** (privado)
4. Haz clic en **"Create repository"**
5. Haz clic en **"uploading an existing file"**
6. Arrastra TODOS los archivos de la carpeta `chilango-bot` al navegador
   (excepto el archivo `.env.example` — ese no lo subas)
7. Haz clic en **"Commit changes"**

### 3.3 Desplegar en Railway
1. Ve a **https://railway.app** y crea cuenta con tu GitHub
2. Haz clic en **"New Project"**
3. Selecciona **"Deploy from GitHub repo"**
4. Elige el repositorio `chilango-bot`
5. Railway detectará automáticamente que es Python

### 3.4 Agregar las variables de entorno en Railway
1. En tu proyecto de Railway, haz clic en el servicio
2. Ve a la pestaña **"Variables"**
3. Agrega estas variables una por una (clic en "+ New Variable"):

   | Variable | Valor |
   |----------|-------|
   | `ANTHROPIC_API_KEY` | tu clave de Claude (del Paso 1) |
   | `TWILIO_ACCOUNT_SID` | tu Account SID de Twilio |
   | `TWILIO_AUTH_TOKEN` | tu Auth Token de Twilio |

4. Railway reiniciará el bot automáticamente

### 3.5 Obtener tu URL
1. Ve a la pestaña **"Settings"** del servicio
2. En la sección **"Networking"**, haz clic en **"Generate Domain"**
3. Copia la URL que aparece (ej: `https://chilango-bot-production.up.railway.app`)

---

## PASO 4 — Conectar Twilio con Railway

1. Vuelve a Twilio → **Messaging → Try it out → Send a WhatsApp message**
2. Baja hasta la sección **"Sandbox Settings"**
3. En el campo **"When a message comes in"**, pega:
   ```
   https://TU-URL-DE-RAILWAY.up.railway.app/webhook
   ```
   (reemplaza con tu URL real)
4. Asegúrate que el método sea **HTTP POST**
5. Haz clic en **Save**

---

## PASO 5 — ¡Probar el bot!

1. Desde tu WhatsApp, envía cualquier mensaje al número de Twilio
2. El bot debería responder en segundos 🎉

**Pruebas recomendadas:**
- Escribe: `Hola`
- Escribe: `Quiero ver la carta`
- Escribe: `¿Qué lleva la quesabirria?`
- Escribe: `Quiero pedir`

---

## ¿Dónde ver los pedidos en Excel?

El archivo `pedidos_chilango.xlsx` se guarda en el servidor de Railway.

> ⚠️ **Importante:** Railway borra el archivo cuando el servidor se reinicia.
> Para no perder pedidos, tienes dos opciones:

**Opción A (recomendada) — Copiar pedidos manualmente:**
- Railway tiene una terminal integrada
- Ve a tu proyecto → pestaña "Deploy" → "View Logs"
- Ahí verás cada pedido registrado en tiempo real en los logs

**Opción B — Railway Volumes (más avanzado):**
- En Railway, agrega un "Volume" para guardar el archivo permanentemente
- Contacta si necesitas ayuda con esto

---

## Costos aproximados

| Servicio | Costo |
|----------|-------|
| Railway | ~$5/mes (plan Hobby) |
| Twilio WhatsApp | ~$0.005 por mensaje (~S/ 0.02) |
| Claude API (IA) | ~$0.01 por conversación completa |
| **Total estimado** | **~$8-10/mes** |

---

## Preguntas frecuentes

**¿El bot funciona 24/7?**
Sí, pero solo responde pedidos cuando se lo indicas en el horario (Vie-Dom 5-11pm).

**¿Puedo cambiar el menú?**
Sí, edita el archivo `menu.py` y vuelve a subir a GitHub — Railway se actualiza solo.

**¿El bot habla solo español?**
Sí, está configurado en español peruano/mexicano.

**¿Qué pasa si el cliente escribe cosas raras?**
Claude (la IA) maneja conversaciones naturales, responderá amablemente y redirigirá al menú.

---

## Soporte

Si algo no funciona, revisa:
1. Que las variables de entorno estén bien escritas en Railway
2. Que el webhook en Twilio tenga la URL correcta
3. Los logs en Railway → pestaña "Deployments" → "View Logs"
