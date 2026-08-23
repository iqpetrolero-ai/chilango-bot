[INSTRUCCIONES.md](https://github.com/user-attachments/files/31356010/INSTRUCCIONES.md)
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

## PASO 2 — Crear tu app de WhatsApp en Meta (Meta for Developers)

Este bot usa la **API oficial de WhatsApp de Meta** (no Twilio). Necesitas:

1. Ve a **https://developers.facebook.com** y crea una cuenta de desarrollador (con tu Facebook)
2. Clic en **"My Apps" → "Create App"**
3. Elige el tipo **"Business"** y ponle un nombre (ej. `chilango-bot`)
4. Dentro de tu app, busca el producto **WhatsApp** y haz clic en **"Set up"**
5. En **WhatsApp → API Setup** verás (para pruebas, gratis):
   - Un **número de prueba de Meta** ya activo
   - Un **token de acceso temporal** (dura 24h — luego generas uno permanente en el Paso 4)
   - El **Phone number ID** (una serie de números, no tu teléfono)
6. Anota estos datos, los usarás en el Paso 4:
   - **Access Token** (empieza con `EAA...`)
   - **Phone Number ID**
7. Para probar: en la misma pantalla hay un campo "To" donde agregas tu número de WhatsApp personal como destinatario de prueba y puedes enviarte una plantilla de ejemplo

### Obtener el App Secret (necesario para seguridad del webhook)

1. Ve a **Configuración de la app → Básica** (App Settings → Basic)
2. Copia el **App Secret** (haz clic en "Show" y confirma tu contraseña de Facebook)
3. Este valor va en la variable `META_APP_SECRET` — es lo que verifica que los mensajes que llegan al bot realmente vienen de Meta y no de un tercero. **No lo dejes vacío.**

### Token permanente (para producción, cuando el bot ya no sea solo de prueba)

El token temporal que anotaste arriba expira en 24 horas. Para producción:
1. Crea un **System User** en Meta Business Suite (Configuración del negocio → Usuarios del sistema)
2. Genera un token permanente para ese usuario con permisos `whatsapp_business_messaging` y `whatsapp_business_management`
3. Necesitarás verificar tu negocio (Business Verification) para enviar mensajes fuera del modo de prueba

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
   | `META_ACCESS_TOKEN` | el Access Token de Meta (del Paso 2) |
   | `META_PHONE_NUMBER_ID` | el Phone Number ID de Meta (del Paso 2) |
   | `META_VERIFY_TOKEN` | invéntate una palabra/frase secreta (ej. `chilango2026verify`) — la usarás también en el Paso 4 |
   | `META_APP_SECRET` | el App Secret de Meta (del Paso 2) — **obligatorio, no lo dejes vacío** |
   | `ADMIN_PASSWORD` | una contraseña tuya para entrar al panel `/admin` (usuario fijo: `admin`) |
   | `OWNER_PHONE` | tu número de WhatsApp (con código de país, sin `+`), para recibir notificaciones de pedidos |

   Opcionales según lo que uses: `YAPE_PLIN_NUMBER`, `OWNER_NAME`, `PICKUP_ADDRESS`, `DELIVERY_SERVICE_PHONE`, `DELIVERY_1_PHONE`/`DELIVERY_1_NAME` (hasta `DELIVERY_4_...`).

4. Railway reiniciará el bot automáticamente

### 3.5 Obtener tu URL
1. Ve a la pestaña **"Settings"** del servicio
2. En la sección **"Networking"**, haz clic en **"Generate Domain"**
3. Copia la URL que aparece (ej: `https://chilango-bot-production.up.railway.app`)

---

## PASO 4 — Conectar Meta con Railway (configurar el webhook)

1. Vuelve a **developers.facebook.com** → tu app → **WhatsApp → Configuration**
2. En **"Webhook"**, haz clic en **"Edit"**
3. En **"Callback URL"**, pega:
   ```
   https://TU-URL-DE-RAILWAY.up.railway.app/webhook
   ```
   (reemplaza con tu URL real del Paso 3.5)
4. En **"Verify token"**, escribe **exactamente la misma palabra** que pusiste en la variable `META_VERIFY_TOKEN` en Railway
5. Haz clic en **"Verify and save"** — Meta le hará una petición a tu bot para confirmar que responde correctamente; si `META_VERIFY_TOKEN` no coincide, fallará
6. Debajo, en **"Webhook fields"**, busca **`messages`** y haz clic en **"Subscribe"** — sin este paso el bot nunca recibirá los mensajes de tus clientes

---

## PASO 5 — ¡Probar el bot!

1. Desde tu WhatsApp, envía cualquier mensaje al número de WhatsApp de Meta (el que anotaste en el Paso 2)
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
| WhatsApp Cloud API (Meta) | Las primeras 1,000 conversaciones/mes son gratis; luego cobra por conversación según el país |
| Claude API (IA) | ~$0.01 por conversación completa |
| **Total estimado** | **~$5-10/mes** (varía según volumen de mensajes) |

---

## Preguntas frecuentes

**¿El bot funciona 24/7?**
Sí, pero solo responde pedidos cuando se lo indicas en el horario (Vie-Dom 5-11pm).

**¿Puedo cambiar el menú?**
Sí, entra a `https://TU-URL-DE-RAILWAY.up.railway.app/admin/menu` (con tu usuario `admin` y `ADMIN_PASSWORD`) y edita precios, nombres o desactiva items — el cambio se aplica al bot de inmediato, sin reiniciar ni tocar código.

**¿El bot habla solo español?**
Sí, está configurado en español peruano/mexicano.

**¿Qué pasa si el cliente escribe cosas raras?**
Claude (la IA) maneja conversaciones naturales, responderá amablemente y redirigirá al menú.

---

## Soporte

Si algo no funciona, revisa:
1. Que las variables de entorno estén bien escritas en Railway (sobre todo `META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID` y `META_APP_SECRET`)
2. Que el webhook en Meta tenga la URL correcta, el `Verify token` coincida con `META_VERIFY_TOKEN`, y que estés suscrito al campo `messages`
3. Los logs en Railway → pestaña "Deployments" → "View Logs"
