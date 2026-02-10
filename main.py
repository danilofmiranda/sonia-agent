"""
╔═══════════════════════════════════════════════════════════════════════════════╗
║                         SonIA - WhatsApp Quotation Agent                       ║
║                              BloomsPal                         ║
╚═══════════════════════════════════════════════════════════════════════════════╝

Agente de WhatsApp para cotizaciones de envío - BloomsPal.
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
import xmlrpc.client
import uuid

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
FEDEX_TRACK_API_KEY = os.getenv("FEDEX_TRACK_API_KEY", FEDEX_API_KEY)
FEDEX_TRACK_SECRET_KEY = os.getenv("FEDEX_TRACK_SECRET_KEY", FEDEX_SECRET_KEY)
FEDEX_ACCOUNT_USA = os.getenv("FEDEX_ACCOUNT_USA", "202958384")  # Andean-2 (legacy var, misma cuenta)
FEDEX_ACCOUNT_WORLD = os.getenv("FEDEX_ACCOUNT_WORLD", "202958384")  # Andean-2
FEDEX_BASE_URL = "https://apis.fedex.com"

# Precios fijos para envíos USA <70kg
PRECIO_POR_KG_USA = 5.0  # USD por kg
PRECIO_POR_DIRECCION = 8.0  # USD por dirección

# Odoo API
ODOO_URL = os.getenv("ODOO_URL", "https://bloomspal.odoo.com")
ODOO_DB = os.getenv("ODOO_DB", "bloomspal")
ODOO_USER = os.getenv("ODOO_USER", "danilo@bloomspal.com")
ODOO_API_KEY = os.getenv("ODOO_API_KEY", "")
ODOO_HELPDESK_TEAM_ID = int(os.getenv("ODOO_HELPDESK_TEAM_ID", "1"))
ODOO_SALES_TEAM_ID = int(os.getenv("ODOO_SALES_TEAM_ID", "7"))
ODOO_SPREADSHEET_ID = int(os.getenv("ODOO_SPREADSHEET_ID", "114"))

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


    # Tabla de usuarios WhatsApp
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS whatsapp_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE NOT NULL,
            cliente TEXT DEFAULT '',
            nombre TEXT DEFAULT '',
            nickname TEXT DEFAULT '',
            rol TEXT DEFAULT 'cliente',
            spreadsheet_row INTEGER DEFAULT 0,
            bloqueo TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
# NORMALIZACIÓN DE ESTADOS DE ENVÍO
# ══════════════════════════════════════════════════════════════════════════════

def get_short_status(status_code: str, description: str) -> str:
    """Normaliza estado del transportista a status corto de SonIA"""
    code_upper = (status_code or "").upper()
    desc_lower = (description or "").lower()

    # Prioridad 1: Mapeo por código
    code_map = {
        "DL": "Delivered",
        "IT": "In Transit",
        "PU": "Picked Up",
        "OD": "Out for Delivery",
        "CD": "In Customs",
        "IN": "Label Created",
        "SP": "Label Created",
        "PL": "Label Created",
        "DE": "Exception",
        "SE": "Exception",
        "OC": "Exception",
    }
    if code_upper in code_map:
        return code_map[code_upper]

    # Prioridad 2: Mapeo por descripción
    if "delivered" in desc_lower:
        return "Delivered"
    elif "out for delivery" in desc_lower:
        return "Out for Delivery"
    elif "picked up" in desc_lower or "package received" in desc_lower:
        return "Picked Up"
    elif any(w in desc_lower for w in ["in transit", "departed", "arrived", "on fedex vehicle", "left origin"]):
        return "In Transit"
    elif "clearance" in desc_lower or "customs" in desc_lower:
        return "In Customs"
    elif "delay" in desc_lower:
        return "Delayed"
    elif "exception" in desc_lower or "hold" in desc_lower:
        return "Exception"
    elif "return" in desc_lower:
        return "Returned to Sender"
    elif "shipment information sent" in desc_lower or "label" in desc_lower:
        return "Label Created"
    else:
        return description or "Unknown"


# ══════════════════════════════════════════════════════════════════════════════
# CLIENTE FEDEX
# ══════════════════════════════════════════════════════════════════════════════

class FedExClient:
    """Cliente para interactuar con FedEx API"""

    def __init__(self, api_key=None, secret_key=None):
        self.api_key = api_key or FEDEX_API_KEY
        self.secret_key = secret_key or FEDEX_SECRET_KEY
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
        declared_value: float = None,
        shipping_date: str = None
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
                "preferredCurrency": "USD",
                "packageCount": len(package_line_items),
                "requestedPackageLineItems": package_line_items,
                "shipDateStamp": shipping_date or datetime.now().strftime("%Y-%m-%d")
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

    async def track_shipment(self, tracking_number: str) -> Dict:
        """Obtiene información de rastreo de FedEx Track API"""

        if not self.token:
            token = await self.get_token()
            if not token:
                return {"error": "No se pudo autenticar con el sistema de rastreo"}

        url = f"{self.base_url}/track/v1/trackingnumbers"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-locale": "en_US"
        }

        payload = {
            "includeDetailedScans": True,
            "trackingInfo": [
                {
                    "trackingNumberInfo": {
                        "trackingNumber": tracking_number
                    }
                }
            ]
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(url, headers=headers, json=payload)

                if response.status_code == 401:
                    logger.info("🔄 Token expirado, renovando para tracking...")
                    await self.get_token()
                    headers["Authorization"] = f"Bearer {self.token}"
                    response = await client.post(url, headers=headers, json=payload)

                if response.status_code != 200:
                    logger.error(f"❌ FedEx Track error: {response.status_code} - {response.text[:500]}")
                    return {"error": f"Error del sistema de rastreo: {response.status_code}"}

                logger.info(f"✅ FedEx Track API respondió exitosamente para {tracking_number}")
                return response.json()
        except Exception as e:
            logger.error(f"❌ Error rastreando con FedEx: {e}")
            return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# PROCESADOR DE IA (CLAUDE)
# ══════════════════════════════════════════════════════════════════════════════

class SonIAProcessor:
    """Procesador de mensajes usando Claude AI"""

    SYSTEM_PROMPT = """Eres SonIA, la asistente virtual de BloomsPal.
Tu trabajo es ayudar a los clientes con cotizaciones de envío y rastreo de guías.

ESTILO DE COMUNICACIÓN:
- Tono amigable, profesional y siempre dispuesta a ayudar
- Respuestas CONCISAS y directas, sin rodeos innecesarios
- Cuando necesites pedir información al usuario, usa bullet points para dar claridad
- No repitas información que el usuario ya te dio
- Sé cálida pero eficiente

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
12. Fecha de salida / shipping date (formato YYYY-MM-DD) - OBLIGATORIO, siempre preguntar cuándo desea despachar

