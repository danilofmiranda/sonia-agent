"""
╔═══════════════════════════════════════════════════════════════════════════════╗
║                         SonIA - WhatsApp Quotation Agent                       ║
║                              BloomsPal / Andean Fields                         ║
╚═══════════════════════════════════════════════════════════════════════════════╝

Agente de WhatsApp para cotizaciones de envío FedEx.
- Recibe mensajes de texto y audio via WhatsApp
- Procesa con Claude AI para extraer información
- Consulta FedEx API para cotizaciones
- Responde con cotización profesional

Desarrollado por Claude para BloomsPal - Febrero 2026
"""

import os
import json
import base64
import httpx
import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel
import anthropic

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN - Variables de Entorno
# ══════════════════════════════════════════════════════════════════════════════

# WhatsApp API (Meta)
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "275484188971164")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "sonia_bloomspal_2026")
WHATSAPP_API_URL = "https://graph.facebook.com/v18.0"

# Claude API (Anthropic)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# FedEx API
FEDEX_API_KEY = os.getenv("FEDEX_API_KEY", "l7e4ca666923294740bae8dfde52ca1f52")
FEDEX_SECRET_KEY = os.getenv("FEDEX_SECRET_KEY", "81d7f9db60554e9b97ffa7c76075763c")
FEDEX_ACCOUNT_USA = os.getenv("FEDEX_ACCOUNT_USA", "740561073")
FEDEX_ACCOUNT_WORLD = os.getenv("FEDEX_ACCOUNT_WORLD", "202958384")
FEDEX_BASE_URL = "https://apis.fedex.com"

# Precios fijos para envíos USA <70kg
PRECIO_POR_KG_USA = 5.0
PRECIO_POR_DIRECCION = 8.0

# ══════════════════════════════════════════════════════════════════════════════
# BASE DE DATOS
# ══════════════════════════════════════════════════════════════════════════════

