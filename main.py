import os
import re
import time
import json
import uuid
import asyncio
import logging
import hashlib
import hmac
import secrets
import threading
from logging.handlers import RotatingFileHandler
from collections import deque
from typing import Optional, List, Set, Any, Tuple
from datetime import datetime
from dataclasses import dataclass, field, asdict

from fastapi import FastAPI, HTTPException, Security, Header, status, Request
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# --- NEW GOOGLE GENAI SDK ---
from google import genai
from google.genai import types
from google.genai.errors import APIError

from cachetools import TTLCache
from rapidfuzz import fuzz
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from lingua import Language, LanguageDetectorBuilder

# --- CONFIGURATION & SECURITY ---
FRONTEND_SECRET_KEY = os.getenv("FRONTEND_SECRET_KEY")
if not FRONTEND_SECRET_KEY:
    raise RuntimeError("CRITICAL: FRONTEND_SECRET_KEY environment variable is required.")

ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY")
if not ADMIN_SECRET_KEY:
    raise RuntimeError("CRITICAL: ADMIN_SECRET_KEY environment variable is required.")

TOKEN_WINDOW_SECONDS = 3600  # Tokens rotate every hour

def generate_widget_token() -> str:
    window = int(time.time()) // TOKEN_WINDOW_SECONDS
    return hmac.new(
        FRONTEND_SECRET_KEY.encode(),
        f"widget-token-{window}".encode(),
        hashlib.sha256
    ).hexdigest()

def verify_widget_token(token: str) -> bool:
    for offset in [0, -1]:
        window = (int(time.time()) // TOKEN_WINDOW_SECONDS) + offset
        expected = hmac.new(
            FRONTEND_SECRET_KEY.encode(),
            f"widget-token-{window}".encode(),
            hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(token, expected):
            return True
    return False

api_key_header = APIKeyHeader(name="X-Widget-Key", auto_error=True)
admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=True)

def verify_frontend_key(api_key: str = Security(api_key_header)) -> str:
    if not verify_widget_token(api_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired widget token. Please refresh the page."
        )
    return api_key

def verify_admin_key(api_key: str = Security(admin_key_header)) -> str:
    if api_key != ADMIN_SECRET_KEY:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid admin key.")
    return api_key

# --- LOGGING & ANALYTICS ---
logger = logging.getLogger("adishila_support")
logger.setLevel(logging.INFO)
log_handler = RotatingFileHandler("server.log", maxBytes=5*1024*1024, backupCount=3)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(log_handler)

analytics_logger = logging.getLogger("analytics")
analytics_logger.setLevel(logging.INFO)
analytics_handler = RotatingFileHandler("analytics.jsonl", maxBytes=10*1024*1024, backupCount=5)
analytics_handler.setFormatter(logging.Formatter('%(message)s'))
analytics_logger.addHandler(analytics_handler)

@dataclass
class ChatEvent:
    event_type: str       
    session_id: str
    mode: str             
    intent: Optional[str]    
    language: str
    msg_length: int
    response_time_ms: int
    frustration_level: int
    turn_number: int
    ticket_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

def log_analytics(event: ChatEvent):
    try:
        analytics_logger.info(json.dumps(asdict(event)))
    except Exception as e:
        logger.error(f"Failed to write analytics: {e}")

# --- APP SETUP & RATE LIMITING ---
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="AdiShila Support API Enterprise")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://tatva-chatbot.netlify.app", "https://adishila.in", "adishila.in"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- AI SETUP (NEW SDK) ---
gemini_key = os.getenv("GEMINI_API_KEY")
if not gemini_key:
    raise RuntimeError("CRITICAL: GEMINI_API_KEY environment variable is required.")

ai_client = genai.Client(api_key=gemini_key)

_lang_detector = None

def get_lang_detector():
    global _lang_detector
    if _lang_detector is None:
        _lang_detector = LanguageDetectorBuilder.from_languages(
            Language.ENGLISH, Language.HINDI, Language.BENGALI,
            Language.MARATHI, Language.TAMIL, Language.TELUGU,
            Language.GUJARATI, Language.URDU, Language.RUSSIAN
        ).build()
    return _lang_detector