REGLAS DE PRECIOS:
- Cajas sueltas < 70kg desde Colombia a USA: $5 USD/kg + $8 USD por dirección (precio fijo)
- Pallets a cualquier destino: Cotizar con el sistema
- Cajas sueltas ≥ 70kg a USA: Cotizar con el sistema
- Cualquier envío fuera de USA: Cotizar con el sistema
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
        "declared_value": 1500,
        "shipping_date": "2026-02-15"
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
    "missing": ["origin_postal", "dimensions", "declared_value", "shipping_date"],
    "message": "Tu mensaje preguntando por la información faltante"
}

Si es una conversación general:
{
    "action": "chat",
    "message": "Tu respuesta conversacional"
}

RASTREO DE ENVÍOS:
Si el cliente envía un número de rastreo (9-30 dígitos) o pregunta por el estado de un envío/guía:
{
    "action": "track",
    "tracking_number": "794629639030",
    "message": "Consultando el estado de tu envío..."
}

CÓMO DETECTAR SOLICITUDES DE RASTREO:
- El cliente envía un número largo (9-30 dígitos) sin contexto de cotización
- Usa palabras como "rastrear", "tracking", "guía", "estado del envío", "dónde está mi paquete", "seguimiento"
- Si el cliente pide rastreo pero NO da número, usa action "ask" pidiendo el tracking_number

IMPORTANTE: Responde SIEMPRE con un JSON válido. No incluyas texto fuera del JSON.

Siempre responde en español, de forma amigable y profesional.
IMPORTANTE: NUNCA menciones FedEx ni ningún proveedor de transporte específico al cliente. Siempre habla de "BloomsPal" como el servicio de envío. No reveles nombres de transportistas.


SOPORTE Y CONTACTO (ODOO):
IMPORTANTE - ANTES de crear CUALQUIER ticket (soporte u orden):
- Si el CONTEXTO USUARIO ya tiene nombre y empresa, USA esos datos directamente. NO vuelvas a preguntar.
- Solo pregunta nombre y empresa si NO están en el CONTEXTO USUARIO (usuario nuevo o datos faltantes).
- NO generes el JSON de action "support" ni "order" hasta tener AMBOS datos (nombre persona + compañía)

Si el cliente necesita:
1. Crear un caso de soporte/queja/reclamo (SOLO cuando ya tengas nombre y compañía):
{
    "action": "support",
    "data": {
        "subject": "Resumen breve del problema",
        "description": "Descripción detallada incluyendo nombre del contacto, compañía y detalles del problema",
        "company_name": "Nombre de la compañía del cliente",
        "contact_name": "Nombre de la persona"
    },
    "message": "He creado tu caso de soporte..."
}

2. Buscar un contacto de BloomsPal o preguntar por alguien:
{
    "action": "contact",
    "data": {
        "query": "nombre o término de búsqueda"
    },
    "message": "Buscando la información de contacto..."
}

3. OPORTUNIDAD DE VENTA - Cuando el cliente dice SÍ/proceder/confirmar después de una cotización:
IMPORTANTE: Si el CONTEXTO USUARIO ya tiene nombre y empresa, úsalos directamente para la orden. Solo pregunta si NO están disponibles.
{
    "action": "order",
    "data": {
        "company_name": "Nombre de la compañía del cliente",
        "contact_name": "Nombre de la persona",
        "quote_summary": "Resumen COMPLETO de la cotización: origen, destino, peso, dimensiones de cada caja, número de cajas, valor declarado, fecha de envío, precio cotizado por servicio, y todos los detalles necesarios para que el equipo cree un lead y cotice manualmente sin necesitar más información"
    },
    "message": "Estoy registrando tu orden de envío..."
}

CÓMO DETECTAR SOLICITUDES DE SOPORTE:
- Palabras: "queja", "reclamo", "problema", "soporte", "ayuda con mi envío", "daño", "pérdida", "retraso", "caso", "ticket"
- PRIMERO pregunta el nombre del contacto y el nombre de la compañía
- Luego pregunta detalles del problema
- Solo cuando tengas toda la info, genera el JSON con action "support"

CÓMO DETECTAR CONFIRMACIÓN DE ORDEN:
- Palabras: "sí", "si", "proceder", "confirmar", "adelante", "sí quiero", "vamos", "acepto", "de acuerdo"
- Solo aplica si previamente se presentó una cotización en la conversación
- PRIMERO pregunta el nombre del contacto y el nombre de la compañía
- Luego genera el JSON con action "order" incluyendo TODOS los detalles de la cotización

CÓMO DETECTAR SOLICITUDES DE CONTACTO:
- Palabras: "contactar", "hablar con", "teléfono de", "email de", "quién maneja", "responsable de"
- Si piden hablar con alguien específico, busca el contacto


IDENTIFICACIÓN DE USUARIO:
Al inicio de cada conversación se te proporcionará un CONTEXTO USUARIO con información del usuario.

Si el usuario es NUEVO (no registrado):
- Preséntate como SonIA de BloomsPal
- Pregúntale su nombre completo y a qué empresa pertenece
- Pregúntale: "¿Hay algún nombre o apodo por el que prefieras que te llame?"
- Si no quiere nickname, déjalo vacío
- Cuando tengas nombre y empresa (nickname es opcional), responde con:
{
    "action": "register_user",
    "data": {
        "nombre": "Nombre Completo",
        "cliente": "Nombre Empresa",
        "nickname": "apodo o vacío si no dio"
    },
    "message": "Tu mensaje amigable confirmando registro y preguntando en qué ayudar"
}

Si un usuario REGISTRADO dice "soy empleado", "trabajo en BloomsPal", "soy de BloomsPal" o similar:
{
    "action": "claim_employee",
    "message": "Mensaje confirmando que se registró como empleado"
}

Si un usuario registrado dice "llámame...", "dime...", "me gusta que me llamen...", "preféro que me digan..." o similar:
{
    "action": "update_nickname",
    "data": {
        "nickname": "el nuevo apodo que quiere"
    },
    "message": "Tu mensaje confirmando el cambio de nombre"
}

REGLAS DE NOMBRE:
- Si el contexto incluye un nombre/nickname, SIEMPRE úsalo para personalizar tus respuestas
- Sé amigable y cercana, como una amiga que ayuda
- NUNCA preguntes a un cliente si es empleado. Solo el usuario puede decirte que es empleado
- Si el usuario ya está registrado, NO vuelvas a pedir su nombre

