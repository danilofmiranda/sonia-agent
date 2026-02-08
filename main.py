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
import asyncio
import logging
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
# LOGGING CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("sonia")

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
FEDEX_ACCOUNT_USA = os.getenv("FEDEX_ACCOUNT_USA", "202958384")  # Andean-2 (legacy var, misma cuenta)
FEDEX_ACCOUNT_WORLD = os.getenv("FEDEX_ACCOUNT_WORLD", "202958384")  # Andean-2
FEDEX_BASE_URL = "https://apis.fedex.com"

# Precios fijos para envíos USA <70kg
PRECIO_POR_KG_USA = 5.0  # USD por kg
PRECIO_POR_DIRECCION = 8.0  # USD por dirección

# ══════════════════════════════════════════════════════════════════════════════
# VALIDACIÓN DE VARIABLES CRÍTICAS AL INICIAR
# ══════════════════════════════════════════════════════════════════════════════

def validate_environment():
    """Valida que las variables de entorno críticas estén configuradas"""
    errors = []

    if not WHATSAPP_TOKEN:
        errors.append("WHATSAPP_TOKEN no está configurado - El bot NO podrá enviar mensajes")
    else:
        logger.info(f"✅ WHATSAPP_TOKEN configurado ({WHATSAPP_TOKEN[:15]}...)")

    if not ANTHROPIC_API_KEY:
        errors.append("ANTHROPIC_API_KEY no está configurado - El bot NO podrá procesar mensajes con IA")
    else:
        logger.info(f"✅ ANTHROPIC_API_KEY configurado ({ANTHROPIC_API_KEY[:10]}...)")

    logger.info(f"📱 WHATSAPP_PHONE_NUMBER_ID: {WHATSAPP_PHONE_NUMBER_ID}")
    logger.info(f"🔑 WHATSAPP_VERIFY_TOKEN: {WHATSAPP_VERIFY_TOKEN}")

    if errors:
        for err in errors:
            logger.error(f"❌ {err}")
        raise ValueError(
            "Variables de entorno críticas faltantes:\n" + "\n".join(f"  - {e}" for e in errors)
        )

# ══════════════════════════════════════════════════════════════════════════════
# BASE DE DATOS
# ══════════════════════════════════════════════════════════════════════════════