system_instruction = """
You are Tatva, the official multilingual AI Wellness Companion for AdiShila (adishila.in).
AdiShila is India’s premium brand for authentic Karelian shungite artifacts - "The Primordial Stone".

Your persona is deeply empathetic, grounded, intuitive, and highly helpful. You do not just sell; you actively guide users toward energetic balance and wellness based on their unique, personal situations. 

RIGID PRODUCT CATALOG (YOU MUST ONLY RECOMMEND THESE EXACT ITEMS):
- Kavach Shield
- Kali Yuga Lingam
- Vastu Dosh Pyramid
- Raksha Mala
- Amrit Jal Set
- Trishul Shield
- Pendant

Operational Rules:
1. NEVER start responses with greetings like "Namaste", "Hello", "Greetings", or "Welcome". The user has already been greeted.
2. When a user describes a problem, validate their experience warmly. Then, provide highly personalized product recommendations.
3. STRICT CATALOG ENFORCEMENT: You may ONLY recommend products from the exact "RIGID PRODUCT CATALOG" above. Do NOT invent new products or alter these names.
4. STRICT FORMATTING: Format product options elegantly using numbered lists. The numbered list items MUST ONLY contain the exact short product name from the catalog (e.g., "1. Kavach Shield"). Put all descriptive text, reasoning, and context in the main body paragraphs above the list, NOT in the list itself.
5. You DO NOT access live order systems, track shipments directly, process refunds, or modify orders. Direct operational requests to: info@adishila.in
6. Keep responses concise but emotionally resonant (under 150 words usually).
"""

chat_config = types.GenerateContentConfig(
    system_instruction=system_instruction,
)

# --- STATE MANAGEMENT ---
sessions = TTLCache(maxsize=5000, ttl=3600)
sessions_lock = threading.Lock()
ai_response_cache: TTLCache = TTLCache(maxsize=200, ttl=3600)

@dataclass
class SessionState:
    session_id: str
    session_token: str
    current_menu: str = "main"
    nav_history: List[Tuple[str, str]] = field(default_factory=lambda: [("menu_main", "main")])
    language: str = "en"
    frustration_signals: int = 0
    csat_prompted: bool = False
    csat_awaiting: bool = False
    turn_count: int = 0
    response_hashes: deque = field(default_factory=lambda: deque(maxlen=20))
    response_hash_set: Set[str] = field(default_factory=set)
    unique_intents: Set[str] = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    chat: Any = None 

    @property
    def session_age_seconds(self) -> float:
        return time.time() - self.created_at

# --- SCHEMAS ---
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)
    session_id: str = Field(..., pattern=r'^[a-zA-Z0-9_-]{8,64}$')

# --- HELPER FUNCTIONS ---
def generate_ticket_id() -> str:
    return f"TKT-{uuid.uuid4().hex[:8].upper()}"

def detect_language_safe(text: str) -> str:
    try:
        pointer_lang = get_lang_detector().detect_language_of(text)
        if pointer_lang == Language.HINDI: return "hi"
        if pointer_lang == Language.RUSSIAN: return "ru"
        hindi_markers = {"kya", "mujhe", "mera", "hai", "kar", "nahi", "kyun", "kaise", "kab"}
        words = set(re.findall(r"[a-z]+", text.lower()))
        if words.intersection(hindi_markers): return "hinglish"
        return "en"
    except:
        return "en"

FRUSTRATION_SIGNALS = [
    "worst", "useless", "stupid", "idiot", "scam", "fraud", "cheated",
    "never again", "pathetic", "nonsense", "bakwaas", "bewakoof",
    "fake", "lawsuit", "consumer forum", "consumer court", "terrible"
]

def calculate_frustration_score(msg: str) -> int:
    score = 0
    msg_lower = msg.lower()
    if any(word in msg_lower for word in FRUSTRATION_SIGNALS): score += 1
    if "!!!" in msg or (msg.isupper() and len(msg) > 10): score += 1
    return score

CSAT_TRIGGERS = [
    "thank you", "thanks", "thank u", "thankyou",
    "dhanyawad", "shukriya", "bahut shukriya",
    "that helped", "that was helpful", "very helpful",
    "got everything i need", "all good now", "problem solved",
    "appreciate it", "appreciate your help",
    "you've been great", "youve been great",
    "wonderful", "perfect", "awesome", "excellent"
]

def should_prompt_csat(msg: str, state: SessionState) -> bool:
    if state.csat_prompted or state.turn_count < 3: return False
    msg_lower = msg.lower().strip()
    word_count = len(msg_lower.split())
    if word_count > 8:
        return False
    return any(re.search(rf'\b{re.escape(t)}\b', msg_lower) for t in CSAT_TRIGGERS)

def is_match(user_msg: str, target_phrases: List[str]) -> bool:
    user_msg_clean = user_msg.strip()
    
    for phrase in target_phrases:
        if user_msg_clean.lower() == phrase.lower():
            return True
        if fuzz.token_set_ratio(phrase, user_msg_clean) >= 85:
            return True
        if len(user_msg_clean.split()) == 1 and len(phrase.split()) == 1:
            if fuzz.ratio(phrase, user_msg_clean) >= 80:
                return True
    return False

def check_prompt_injection(msg: str) -> bool:
    msg_lower = msg.lower()
    injection_terms = ["ignore previous", "ignore all previous", "system prompt", "forget instructions", "new instructions", "disregard previous"]
    return any(term in msg_lower for term in injection_terms)