Empresa: BloomsPal"""

    def __init__(self):
        # CORREGIDO: Usar cliente ASÍNCRONO en vez de síncrono
        self.client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    async def process_text(self, text: str, conversation_history: List[Dict] = None, user_context: str = "") -> Dict:
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
                system=(user_context + "\n\n" + self.SYSTEM_PROMPT) if user_context else self.SYSTEM_PROMPT,
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
        shipping_date = quote_data.get("shipping_date")

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
                declared_value=declared_value,
                shipping_date=shipping_date
            )

            # Extraer el precio de la respuesta de FedEx
            logger.info(f"\U0001f4cb FedEx response keys: {list(fedex_response.keys()) if isinstance(fedex_response, dict) else 'not a dict'}")
            if "output" in fedex_response:
                rate_details = fedex_response["output"].get("rateReplyDetails", [])
                # Log de la primera respuesta para verificar moneda
                if rate_details:
                    first_rate = rate_details[0]
                    first_shipment = first_rate.get("ratedShipmentDetails", [{}])[0]
                    logger.info(f"\U0001f4b0 FedEx primer servicio: {first_rate.get('serviceType')} - charge={first_shipment.get('totalNetCharge')} currency={first_shipment.get('currency', 'NOT_SET')}")
                if rate_details:
                    all_services = []
                    for rate in rate_details:
                        rated_shipment = rate.get("ratedShipmentDetails", [{}])[0]
                        total_charge = rated_shipment.get("totalNetCharge", 0)
                        response_currency = rated_shipment.get("currency", "USD")
                        service_type = rate.get("serviceType", "")
                        service_name = rate.get("serviceName", service_type)
                        # Intentar múltiples paths para transit days según versión de FedEx API
                        commit_obj = rate.get("commit", {})
                        logger.info(f"🔍 FedEx commit object for {service_type}: {commit_obj}")
                        transit_days = "N/A"
                        if commit_obj:
                            # Path 1: commit.dateDetail.dayCount (FedEx Rate API v1)
                            date_detail = commit_obj.get("dateDetail", {})
                            if date_detail and date_detail.get("dayCount"):
                                transit_days = str(date_detail.get("dayCount"))
                            # Path 2: commit.transitDays (string directo)
                            elif commit_obj.get("transitDays") and not isinstance(commit_obj.get("transitDays"), dict):
                                transit_days = str(commit_obj.get("transitDays"))
                            # Path 3: operationalDetail.transitDays
                            elif rate.get("operationalDetail", {}).get("transitDays"):
                                transit_days = str(rate["operationalDetail"]["transitDays"])

                        charge_float = round(float(total_charge), 2)

                        # Si FedEx devuelve en COP a pesar del preferredCurrency, convertir a USD
                        if response_currency == "COP":
                            cop_to_usd_rate = 4200  # 1 USD ~ 4200 COP
                            charge_usd = round(charge_float / cop_to_usd_rate, 2)
                            logger.info(f"\U0001f4b1 Moneda FedEx: COP - Convirtiendo ${charge_float:,.0f} COP -> ${charge_usd:.2f} USD (tasa {cop_to_usd_rate})")
                            charge_float = charge_usd
                        elif response_currency != "USD":
                            logger.warning(f"\u26a0\ufe0f Moneda inesperada de FedEx: {response_currency} (valor: {charge_float})")

                        all_services.append({
                            "service_type": service_type,
                            "service_name": service_name,
                            "total_charge": charge_float,
                            "transit_days": str(transit_days),
                            "original_currency": response_currency
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
                        result["details"] = f"Opción más económica ({cheapest['transit_days']} días)"

                        logger.info(f"📊 Servicios FedEx disponibles ({len(all_services)}):")
                        for svc in all_services:
                            logger.info(f"   - {svc['service_name']}: ${svc['total_charge']} ({svc['transit_days']} días)")

                        return result

            error_msg = fedex_response.get("error", "")
            error_details = fedex_response.get("details", "")
            if error_msg:
                result["success"] = False
                result["details"] = f"Error en el sistema de cotización. Por favor intente de nuevo."
                return result

            result["success"] = False
            result["details"] = "No se pudo obtener cotización en este momento. Por favor contacte a soporte."

        except Exception as e:
            result["success"] = False
            result["details"] = f"Error al consultar el sistema de cotización: {str(e)}"

        return result


# ══════════════════════════════════════════════════════════════════════════════
# PROCESADOR DE RASTREO
# ══════════════════════════════════════════════════════════════════════════════

class TrackingProcessor:
    """Procesa información de rastreo de envíos"""

    def __init__(self):
        self.fedex = FedExClient(api_key=FEDEX_TRACK_API_KEY, secret_key=FEDEX_TRACK_SECRET_KEY)

    async def track(self, tracking_number: str) -> Dict:
        """Obtiene y procesa información de rastreo"""

        # Validar formato
        clean_number = tracking_number.strip()
        if not clean_number or len(clean_number) < 9:
            return {"success": False, "error": "Número de rastreo inválido"}

        logger.info(f"🔍 Rastreando envío: {clean_number}")

        # Llamar a FedEx Track API
        fedex_response = await self.fedex.track_shipment(clean_number)

        if "error" in fedex_response:
            return {"success": False, "error": fedex_response["error"]}

        # Parsear respuesta
        try:
            track_results = fedex_response.get("output", {}).get("completeTrackResults", [])
            if not track_results:
                return {"success": False, "error": "Número de rastreo no encontrado en el sistema"}

            track_detail = track_results[0].get("trackResults", [{}])[0]

            # Verificar si hay error en el tracking
            if track_detail.get("error"):
                error_msg = track_detail["error"].get("message", "Guía no encontrada")
                logger.warning(f"⚠️ FedEx track error para {clean_number}: {error_msg}")
                return {"success": False, "error": "Número de rastreo no encontrado. Verifica que sea correcto."}

            # Estado principal
            status_detail = track_detail.get("latestStatusDetail", {})
            status_code = status_detail.get("code", "")
            status_description = status_detail.get("description", "")

            sonia_status = get_short_status(status_code, status_description)
            logger.info(f"📊 Estado: {sonia_status} (código: {status_code}, desc: {status_description})")

            # Extraer últimos 3 scan events
            scan_events = track_detail.get("scanEvents", [])
            last_events = []

            for event in scan_events[:3]:
                event_date_raw = event.get("date", "")
                event_desc = event.get("eventDescription", "")
                event_city = event.get("scanLocation", {}).get("city", "")
                event_country = event.get("scanLocation", {}).get("countryCode", "")

                # Formatear fecha ISO8601
                formatted_date = ""
                if event_date_raw:
                    try:
                        dt = datetime.fromisoformat(event_date_raw.replace("Z", "+00:00"))
                        formatted_date = dt.strftime("%d/%m/%Y %H:%M")
                    except Exception:
                        formatted_date = event_date_raw[:16]

                location = f" ({event_city}, {event_country})" if event_city else ""
                last_events.append({
                    "date": formatted_date,
                    "description": f"{event_desc}{location}"
                })

            return {
                "success": True,
                "tracking_number": clean_number,
                "sonia_status": sonia_status,
                "carrier_status": status_description,
                "last_events": last_events
            }

        except Exception as e:
            logger.error(f"❌ Error procesando respuesta de rastreo: {e}")
            return {"success": False, "error": f"Error procesando información: {str(e)}"}




class OdooClient:
    """Cliente para interactuar con Odoo via XML-RPC"""

    def __init__(self):
        self.url = ODOO_URL
        self.db = ODOO_DB
        self.username = ODOO_USER
        self.api_key = ODOO_API_KEY
        self.uid = None
        self.models = None

    def _connect(self):
        """Establece conexión con Odoo"""
        if self.uid and self.models:
            return True
        try:
            if not self.api_key:
                logger.warning("⚠️ ODOO_API_KEY no configurada - integración Odoo deshabilitada")
                return False
            common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
            self.uid = common.authenticate(self.db, self.username, self.api_key, {})
            if not self.uid:
                logger.error("❌ Autenticación Odoo fallida")
                return False
            self.models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")
            logger.info(f"✅ Conectado a Odoo como uid={self.uid}")
            return True
        except Exception as e:
            logger.error(f"❌ Error conectando a Odoo: {e}")
            return False

    def _execute(self, model, method, *args, **kwargs):
        """Ejecuta operación en Odoo"""
        if not self._connect():
            return None
        try:
            return self.models.execute_kw(
                self.db, self.uid, self.api_key,
                model, method, *args, **kwargs
            )
        except Exception as e:
            logger.error(f"❌ Error Odoo {model}.{method}: {e}")
            return None

    def create_ticket(self, name: str, description: str, phone: str = None, team_id: int = None) -> Dict:
        """Crea un ticket de soporte en Odoo Helpdesk"""
        try:
            partner_id = None
            if phone:
                partner_id = self._find_or_create_partner(phone)

            ticket_data = {
                'name': name,
                'description': description,
                'team_id': team_id or ODOO_HELPDESK_TEAM_ID,
            }
            if partner_id:
                ticket_data['partner_id'] = partner_id

            ticket_id = self._execute('helpdesk.ticket', 'create', [ticket_data])
            if ticket_id:
                ticket_info = self._execute('helpdesk.ticket', 'read', [ticket_id], {'fields': ['id', 'name', 'stage_id']})
                stage = ticket_info[0]['stage_id'][1] if ticket_info and ticket_info[0].get('stage_id') else 'Nuevo'
                logger.info(f"🎫 Ticket creado en Odoo: ID={ticket_id}")
                return {"success": True, "ticket_id": ticket_id, "stage": stage}
            return {"success": False, "error": "No se pudo crear el ticket"}
        except Exception as e:
            logger.error(f"❌ Error creando ticket Odoo: {e}")
            return {"success": False, "error": str(e)}

    def search_contacts(self, query: str) -> Dict:
        """Busca contactos en Odoo"""
        try:
            domain = ['|', '|',
                ['name', 'ilike', query],
                ['email', 'ilike', query],
                ['phone', 'ilike', query]
            ]
            contacts = self._execute('res.partner', 'search_read',
                [domain],
                {'fields': ['name', 'email', 'phone', 'mobile', 'function', 'city', 'is_company'], 'limit': 5}
            )
            if contacts:
                logger.info(f"🔍 Encontrados {len(contacts)} contactos para '{query}'")
                return {"success": True, "contacts": contacts}
            return {"success": True, "contacts": []}
        except Exception as e:
            logger.error(f"❌ Error buscando contactos: {e}")
            return {"success": False, "error": str(e)}

    def _find_or_create_partner(self, phone: str) -> int:
        """Busca un contacto por teléfono, o crea uno nuevo"""
        try:
            clean_phone = phone.strip().replace(" ", "")
            if not clean_phone.startswith("+"):
                clean_phone = "+" + clean_phone

            partners = self._execute('res.partner', 'search',
                [['|', ['phone', 'like', clean_phone[-10:]], ['mobile', 'like', clean_phone[-10:]]]],
                {'limit': 1}
            )
            if partners:
                return partners[0]

            new_partner = self._execute('res.partner', 'create', [{
                'name': f'WhatsApp {clean_phone}',
                'phone': clean_phone,
                'comment': 'Contacto creado automáticamente por SonIA WhatsApp Agent'
            }])
            if new_partner:
                logger.info(f"👤 Nuevo contacto creado en Odoo: ID={new_partner}")
            return new_partner
        except Exception as e:
            logger.error(f"❌ Error buscando/creando partner: {e}")
            return None


    def read_spreadsheet_users(self) -> List[Dict]:
        """Lee todos los usuarios de la hoja WHATSAPP BBDD"""
        try:
            result = self._execute('documents.document', 'read',
                                   [[ODOO_SPREADSHEET_ID], ['spreadsheet_snapshot']])
            if not result:
                return []
            snapshot_b64 = result[0].get('spreadsheet_snapshot')
            if not snapshot_b64:
                return []
            snapshot_json = base64.b64decode(snapshot_b64).decode('utf-8')
            snapshot = json.loads(snapshot_json)
            cells = snapshot.get('sheets', [{}])[0].get('cells', {})
            users = []
            row = 2
            while row < 500:
                a = cells.get(f"A{row}", "")
                b = cells.get(f"B{row}", "")
                d = cells.get(f"D{row}", "")
                if not a and not b and not d:
                    break
                users.append({
                    'cliente': a, 'nombre': b,
                    'nickname': cells.get(f"C{row}", ""),
                    'whatsapp': d,
                    'rol': cells.get(f"E{row}", ""),
                    'clave': cells.get(f"F{row}", ""),
                    'bloqueo': cells.get(f"G{row}", ""),
                    'row': row
                })
                row += 1
            return users
        except Exception as e:
            logger.error(f"❌ Error leyendo spreadsheet usuarios: {e}")
            return []

    def find_user_by_phone(self, phone: str) -> Optional[Dict]:
        """Busca usuario por número de WhatsApp en la hoja"""
        users = self.read_spreadsheet_users()
        clean = phone.strip().replace("+", "").replace(" ", "")
        for u in users:
            up = u['whatsapp'].strip().replace("+", "").replace(" ", "")
            if not up:
                continue
            if up == clean or clean[-10:] == up[-10:]:
                return u
        return None

    def _get_spreadsheet_rev_id(self) -> Optional[str]:
        """Obtiene el revisionId actual del spreadsheet"""
        try:
            result = self._execute('documents.document', 'read',
                                   [[ODOO_SPREADSHEET_ID], ['spreadsheet_snapshot']])
            if result:
                snapshot = json.loads(base64.b64decode(result[0]['spreadsheet_snapshot']).decode('utf-8'))
                return snapshot.get('revisionId')
        except Exception:
            pass
        return None

    def _dispatch_spreadsheet_cmd(self, commands: list) -> bool:
        """Envía comandos al spreadsheet de Odoo"""
        try:
            rev_id = self._get_spreadsheet_rev_id()
            if not rev_id:
                return False
            message = {
                "type": "REMOTE_REVISION",
                "nextRevisionId": str(uuid.uuid4()),
                "serverRevisionId": rev_id,
                "commands": commands,
                "clientId": "sonia-bot"
            }
            result = self._execute('documents.document', 'dispatch_spreadsheet_message',
                                   [[ODOO_SPREADSHEET_ID], message])
            return bool(result)
        except Exception as e:
            logger.error(f"❌ Error dispatch spreadsheet: {e}")
            return False

    def _get_next_spreadsheet_row(self) -> int:
        """Obtiene la siguiente fila disponible"""
        users = self.read_spreadsheet_users()
        if not users:
            return 2
        return max(u['row'] for u in users) + 1

    def add_user_to_spreadsheet(self, cliente: str, nombre: str, nickname: str,
                                 whatsapp: str, rol: str = "cliente") -> int:
        """Agrega usuario a la hoja WHATSAPP BBDD. Retorna el row number o 0 si falla."""
        try:
            row = self._get_next_spreadsheet_row()
            commands = []
            data = [(0, cliente), (1, nombre), (2, nickname), (3, whatsapp), (4, rol)]
            for col, val in data:
                if val:
                    commands.append({
                        "type": "UPDATE_CELL", "sheetId": "sheet1",
                        "col": col, "row": row - 1, "content": str(val)
                    })
            if commands and self._dispatch_spreadsheet_cmd(commands):
                logger.info(f"📝 Usuario agregado al spreadsheet fila {row}: {nombre}")
                return row
            return 0
        except Exception as e:
            logger.error(f"❌ Error agregando usuario al spreadsheet: {e}")
            return 0

    def update_spreadsheet_cell(self, row: int, col: int, value: str) -> bool:
        """Actualiza una celda específica del spreadsheet"""
        return self._dispatch_spreadsheet_cmd([{
            "type": "UPDATE_CELL", "sheetId": "sheet1",
            "col": col, "row": row - 1, "content": str(value)
        }])

    def get_user_clave(self, phone: str) -> str:
        """Obtiene la clave actual de un usuario desde el spreadsheet (lectura fresca)"""
        user = self.find_user_by_phone(phone)
        return user.get('clave', '') if user else ''

# ══════════════════════════════════════════════════════════════════════════════
# APLICACIÓN FASTAPI

# ══════════════════════════════════════════════════════════════════════════════
# CACHÉ DE USUARIOS WHATSAPP
# ══════════════════════════════════════════════════════════════════════════════

class UserCache:
    """Caché en memoria para usuarios de WhatsApp"""

    def __init__(self):
        self.users = {}
        self.employee_daily_checks = {}
        self.pending_key = set()
        self.last_odoo_check = {}

    def get(self, phone: str) -> Optional[Dict]:
        clean = phone.strip().replace("+", "")
        for k, v in self.users.items():
            if k == clean or k.endswith(clean[-10:]) or clean.endswith(k[-10:]):
                return v
        return None

    def set(self, phone: str, data: Dict):
        clean = phone.strip().replace("+", "")
        self.users[clean] = data

    def remove(self, phone: str):
        """Elimina usuario del caché"""
        clean = phone.strip().replace("+", "")
        keys_to_remove = [k for k in self.users if k == clean or k.endswith(clean[-10:]) or clean.endswith(k[-10:])]
        for k in keys_to_remove:
            del self.users[k]
        self.last_odoo_check.pop(clean, None)

    def needs_odoo_recheck(self, phone: str, interval_minutes: int = 15) -> bool:
        """Verifica si necesita re-consultar Odoo (cada interval_minutes)"""
        clean = phone.strip().replace("+", "")
        last = self.last_odoo_check.get(clean)
        if not last:
            return True
        return (datetime.now() - last).total_seconds() > interval_minutes * 60

    def mark_odoo_checked(self, phone: str):
        """Marca que se verificó contra Odoo"""
        clean = phone.strip().replace("+", "")
        self.last_odoo_check[clean] = datetime.now()


    def is_employee_validated_today(self, phone: str) -> bool:
        clean = phone.strip().replace("+", "")
        last = self.employee_daily_checks.get(clean)
        return last == datetime.now().strftime("%Y-%m-%d")

    def mark_employee_validated(self, phone: str):
        clean = phone.strip().replace("+", "")
        self.employee_daily_checks[clean] = datetime.now().strftime("%Y-%m-%d")

    def set_pending_key(self, phone: str):
        clean = phone.strip().replace("+", "")
        self.pending_key.add(clean)

    def is_pending_key(self, phone: str) -> bool:
        clean = phone.strip().replace("+", "")
        return clean in self.pending_key

    def clear_pending_key(self, phone: str):
        clean = phone.strip().replace("+", "")
        self.pending_key.discard(clean)

user_cache = UserCache()

def get_user_from_db(phone: str) -> Optional[Dict]:
    """Busca usuario en SQLite local"""
    conn = sqlite3.connect("sonia_conversations.db")
    cursor = conn.cursor()
    clean = phone.strip().replace("+", "")
    cursor.execute("SELECT phone, cliente, nombre, nickname, rol, spreadsheet_row, bloqueo FROM whatsapp_users WHERE phone = ?", (clean,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {'whatsapp': row[0], 'cliente': row[1], 'nombre': row[2], 'nickname': row[3], 'rol': row[4], 'clave': '', 'row': row[5], 'bloqueo': row[6] if len(row) > 6 else ''}
    return None

def save_user_to_db(phone: str, cliente: str, nombre: str, nickname: str, rol: str = "cliente", spreadsheet_row: int = 0, bloqueo: str = ""):
    """Guarda usuario en SQLite local"""
    conn = sqlite3.connect("sonia_conversations.db")
    cursor = conn.cursor()
    clean = phone.strip().replace("+", "")
    cursor.execute("""
        INSERT OR REPLACE INTO whatsapp_users (phone, cliente, nombre, nickname, rol, spreadsheet_row, bloqueo, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (clean, cliente, nombre, nickname, rol, spreadsheet_row, bloqueo))
    conn.commit()
    conn.close()