def init_database():
    """Inicializa la base de datos SQLite"""
    conn = sqlite3.connect("sonia_conversations.db")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            message_type TEXT DEFAULT 'text',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER,
            phone_number TEXT,
            destination_country TEXT,
            destination_city TEXT,
            destination_postal TEXT,
            weight_kg REAL,
            is_pallet BOOLEAN DEFAULT FALSE,
            num_boxes INTEGER DEFAULT 1,
            dimensions TEXT,
            declared_value REAL,
            quote_amount REAL,
            fedex_account_used TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        )
    """)

    conn.commit()
    conn.close()


def get_or_create_conversation(phone_number: str) -> int:
    """Obtiene o crea una conversación para un número de teléfono"""
    conn = sqlite3.connect("sonia_conversations.db")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id FROM conversations
        WHERE phone_number = ?
        AND updated_at > datetime('now', '-24 hours')
        ORDER BY updated_at DESC LIMIT 1
    """, (phone_number,))

    result = cursor.fetchone()

    if result:
        conv_id = result[0]
        cursor.execute("UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (conv_id,))
    else:
        cursor.execute("INSERT INTO conversations (phone_number) VALUES (?)", (phone_number,))
        conv_id = cursor.lastrowid

    conn.commit()
    conn.close()
    return conv_id


def save_message(conversation_id: int, role: str, content: str, message_type: str = "text"):
    """Guarda un mensaje en la base de datos"""
    conn = sqlite3.connect("sonia_conversations.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (conversation_id, role, content, message_type) VALUES (?, ?, ?, ?)",
        (conversation_id, role, content, message_type)
    )
    conn.commit()
    conn.close()


def get_conversation_history(conversation_id: int, limit: int = 10) -> List[Dict]:
    """Obtiene el historial de mensajes de una conversación"""
    conn = sqlite3.connect("sonia_conversations.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT role, content FROM messages
        WHERE conversation_id = ?
        ORDER BY created_at DESC LIMIT ?
    """, (conversation_id, limit))

    messages = [{"role": row[0], "content": row[1]} for row in cursor.fetchall()]
    conn.close()
    return list(reversed(messages))


def save_quotation(conversation_id: int, phone_number: str, quote_data: Dict):
    """Guarda una cotización en la base de datos"""
    conn = sqlite3.connect("sonia_conversations.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO quotations (
            conversation_id, phone_number, destination_country, destination_city,
            destination_postal, weight_kg, is_pallet, num_boxes, dimensions,
            declared_value, quote_amount, fedex_account_used
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        conversation_id, phone_number,
        quote_data.get("destination_country"),
        quote_data.get("destination_city"),
        quote_data.get("destination_postal"),
        quote_data.get("weight_kg"),
        quote_data.get("is_pallet", False),
        quote_data.get("num_boxes", 1),
        json.dumps(quote_data.get("dimensions", {})),
        quote_data.get("declared_value"),
        quote_data.get("quote_amount"),
        quote_data.get("fedex_account_used")
    ))
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# CLIENTE WHATSAPP
# ══════════════════════════════════════════════════════════════════════════════

class WhatsAppClient:
    """Cliente para interactuar con WhatsApp Cloud API"""

    def __init__(self):
        self.token = WHATSAPP_TOKEN
        self.phone_number_id = WHATSAPP_PHONE_NUMBER_ID
        self.api_url = WHATSAPP_API_URL

    async def send_message(self, to: str, text: str):
        """Envía un mensaje de texto"""
        url = f"{self.api_url}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"body": text}
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload)
            return response.json()

    async def download_media(self, media_id: str) -> bytes:
        """Descarga un archivo multimedia (audio, imagen, etc.)"""
        url = f"{self.api_url}/{media_id}"
        headers = {"Authorization": f"Bearer {self.token}"}

        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers)
            media_data = response.json()
            media_url = media_data.get("url")

            if media_url:
                download_response = await client.get(
                    media_url,
                    headers=headers,
                    follow_redirects=True
                )
                return download_response.content

        return None

    async def mark_as_read(self, message_id: str):
        """Marca un mensaje como leído"""
        url = f"{self.api_url}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id
        }

        async with httpx.AsyncClient() as client:
            await client.post(url, headers=headers, json=payload)


# ══════════════════════════════════════════════════════════════════════════════
# CLIENTE FEDEX
# ══════════════════════════════════════════════════════════════════════════════

class FedExClient:
    """Cliente para interactuar con FedEx API"""

    def __init__(self):
        self.api_key = FEDEX_API_KEY
        self.secret_key = FEDEX_SECRET_KEY
        self.base_url = FEDEX_BASE_URL
        self.token = None
        self.token_expires = None

    async def get_token(self) -> str:
        """Obtiene token de autenticación OAuth2"""
        url = f"{self.base_url}/oauth/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.secret_key
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, data=data)
            token_data = response.json()
            self.token = token_data.get("access_token")
            return self.token

    async def get_rate_quote(
        self,
        origin_postal: str,
        origin_country: str,
        dest_postal: str,
        dest_country: str,
        weight_kg: float,
        dimensions: Dict = None,
        is_pallet: bool = False,
        account_number: str = None
    ) -> Dict:
        """Obtiene cotización de FedEx"""

        if not self.token:
            await self.get_token()

        if account_number is None:
            if is_pallet or dest_country != "US":
                account_number = FEDEX_ACCOUNT_WORLD
            elif weight_kg >= 70:
                account_number = FEDEX_ACCOUNT_WORLD
            else:
                account_number = FEDEX_ACCOUNT_USA

        url = f"{self.base_url}/rate/v1/rates/quotes"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-locale": "en_US"
        }

        weight_lb = weight_kg * 2.20462

        if dimensions is None:
            dimensions = {"length": 30, "width": 30, "height": 30}

        payload = {
            "accountNumber": {"value": account_number},
            "requestedShipment": {
                "shipper": {
                    "address": {
                        "postalCode": origin_postal,
                        "countryCode": origin_country
                    }
                },
                "recipient": {
                    "address": {
                        "postalCode": dest_postal,
                        "countryCode": dest_country
                    }
                },
                "pickupType": "DROPOFF_AT_FEDEX_LOCATION",
                "rateRequestType": ["ACCOUNT", "LIST"],
                "requestedPackageLineItems": [{
                    "weight": {
                        "units": "LB",
                        "value": weight_lb
                    },
                    "dimensions": {
                        "length": dimensions.get("length", 30),
                        "width": dimensions.get("width", 30),
                        "height": dimensions.get("height", 30),
                        "units": "CM"
                    }
                }]
            }
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload, timeout=30)
            return response.json()


# ══════════════════════════════════════════════════════════════════════════════
# PROCESADOR DE IA (CLAUDE)
# ══════════════════════════════════════════════════════════════════════════════

class SonIAProcessor:
    """Procesador de mensajes usando Claude AI"""

    SYSTEM_PROMPT = """Eres SonIA, la asistente virtual de cotizaciones de BloomsPal/Andean Fields.
Tu trabajo es ayudar a los clientes a obtener cotizaciones de envío FedEx.

INFORMACIÓN QUE NECESITAS EXTRAER:
1. País de destino
2. Ciudad de destino
3. Código postal de destino
4. Peso total en kg
5. ¿Es paletizado? (sí/no)
6. Número de cajas (si aplica)
7. Dimensiones (largo x ancho x alto en cm)
8. Valor declarado (opcional)

REGLAS DE PRECIOS:
- Cajas sueltas < 70kg a USA: $5 USD/kg + $8 USD por dirección (precio fijo)
- Pallets a cualquier destino: Cotizar con FedEx API
- Cajas sueltas >= 70kg a USA: Cotizar con FedEx API
- Cualquier envío fuera de USA: Cotizar con FedEx API

COMPORTAMIENTO:
1. Saluda amablemente y pregunta cómo puedes ayudar
2. Extrae la información del mensaje del cliente
3. Si falta información crítica (destino, peso), pregunta educadamente
4. Cuando tengas toda la información, genera la cotización
5. Presenta la cotización de forma profesional y clara

FORMATO DE RESPUESTA PARA COTIZACIÓN:
{
    "action": "quote",
    "data": {
        "destination_country": "US",
        "destination_city": "Miami",
        "destination_postal": "33101",
        "weight_kg": 25,
        "is_pallet": false,
        "num_boxes": 5,
        "dimensions": {"length": 40, "width": 30, "height": 30},
        "declared_value": 500
    },
    "message": "Mensaje amigable para el cliente"
}

Si necesitas más información:
{
    "action": "ask",
    "missing": ["postal_code", "weight"],
    "message": "Tu mensaje preguntando por la información faltante"
}

Si es una conversación general:
{
    "action": "chat",
    "message": "Tu respuesta conversacional"
}

Siempre responde en español, de forma amigable y profesional.
Origen de envíos: Miami, FL 33166, USA (BloomsPal warehouse)"""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    async def process_text(self, text: str, conversation_history: List[Dict] = None) -> Dict:
        """Procesa un mensaje de texto"""
        messages = []

        if conversation_history:
            for msg in conversation_history[-6:]:
                messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })

        messages.append({"role": "user", "content": text})

        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=self.SYSTEM_PROMPT,
            messages=messages
        )

        response_text = response.content[0].text

        try:
            if "{" in response_text and "}" in response_text:
                start = response_text.find("{")
                end = response_text.rfind("}") + 1
                json_str = response_text[start:end]
                return json.loads(json_str)
        except json.JSONDecodeError:
            pass

        return {
            "action": "chat",
            "message": response_text
        }

    async def process_audio(self, audio_data: bytes, mime_type: str = "audio/ogg") -> str:
        """Transcribe audio usando Claude"""
        audio_base64 = base64.standard_b64encode(audio_data).decode("utf-8")

        media_type = "audio/webm"
        if "ogg" in mime_type:
            media_type = "audio/ogg"
        elif "mp4" in mime_type or "m4a" in mime_type:
            media_type = "audio/mp4"

        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Transcribe el siguiente audio de voz. Solo devuelve la transcripción del texto hablado, sin agregar comentarios ni explicaciones."
                    },
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": audio_base64
                        }
                    }
                ]
            }]
        )

        return response.content[0].text


# ══════════════════════════════════════════════════════════════════════════════
# CALCULADORA DE COTIZACIONES
# ══════════════════════════════════════════════════════════════════════════════

class QuoteCalculator:
    """Calcula cotizaciones de envío"""

    def __init__(self):
        self.fedex = FedExClient()

    async def calculate(self, quote_data: Dict) -> Dict:
        """Calcula la cotización según las reglas de negocio"""

        dest_country = quote_data.get("destination_country", "").upper()
        weight_kg = quote_data.get("weight_kg", 0)
        is_pallet = quote_data.get("is_pallet", False)
        num_boxes = quote_data.get("num_boxes", 1)

        result = {
            "success": True,
            "quote_type": "",
            "amount": 0,
            "currency": "USD",
            "details": "",
            "fedex_account_used": ""
        }

        # Regla 1: Cajas sueltas < 70kg a USA = precio fijo
        if dest_country == "US" and not is_pallet and weight_kg < 70:
            total = (weight_kg * PRECIO_POR_KG_USA) + PRECIO_POR_DIRECCION
            result["quote_type"] = "fixed_rate"
            result["amount"] = round(total, 2)
            result["fedex_account_used"] = FEDEX_ACCOUNT_USA
            result["details"] = f"Precio fijo: ${PRECIO_POR_KG_USA}/kg x {weight_kg}kg + ${PRECIO_POR_DIRECCION} por dirección"
            return result

        # Regla 2: Todo lo demás = cotizar con FedEx API
        try:
            fedex_response = await self.fedex.get_rate_quote(
                origin_postal="33166",
                origin_country="US",
                dest_postal=quote_data.get("destination_postal", ""),
                dest_country=dest_country,
                weight_kg=weight_kg,
                dimensions=quote_data.get("dimensions"),
                is_pallet=is_pallet
            )

            if "output" in fedex_response:
                rate_details = fedex_response["output"].get("rateReplyDetails", [])
                if rate_details:
                    best_rate = None
                    for rate in rate_details:
                        rated_shipment = rate.get("ratedShipmentDetails", [{}])[0]
                        total_charge = rated_shipment.get("totalNetCharge", 0)
                        if best_rate is None or total_charge < best_rate:
                            best_rate = total_charge
                            result["service_type"] = rate.get("serviceType", "")

                    if best_rate:
                        result["quote_type"] = "fedex_api"
                        result["amount"] = round(float(best_rate), 2)
                        result["fedex_account_used"] = FEDEX_ACCOUNT_WORLD if (is_pallet or dest_country != "US" or weight_kg >= 70) else FEDEX_ACCOUNT_USA
                        result["details"] = f"Cotización FedEx - Servicio: {result.get('service_type', 'Standard')}"
                        return result

            result["success"] = False
            result["details"] = "No se pudo obtener cotización de FedEx. Por favor contacte a soporte."

        except Exception as e:
            result["success"] = False
            result["details"] = f"Error al consultar FedEx: {str(e)}"

        return result


# ══════════════════════════════════════════════════════════════════════════════
# APLICACIÓN FASTAPI
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicialización y limpieza de la aplicación"""
    init_database()
    print("🚀 SonIA WhatsApp Agent iniciado")
    print(f"📱 Phone Number ID: {WHATSAPP_PHONE_NUMBER_ID}")
    yield
    print("👋 SonIA WhatsApp Agent detenido")


app = FastAPI(
    title="SonIA - WhatsApp Quotation Agent",
    description="Agente de WhatsApp para cotizaciones de envío FedEx - BloomsPal",
    version="1.0.0",
    lifespan=lifespan
)

whatsapp = WhatsAppClient()
processor = SonIAProcessor()
calculator = QuoteCalculator()


@app.get("/")
async def root():
    """Endpoint raíz - Health check"""
    return {
        "status": "online",
        "service": "SonIA WhatsApp Agent",
        "version": "1.0.0",
        "company": "BloomsPal / Andean Fields"
    }


@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge")
):
    """Verificación del webhook de WhatsApp"""
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        print(f"✅ Webhook verificado exitosamente")
        return PlainTextResponse(content=hub_challenge)

    raise HTTPException(status_code=403, detail="Token de verificación inválido")


@app.post("/webhook")
async def handle_webhook(request: Request):
    """Maneja los mensajes entrantes de WhatsApp"""
    try:
        body = await request.json()

        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return {"status": "no_messages"}

        message = messages[0]
        from_number = message.get("from")
        message_id = message.get("id")
        message_type = message.get("type")

        print(f"📨 Mensaje recibido de {from_number} - Tipo: {message_type}")

        await whatsapp.mark_as_read(message_id)

        conversation_id = get_or_create_conversation(from_number)
        history = get_conversation_history(conversation_id)

        user_text = ""

        if message_type == "text":
            user_text = message.get("text", {}).get("body", "")

        elif message_type == "audio":
            audio_info = message.get("audio", {})
            media_id = audio_info.get("id")
            mime_type = audio_info.get("mime_type", "audio/ogg")

            if media_id:
                audio_data = await whatsapp.download_media(media_id)
                if audio_data:
                    user_text = await processor.process_audio(audio_data, mime_type)
                    print(f"🎤 Transcripción: {user_text}")

        if not user_text:
            return {"status": "no_text_content"}

        save_message(conversation_id, "user", user_text, message_type)

        response = await processor.process_text(user_text, history)

        action = response.get("action", "chat")
        response_message = response.get("message", "")

        if action == "quote":
            quote_data = response.get("data", {})
            quote_result = await calculator.calculate(quote_data)

            if quote_result["success"]:
                response_message = f"""✅ *COTIZACIÓN SonIA*

📍 *Destino:* {quote_data.get('destination_city', '')}, {quote_data.get('destination_country', '')}
📦 *Peso:* {quote_data.get('weight_kg', 0)} kg
{'🎁 *Paletizado:* Sí' if quote_data.get('is_pallet') else '📦 *Cajas:* ' + str(quote_data.get('num_boxes', 1))}

💰 *PRECIO: ${quote_result['amount']:.2f} USD*

📝 {quote_result['details']}

¿Deseas proceder con este envío? Responde *SÍ* para confirmar o escríbeme si necesitas otra cotización."""

                quote_data["quote_amount"] = quote_result["amount"]
                quote_data["fedex_account_used"] = quote_result["fedex_account_used"]
                save_quotation(conversation_id, from_number, quote_data)
            else:
                response_message = f"❌ {quote_result['details']}\n\nPor favor verifica la información e intenta de nuevo."

        save_message(conversation_id, "assistant", response_message)
        await whatsapp.send_message(from_number, response_message)

        return {"status": "processed"}

    except Exception as e:
        print(f"❌ Error procesando webhook: {str(e)}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}


@app.get("/stats")
async def get_stats():
    """Obtiene estadísticas del sistema"""
    conn = sqlite3.connect("sonia_conversations.db")
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM conversations")
    total_conversations = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM messages")
    total_messages = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM quotations")
    total_quotations = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*) FROM quotations
        WHERE date(created_at) = date('now')
    """)
    today_quotations = cursor.fetchone()[0]

    conn.close()

    return {
        "total_conversations": total_conversations,
        "total_messages": total_messages,
        "total_quotations": total_quotations,
        "today_quotations": today_quotations
    }


# ══════════════════════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