def init_database():
    """Inicializa la base de datos SQLite"""
    conn = sqlite3.connect("sonia_conversations.db")
    cursor = conn.cursor()

    # Tabla de conversaciones
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Tabla de mensajes
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

    # Tabla de cotizaciones
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

    # Buscar conversación existente (últimas 24 horas)
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

    async def send_message(self, to: str, text: str, retries: int = 3) -> Dict:
        """Envía un mensaje de texto con reintentos"""
        url = f"{self.api_url}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        # WhatsApp tiene límite de 4096 caracteres por mensaje
        if len(text) > 4096:
            text = text[:4090] + "..."

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"body": text}
        }

        last_error = None
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(url, headers=headers, json=payload)

                    if response.status_code == 200 or response.status_code == 201:
                        result = response.json()
                        logger.info(f"✅ Mensaje enviado exitosamente a {to}")
                        return result
                    else:
                        error_text = response.text
                        logger.error(
                            f"❌ WhatsApp API error (intento {attempt+1}/{retries}): "
                            f"Status {response.status_code} - {error_text}"
                        )
                        last_error = f"HTTP {response.status_code}: {error_text}"

                        # Si es error de autenticación, no reintentar
                        if response.status_code in (401, 403):
                            logger.error("🔐 TOKEN DE WHATSAPP INVÁLIDO O EXPIRADO - Verificar en Railway")
                            raise Exception(f"Token WhatsApp inválido: {response.status_code}")

            except httpx.TimeoutException:
                logger.error(f"⏱️ Timeout enviando mensaje (intento {attempt+1}/{retries})")
                last_error = "Timeout"
            except Exception as e:
                if "Token WhatsApp inválido" in str(e):
                    raise  # No reintentar errores de auth
                logger.error(f"❌ Error enviando mensaje (intento {attempt+1}/{retries}): {e}")
                last_error = str(e)

            if attempt < retries - 1:
                wait_time = 2 * (attempt + 1)
                logger.info(f"⏳ Reintentando en {wait_time}s...")
                await asyncio.sleep(wait_time)

        raise Exception(f"No se pudo enviar mensaje después de {retries} intentos: {last_error}")

    async def download_media(self, media_id: str) -> Optional[bytes]:
        """Descarga un archivo multimedia (audio, imagen, etc.)"""
        # Primero obtener la URL del media
        url = f"{self.api_url}/{media_id}"
        headers = {"Authorization": f"Bearer {self.token}"}

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(url, headers=headers)

                if response.status_code != 200:
                    logger.error(f"❌ Error obteniendo URL del media {media_id}: {response.status_code}")
                    return None

                media_data = response.json()
                media_url = media_data.get("url")

                if not media_url:
                    logger.error(f"❌ No se encontró URL del media en respuesta: {media_data}")
                    return None

                # Descargar el archivo
                download_response = await client.get(
                    media_url,
                    headers=headers,
                    follow_redirects=True
                )

                if download_response.status_code != 200:
                    logger.error(f"❌ Error descargando media: {download_response.status_code}")
                    return None

                logger.info(f"✅ Media descargado: {len(download_response.content)} bytes")
                return download_response.content

        except Exception as e:
            logger.error(f"❌ Error descargando media {media_id}: {e}")
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

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, headers=headers, json=payload)
        except Exception as e:
            logger.warning(f"⚠️ No se pudo marcar mensaje como leído: {e}")


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

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(url, headers=headers, data=data)

                if response.status_code != 200:
                    logger.error(f"❌ FedEx OAuth error: {response.status_code} - {response.text}")
                    return None

                token_data = response.json()
                self.token = token_data.get("access_token")
                logger.info("✅ FedEx token obtenido")
                return self.token
        except Exception as e:
            logger.error(f"❌ Error obteniendo token FedEx: {e}")
            return None

    async def get_rate_quote(
        self,
        origin_postal: str,
        origin_country: str,
        dest_postal: str,
        dest_country: str,
        weight_kg: float,
        packages: List[Dict] = None,
        dimensions: Dict = None,
        is_pallet: bool = False,
        account_number: str = None,
        declared_value: float = None
    ) -> Dict:
        """Obtiene cotización de FedEx con soporte para múltiples paquetes"""

        if not self.token:
            token = await self.get_token()
            if not token:
                return {"error": "No se pudo autenticar con FedEx"}

        # Siempre usar cuenta Andean-2
        if account_number is None:
            account_number = FEDEX_ACCOUNT_WORLD

        url = f"{self.base_url}/rate/v1/rates/quotes"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-locale": "en_US"
        }

        # Construir lista de paquetes
        package_line_items = []

        if packages and len(packages) > 0:
            for pkg in packages:
                pkg_weight_kg = pkg.get("weight_kg", weight_kg / max(len(packages), 1))
                pkg_weight_lb = pkg_weight_kg * 2.20462
                item = {
                    "weight": {
                        "units": "LB",
                        "value": round(pkg_weight_lb, 1)
                    }
                }
                pkg_length = pkg.get("length")
                pkg_width = pkg.get("width")
                pkg_height = pkg.get("height")
                if pkg_length and pkg_width and pkg_height:
                    item["dimensions"] = {
                        "length": pkg_length,
                        "width": pkg_width,
                        "height": pkg_height,
                        "units": "CM"
                    }
                package_line_items.append(item)
        else:
            weight_lb = weight_kg * 2.20462
            item = {
                "weight": {
                    "units": "LB",
                    "value": round(weight_lb, 1)
                }
            }
            if dimensions:
                item["dimensions"] = {
                    "length": dimensions.get("length"),
                    "width": dimensions.get("width"),
                    "height": dimensions.get("height"),
                    "units": "CM"
                }
            package_line_items.append(item)

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
                "pickupType": "CONTACT_FEDEX_TO_SCHEDULE",
                "rateRequestType": ["ACCOUNT", "LIST"],
                "packageCount": len(package_line_items),
                "requestedPackageLineItems": package_line_items
            }
        }

        if declared_value and declared_value > 0:
            payload["requestedShipment"]["customsClearanceDetail"] = {
                "dutiesPayment": {
                    "paymentType": "SENDER"
                },
                "commodities": [{
                    "description": "General Merchandise",
                    "quantity": len(package_line_items),
                    "quantityUnits": "PCS",
                    "weight": {
                        "units": "LB",
                        "value": round(weight_kg * 2.20462, 1)
                    },
                    "customsValue": {
                        "amount": declared_value,
                        "currency": "USD"
                    }
                }]
            }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                logger.info(f"📦 FedEx request: {len(package_line_items)} paquete(s), pickup=CONTACT_FEDEX_TO_SCHEDULE, declared_value={declared_value}")
                response = await client.post(url, headers=headers, json=payload)

                if response.status_code != 200:
                    logger.error(f"❌ FedEx Rate error: {response.status_code} - {response.text}")
                    if response.status_code == 401:
                        logger.info("🔄 Renovando token FedEx...")
                        await self.get_token()
                    return {"error": f"FedEx API error: {response.status_code}", "details": response.text[:500]}

                return response.json()
        except Exception as e:
            logger.error(f"❌ Error consultando FedEx: {e}")
            return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# PROCESADOR DE IA (CLAUDE)