def delete_user_from_db(phone: str):
    """Elimina usuario de SQLite local"""
    conn = sqlite3.connect("sonia_conversations.db")
    cursor = conn.cursor()
    clean = phone.strip().replace("+", "")
    cursor.execute("DELETE FROM whatsapp_users WHERE phone = ?", (clean,))
    conn.commit()
    conn.close()
    logger.info(f"🗑️ Usuario {clean} eliminado de SQLite")

def get_display_name(user_data: Dict) -> str:
    """Obtiene el nombre para mostrar: nickname > primer nombre"""
    if user_data.get('nickname'):
        return user_data['nickname']
    nombre = user_data.get('nombre', '')
    if nombre:
        return nombre.split()[0]
    return "amigo/a"

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

        # Migración: agregar columna bloqueo si no existe
    try:
        cursor.execute("ALTER TABLE whatsapp_users ADD COLUMN bloqueo TEXT DEFAULT ''")
        conn.commit()
        logger.info("📋 Columna 'bloqueo' agregada a whatsapp_users")
    except Exception:
        pass  # La columna ya existe

    logger.info("✅ Base de datos inicializada")
    logger.info("✅ SonIA WhatsApp Agent LISTO y escuchando")
    logger.info("=" * 60)
    yield
    # Shutdown
    logger.info("👋 SonIA WhatsApp Agent detenido")