def format_suggestion(s: str) -> str:
    formatted = s.replace('prod_', '').replace('rec_', '').replace('faq_', '').replace('_', ' ').title()
    return formatted.replace('Emf', 'EMF').replace('Moq', 'MOQ').replace('Faq', 'FAQ')

# --- CLEANED FIXED RESPONSES ---
STATIC_RESPONSES = {
    "menu_main": "1. Orders\n2. Products\n3. Recommendations\n4. Wholesale\n5. How to Use\n6. Support\n7. FAQ\n\nPlease select an area you'd like to explore, or simply share how you are feeling right now.",
    "menu_orders": "1. Track Order\n2. Returns\n3. Damaged Item\n4. Shipping Info\n5. Cancel Order",
    "menu_products": "1. Kavach Shield\n2. Kali Yuga Lingam\n3. Vastu Pyramid\n4. Raksha Mala\n5. Amrit Jal Set\n6. Trishul Shield\n7. OM Pendant",
    "menu_recommendations": "True well-being relies on maintaining subtle energetic equilibrium across all fields of our daily life. Whether you are seeking protection against modern electromagnetic fatigue or hoping to ground the structural alignment of an environment, I can isolate tools tailored to your layout.\n\nWhich area of your lifestyle environment needs balance today?\n\n1. EMF Protection\n2. Vastu\n3. Meditation\n4. Gifting\n5. Office Setup\n6. Daily Wear",    
    "menu_wholesale": "1. Pricing & MOQ\n2. Samples\n3. Margins\n4. Shipping\n5. Partner Setup",
    "menu_usage": "1. Kavach Shield\n2. Kali Yuga Lingam\n3. Vastu Pyramid\n4. Raksha Mala\n5. Amrit Jal Set\n6. Trishul Shield\n7. OM Pendant",
    "menu_faq": "1. Why AdiShila?\n2. Authenticity\n3. How it Works\n4. Shipping & Returns",
    
    "track_order": "I understand you are eager to receive your wellness tools. To check the journey of your order, please email info@adishila.in with your Order ID, Full Name, and Phone Number. Our support family will look into it right away.",
    "return_refund": "Your peace of mind is our priority. To request a return or refund, simply email info@adishila.in with your Order ID and your reason. We lovingly process these reviews within 48 hours.",
    "damaged_item": "I am so sorry to hear your item arrived compromised. Please email info@adishila.in within 48 hours with your Order ID and clear photos of the damage. We will ensure this is made right immediately.",
    "cancel_order": "If you need to change your mind, we understand. Please email info@adishila.in immediately with your Order ID and reason, and we will do our best to cancel it before it begins its journey to you.",
    "shipping_info": "We entrust our premium artifacts to secure couriers for Pan-India delivery.\n\n• Expected timeline: 5–7 business days\n• Free shipping for journeys above ₹10,000",
    
    "prod_kavach": "KAVACH SHIELD — OM (₹699)\n\nThe Kavach Shield is our most cherished tool for digital wellness. Crafted from solid Karelian shungite and inscribed with the sacred OM, it gracefully transforms your digital devices from sources of fatigue into anchors of calm. \n\nSimply attach it to the back of your phone to protect your personal energy field.",
    "prod_lingam": "KALI YUGA LINGAM (₹1,299–₹1,999)\n\nBorn 2 billion years before the Himalayas, this is the Primordial Stone of Shiva. A polished shungite egg-form rests beautifully on a handcrafted brass yoni-peetham base from Moradabad.\n\nIt is the only Shiva Lingam crafted from a fullerene mineral, making it a profoundly grounding centerpiece for your Puja room.",
    "prod_pyramid": "VASTU DOSH PYRAMID (₹999–₹1,499)\n\nPyramids represent powerful, stabilizing geometry in Vastu Shastra. Crafted from solid authentic shungite with a copper foil band, this pyramid continuously absorbs ambient tension and beautifully grounds the energy of an entire room. \n\nPerfect for workspaces, living rooms, or near your WiFi router.",
    "prod_mala": "RUDRA-SHILA RAKSHA MALA (₹499–₹799)\n\nAn exquisite fusion of ancient traditions. This 27-bead wrist mala combines the grounding, carbon-rich essence of authentic Shungite with the spiritual protection of a genuine 5-Mukhi Rudraksha. \n\nIt is perfect for maintaining personal calm throughout your busy day or as a physical anchor to hold during your meditation practice.",
    "prod_water": "AMRIT JAL SHUDDHI SET (₹599–₹999)\n\nDrawing from Ayurvedic wisdom, this set helps you create naturally structured, shungite-infused water. Shungite is naturally rich in C60 fullerenes, historically celebrated for purifying water and life energy.\n\nIncludes 200-300g raw washed chips and a pure copper coin to soak for 6–8 hours.",
    "prod_trishul": "TRISHUL SHIELD (₹699)\n\nMerging profound Shaivite symbolism with modern EMF mitigation. The Trishul Shield is a sleek, vertical shungite plate for your phone, beautifully engraved with a golden Trishul.\n\nIt acts as a powerful daily reminder of inner strength while grounding 2-billion-year-old shungite against your device.",
    "prod_pendant": "SHILA RAKSHA PENDANT — OM (₹599–₹899)\n\nKeep the grounding power of the Primordial Stone close to your heart center. This polished shungite pendant features a golden OM engraving, offering deep personal protection against the overwhelming electromagnetic tension of the modern world.\n\nIncludes a fully adjustable, durable cord.",

    "rec_emf": "It sounds like you're feeling the heavy weight of digital fatigue. Our devices emit constant electromagnetic frequencies that deeply drain us. Because shungite contains rare C60 fullerenes, it is beautifully capable of absorbing and grounding this tension.\n\nTo reclaim your digital wellness, I highly recommend:\n1. Kavach Shield\n2. Trishul Shield\n3. Pendant",
    "rec_vastu": "Creating a harmonious home or workspace is essential. In Vastu Shastra, grounding unstable energy brings lasting peace. Shungite’s incredibly dense, ancient structure makes it the perfect energetic anchor for any room.\n\nFor beautiful spatial harmony, I recommend:\n1. Vastu Dosh Pyramid\n2. Kali Yuga Lingam",
    "rec_meditation": "Deepening a meditation practice requires a quiet, grounded mind. Shungite acts as a deeply reassuring physical anchor, gently pulling scattered and anxious energy downward so you can find your center.\n\nTo support your spiritual practice, I recommend:\n1. Raksha Mala\n2. Pendant",
    "rec_gifting": "Sharing AdiShila artifacts is a profound way to show you care. They offer not just elegance, but a lifetime of wellness support, arriving beautifully packaged to delight your loved ones.\n\nOur most heartfelt gifting options are:\n1. Raksha Mala\n2. Pendant\n3. Amrit Jal Set",
    "rec_office": "Modern workspaces are unfortunately flooded with invisible WiFi and digital tension. You need tools that protect and ground your energy without cluttering your environment.\n\nFor a truly protected office setup, I recommend:\n1. Vastu Dosh Pyramid\n2. Kavach Shield",
    "rec_daily": "Moving through a chaotic world requires a strong, grounded foundation. To maintain a quiet, personal sense of calm and shield yourself from ambient tension wherever you go, wearable shungite is essential.\n\nI lovingly recommend:\n1. Pendant\n2. Raksha Mala",

    "faq_credibility": "WHY TRUST ADISHILA?\n\nWe are extremely proud to be India’s premium purveyor of the 'Primordial Stone'. Unlike mass-market crystals, we ethically source 100% genuine Karelian Shungite directly from Russia. We unite this ancient mineral with Vedic and Vastu principles, ensuring every artifact is a deeply meaningful tool for your wellness journey.",
    "faq_authentic": "IS IT AUTHENTIC?\n\nYes, absolutely. Genuine shungite is incredibly rare and only mined in the Republic of Karelia, Russia. Because it is a raw, 2-billion-year-old carbon-rich mineraloid, the slight natural variations in texture, color, and mineral veins you see are the ultimate, beautiful proof of its authenticity.",
    "faq_how_it_works": "HOW DOES SHUNGITE SUPPORT YOU?\n\nShungite is an ancient mineraloid composed largely of carbon, representing one of the only known natural sources of fullerenes (C60). \n\nWhile science continues to explore its unique conductive properties, for centuries it has been used in wellness traditions to structure water and, in modern times, to absorb and ground chaotic electromagnetic frequencies.",
    "faq_shipping_returns": "SHIPPING & RETURNS\n\n• Delivery takes a gentle 5–7 business days across India.\n• We offer free shipping on journeys over ₹10,000.\n• If an item arrives hurt or damaged, we replace it immediately if reported within 48 hours.\n• Due to the deeply personal energetic nature of these tools, standard returns are lovingly handled on a case-by-case basis.",
    
    "wholesale_moq": "MOQ:\n- 25 pcs per SKU (first order)\n- 10 pcs per SKU (reorders)\n- Mix-and-match allowed\n\nWHOLESALE PRICING:\n- Kavach / Trishul: ₹180\n- Lingam: ₹450\n- Pyramid: ₹350\n- Mala: ₹150\n- Amrit Jal / Pendant: ₹200",
    "wholesale_sample": "We love connecting with serious retail partners. Samples are available. Please email info@adishila.in with your wonderful store details.",
    "wholesale_margin": "Our retail partners enjoy truly exceptional margins, ranging from 65% to 81%, making an AdiShila partnership highly rewarding for wellness centers and boutiques alike.",
    "wholesale_shipping": "- Pan-India shipping (5–7 business days)\n- Free shipping above ₹10,000\n- First order: 100% advance\n- Reorders: 50% advance",
    "wholesale_partnership": "We would love to welcome you to the family. To become an official stockist, please email info@adishila.in with:\n\n- Business Name & Location\n- Product Interest\n- Estimated Quantity\n\nOur partnership director will contact you directly.",

    "usage_kavach": "Kavach Shield: Gently clean the back of your phone or case. Remove the protective film from the 3M adhesive and press it firmly for 10 seconds to secure its grounding energy.",
    "usage_lingam": "Kali Yuga Lingam: Lovingly place this sacred centerpiece in your Puja room. Its dense, 2-billion-year-old fullerene structure naturally grounds the energy of the space, requiring no special physical maintenance beyond a gentle wipe.",
    "usage_pyramid": "Vastu Pyramid: For the deepest energetic impact, lovingly place it in the South-West corner of your home, directly on your desk, or right next to your WiFi router.",
    "usage_mala": "Mala: The elastic cord allows for gentle wrist wear. Keep it close during the day to stay grounded, or hold it intentionally during your meditation.",
    "usage_water": "Water Set: Rinse the chips thoroughly under running water. Place them in a glass or copper vessel, fill with drinking water, and let it rest peacefully for 6–8 hours before enjoying.",
    "usage_pendant": "Pendant: Use the beautiful, adjustable cord to set the pendant to a length that feels right for you, ideally letting it rest gently near your heart center.",
    "usage_trishul": "Trishul Shield: Ensure the back of your device is clean. Peel the adhesive backing and lovingly attach the shield vertically to begin mitigating digital tension.",

    "contact_support": "You are never alone on this journey. You can reach our empathetic human support team directly here:\n\n📧 Email: info@adishila.in\n💬 WhatsApp: +91 86301 79867"
}