# ══════════════════════════════════════════════════════════════════════════════

class SonIAProcessor:
    """Procesador de mensajes usando Claude AI"""

    SYSTEM_PROMPT = """Eres SonIA, la asistente virtual de cotizaciones de BloomsPal/Andean Fields.
Tu trabajo es ayudar a los clientes a obtener cotizaciones de envío FedEx.

INFORMACIÓN QUE NECESITAS EXTRAER (TODAS SON OBLIGATORIAS):
1. País de ORIGEN del envío
2. Ciudad de ORIGEN del envío
3. Código postal de ORIGEN
4. País de DESTINO
5. Ciudad de DESTINO
6. Código postal de DESTINO
7. Peso total en kg
8. ¿Es paletizado? (sí/no)
9. Número de cajas/paquetes (SIEMPRE preguntar cuántos)
10. Dimensiones de CADA paquete o pallet (largo x ancho x alto en cm) - OBLIGATORIO, no asumir valores
11. Valor declarado de la mercancía en USD - OBLIGATORIO, siempre preguntar

REGLAS DE PRECIOS:
- Cajas sueltas < 70kg desde Colombia a USA: $5 USD/kg + $8 USD por dirección (precio fijo, no necesita cotizar FedEx)
- Pallets a cualquier destino: Cotizar con FedEx API
- Cajas sueltas ≥ 70kg a USA: Cotizar con FedEx API
- Cualquier envío fuera de USA: Cotizar con FedEx API
- SIEMPRE cotizar la opción más económica disponible

COMPORTAMIENTO:
1. Saluda amablemente y pregunta cómo puedes ayudar
2. Extrae la información del mensaje del cliente
3. Si falta CUALQUIER información de la lista de arriba, pregunta educadamente. NO asumas valores por defecto para dimensiones ni valor declarado.
4. Cuando tengas TODA la información, genera la cotización
5. Presenta la cotización de forma profesional y clara

FORMATO DE RESPUESTA PARA COTIZACIÓN:
Cuando tengas toda la información, responde con un JSON estructurado así:
{
    "action": "quote",
    "data": {
        "origin_country": "CO",
        "origin_city": "Bogota",
        "origin_postal": "110111",
        "destination_country": "US",
        "destination_city": "Miami",
        "destination_postal": "33101",
        "weight_kg": 25,
        "is_pallet": false,
        "num_boxes": 3,
        "packages": [
            {"weight_kg": 10, "length": 40, "width": 30, "height": 30},
            {"weight_kg": 8, "length": 35, "width": 25, "height": 25},
            {"weight_kg": 7, "length": 30, "width": 20, "height": 20}
        ],
        "declared_value": 1500
    },
    "message": "Mensaje amigable para el cliente"
}

NOTA SOBRE PAQUETES:
- Si el cliente dice que tiene múltiples cajas/paquetes, pregunta las dimensiones y peso de CADA uno
- Si todos los paquetes son iguales, puedes usar la misma dimensión para todos
- El campo "packages" es una lista con cada paquete individual
- Si solo hay 1 paquete, la lista tiene un solo elemento

Si necesitas más información:
{
    "action": "ask",
    "missing": ["origin_postal", "dimensions", "declared_value"],
    "message": "Tu mensaje preguntando por la información faltante"
}

Si es una conversación general:
{
    "action": "chat",
    "message": "Tu respuesta conversacional"
}

IMPORTANTE: Responde SIEMPRE con un JSON válido. No incluyas texto fuera del JSON.

Siempre responde en español, de forma amigable y profesional.
Empresa: BloomsPal / Andean Fields (CI ANDEAN FIELDS)"""

    def __init__(self):
        # CORREGIDO: Usar cliente ASÍNCRONO en vez de síncrono
        self.client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    async def process_text(self, text: str, conversation_history: List[Dict] = None) -> Dict:
        """Procesa un mensaje de texto con Claude AI"""
        messages = []

        # Agregar historial de conversación
        if conversation_history:
            for msg in conversation_history[-6:]:  # Últimos 6 mensajes
                messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })

        # Agregar mensaje actual
        messages.append({"role": "user", "content": text})

        try:
            # CORREGIDO: Ahora usa await correctamente con AsyncAnthropic
            response = await self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                system=self.SYSTEM_PROMPT,
                messages=messages
            )

            if not response.content:
                logger.error("❌ Claude devolvió respuesta vacía")
                return {"action": "chat", "message": "Disculpa, tuve un problema procesando tu mensaje. ¿Podrías repetirlo?"}

            response_text = response.content[0].text
            logger.info(f"🤖 Claude respondió: {response_text[:100]}...")

            # Intentar parsear como JSON
            try:
                if "{" in response_text and "}" in response_text:
                    start = response_text.find("{")
                    end = response_text.rfind("}") + 1
                    json_str = response_text[start:end]
                    return json.loads(json_str)
            except json.JSONDecodeError:
                logger.warning(f"⚠️ Claude no devolvió JSON válido, usando como chat")

            # Si no es JSON, devolver como chat
            return {
                "action": "chat",
                "message": response_text
            }

        except anthropic.AuthenticationError:
            logger.error("❌ ANTHROPIC_API_KEY es inválida - verificar en Railway")
            return {"action": "chat", "message": "Disculpa, tenemos un problema técnico. Por favor intenta más tarde."}
        except anthropic.RateLimitError:
            logger.error("❌ Rate limit alcanzado en Anthropic API")
            return {"action": "chat", "message": "Estamos recibiendo muchas consultas. Por favor intenta en unos minutos."}
        except Exception as e:
            logger.error(f"❌ Error procesando con Claude: {e}")
            return {"action": "chat", "message": "Disculpa, tuve un problema procesando tu mensaje. ¿Podrías repetirlo?"}

    async def process_audio(self, audio_data: bytes, mime_type: str = "audio/ogg") -> Optional[str]:
        """Transcribe audio usando Claude"""
        # Codificar audio en base64
        audio_base64 = base64.standard_b64encode(audio_data).decode("utf-8")

        # Determinar el tipo de media
        media_type = "audio/webm"  # WhatsApp usa opus en webm/ogg
        if "ogg" in mime_type:
            media_type = "audio/ogg"
        elif "mp4" in mime_type or "m4a" in mime_type:
            media_type = "audio/mp4"

        try:
            # CORREGIDO: Ahora usa await correctamente con AsyncAnthropic
            response = await self.client.messages.create(
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

            if not response.content:
                logger.error("❌ Claude devolvió respuesta vacía al transcribir audio")
                return None

            transcription = response.content[0].text
            logger.info(f"🎤 Transcripción exitosa: {transcription[:80]}...")
            return transcription

        except Exception as e:
            logger.error(f"❌ Error transcribiendo audio: {e}")
            return None


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
        origin_country = quote_data.get("origin_country", "CO").upper()
        origin_postal = quote_data.get("origin_postal", "110111")
        weight_kg = quote_data.get("weight_kg", 0)
        is_pallet = quote_data.get("is_pallet", False)
        num_boxes = quote_data.get("num_boxes", 1)
        packages = quote_data.get("packages", [])
        declared_value = quote_data.get("declared_value", 0)

        result = {
            "success": True,
            "quote_type": "",
            "amount": 0,
            "currency": "USD",
            "details": "",
            "fedex_account_used": "",
            "all_services": []
        }

        # Regla 1: Cajas sueltas < 70kg desde Colombia a USA = precio fijo
        if dest_country == "US" and origin_country == "CO" and not is_pallet and weight_kg < 70:
            total = (weight_kg * PRECIO_POR_KG_USA) + PRECIO_POR_DIRECCION
            result["quote_type"] = "fixed_rate"
            result["amount"] = round(total, 2)
            result["fedex_account_used"] = FEDEX_ACCOUNT_WORLD
            result["details"] = f"Precio fijo: ${PRECIO_POR_KG_USA}/kg x {weight_kg}kg + ${PRECIO_POR_DIRECCION} por dirección"
            return result

        # Regla 2: Todo lo demás = cotizar con FedEx API
        try:
            fedex_response = await self.fedex.get_rate_quote(
                origin_postal=origin_postal,
                origin_country=origin_country,
                dest_postal=quote_data.get("destination_postal", ""),
                dest_country=dest_country,
                weight_kg=weight_kg,
                packages=packages,
                dimensions=quote_data.get("dimensions"),
                is_pallet=is_pallet,
                declared_value=declared_value
            )

            if "output" in fedex_response:
                rate_details = fedex_response["output"].get("rateReplyDetails", [])
                if rate_details:
                    all_services = []
                    for rate in rate_details:
                        rated_shipment = rate.get("ratedShipmentDetails", [{}])[0]
                        total_charge = rated_shipment.get("totalNetCharge", 0)
                        service_type = rate.get("serviceType", "")
                        service_name = rate.get("serviceName", service_type)
                        transit_days = rate.get("commit", {}).get("transitDays", {})
                        if isinstance(transit_days, dict):
                            transit_days = transit_days.get("description", "N/A")

                        all_services.append({
                            "service_type": service_type,
                            "service_name": service_name,
                            "total_charge": round(float(total_charge), 2),
                            "transit_days": str(transit_days)
                        })

                    all_services.sort(key=lambda x: x["total_charge"])
                    result["all_services"] = all_services

                    if all_services:
                        cheapest = all_services[0]
                        result["quote_type"] = "fedex_api"
                        result["amount"] = cheapest["total_charge"]
                        result["service_type"] = cheapest["service_type"]
                        result["service_name"] = cheapest["service_name"]
                        result["transit_days"] = cheapest["transit_days"]
                        result["fedex_account_used"] = FEDEX_ACCOUNT_WORLD
                        result["details"] = f"Servicio: {cheapest['service_name']} ({cheapest['transit_days']} días)"

                        logger.info(f"📊 Servicios FedEx disponibles ({len(all_services)}):")
                        for svc in all_services:
                            logger.info(f"   - {svc['service_name']}: ${svc['total_charge']} ({svc['transit_days']} días)")

                        return result

            error_msg = fedex_response.get("error", "")
            error_details = fedex_response.get("details", "")
            if error_msg:
                result["success"] = False
                result["details"] = f"Error FedEx: {error_msg}. {error_details}"
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
    # Startup
    logger.info("=" * 60)
    logger.info("🚀 Iniciando SonIA WhatsApp Agent...")
    logger.info("=" * 60)

    # Validar variables de entorno ANTES de arrancar
    validate_environment()

    init_database()

    logger.info("✅ Base de datos inicializada")
    logger.info("✅ SonIA WhatsApp Agent LISTO y escuchando")
    logger.info("=" * 60)
    yield
    # Shutdown
    logger.info("👋 SonIA WhatsApp Agent detenido")


app = FastAPI(
    title="SonIA - WhatsApp Quotation Agent",
    description="Agente de WhatsApp para cotizaciones de envío FedEx - BloomsPal",
    version="1.0.0",
    lifespan=lifespan
)

# Instancias globales
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
        logger.info("✅ Webhook verificado exitosamente")
        return PlainTextResponse(content=hub_challenge)

    raise HTTPException(status_code=403, detail="Token de verificación inválido")


@app.post("/webhook")
async def handle_webhook(request: Request):
    """Maneja los mensajes entrantes de WhatsApp"""
    from_number = None  # Inicializar para evitar NameError en except
    try:
        body = await request.json()
        logger.info(f"📩 Webhook recibido: {json.dumps(body)[:200]}...")

        # Extraer información del mensaje
        entry = body.get("entry", [])
        if not entry:
            return {"status": "no_entry"}

        changes = entry[0].get("changes", [])
        if not changes:
            return {"status": "no_changes"}

        value = changes[0].get("value", {})
        messages = value.get("messages", [])

        if not messages:
            # Puede ser una notificación de estado (delivered, read, etc.)
            statuses = value.get("statuses", [])
            if statuses:
                logger.info(f"📊 Notificación de estado: {statuses[0].get('status', 'unknown')}")
            return {"status": "no_messages"}

        message = messages[0]
        from_number = message.get("from")
        message_id = message.get("id")
        message_type = message.get("type")

        # VALIDACIÓN: verificar que los campos críticos no sean None
        if not from_number or not message_id or not message_type:
            logger.error(f"❌ Datos incompletos: from={from_number}, id={message_id}, type={message_type}")
            return {"status": "invalid_data"}

        logger.info(f"📨 Mensaje recibido de {from_number} - Tipo: {message_type}")

        # Marcar como leído (no bloqueante, puede fallar sin afectar)
        await whatsapp.mark_as_read(message_id)

        # Obtener o crear conversación
        conversation_id = get_or_create_conversation(from_number)

        # Obtener historial
        history = get_conversation_history(conversation_id)

        # Procesar según tipo de mensaje
        user_text = ""

        if message_type == "text":
            user_text = message.get("text", {}).get("body", "")
            logger.info(f"💬 Texto recibido: {user_text[:100]}")

        elif message_type == "audio":
            # Descargar y transcribir audio
            audio_info = message.get("audio", {})
            media_id = audio_info.get("id")
            mime_type = audio_info.get("mime_type", "audio/ogg")

            if media_id:
                logger.info(f"🎵 Descargando audio {media_id}...")
                audio_data = await whatsapp.download_media(media_id)
                if audio_data:
                    user_text = await processor.process_audio(audio_data, mime_type)
                    if user_text:
                        logger.info(f"🎤 Transcripción: {user_text[:100]}")
                    else:
                        logger.error("❌ Transcripción de audio falló")
                else:
                    logger.error("❌ No se pudo descargar el audio")
            else:
                logger.error("❌ Audio sin media_id")

        else:
            logger.info(f"⚠️ Tipo de mensaje no soportado: {message_type}")
            # Enviar mensaje al usuario informando
            try:
                await whatsapp.send_message(
                    from_number,
                    "Disculpa, por el momento solo puedo procesar mensajes de texto y audio. ¿Podrías escribirme tu consulta?"
                )
            except Exception:
                pass
            return {"status": "unsupported_type"}

        if not user_text:
            # Enviar feedback al usuario en vez de fallar silenciosamente
            try:
                await whatsapp.send_message(
                    from_number,
                    "No pude procesar tu mensaje. ¿Podrías intentar enviarlo de nuevo como texto?"
                )
            except Exception as send_err:
                logger.error(f"❌ No se pudo enviar mensaje de error: {send_err}")
            return {"status": "no_text_content"}

        # Guardar mensaje del usuario
        save_message(conversation_id, "user", user_text, message_type)

        # Procesar con Claude
        logger.info("🤖 Procesando con Claude AI...")
        response = await processor.process_text(user_text, history)

        action = response.get("action", "chat")
        response_message = response.get("message", "")
        logger.info(f"🤖 Claude action={action}, mensaje={response_message[:80]}...")

        # Si es una solicitud de cotización
        if action == "quote":
            quote_data = response.get("data", {})
            logger.info(f"📊 Calculando cotización: {quote_data}")
            quote_result = await calculator.calculate(quote_data)

            if quote_result["success"]:
                # Formatear mensaje de cotización
                origin_info = f"{quote_data.get('origin_city', 'Origen')}, {quote_data.get('origin_country', '')}"
                dest_info = f"{quote_data.get('destination_city', 'Destino')}, {quote_data.get('destination_country', '')}"
                num_pkgs = len(quote_data.get('packages', [])) or quote_data.get('num_boxes', 1)
                declared_val = quote_data.get('declared_value', 0)

                response_message = f"""✅ *COTIZACIÓN SonIA*

📤 *Origen:* {origin_info} (CP {quote_data.get('origin_postal', '')})
📍 *Destino:* {dest_info} (CP {quote_data.get('destination_postal', '')})
📦 *Peso total:* {quote_data.get('weight_kg', 0)} kg
{'🎁 *Paletizado:* Sí' if quote_data.get('is_pallet') else f'📦 *Paquetes:* {num_pkgs}'}
💎 *Valor declarado:* ${declared_val:,.2f} USD
🚛 *Recogida:* FedEx recoge en dirección de origen

💰 *PRECIO MÁS ECONÓMICO: ${quote_result['amount']:.2f} USD*
🏷️ *Servicio:* {quote_result.get('service_name', quote_result.get('service_type', 'FedEx'))}
📅 *Tiempo estimado:* {quote_result.get('transit_days', 'N/A')} días

📝 {quote_result['details']}"""

                # Agregar otros servicios disponibles si hay más de uno
                all_services = quote_result.get("all_services", [])
                if len(all_services) > 1:
                    response_message += "\n\n📋 *Otros servicios disponibles:*"
                    for svc in all_services[1:4]:
                        response_message += f"\n  • {svc['service_name']}: ${svc['total_charge']:.2f} USD ({svc['transit_days']} días)"

                response_message += "\n\n¿Deseas proceder con este envío? Responde *SÍ* para confirmar o escríbeme si necesitas otra cotización."

                # Guardar cotización
                quote_data["quote_amount"] = quote_result["amount"]
                quote_data["fedex_account_used"] = quote_result["fedex_account_used"]
                save_quotation(conversation_id, from_number, quote_data)
            else:
                response_message = f"❌ {quote_result['details']}\n\nPor favor verifica la información e intenta de nuevo."

        # Validar que hay mensaje para enviar
        if not response_message:
            response_message = "Disculpa, no pude generar una respuesta. ¿Podrías repetir tu consulta?"

        # Guardar respuesta
        save_message(conversation_id, "assistant", response_message)

        # Enviar respuesta por WhatsApp
        logger.info(f"📤 Enviando respuesta a {from_number}...")
        send_result = await whatsapp.send_message(from_number, response_message)
        logger.info(f"✅ Respuesta enviada exitosamente a {from_number}")

        return {"status": "processed"}

    except Exception as e:
        logger.error(f"❌ Error procesando webhook: {str(e)}")
        import traceback
        traceback.print_exc()

        # Intentar enviar mensaje de error al usuario
        try:
            if from_number:
                await whatsapp.send_message(
                    from_number,
                    "Disculpa, tuve un problema procesando tu mensaje. Por favor intenta de nuevo en unos momentos."
                )
        except Exception:
            logger.error("❌ No se pudo enviar mensaje de error al usuario")

        return {"status": "error", "message": str(e)}


@app.get("/stats")
async def get_stats():
    """Obtiene estadísticas del sistema"""
    conn = sqlite3.connect("sonia_conversations.db")
    cursor = conn.cursor()

    # Total conversaciones
    cursor.execute("SELECT COUNT(*) FROM conversations")
    total_conversations = cursor.fetchone()[0]

    # Total mensajes
    cursor.execute("SELECT COUNT(*) FROM messages")
    total_messages = cursor.fetchone()[0]

    # Total cotizaciones
    cursor.execute("SELECT COUNT(*) FROM quotations")
    total_quotations = cursor.fetchone()[0]

    # Cotizaciones de hoy
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