app = FastAPI(
    title="SonIA - WhatsApp Quotation Agent",
    description="Agente de WhatsApp para cotizaciones de envío - BloomsPal",
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
        "company": "BloomsPal"
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

        
        # ===== IDENTIFICACIÓN DE USUARIO =====
        user_data = user_cache.get(from_number)

        # Si no está en caché, buscar en SQLite
        if not user_data:
            user_data = get_user_from_db(from_number)
            if user_data:
                user_cache.set(from_number, user_data)

        # Si no está en SQLite, buscar en Odoo spreadsheet
        if not user_data:
            try:
                odoo_lookup = OdooClient()
                user_data = odoo_lookup.find_user_by_phone(from_number)
                if user_data:
                    user_cache.set(from_number, user_data)
                    save_user_to_db(from_number, user_data.get('cliente', ''), user_data.get('nombre', ''),
                                    user_data.get('nickname', ''), user_data.get('rol', 'cliente'),
                                    user_data.get('row', 0), user_data.get('bloqueo', ''))
            except Exception as e:
                logger.warning(f"⚠️ Error buscando usuario en Odoo: {e}")

                # ===== RE-VERIFICACIÓN CONTRA ODOO (cada 15 min) =====
        if user_data and user_cache.needs_odoo_recheck(from_number):
            try:
                odoo_verify = OdooClient()
                odoo_user = odoo_verify.find_user_by_phone(from_number)
                if not odoo_user:
                    logger.info(f"🔄 Usuario {from_number} eliminado de Odoo, limpiando caché y SQLite")
                    user_cache.remove(from_number)
                    delete_user_from_db(from_number)
                    user_data = None
                else:
                    user_cache.set(from_number, odoo_user)
                    user_data = odoo_user
                    save_user_to_db(from_number, odoo_user.get('cliente', ''), odoo_user.get('nombre', ''),
                                    odoo_user.get('nickname', ''), odoo_user.get('rol', 'cliente'),
                                    odoo_user.get('row', 0), odoo_user.get('bloqueo', ''))
                user_cache.mark_odoo_checked(from_number)
            except Exception as e:
                logger.warning(f"⚠️ Error re-verificando usuario en Odoo: {e}")

# ===== BLOQUEO DE USUARIO =====
        if user_data and user_data.get('bloqueo', '').strip().upper() == 'SI':
            logger.info(f"⛔ Mensaje ignorado de usuario bloqueado: {from_number}")
            return JSONResponse(content={"status": "blocked"}, status_code=200)

        # Manejar estado pending_key (empleado debe dar clave)
        if user_cache.is_pending_key(from_number):
            user_cache.clear_pending_key(from_number)
            if user_data:
                try:
                    odoo_key = OdooClient()
                    fresh_clave = odoo_key.get_user_clave(from_number)
                except Exception:
                    fresh_clave = ""
                if fresh_clave:
                    if user_text.strip() == fresh_clave.strip():
                        user_cache.mark_employee_validated(from_number)
                        dn = get_display_name(user_data)
                        response_message = f"✅ Clave verificada. ¡Bienvenido/a {dn}! ¿En qué puedo ayudarte hoy?"
                    else:
                        response_message = "❌ Clave incorrecta. Intenta de nuevo escribiendo tu clave."
                        user_cache.set_pending_key(from_number)
                else:
                    response_message = "⏳ Tu clave aún no ha sido configurada por el administrador. Puedes seguir usando el servicio mientras tanto. ¿En qué puedo ayudarte?"
            else:
                response_message = "No pude verificar tu información. ¿Podrías intentar de nuevo?"
            save_message(conversation_id, "user", user_text, message_type)
            save_message(conversation_id, "assistant", response_message)
            await whatsapp.send_message(from_number, response_message)
            return {"status": "key_validation"}

        # Verificación diaria de empleados
        if user_data and user_data.get('rol', '').lower() == 'empleado':
            if not user_cache.is_employee_validated_today(from_number):
                try:
                    odoo_check = OdooClient()
                    fresh_clave = odoo_check.get_user_clave(from_number)
                except Exception:
                    fresh_clave = ""
                if fresh_clave:
                    user_cache.set_pending_key(from_number)
                    dn = get_display_name(user_data)
                    save_message(conversation_id, "user", user_text, message_type)
                    response_message = f"¡Hola {dn}! 👋 Para continuar hoy necesito verificar tu identidad. Por favor escribe tu clave de acceso:"
                    save_message(conversation_id, "assistant", response_message)
                    await whatsapp.send_message(from_number, response_message)
                    return {"status": "key_required"}

        # Construir contexto de usuario para Claude
        user_context = ""
        if user_data:
            dn = get_display_name(user_data)
            rol = user_data.get('rol', 'cliente').lower()
            if rol == 'empleado':
                if user_cache.is_employee_validated_today(from_number):
                    user_context = f"CONTEXTO USUARIO: Empleado VERIFICADO de BloomsPal. Nombre: {user_data['nombre']}. Llámalo '{dn}'. Empresa: BloomsPal. NOTA: Ya tienes sus datos, NO le pidas nombre ni empresa."
                else:
                    user_context = f"CONTEXTO USUARIO: Empleado de BloomsPal (sin clave configurada aún). Nombre: {user_data['nombre']}. Llámalo '{dn}'. Empresa: BloomsPal. NOTA: Ya tienes sus datos, NO le pidas nombre ni empresa."
            else:
                user_context = f"CONTEXTO USUARIO: Cliente registrado. Nombre: {user_data['nombre']}. Llámalo '{dn}'. Empresa: {user_data.get('cliente', 'No especificada')}. NOTA: Ya tienes nombre y empresa, NO los vuelvas a preguntar."
        else:
            user_context = f"CONTEXTO USUARIO: Usuario NUEVO, no registrado. Su número de WhatsApp es {from_number}. IMPORTANTE: NO proceses cotizaciones, tracking, tickets ni ninguna otra función hasta que el usuario se registre. Tu ÚNICA tarea ahora es recopilar su información (nombre completo, empresa, y opcionalmente un nickname/apodo). Cuando tengas los datos, usa action 'register_user'."

# Procesar con Claude
        logger.info("🤖 Procesando con Claude AI...")
        response = await processor.process_text(user_text, history, user_context)

        action = response.get("action", "chat")
        response_message = response.get("message", "")
        logger.info(f"🤖 Claude action={action}, mensaje={response_message[:80]}...")

        # ===== GATE: Usuario no registrado no puede usar funciones =====
        if not user_data and action not in ("register_user", "chat"):
            logger.info(f"🚫 Acción '{action}' bloqueada - usuario no registrado")
            response_message = "Antes de poder ayudarte con eso, necesito conocerte un poco. ¿Podrías decirme tu nombre completo y a qué empresa perteneces?"
            action = "chat"

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

                response_message = f"""✅ *COTIZACIÓN BloomsPal*

📤 *Origen:* {origin_info} (CP {quote_data.get('origin_postal', '')})
📍 *Destino:* {dest_info} (CP {quote_data.get('destination_postal', '')})
📦 *Peso total:* {quote_data.get('weight_kg', 0)} kg
{'🎁 *Paletizado:* Sí' if quote_data.get('is_pallet') else f'📦 *Paquetes:* {num_pkgs}'}
💎 *Valor declarado:* ${declared_val:,.2f} USD
🚛 *Recogida:* En dirección de origen
📆 *Fecha de salida:* {quote_data.get('shipping_date', 'Por confirmar')}

💰 *PRECIO MÁS ECONÓMICO: ${quote_result['amount']:.2f} USD*
🏷️ *Servicio:* {'BloomsPal'}
📅 *Tiempo estimado:* {quote_result.get('transit_days', 'N/A')} días
⚖️ *Costo por kilo:* ${quote_result['amount'] / quote_data.get('weight_kg', 1):.2f} USD/kg

📝 {quote_result['details']}"""

                # Agregar otros servicios disponibles si hay más de uno
                all_services = quote_result.get("all_services", [])
                if len(all_services) > 1:
                    response_message += "\n\n📋 *Otras opciones disponibles:*"
                    for i, svc in enumerate(all_services[1:4], 2):
                        svc_cost_per_kg = svc['total_charge'] / (quote_data.get('weight_kg', 1) or 1)
                        response_message += f"\n  • Opción {i}: ${svc['total_charge']:.2f} USD | {svc['transit_days']} días | ${svc_cost_per_kg:.2f} USD/kg"

                response_message += "\n\n¿Deseas proceder con este envío? Responde *SÍ* para confirmar o escríbeme si necesitas otra cotización."

                # Guardar cotización
                quote_data["quote_amount"] = quote_result["amount"]
                quote_data["fedex_account_used"] = quote_result["fedex_account_used"]
                save_quotation(conversation_id, from_number, quote_data)
            else:
                response_message = f"❌ {quote_result['details']}\n\nPor favor verifica la información e intenta de nuevo."

        # Si es una solicitud de rastreo
        elif action == "track":
            tracking_number = response.get("tracking_number", "")
            logger.info(f"📦 Procesando rastreo para: {tracking_number}")

            tracker = TrackingProcessor()
            track_result = await tracker.track(tracking_number)

            if track_result["success"]:
                sonia_status = track_result["sonia_status"]
                carrier_status = track_result["carrier_status"]
                last_events = track_result["last_events"]

                response_message = f"""📦 *RASTREO BloomsPal*

🔍 *Guía:* {track_result['tracking_number']}
📊 *SonIA Status:* {sonia_status}
📝 *Estado:* {carrier_status}

📋 *Últimas actualizaciones:*"""

                if last_events:
                    for i, event in enumerate(last_events, 1):
                        response_message += f"\n{i}. {event['date']} - {event['description']}"
                else:
                    response_message += "\nNo hay actualizaciones disponibles aún."

                response_message += "\n\n¿Necesitas algo más? Puedo ayudarte con otra guía o una cotización."
            else:
                error_msg = track_result.get("error", "Error desconocido")
                response_message = f"❌ {error_msg}\n\nPor favor verifica el número de rastreo e intenta de nuevo, o escríbeme si necesitas ayuda."


        # Si es una solicitud de soporte
        elif action == "support":
            support_data = response.get("data", {})
            subject = support_data.get("subject", "Solicitud de soporte via WhatsApp")
            description = support_data.get("description", response_message)
            # Usar datos del JSON de Claude, con fallback a user_data registrado
            company_name = support_data.get("company_name", "") or (user_data.get('cliente', '') if user_data else "")
            contact_name = support_data.get("contact_name", "") or (user_data.get('nombre', '') if user_data else "")

            # Enriquecer descripción con info de contacto
            full_description = "TICKET DE SOPORTE VIA WHATSAPP\n\n"
            if company_name:
                full_description += f"Compañía: {company_name}\n"
            if contact_name:
                full_description += f"Contacto: {contact_name}\n"
            full_description += f"Teléfono WhatsApp: {from_number}\n\n"
            full_description += f"Descripción del problema:\n{description}"

            logger.info(f"🎫 Creando ticket de soporte: {subject}")

            odoo = OdooClient()
            ticket_result = odoo.create_ticket(subject, full_description, from_number)

            if ticket_result["success"]:
                ticket_id = ticket_result["ticket_id"]
                response_message = f"""🎫 *CASO DE SOPORTE CREADO*

✅ *Ticket #:* {ticket_id}
📋 *Asunto:* {subject}
🏢 *Compañía:* {company_name}
👤 *Contacto:* {contact_name}
📊 *Estado:* {ticket_result.get('stage', 'Nuevo')}

Nuestro equipo de atención al cliente revisará tu caso y te contactará pronto.

¿Necesitas algo más?"""
            else:
                response_message = f"❌ No pude crear el caso de soporte en este momento. Por favor intenta de nuevo o escribe a customer-care@bloomspal.odoo.com\n\nError: {ticket_result.get('error', 'desconocido')}"

        
        # Si es una confirmación de orden / oportunidad de venta
        elif action == "order":
            order_data = response.get("data", {})
            # Usar datos del JSON de Claude, con fallback a user_data registrado
            company_name = order_data.get("company_name", "") or (user_data.get('cliente', '') if user_data else "")
            contact_name = order_data.get("contact_name", "") or (user_data.get('nombre', '') if user_data else "")
            quote_summary = order_data.get("quote_summary", "")

            # Construir descripción con todos los detalles
            description = "OPORTUNIDAD DE VENTA VIA WHATSAPP\n\n"
            description += f"Compañía: {company_name}\n"
            description += f"Contacto: {contact_name}\n"
            description += f"Teléfono WhatsApp: {from_number}\n\n"
            description += f"DETALLES DE COTIZACIÓN:\n{quote_summary}\n"

            subject = f"Orden WhatsApp - {company_name}" if company_name else "Orden via WhatsApp"

            logger.info(f"📋 Creando oportunidad de venta: {subject}")

            odoo = OdooClient()
            ticket_result = odoo.create_ticket(subject, description, from_number, team_id=ODOO_SALES_TEAM_ID)

            if ticket_result["success"]:
                ticket_id = ticket_result["ticket_id"]
                response_message = f"""📋 *ORDEN DE ENVÍO REGISTRADA*

✅ *Ticket #:* {ticket_id}
🏢 *Compañía:* {company_name}
👤 *Contacto:* {contact_name}
📊 *Estado:* {ticket_result.get('stage', 'Nuevo')}

Nuestro equipo de ventas revisará los detalles y te contactará para confirmar tu orden.

¿Necesitas algo más?"""
            else:
                response_message = f"❌ No pudimos registrar tu orden. Por favor intenta de nuevo o escribe a customer-care@bloomspal.odoo.com\n\nError: {ticket_result.get('error', 'desconocido')}"
# Si es una solicitud de contacto

        # Si es un registro de nuevo usuario
        elif action == "register_user":
            reg_data = response.get("data", {})
            nombre = reg_data.get("nombre", "")
            cliente = reg_data.get("cliente", "")
            nickname = reg_data.get("nickname", "")

            try:
                odoo_reg = OdooClient()
                ss_row = odoo_reg.add_user_to_spreadsheet(
                    cliente=cliente, nombre=nombre, nickname=nickname,
                    whatsapp=from_number, rol="cliente"
                )
            except Exception as e:
                logger.error(f"❌ Error escribiendo en spreadsheet: {e}")
                ss_row = 0

            save_user_to_db(from_number, cliente, nombre, nickname, "cliente", ss_row)
            new_user = {'cliente': cliente, 'nombre': nombre, 'nickname': nickname,
                        'whatsapp': from_number, 'rol': 'cliente', 'clave': '', 'row': ss_row}
            user_cache.set(from_number, new_user)
            logger.info(f"👤 Usuario registrado: {nombre} ({cliente}) - {from_number}")

        # Si alguien dice que es empleado
        elif action == "update_nickname":
            new_nick = response.get("data", {}).get("nickname", "")
            if user_data and new_nick:
                try:
                    odoo_nick = OdooClient()
                    row_to_update = user_data.get('row', 0)
                    if row_to_update == 0:
                        found = odoo_nick.find_user_by_phone(from_number)
                        if found:
                            row_to_update = found.get('row', 0)
                    if row_to_update > 0:
                        odoo_nick.update_spreadsheet_cell(row_to_update, 2, new_nick)
                        logger.info(f"✏️ Nickname actualizado en spreadsheet fila {row_to_update}: {new_nick}")
                except Exception as e:
                    logger.error(f"❌ Error actualizando nickname en spreadsheet: {e}")
                user_data['nickname'] = new_nick
                user_cache.set(from_number, user_data)
                save_user_to_db(from_number, user_data.get('cliente', ''), user_data.get('nombre', ''),
                                new_nick, user_data.get('rol', 'cliente'), user_data.get('row', 0))
                logger.info(f"✏️ Nickname actualizado: {new_nick} para {from_number}")

        elif action == "claim_employee":
            if user_data:
                try:
                    odoo_emp = OdooClient()
                    row_to_update = user_data.get('row', 0)
                    logger.info(f"👔 claim_employee: row={row_to_update}, phone={from_number}")
                    if row_to_update == 0:
                        found = odoo_emp.find_user_by_phone(from_number)
                        if found:
                            row_to_update = found.get('row', 0)
                            user_data['row'] = row_to_update
                            logger.info(f"👔 Row encontrado via lookup: {row_to_update}")
                    if row_to_update > 0:
                        success = odoo_emp.update_spreadsheet_cell(row_to_update, 4, "empleado")
                        logger.info(f"📝 Spreadsheet ROL update fila {row_to_update}: {'OK' if success else 'FAIL'}")
                    else:
                        logger.warning(f"⚠️ No se pudo encontrar fila en spreadsheet para {from_number}")
                except Exception as e:
                    logger.error(f"❌ Error actualizando rol en spreadsheet: {e}")
                user_data['rol'] = 'empleado'
                user_cache.set(from_number, user_data)
                save_user_to_db(from_number, user_data.get('cliente', ''), user_data.get('nombre', ''),
                                user_data.get('nickname', ''), 'empleado', user_data.get('row', 0))
                dn = get_display_name(user_data)
                response_message = f"✅ Te he registrado como empleado de BloomsPal, {dn}. Tu administrador te asignará una clave de acceso. Cuando la tengas, podrás verificarte cada día para acceder a funciones de empleado.\n\n¿Necesitas algo más?"
                logger.info(f"👔 Usuario marcado como empleado: {user_data.get('nombre', '')} - {from_number}")
            else:
                response_message = "Primero necesito registrarte. ¿Cuál es tu nombre completo y a qué empresa perteneces?"

        elif action == "contact":
            contact_data = response.get("data", {})
            query = contact_data.get("query", "")
            logger.info(f"🔍 Buscando contacto: {query}")

            odoo = OdooClient()
            contact_result = odoo.search_contacts(query)

            if contact_result["success"] and contact_result["contacts"]:
                contacts = contact_result["contacts"]
                response_message = f"📇 *CONTACTOS ENCONTRADOS* ({len(contacts)}):\n"
                for c in contacts:
                    name = c.get('name', 'N/A')
                    function = c.get('function', '')
                    email = c.get('email', '')
                    phone = c.get('phone', '') or c.get('mobile', '')
                    city = c.get('city', '')

                    response_message += f"\n👤 *{name}*"
                    if function:
                        response_message += f"\n   🏢 {function}"
                    if email:
                        response_message += f"\n   📧 {email}"
                    if phone:
                        response_message += f"\n   📲 {phone}"
                    if city:
                        response_message += f"\n   📍 {city}"
                    response_message += "\n"

                response_message += "\n¿Necesitas algo más?"
            elif contact_result["success"]:
                response_message = f"No encontré contactos que coincidan con '{query}'. ¿Puedes darme más detalles de a quién buscas?"
            else:
                response_message = f"❌ No pude buscar contactos en este momento. Por favor intenta de nuevo más tarde."

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
# API ENDPOINTS (para SonIA Core)
# ══════════════════════════════════════════════════════════════════════════════

SONIA_CORE_API_KEY = os.getenv("SONIA_CORE_API_KEY", "sonia-core-2026")

class SendMessageRequest(BaseModel):
    phone_number: str
    message: str

class SendReportRequest(BaseModel):
    phone_number: str
    report: str
    client_name: str = ""


@app.post("/api/send-message")
async def api_send_message(req: SendMessageRequest, request: Request):
    """Endpoint para que SonIA Core envíe mensajes directos por WhatsApp."""
    # Verificar API key
    api_key = request.headers.get("X-API-Key", "")
    if api_key != SONIA_CORE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    try:
        result = whatsapp.send_message(req.phone_number, req.message)
        return {
            "status": "sent",
            "phone_number": req.phone_number,
            "message_id": result.get("messages", [{}])[0].get("id", "unknown") if isinstance(result, dict) else "sent"
        }
    except Exception as e:
        logger.error(f"Error sending message to {req.phone_number}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/send-report")
async def api_send_report(req: SendReportRequest, request: Request):
    """Endpoint para que SonIA Core envíe reportes de tracking por WhatsApp."""
    # Verificar API key
    api_key = request.headers.get("X-API-Key", "")
    if api_key != SONIA_CORE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    try:
        # Agregar encabezado si hay nombre de cliente
        report_text = req.report
        if req.client_name:
            report_text = f"📦 *Reporte de Tracking - {req.client_name}*\n\n{req.report}"
        
        result = whatsapp.send_message(req.phone_number, report_text)
        return {
            "status": "sent",
            "phone_number": req.phone_number,
            "client_name": req.client_name,
            "message_id": result.get("messages", [{}])[0].get("id", "unknown") if isinstance(result, dict) else "sent"
        }
    except Exception as e:
        logger.error(f"Error sending report to {req.phone_number}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