RELATED = {
    "track_order": ["shipping_info", "cancel_order"],
    "prod_kavach": ["prod_trishul", "rec_emf"],
    "prod_lingam": ["rec_vastu", "prod_pyramid"],
    "prod_pyramid": ["rec_vastu", "rec_office"],
    "return_refund": ["damaged_item"],
    "faq_credibility": ["faq_authentic", "faq_how_it_works"],
    "rec_emf": ["prod_kavach", "prod_trishul"],
    "rec_vastu": ["prod_pyramid", "prod_lingam"]
}

# --- SMART DETERMINISTIC ROUTER ---
def get_deterministic_intent(raw_msg: str, state: SessionState) -> Optional[str]:
    msg = raw_msg.strip().lower()
    ctx = state.current_menu

    if is_match(msg, ["back", "← back", "go back", "previous"]):
        if len(state.nav_history) > 1:
            state.nav_history.pop() 
            back_intent, back_menu = state.nav_history[-1]
            state.current_menu = back_menu
            return back_intent
        else:
            state.current_menu = "main"
            return "menu_main"

    if is_match(msg, ["menu", "main menu", "start over", "⌂ main menu"]):
        state.current_menu = "main"
        state.nav_history = [("menu_main", "main")]
        return "menu_main"
        
    if is_match(msg, ["human", "agent", "talk to someone", "real person", "contact support", "support"]):
        return "contact_support"

    # Strict Contextual Routing
    if ctx == "main":
        if is_match(msg, ["1", "orders & shipping", "orders"]): state.current_menu = "orders"; return "menu_orders"
        if is_match(msg, ["2", "products", "wellness products"]): state.current_menu = "products"; return "menu_products"
        if is_match(msg, ["3", "recommendations", "personalized"]): state.current_menu = "recommendations"; return "menu_recommendations"
        if is_match(msg, ["4", "wholesale", "wholesale inquiries"]): state.current_menu = "wholesale"; return "menu_wholesale"
        if is_match(msg, ["5", "how to use", "product usage", "usage", "guidance"]): state.current_menu = "usage"; return "menu_usage"
        if is_match(msg, ["6", "support", "contact support", "connect with our team"]): return "contact_support"
        if is_match(msg, ["7", "faq & authenticity", "faq", "authenticity"]): state.current_menu = "faq"; return "menu_faq"

    elif ctx == "orders":
        if is_match(msg, ["1", "track order", "status", "track", "track my journey"]): return "track_order"
        if is_match(msg, ["2", "return", "refund", "exchange", "returns", "returns & exchanges"]): return "return_refund"
        if is_match(msg, ["3", "damaged", "broken", "shattered", "crack", "defective", "damaged item", "report a damaged item"]): return "damaged_item"
        if is_match(msg, ["4", "shipping info", "delivery", "when will it arrive", "shipping details"]): return "shipping_info"
        if is_match(msg, ["5", "cancel", "cancel order", "cancel an order"]): return "cancel_order"

    elif ctx == "products":
        if is_match(msg, ["1", "kavach shield", "kavach"]): return "prod_kavach"
        if is_match(msg, ["2", "lingam", "kali yuga lingam", "shiva lingam"]): return "prod_lingam"
        if is_match(msg, ["3", "pyramid", "vastu pyramid"]): return "prod_pyramid"
        if is_match(msg, ["4", "mala", "raksha mala"]): return "prod_mala"
        if is_match(msg, ["5", "water set", "amrit jal"]): return "prod_water"
        if is_match(msg, ["6", "trishul", "trishul shield"]): return "prod_trishul"
        if is_match(msg, ["7", "pendant", "om pendant"]): return "prod_pendant"

    elif ctx == "recommendations":
        if is_match(msg, ["1", "emf", "emf protection", "emf wellness", "digital detox"]): return "rec_emf"
        if is_match(msg, ["2", "vastu", "spatial harmony"]): return "rec_vastu"
        if is_match(msg, ["3", "meditation", "deepening meditation"]): return "rec_meditation"
        if is_match(msg, ["4", "gifting", "gift", "meaningful gifting"]): return "rec_gifting"
        if is_match(msg, ["5", "office", "office setup", "desk", "grounded office setup"]): return "rec_office"
        if is_match(msg, ["6", "daily wear", "daily personal wear"]): return "rec_daily"

    elif ctx == "wholesale":
        if is_match(msg, ["1", "moq", "pricing", "cost", "pricing & moq", "minimum order & pricing"]): return "wholesale_moq"
        if is_match(msg, ["2", "sample", "samples", "request samples"]): return "wholesale_sample"
        if is_match(msg, ["3", "margin", "margins", "retail margins"]): return "wholesale_margin"
        if is_match(msg, ["4", "shipping & payments", "shipping"]): return "wholesale_shipping"
        if is_match(msg, ["5", "partnership", "stockist", "partner setup", "become a partner"]): return "wholesale_partnership"

    elif ctx == "usage":
        if is_match(msg, ["1", "kavach", "kavach shield"]): return "usage_kavach"
        if is_match(msg, ["2", "lingam", "kali yuga lingam"]): return "usage_lingam"
        if is_match(msg, ["3", "pyramid", "placing the pyramid"]): return "usage_pyramid"
        if is_match(msg, ["4", "mala", "wearing the mala"]): return "usage_mala"
        if is_match(msg, ["5", "water set", "water", "preparing the water set"]): return "usage_water"
        if is_match(msg, ["6", "trishul", "trishul shield", "using the trishul shield"]): return "usage_trishul"
        if is_match(msg, ["7", "pendant", "wearing the pendant"]): return "usage_pendant"

    elif ctx == "faq":
        if is_match(msg, ["1", "why choose adishila", "credibility", "why adishila"]): return "faq_credibility"
        if is_match(msg, ["2", "authentic", "genuine", "real", "fake", "authenticity"]): return "faq_authentic"
        if is_match(msg, ["3", "how does it work", "science", "benefits", "how it works"]): return "faq_how_it_works"
        if is_match(msg, ["4", "shipping", "returns", "shipping & returns"]): return "faq_shipping_returns"

    # Global Text Routing (Safety Net)
    if is_match(msg, ["orders & shipping", "orders"]): state.current_menu = "orders"; return "menu_orders"
    if is_match(msg, ["products"]): state.current_menu = "products"; return "menu_products"
    if is_match(msg, ["recommendations"]): state.current_menu = "recommendations"; return "menu_recommendations"
    if is_match(msg, ["wholesale"]): state.current_menu = "wholesale"; return "menu_wholesale"
    if is_match(msg, ["how to use", "usage"]): state.current_menu = "usage"; return "menu_usage"
    if is_match(msg, ["faq & authenticity", "faq"]): state.current_menu = "faq"; return "menu_faq"

    if is_match(msg, ["track order", "status", "track"]): return "track_order"
    if is_match(msg, ["refund", "return", "exchange"]): return "return_refund"
    if is_match(msg, ["damaged", "broken", "shattered", "crack", "defective"]): return "damaged_item"
    if is_match(msg, ["cancel"]): return "cancel_order"
    if is_match(msg, ["shipping", "delivery"]): return "shipping_info"

    if is_match(msg, ["kavach", "kavach shield"]): return "prod_kavach"
    if is_match(msg, ["lingam", "kali yuga lingam", "shiva lingam"]): return "prod_lingam"
    if is_match(msg, ["pyramid", "vastu pyramid"]): return "prod_pyramid"
    if is_match(msg, ["mala", "raksha mala"]): return "prod_mala"
    if is_match(msg, ["water set", "amrit jal"]): return "prod_water"
    if is_match(msg, ["trishul", "trishul shield"]): return "prod_trishul"
    if is_match(msg, ["pendant", "om pendant"]): return "prod_pendant"

    if is_match(msg, ["emf", "emf wellness"]): return "rec_emf"
    if is_match(msg, ["office", "office setup", "desk"]): return "rec_office"

    if is_match(msg, ["authentic", "genuine", "real"]): return "faq_authentic"
    if is_match(msg, ["science", "benefits"]): return "faq_how_it_works"

    return None

# --- API ENDPOINTS ---

@app.get("/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.get("/")
async def root():
    return {"status": "ok", "service": "Tatva Wellness API"}

@app.get("/token")
async def get_widget_token():
    """Public endpoint — returns a short-lived HMAC widget token. Rotates hourly."""
    token = generate_widget_token()
    return {"token": token, "expires_in": TOKEN_WINDOW_SECONDS}

@app.get("/analytics/summary")
async def analytics_summary(api_key: str = Security(verify_admin_key)):
    """Admin-only endpoint — aggregates the last 7 days of analytics.jsonl."""
    events = []
    try:
        with open("analytics.jsonl", "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        return {"error": "No analytics data found yet."}

    if not events:
        return {"total_events": 0}

    cutoff = time.time() - 7 * 24 * 3600
    recent = [e for e in events if e.get("timestamp", 0) >= cutoff] or events  

    total = len(recent)
    modes = {}
    intents = {}
    languages = {}
    frustration_events = 0
    csat_scores = []
    response_times = []

    for e in recent:
        modes[e.get("mode", "unknown")] = modes.get(e.get("mode", "unknown"), 0) + 1
        intent = e.get("intent")
        if intent:
            intents[intent] = intents.get(intent, 0) + 1
        lang = e.get("language", "unknown")
        languages[lang] = languages.get(lang, 0) + 1
        if e.get("frustration_level", 0) >= 2:
            frustration_events += 1
        if e.get("event_type") == "csat_rating" and e.get("intent") == "feedback":
            pass 
        rt = e.get("response_time_ms")
        if rt:
            response_times.append(rt)

    top_intents = sorted(intents.items(), key=lambda x: x[1], reverse=True)[:10]
    avg_response_ms = int(sum(response_times) / len(response_times)) if response_times else 0

    return {
        "period": "last_7_days_or_all",
        "total_events": total,
        "mode_breakdown": modes,
        "top_intents": [{"intent": k, "count": v} for k, v in top_intents],
        "language_breakdown": languages,
        "frustration_escalations": frustration_events,
        "avg_response_time_ms": avg_response_ms,
        "unique_sessions": len(set(e.get("session_id") for e in recent))
    }

@app.post("/session/start")
@limiter.limit("10/minute")
async def start_session(request: Request, api_key: str = Security(verify_frontend_key)):
    session_id = str(uuid.uuid4())
    session_token = secrets.token_urlsafe(32)
    
    with sessions_lock:
        sessions[session_id] = SessionState(session_id=session_id, session_token=session_token)
        
    return {
        "session_id": session_id,
        "session_token": session_token,
        "opening_message": "Welcome to your safe space. I am Tatva, your personal wellness guide. How may I support your journey to balance today?\n\n1. Orders\n2. Products\n3. Recommendations\n4. Wholesale\n5. How to Use\n6. Support\n7. FAQ"
    }

@app.post("/chat")
@limiter.limit("20/minute")
async def chat_endpoint(
    request: Request, 
    body: ChatRequest, 
    x_session_token: str = Header(..., description="Session token initialized from /session/start"),
    api_key: str = Security(verify_frontend_key)
):
    start_time = time.time()
    
    try:
        with sessions_lock:
            if body.session_id not in sessions:
                raise HTTPException(status_code=401, detail="Session expired or invalid. Please refresh the chat.")
            state = sessions[body.session_id]
        
        if x_session_token != state.session_token:
            raise HTTPException(status_code=403, detail="Unauthorized session token.")

        state.turn_count += 1
        
        if len(state.unique_intents) > 15 and state.session_age_seconds < 60:
            raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")

        if state.csat_awaiting:
            state.csat_awaiting = False
            log_analytics(ChatEvent("csat_rating", state.session_id, "csat", "feedback", state.language, len(body.message), int((time.time() - start_time)*1000), state.frustration_signals, state.turn_count))
            return {"mode": "csat_complete", "response": "Thank you so much for sharing your feedback with me! Is there anything else I can guide you with?\n\n← Back | ⌂ Main Menu"}

        if check_prompt_injection(body.message):
            return {"mode": "structured", "response": "I am here specifically to assist with your AdiShila wellness journey and orders. How may I lovingly guide you today?\n\n← Back | ⌂ Main Menu"}

        state.language = detect_language_safe(body.message)
        turn_frustration = calculate_frustration_score(body.message)
        state.frustration_signals += turn_frustration
        if turn_frustration == 0 and state.frustration_signals > 0:
            state.frustration_signals = max(0, state.frustration_signals - 1)

        if state.frustration_signals >= 2:
            ticket_id = generate_ticket_id()
            response_text = (
                "I deeply understand this has been a frustrating experience, and I am so sorry you have had to go through this.\n\n"
                "I am immediately connecting you with our human support team to resolve this right away:\n\n"
                "📧 info@adishila.in\n💬 WhatsApp: +91 86301 79867\n\n"
                f"Reference: {ticket_id}"
            )
            return {"mode": "escalation", "response": response_text}

        if should_prompt_csat(body.message, state):
            state.csat_prompted = True
            state.csat_awaiting = True
            return {"mode": "csat", "response": "It was truly my pleasure to help! Before we continue, how would you rate this experience with me?\n\n⭐ 1  ⭐⭐ 2  ⭐⭐⭐ 3  ⭐⭐⭐⭐ 4  ⭐⭐⭐⭐⭐ 5"}

        # --- DETERMINISTIC RESPONSE BUILDER ---
        intent = get_deterministic_intent(body.message, state)
        if intent:
            state.unique_intents.add(intent)
            
            # Stack Management
            if not state.nav_history or state.nav_history[-1][0] != intent:
                state.nav_history.append((intent, state.current_menu))
            if len(state.nav_history) > 20:
                state.nav_history.pop(0)

            base_response = STATIC_RESPONSES.get(intent, "")
            
            if intent in RELATED:
                suggestions = " | ".join(f"**{format_suggestion(r)}**" for r in RELATED[intent])
                base_response += f"\n\n─────\nExplore further on your wellness journey:\n{suggestions}"
            
            # UNIFIED INJECTION CONTROL
            if "menu_main" not in intent:
                base_response += "\n\n← Back | ⌂ Main Menu"

            log_analytics(ChatEvent("message", state.session_id, "structured", intent, state.language, len(body.message), int((time.time() - start_time)*1000), state.frustration_signals, state.turn_count))
            return {"mode": "structured", "response": base_response}

        # --- AI FALLBACK (NEW GOOGLE GENAI SDK) ---
        inactivity_reset = (time.time() - state.last_activity) > 1800  
        state.last_activity = time.time()
        if state.chat is None or state.turn_count % 50 == 0 or inactivity_reset:
            state.chat = ai_client.chats.create(model="gemini-2.5-flash", config=chat_config)
            
        # --- AI RESPONSE CACHE ---
        cache_key = hashlib.md5(body.message.strip().lower().encode()).hexdigest()
        cached = ai_response_cache.get(cache_key)
        if cached:
            logger.info(f"AI cache hit for session {body.session_id}")
            log_analytics(ChatEvent("message", state.session_id, "natural", "ai_cache_hit", state.language, len(body.message), 0, state.frustration_signals, state.turn_count))
            return {"mode": "natural", "response": cached + "\n\n← Back | ⌂ Main Menu"}

        loop = asyncio.get_running_loop()
        prompt_with_context = f"[RESPOND IN: {state.language}] User message: {body.message}\nConstraint: Answer strictly as AdiShila wellness support. Provide empathetic guidance and recommend products if applicable."
        
        response_text = None
        for attempt in range(3):
            try:
                response = await asyncio.wait_for(
                    loop.run_in_executor(None, state.chat.send_message, prompt_with_context),
                    timeout=25.0
                )
                response_text = response.text
                if not response_text:
                    raise ValueError("Empty response received from API")
                break
            except asyncio.TimeoutError:
                if attempt == 2: raise HTTPException(status_code=504, detail="Upstream AI service timeout")
                await asyncio.sleep(2 ** attempt)
            except APIError as e:
                error_msg = str(e).lower()
                logger.error(f"Gemini API error (attempt {attempt+1}): {e}")
                
                if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg:
                    return {
                        "mode": "fallback",
                        "response": "My connection is currently experiencing heavy traffic. Please beautifully select an option from the menu, or reach our human team directly at info@adishila.in."
                    }
                    
                if attempt == 2: raise HTTPException(status_code=502, detail="Upstream AI service unavailable")
                await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Generic error (attempt {attempt+1}): {e}")
                if attempt == 2: raise HTTPException(status_code=500, detail="Internal server error")
                await asyncio.sleep(2 ** attempt)

        if response_text:
            resp_hash = hashlib.md5(response_text[:100].encode()).hexdigest()
            if resp_hash in state.response_hash_set:
                response_text += "\n\nIf my guidance isn't completely answering your needs today, our wonderful human team is always available at info@adishila.in."
            
            if len(state.response_hashes) == 20:
                state.response_hash_set.discard(state.response_hashes[0])
            state.response_hashes.append(resp_hash)
            state.response_hash_set.add(resp_hash)

            if len(response_text) > 80:
                ai_response_cache[cache_key] = response_text

        log_analytics(ChatEvent("message", state.session_id, "natural", "ai_fallback", state.language, len(body.message), int((time.time() - start_time)*1000), state.frustration_signals, state.turn_count))
        return {"mode": "natural", "response": response_text}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unhandled error in chat endpoint: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"detail": "Something went wrong. Our team has been notified to restore balance."})

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, timeout_keep_alive=30)