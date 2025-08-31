import asyncio
import logging
import re
import json
import csv
import io
import hmac
import hashlib
import time
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from decimal import Decimal

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import create_engine, and_, or_
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.exc import SQLAlchemyError

from models import (
    Base, User, Service, ServiceCountry, Number, Provider, ServiceProviderMap,
    Reservation, Transaction, Channel, UserChannelReward, Group, UserGroupReward,
    ProviderMessage, ServiceGroup, BlockedMessage, AdminAuditLink,
    NumberStatus, ReservationStatus, TransactionType, ProviderMode,
    SecurityMode, MessageStatus
)

# Use ServiceCountry as Country for admin purposes
Country = ServiceCountry
from config import (
    BOT_TOKEN, ADMIN_ID, ADMIN_PASSWORD, DATABASE_URL, RESERVATION_TIMEOUT_MIN,
    POLL_INTERVAL_SEC, DEFAULT_REWARD_AMOUNT, PAGE_SIZE, PROVIDER_API_TIMEOUT,
    HMAC_SECRET, MESSAGE_TIMESTAMP_WINDOW_MIN
)
from translations import translator, t, SUPPORTED_LANGUAGES
from commands import set_bot_commands, get_text

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database setup
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required")
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = scoped_session(sessionmaker(bind=engine))

# Bot setup
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Global variables for session management
admin_sessions = {}  # {user_id: datetime}
maintenance_mode = False

# FSM States
class UserStates(StatesGroup):
    waiting_for_service = State()
    waiting_for_country = State()
    waiting_for_number_action = State()

class AdminStates(StatesGroup):
    waiting_for_password = State()
    waiting_for_service_name = State()
    waiting_for_service_emoji = State()
    waiting_for_service_price = State()
    waiting_for_service_description = State()
    waiting_for_service_regex = State()
    waiting_for_service_group_id = State()
    waiting_for_service_secret_token = State()
    waiting_for_service_security_mode = State()
    waiting_for_country_name = State()
    waiting_for_country_code = State()
    waiting_for_country_flag = State()
    waiting_for_number_manual = State()
    waiting_for_numbers_file = State()
    waiting_for_provider_name = State()
    waiting_for_provider_url = State()
    waiting_for_provider_key = State()
    waiting_for_channel_title = State()
    waiting_for_channel_username = State()
    waiting_for_channel_reward = State()
    waiting_for_numbers_input = State()
    waiting_for_user_id_balance = State()
    waiting_for_balance_amount = State()
    waiting_for_broadcast_message = State()
    # Edit service states
    waiting_for_edit_service_name = State()
    waiting_for_edit_service_emoji = State()
    waiting_for_edit_service_price = State()
    waiting_for_edit_service_description = State()

# Utility functions
def get_db():
    """Get database session"""
    return SessionLocal()

# Helper function to get user language
def get_user_language(user_id: str) -> str:
    """Get user's preferred language"""
    db = get_db()
    try:
        user = db.query(User).filter(User.telegram_id == user_id).first()
        if user and user.language_code:
            return str(user.language_code)
        return 'ar'
    finally:
        db.close()

# Helper function to update user language
def update_user_language(user_id: str, lang_code: str) -> bool:
    """Update user's preferred language"""
    db = get_db()
    try:
        user = db.query(User).filter(User.telegram_id == user_id).first()
        if user:
            user.language_code = lang_code
            db.commit()
            return True
        return False
    except Exception as e:
        logger.error(f"Error updating user language: {e}")
        db.rollback()
        return False
    finally:
        db.close()

async def get_or_create_user(telegram_id: str, username: Optional[str] = None, first_name: Optional[str] = None, last_name: Optional[str] = None) -> tuple[User, bool]:
    """Get existing user or create new one. Returns (user, is_new_user)"""
    db = get_db()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        is_new_user = False
        if not user:
            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                is_admin=(int(telegram_id) == ADMIN_ID),
                language_code=None  # No language set for new users
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            is_new_user = True
        return user, is_new_user
    finally:
        db.close()

def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return user_id == ADMIN_ID or user_id in admin_sessions

def is_admin_session_valid(user_id: int) -> bool:
    """Check if admin session is still valid"""
    if user_id == ADMIN_ID:
        return True
    if user_id in admin_sessions:
        # Session valid for 1 hour
        return (datetime.now() - admin_sessions[user_id]).seconds < 3600
    return False

def normalize_phone_number(phone: str) -> str:
    """Normalize phone number to international format"""
    # Remove spaces, dashes, parentheses
    phone = re.sub(r'[\s\-\(\)]', '', phone)
    # Ensure starts with +
    if not phone.startswith('+'):
        phone = '+' + phone
    return phone

async def search_code_in_groups(phone_number: str, service_id: int) -> Optional[str]:
    """Search for code in recent group messages for the given phone number"""
    db = get_db()
    try:
        # Find service groups for this service
        service_groups = db.query(ServiceGroup).filter(
            ServiceGroup.service_id == service_id,
            ServiceGroup.active == True
        ).all()
        
        if not service_groups:
            logger.warning(f"No active groups found for service_id {service_id}")
            return None
        
        # Get regex pattern for this service
        service_provider_map = db.query(ServiceProviderMap).filter(
            ServiceProviderMap.service_id == service_id
        ).first()
        regex_pattern = service_provider_map.regex_pattern if service_provider_map else r'\b\d{5,6}\b'
        
        # Search in recent messages for this phone number
        for group in service_groups:
            logger.info(f"Searching for code in group {group.group_chat_id} for number {phone_number}")
            
            # Look for recent messages containing this phone number
            recent_messages = db.query(ProviderMessage).filter(
                ProviderMessage.service_id == service_id,
                ProviderMessage.message_text.contains(phone_number),
                ProviderMessage.created_at >= datetime.now() - timedelta(hours=1)  # Last hour only
            ).order_by(ProviderMessage.created_at.desc()).limit(10).all()
            
            for msg in recent_messages:
                # Try to extract code from message
                number, code = extract_number_and_code(msg.message_text, regex_pattern)
                if number == phone_number and code:
                    logger.info(f"Found code {code} for number {phone_number} in message: {msg.message_text}")
                    return code
        
        logger.info(f"No code found for number {phone_number} in any group messages")
        return None
        
    except Exception as e:
        logger.error(f"Error searching for code in groups: {e}")
        return None
    finally:
        db.close()

async def auto_search_for_code(reservation_id: int):
    """Auto search for code - starts after 15 seconds then every 5 seconds"""
    # Wait 15 seconds before first search
    await asyncio.sleep(15)
    
    max_attempts = 50  # Max 5 minutes (50 * 5 seconds + 15 initial)
    attempts = 0
    
    while attempts < max_attempts:
        db = get_db()
        try:
            # Check if reservation is still valid
            reservation = db.query(Reservation).filter(
                Reservation.id == reservation_id,
                Reservation.status == ReservationStatus.WAITING_CODE
            ).first()
            
            if not reservation:
                logger.info(f"Reservation {reservation_id} no longer valid, stopping auto search")
                return
            
            # Get number for this reservation
            number = db.query(Number).filter(Number.id == reservation.number_id).first()
            if not number:
                logger.warning(f"Number not found for reservation {reservation_id}")
                return
            
            logger.info(f"Auto searching for code attempt {attempts + 1} for number {number.phone_number}")
            
            # Search for code
            code = await search_code_in_groups(number.phone_number, number.service_id)
            
            if code:
                logger.info(f"Auto search found code {code} for reservation {reservation_id}")
                
                # Complete the reservation
                success = await complete_reservation_atomic(reservation_id, code)
                
                if success:
                    # Send code to user
                    service = db.query(Service).filter(Service.id == number.service_id).first()
                    
                    await bot.send_message(
                        reservation.user_id,
                        f"✅ تم استلام كود التحقق!\n\n"
                        f"📱 الرقم: `{number.phone_number}`\n"
                        f"🏷 الخدمة: {service.emoji} {service.name}\n"
                        f"🔢 الكود: `{code}`\n"
                        f"💰 تم الخصم: {service.default_price} وحدة\n\n"
                        f"✅ تمت العملية بنجاح",
                        parse_mode="Markdown"
                    )
                    return
                
        except Exception as e:
            logger.error(f"Error in auto search for reservation {reservation_id}: {e}")
        finally:
            db.close()
        
        attempts += 1
        await asyncio.sleep(5)  # Wait 5 seconds between attempts
    
    logger.info(f"Auto search completed for reservation {reservation_id} after {max_attempts} attempts")

def detect_country_code(phone: str) -> str:
    """Detect country code from phone number"""
    phone = normalize_phone_number(phone)
    
    # Common country codes mapping
    country_codes = {
        '+1': '+1',      # USA/Canada
        '+7': '+7',      # Russia/Kazakhstan  
        '+20': '+20',    # Egypt
        '+33': '+33',    # France
        '+34': '+34',    # Spain
        '+39': '+39',    # Italy
        '+44': '+44',    # UK
        '+49': '+49',    # Germany
        '+52': '+52',    # Mexico
        '+55': '+55',    # Brazil
        '+60': '+60',    # Malaysia
        '+61': '+61',    # Australia
        '+62': '+62',    # Indonesia
        '+63': '+63',    # Philippines
        '+64': '+64',    # New Zealand
        '+65': '+65',    # Singapore
        '+66': '+66',    # Thailand
        '+81': '+81',    # Japan
        '+82': '+82',    # South Korea
        '+84': '+84',    # Vietnam
        '+86': '+86',    # China
        '+90': '+90',    # Turkey
        '+91': '+91',    # India
        '+92': '+92',    # Pakistan
        '+93': '+93',    # Afghanistan
        '+94': '+94',    # Sri Lanka
        '+95': '+95',    # Myanmar
        '+98': '+98',    # Iran
        '+212': '+212',  # Morocco
        '+213': '+213',  # Algeria
        '+216': '+216',  # Tunisia
        '+218': '+218',  # Libya
        '+220': '+220',  # Gambia
        '+221': '+221',  # Senegal
        '+222': '+222',  # Mauritania
        '+223': '+223',  # Mali
        '+224': '+224',  # Guinea
        '+225': '+225',  # Ivory Coast
        '+226': '+226',  # Burkina Faso
        '+227': '+227',  # Niger
        '+228': '+228',  # Togo
        '+229': '+229',  # Benin
        '+230': '+230',  # Mauritius
        '+231': '+231',  # Liberia
        '+232': '+232',  # Sierra Leone
        '+233': '+233',  # Ghana
        '+234': '+234',  # Nigeria
        '+235': '+235',  # Chad
        '+236': '+236',  # Central African Republic
        '+237': '+237',  # Cameroon
        '+238': '+238',  # Cape Verde
        '+239': '+239',  # Sao Tome and Principe
        '+240': '+240',  # Equatorial Guinea
        '+241': '+241',  # Gabon
        '+242': '+242',  # Republic of the Congo
        '+243': '+243',  # Democratic Republic of the Congo
        '+244': '+244',  # Angola
        '+245': '+245',  # Guinea-Bissau
        '+246': '+246',  # British Indian Ocean Territory
        '+248': '+248',  # Seychelles
        '+249': '+249',  # Sudan
        '+250': '+250',  # Rwanda
        '+251': '+251',  # Ethiopia
        '+252': '+252',  # Somalia
        '+253': '+253',  # Djibouti
        '+254': '+254',  # Kenya
        '+255': '+255',  # Tanzania
        '+256': '+256',  # Uganda
        '+257': '+257',  # Burundi
        '+258': '+258',  # Mozambique
        '+260': '+260',  # Zambia
        '+261': '+261',  # Madagascar
        '+262': '+262',  # Reunion
        '+263': '+263',  # Zimbabwe
        '+264': '+264',  # Namibia
        '+265': '+265',  # Malawi
        '+266': '+266',  # Lesotho
        '+267': '+267',  # Botswana
        '+268': '+268',  # Swaziland
        '+269': '+269',  # Comoros
        '+290': '+290',  # Saint Helena
        '+291': '+291',  # Eritrea
        '+297': '+297',  # Aruba
        '+298': '+298',  # Faroe Islands
        '+299': '+299',  # Greenland
        '+350': '+350',  # Gibraltar
        '+351': '+351',  # Portugal
        '+352': '+352',  # Luxembourg
        '+353': '+353',  # Ireland
        '+354': '+354',  # Iceland
        '+355': '+355',  # Albania
        '+356': '+356',  # Malta
        '+357': '+357',  # Cyprus
        '+358': '+358',  # Finland
        '+359': '+359',  # Bulgaria
        '+370': '+370',  # Lithuania
        '+371': '+371',  # Latvia
        '+372': '+372',  # Estonia
        '+373': '+373',  # Moldova
        '+374': '+374',  # Armenia
        '+375': '+375',  # Belarus
        '+376': '+376',  # Andorra
        '+377': '+377',  # Monaco
        '+378': '+378',  # San Marino
        '+380': '+380',  # Ukraine
        '+381': '+381',  # Serbia
        '+382': '+382',  # Montenegro
        '+383': '+383',  # Kosovo
        '+385': '+385',  # Croatia
        '+386': '+386',  # Slovenia
        '+387': '+387',  # Bosnia and Herzegovina
        '+389': '+389',  # North Macedonia
        '+420': '+420',  # Czech Republic
        '+421': '+421',  # Slovakia
        '+423': '+423',  # Liechtenstein
        '+500': '+500',  # Falkland Islands
        '+501': '+501',  # Belize
        '+502': '+502',  # Guatemala
        '+503': '+503',  # El Salvador
        '+504': '+504',  # Honduras
        '+505': '+505',  # Nicaragua
        '+506': '+506',  # Costa Rica
        '+507': '+507',  # Panama
        '+508': '+508',  # Saint Pierre and Miquelon
        '+509': '+509',  # Haiti
        '+590': '+590',  # Guadeloupe
        '+591': '+591',  # Bolivia
        '+592': '+592',  # Guyana
        '+593': '+593',  # Ecuador
        '+594': '+594',  # French Guiana
        '+595': '+595',  # Paraguay
        '+596': '+596',  # Martinique
        '+597': '+597',  # Suriname
        '+598': '+598',  # Uruguay
        '+599': '+599',  # Netherlands Antilles
        '+670': '+670',  # East Timor
        '+672': '+672',  # Antarctica
        '+673': '+673',  # Brunei
        '+674': '+674',  # Nauru
        '+675': '+675',  # Papua New Guinea
        '+676': '+676',  # Tonga
        '+677': '+677',  # Solomon Islands
        '+678': '+678',  # Vanuatu
        '+679': '+679',  # Fiji
        '+680': '+680',  # Palau
        '+681': '+681',  # Wallis and Futuna
        '+682': '+682',  # Cook Islands
        '+683': '+683',  # Niue
        '+684': '+684',  # American Samoa
        '+685': '+685',  # Samoa
        '+686': '+686',  # Kiribati
        '+687': '+687',  # New Caledonia
        '+688': '+688',  # Tuvalu
        '+689': '+689',  # French Polynesia
        '+690': '+690',  # Tokelau
        '+691': '+691',  # Micronesia
        '+692': '+692',  # Marshall Islands
        '+850': '+850',  # North Korea
        '+852': '+852',  # Hong Kong
        '+853': '+853',  # Macau
        '+855': '+855',  # Cambodia
        '+856': '+856',  # Laos
        '+880': '+880',  # Bangladesh
        '+886': '+886',  # Taiwan
        '+960': '+960',  # Maldives
        '+961': '+961',  # Lebanon
        '+962': '+962',  # Jordan
        '+963': '+963',  # Syria
        '+964': '+964',  # Iraq
        '+965': '+965',  # Kuwait
        '+966': '+966',  # Saudi Arabia
        '+967': '+967',  # Yemen
        '+968': '+968',  # Oman
        '+970': '+970',  # Palestine
        '+971': '+971',  # United Arab Emirates
        '+972': '+972',  # Israel
        '+973': '+973',  # Bahrain
        '+974': '+974',  # Qatar
        '+975': '+975',  # Bhutan
        '+976': '+976',  # Mongolia
        '+977': '+977',  # Nepal
        '+992': '+992',  # Tajikistan
        '+993': '+993',  # Turkmenistan
        '+994': '+994',  # Azerbaijan
        '+995': '+995',  # Georgia
        '+996': '+996',  # Kyrgyzstan
        '+998': '+998',  # Uzbekistan
    }
    
    # Check for exact matches (longest first)
    for length in [4, 3, 2]:
        if len(phone) >= length + 1:  # +1 for the '+' sign
            prefix = phone[:length + 1]
            if prefix in country_codes:
                return country_codes[prefix]
    
    # Default fallback
    return '+1'  # Default to US/Canada if no match found

def get_country_name_and_flag(country_code: str) -> tuple[str, str]:
    """Get country name and flag from country code"""
    country_info = {
        '+1': ('الولايات المتحدة', '🇺🇸'),
        '+7': ('روسيا', '🇷🇺'),
        '+20': ('مصر', '🇪🇬'),
        '+33': ('فرنسا', '🇫🇷'),
        '+34': ('إسبانيا', '🇪🇸'),
        '+39': ('إيطاليا', '🇮🇹'),
        '+44': ('المملكة المتحدة', '🇬🇧'),
        '+49': ('ألمانيا', '🇩🇪'),
        '+52': ('المكسيك', '🇲🇽'),
        '+55': ('البرازيل', '🇧🇷'),
        '+60': ('ماليزيا', '🇲🇾'),
        '+61': ('أستراليا', '🇦🇺'),
        '+62': ('إندونيسيا', '🇮🇩'),
        '+63': ('الفلبين', '🇵🇭'),
        '+64': ('نيوزيلندا', '🇳🇿'),
        '+65': ('سنغافورة', '🇸🇬'),
        '+66': ('تايلاند', '🇹🇭'),
        '+81': ('اليابان', '🇯🇵'),
        '+82': ('كوريا الجنوبية', '🇰🇷'),
        '+84': ('فيتنام', '🇻🇳'),
        '+86': ('الصين', '🇨🇳'),
        '+90': ('تركيا', '🇹🇷'),
        '+91': ('الهند', '🇮🇳'),
        '+92': ('باكستان', '🇵🇰'),
        '+93': ('أفغانستان', '🇦🇫'),
        '+94': ('سريلانكا', '🇱🇰'),
        '+95': ('ميانمار', '🇲🇲'),
        '+98': ('إيران', '🇮🇷'),
        '+212': ('المغرب', '🇲🇦'),
        '+213': ('الجزائر', '🇩🇿'),
        '+216': ('تونس', '🇹🇳'),
        '+218': ('ليبيا', '🇱🇾'),
        '+220': ('غامبيا', '🇬🇲'),
        '+221': ('السنغال', '🇸🇳'),
        '+222': ('موريتانيا', '🇲🇷'),
        '+223': ('مالي', '🇲🇱'),
        '+224': ('غينيا', '🇬🇳'),
        '+225': ('ساحل العاج', '🇨🇮'),
        '+226': ('بوركينا فاسو', '🇧🇫'),
        '+227': ('النيجر', '🇳🇪'),
        '+228': ('توغو', '🇹🇬'),
        '+229': ('بنين', '🇧🇯'),
        '+230': ('موريشيوس', '🇲🇺'),
        '+231': ('ليبيريا', '🇱🇷'),
        '+232': ('سيراليون', '🇸🇱'),
        '+233': ('غانا', '🇬🇭'),
        '+234': ('نيجيريا', '🇳🇬'),
        '+235': ('تشاد', '🇹🇩'),
        '+236': ('جمهورية أفريقيا الوسطى', '🇨🇫'),
        '+237': ('الكاميرون', '🇨🇲'),
        '+238': ('الرأس الأخضر', '🇨🇻'),
        '+239': ('ساو تومي وبرينسيبي', '🇸🇹'),
        '+240': ('غينيا الاستوائية', '🇬🇶'),
        '+241': ('الغابون', '🇬🇦'),
        '+242': ('جمهورية الكونغو', '🇨🇬'),
        '+243': ('جمهورية الكونغو الديمقراطية', '🇨🇩'),
        '+244': ('أنغولا', '🇦🇴'),
        '+245': ('غينيا بيساو', '🇬🇼'),
        '+248': ('سيشل', '🇸🇨'),
        '+249': ('السودان', '🇸🇩'),
        '+250': ('رواندا', '🇷🇼'),
        '+251': ('إثيوبيا', '🇪🇹'),
        '+252': ('الصومال', '🇸🇴'),
        '+253': ('جيبوتي', '🇩🇯'),
        '+254': ('كينيا', '🇰🇪'),
        '+255': ('تنزانيا', '🇹🇿'),
        '+256': ('أوغندا', '🇺🇬'),
        '+257': ('بوروندي', '🇧🇮'),
        '+258': ('موزمبيق', '🇲🇿'),
        '+260': ('زامبيا', '🇿🇲'),
        '+261': ('مدغشقر', '🇲🇬'),
        '+263': ('زيمبابوي', '🇿🇼'),
        '+264': ('ناميبيا', '🇳🇦'),
        '+265': ('ملاوي', '🇲🇼'),
        '+266': ('ليسوتو', '🇱🇸'),
        '+267': ('بوتسوانا', '🇧🇼'),
        '+268': ('إسواتيني', '🇸🇿'),
        '+269': ('جزر القمر', '🇰🇲'),
        '+351': ('البرتغال', '🇵🇹'),
        '+352': ('لوكسمبورغ', '🇱🇺'),
        '+353': ('أيرلندا', '🇮🇪'),
        '+354': ('أيسلندا', '🇮🇸'),
        '+355': ('ألبانيا', '🇦🇱'),
        '+356': ('مالطا', '🇲🇹'),
        '+357': ('قبرص', '🇨🇾'),
        '+358': ('فنلندا', '🇫🇮'),
        '+359': ('بلغاريا', '🇧🇬'),
        '+370': ('ليتوانيا', '🇱🇹'),
        '+371': ('لاتفيا', '🇱🇻'),
        '+372': ('إستونيا', '🇪🇪'),
        '+373': ('مولدوفا', '🇲🇩'),
        '+374': ('أرمينيا', '🇦🇲'),
        '+375': ('بيلاروس', '🇧🇾'),
        '+376': ('أندورا', '🇦🇩'),
        '+377': ('موناكو', '🇲🇨'),
        '+378': ('سان مارينو', '🇸🇲'),
        '+380': ('أوكرانيا', '🇺🇦'),
        '+381': ('صربيا', '🇷🇸'),
        '+382': ('الجبل الأسود', '🇲🇪'),
        '+383': ('كوسوفو', '🇽🇰'),
        '+385': ('كرواتيا', '🇭🇷'),
        '+386': ('سلوفينيا', '🇸🇮'),
        '+387': ('البوسنة والهرسك', '🇧🇦'),
        '+389': ('مقدونيا الشمالية', '🇲🇰'),
        '+420': ('التشيك', '🇨🇿'),
        '+421': ('سلوفاكيا', '🇸🇰'),
        '+423': ('ليختنشتاين', '🇱🇮'),
        '+500': ('جزر فوكلاند', '🇫🇰'),
        '+501': ('بليز', '🇧🇿'),
        '+502': ('غواتيمالا', '🇬🇹'),
        '+503': ('السلفادور', '🇸🇻'),
        '+504': ('هندوراس', '🇭🇳'),
        '+505': ('نيكاراغوا', '🇳🇮'),
        '+506': ('كوستاريكا', '🇨🇷'),
        '+507': ('بنما', '🇵🇦'),
        '+509': ('هايتي', '🇭🇹'),
        '+590': ('غوادلوب', '🇬🇵'),
        '+591': ('بوليفيا', '🇧🇴'),
        '+592': ('غيانا', '🇬🇾'),
        '+593': ('الإكوادور', '🇪🇨'),
        '+594': ('غيانا الفرنسية', '🇬🇫'),
        '+595': ('باراغواي', '🇵🇾'),
        '+596': ('مارتينيك', '🇲🇶'),
        '+597': ('سورينام', '🇸🇷'),
        '+598': ('أوروغواي', '🇺🇾'),
        '+670': ('تيمور الشرقية', '🇹🇱'),
        '+673': ('بروناي', '🇧🇳'),
        '+674': ('ناورو', '🇳🇷'),
        '+675': ('بابوا غينيا الجديدة', '🇵🇬'),
        '+676': ('تونغا', '🇹🇴'),
        '+677': ('جزر سليمان', '🇸🇧'),
        '+678': ('فانواتو', '🇻🇺'),
        '+679': ('فيجي', '🇫🇯'),
        '+680': ('بالاو', '🇵🇼'),
        '+681': ('واليس وفوتونا', '🇼🇫'),
        '+682': ('جزر كوك', '🇨🇰'),
        '+683': ('نيوي', '🇳🇺'),
        '+684': ('ساموا الأمريكية', '🇦🇸'),
        '+685': ('ساموا', '🇼🇸'),
        '+686': ('كيريباتي', '🇰🇮'),
        '+687': ('كاليدونيا الجديدة', '🇳🇨'),
        '+688': ('توفالو', '🇹🇻'),
        '+689': ('بولينيزيا الفرنسية', '🇵🇫'),
        '+690': ('توكيلاو', '🇹🇰'),
        '+691': ('ميكرونيزيا', '🇫🇲'),
        '+692': ('جزر مارشال', '🇲🇭'),
        '+850': ('كوريا الشمالية', '🇰🇵'),
        '+852': ('هونغ كونغ', '🇭🇰'),
        '+853': ('ماكاو', '🇲🇴'),
        '+855': ('كمبوديا', '🇰🇭'),
        '+856': ('لاوس', '🇱🇦'),
        '+880': ('بنغلاديش', '🇧🇩'),
        '+886': ('تايوان', '🇹🇼'),
        '+960': ('المالديف', '🇲🇻'),
        '+961': ('لبنان', '🇱🇧'),
        '+962': ('الأردن', '🇯🇴'),
        '+963': ('سوريا', '🇸🇾'),
        '+964': ('العراق', '🇮🇶'),
        '+965': ('الكويت', '🇰🇼'),
        '+966': ('السعودية', '🇸🇦'),
        '+967': ('اليمن', '🇾🇪'),
        '+968': ('عمان', '🇴🇲'),
        '+970': ('فلسطين', '🇵🇸'),
        '+971': ('الإمارات', '🇦🇪'),
        '+972': ('إسرائيل', '🇮🇱'),
        '+973': ('البحرين', '🇧🇭'),
        '+974': ('قطر', '🇶🇦'),
        '+975': ('بوتان', '🇧🇹'),
        '+976': ('منغوليا', '🇲🇳'),
        '+977': ('نيبال', '🇳🇵'),
        '+992': ('طاجيكستان', '🇹🇯'),
        '+993': ('تركمانستان', '🇹🇲'),
        '+994': ('أذربيجان', '🇦🇿'),
        '+995': ('جورجيا', '🇬🇪'),
        '+996': ('قيرغيزستان', '🇰🇬'),
        '+998': ('أوزبكستان', '🇺🇿'),
    }
    
    return country_info.get(country_code, ('دولة غير معروفة', '🌍'))

def ensure_service_country_exists(service_id: int, country_code: str, db_session) -> ServiceCountry:
    """Ensure ServiceCountry entry exists for the given service and country code"""
    # Check if ServiceCountry already exists
    service_country = db_session.query(ServiceCountry).filter(
        ServiceCountry.service_id == service_id,
        ServiceCountry.country_code == country_code
    ).first()
    
    if not service_country:
        # Get country name and flag
        country_name, flag = get_country_name_and_flag(country_code)
        
        # Create new ServiceCountry entry
        service_country = ServiceCountry(
            service_id=service_id,
            country_name=country_name,
            country_code=country_code,
            flag=flag,
            active=True
        )
        db_session.add(service_country)
        db_session.flush()  # Flush to get the ID
        
        logger.info(f"Auto-created ServiceCountry: {country_name} ({country_code}) for service {service_id}")
    
    return service_country

async def notify_admin_low_stock(service_id: int, country_code: str, country_name: str):
    """Notify admin when a country runs out of numbers"""
    try:
        message = (
            f"⚠️ تنبيه نفاد المخزون!\n\n"
            f"🌍 الدولة: {country_name} ({country_code})\n"
            f"📱 الخدمة: {service_id}\n\n"
            f"لا توجد أرقام متاحة لهذه الدولة.\n"
            f"يرجى إضافة أرقام جديدة."
        )
        
        await bot.send_message(ADMIN_ID, message)
        logger.info(f"Sent low stock notification for {country_name} ({country_code})")
    except Exception as e:
        logger.error(f"Failed to send low stock notification: {e}")

async def check_and_notify_empty_countries():
    """Check for countries with no available numbers and notify admin"""
    db = get_db()
    try:
        # Get all service-country combinations with their available number counts
        countries_with_zero = db.query(ServiceCountry).filter(
            ServiceCountry.active == True
        ).all()
        
        for service_country in countries_with_zero:
            available_count = db.query(Number).filter(
                Number.service_id == service_country.service_id,
                Number.country_code == service_country.country_code,
                Number.status == NumberStatus.AVAILABLE
            ).count()
            
            if available_count == 0:
                # Check if we already notified recently (within last hour)
                # This is a simple check to avoid spam notifications
                await notify_admin_low_stock(
                    int(service_country.service_id), 
                    str(service_country.country_code), 
                    str(service_country.country_name)
                )
                
    finally:
        db.close()

def verify_hmac_signature(message_text: str, secret_token: str) -> bool:
    """Verify HMAC signature in message"""
    try:
        # Expected format: "to:+1234567890 code:123456 ts:1640000000 hmac:abcdef123456"
        hmac_match = re.search(r'hmac:([a-fA-F0-9]+)', message_text)
        ts_match = re.search(r'ts:(\d+)', message_text)
        
        if not hmac_match or not ts_match:
            return False
        
        received_hmac = hmac_match.group(1)
        timestamp = int(ts_match.group(1))
        
        # Check timestamp window (5 minutes)
        current_time = int(time.time())
        if abs(current_time - timestamp) > MESSAGE_TIMESTAMP_WINDOW_MIN * 60:
            return False
        
        # Extract payload for HMAC calculation
        number_match = re.search(r'to:(\+\d+)', message_text)
        code_match = re.search(r'code:(\d+)', message_text)
        
        if not number_match or not code_match:
            return False
        
        number = number_match.group(1)
        code = code_match.group(1)
        
        # Calculate expected HMAC
        payload = f"{number}|{code}|{timestamp}"
        expected_hmac = hmac.new(
            secret_token.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(expected_hmac, received_hmac)
    
    except Exception as e:
        logger.error(f"HMAC verification error: {e}")
        return False

def format_sms_message(phone_number: str, code: str) -> str:
    """Format SMS message in 'to: code:' format"""
    try:
        normalized_phone = normalize_phone_number(phone_number)
        return f"to: {normalized_phone}\ncode: {code}"
    except Exception as e:
        logger.error(f"Error formatting SMS message: {e}")
        return f"to: {phone_number}\ncode: {code}"

def create_example_sms_message(service_name: str = "Example", phone_number: str = "+1234567890", code: str = "123456") -> str:
    """Create an example SMS message for group demonstration"""
    formatted = format_sms_message(phone_number, code)
    return f"📱 {service_name} SMS:\n{formatted}"

async def send_formatted_sms_to_group(group_chat_id: str, phone_number: str, code: str, service_name: str = "SMS"):
    """Send formatted SMS message to a group"""
    try:
        formatted_message = create_example_sms_message(service_name, phone_number, code)
        await bot.send_message(
            chat_id=group_chat_id,
            text=formatted_message
        )
        return True
    except Exception as e:
        logger.error(f"Error sending formatted SMS to group {group_chat_id}: {e}")
        return False

def test_extract_number_and_code():
    """Test function for number and code extraction"""
    test_cases = [
        "to:+20112763404 code:123456",
        "to: +20112763404 code: 123456", 
        "TO:+20112763404 CODE:123456",
        "message to:+20112763404 code:654321 end",
        "استلمت رمز to:+971501234567 code:789012 للتحقق"
    ]
    
    print("Testing extract_number_and_code function:")
    for i, test_msg in enumerate(test_cases, 1):
        number, code = extract_number_and_code(test_msg, r'\d{6}')
        print(f"Test {i}: '{test_msg}' -> number={number}, code={code}")
    
    return True

def extract_number_and_code(message_text: str, regex_pattern: str) -> tuple[Optional[str], Optional[str]]:
    """Extract phone number and code from message text in format: to:+20112763404 code:123456"""
    try:
        # Extract number from 'to:' format (with or without spaces)
        number_match = re.search(r'to:\s*(\+\d+)', message_text, re.IGNORECASE)
        number = normalize_phone_number(number_match.group(1)) if number_match else None
        
        # Extract code from 'code:' format (with or without spaces)
        code_match = re.search(r'code:\s*(\d+)', message_text, re.IGNORECASE)
        if code_match:
            code = code_match.group(1)
        else:
            # Fallback to service-specific regex pattern
            code_match = re.search(regex_pattern, message_text)
            code = code_match.group() if code_match else None
        
        # Log for debugging
        if number and code:
            logger.info(f"Successfully extracted from '{message_text}': number={number}, code={code}")
        else:
            logger.warning(f"Failed to extract from '{message_text}': number={number}, code={code}")
        
        return number, code
    except Exception as e:
        logger.error(f"Error extracting number and code from '{message_text}': {e}")
        return None, None

async def is_user_admin_in_chat(user_id: int, chat_id: str) -> bool:
    """Check if user is admin in the chat"""
    try:
        chat_member = await bot.get_chat_member(chat_id, user_id)
        return chat_member.status in ['administrator', 'creator']
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return False

async def extract_code_from_message(text: str, service_name: str) -> Optional[str]:
    """Extract OTP code from message text based on service regex"""
    db = get_db()
    try:
        service = db.query(Service).filter(Service.name == service_name).first()
        if not service:
            return None
        
        # Get regex pattern for this service
        mapping = db.query(ServiceProviderMap).filter(ServiceProviderMap.service_id == service.id).first()
        if not mapping:
            # Default regex for common OTP patterns
            pattern = r'\b\d{5,6}\b'
        else:
            pattern = str(mapping.regex_pattern)
        
        match = re.search(pattern, text)
        return match.group() if match else None
    finally:
        db.close()

async def create_main_keyboard(user_id: str = None) -> InlineKeyboardMarkup:
    """Create main menu keyboard"""
    keyboard = InlineKeyboardBuilder()
    
    # Get user language
    lang_code = 'ar'  # Default to Arabic
    if user_id:
        lang_code = get_user_language(user_id)
    
    # Get active services
    db = get_db()
    try:
        services = db.query(Service).filter(Service.active == True).all()
        
        # Add service buttons (2 per row)
        for i in range(0, len(services), 2):
            row = []
            for j in range(2):
                if i + j < len(services):
                    service = services[i + j]
                    translated_name = await get_text(service.name, lang_code)
                    row.append(InlineKeyboardButton(
                        text=f"{service.emoji} {translated_name}",
                        callback_data=f"svc_{service.id}"
                    ))
            keyboard.row(*row)
        
        # Additional buttons with localization
        free_credits_text = t('free_credits', lang_code)
        balance_text = t('my_balance', lang_code)
        
        keyboard.row(
            InlineKeyboardButton(text=free_credits_text, callback_data="free_credits"),
            InlineKeyboardButton(text=balance_text, callback_data="my_balance")
        )
        
        # Show admin button only for admin
        if user_id and (int(user_id) == ADMIN_ID or is_admin_session_valid(int(user_id))):
            keyboard.row(
                InlineKeyboardButton(text=t('help', lang_code), callback_data="help"),
                InlineKeyboardButton(text=t('admin_panel', lang_code), callback_data="admin")
            )
        else:
            keyboard.row(
                InlineKeyboardButton(text=t('help', lang_code), callback_data="help"),
                InlineKeyboardButton(text=t('settings', lang_code), callback_data="settings")
            )
        
        return keyboard.as_markup()
    finally:
        db.close()

def create_countries_keyboard(service_id: int, page: int = 0) -> InlineKeyboardMarkup:
    """Create countries selection keyboard for a service"""
    keyboard = InlineKeyboardBuilder()
    
    db = get_db()
    try:
        # First, get all countries for this service and filter those with available numbers
        all_countries = db.query(ServiceCountry).filter(
            ServiceCountry.service_id == service_id,
            ServiceCountry.active == True
        ).all()
        
        # Filter countries to only include those with available numbers
        countries_with_numbers = []
        for country in all_countries:
            # Count available numbers for this country and service
            available_count = db.query(Number).filter(
                Number.service_id == service_id,
                Number.country_code == country.country_code,
                Number.status == NumberStatus.AVAILABLE
            ).count()
            
            # Only include countries with available numbers
            if available_count > 0:
                countries_with_numbers.append((country, available_count))
        
        # Sort countries by name for consistent display
        countries_with_numbers.sort(key=lambda x: x[0].country_name)
        
        # Apply pagination to filtered results
        total_countries_with_numbers = len(countries_with_numbers)
        start_index = page * PAGE_SIZE
        end_index = start_index + PAGE_SIZE
        page_countries = countries_with_numbers[start_index:end_index]
        
        # Create buttons for countries on current page
        for country, available_count in page_countries:
            keyboard.row(InlineKeyboardButton(
                text=f"{country.flag} {country.country_name} (✅ {available_count})",
                callback_data=f"cty_{service_id}_{country.country_code}"
            ))
        
        # Navigation buttons based on filtered results
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="⏮️ السابق", callback_data=f"cty_page_{service_id}_{page-1}"))
        
        if end_index < total_countries_with_numbers:
            nav_buttons.append(InlineKeyboardButton(text="⏭️ التالي", callback_data=f"cty_page_{service_id}_{page+1}"))
        
        if nav_buttons:
            keyboard.row(*nav_buttons)
        
        # Add information about current page
        if total_countries_with_numbers > PAGE_SIZE:
            current_start = start_index + 1
            current_end = min(end_index, total_countries_with_numbers)
            keyboard.row(InlineKeyboardButton(
                text=f"📄 {current_start}-{current_end} من {total_countries_with_numbers}",
                callback_data="no_action"
            ))
        
        keyboard.row(InlineKeyboardButton(text="🔙 الرئيسية", callback_data="main_menu"))
        
        return keyboard.as_markup()
    finally:
        db.close()

def create_service_groups_keyboard() -> InlineKeyboardMarkup:
    """Create service groups management keyboard"""
    keyboard = InlineKeyboardBuilder()
    
    db = get_db()
    try:
        service_groups = db.query(ServiceGroup).join(Service).filter(
            ServiceGroup.active == True,
            Service.active == True
        ).all()
        
        for sg in service_groups:
            status = "✅" if sg.active else "❌"
            keyboard.row(InlineKeyboardButton(
                text=f"{status} {sg.service.emoji} {sg.service.name} - Group: {sg.group_chat_id}",
                callback_data=f"edit_service_group_{sg.id}"
            ))
        
        keyboard.row(
            InlineKeyboardButton(text="➕ ربط خدمة بجروب", callback_data="admin_add_service"),
            InlineKeyboardButton(text="📊 إحصائيات الرسائل", callback_data="admin_messages_stats")
        )
        keyboard.row(InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin"))
        
        return keyboard.as_markup()
    finally:
        db.close()

def create_number_action_keyboard(reservation_id: int) -> InlineKeyboardMarkup:
    """Create keyboard for number actions"""
    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(text="🔄 تغيير الرقم", callback_data=f"change_number_{reservation_id}"),
        InlineKeyboardButton(text="🌍 تغيير الدولة", callback_data=f"change_country_{reservation_id}")
    )
    keyboard.row(InlineKeyboardButton(text="🔙 الرئيسية", callback_data="main_menu"))
    return keyboard.as_markup()

def create_admin_keyboard() -> InlineKeyboardMarkup:
    """Create admin panel keyboard"""
    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(text="🛠 إدارة الخدمات", callback_data="admin_services"),
        InlineKeyboardButton(text="🌍 إدارة الدول", callback_data="admin_countries")
    )
    keyboard.row(
        InlineKeyboardButton(text="📱 إدارة الأرقام", callback_data="admin_numbers"),
        InlineKeyboardButton(text="🔗 إدارة الجروبات", callback_data="admin_service_groups")
    )
    keyboard.row(
        InlineKeyboardButton(text="👥 إدارة المستخدمين", callback_data="admin_users"),
        InlineKeyboardButton(text="📢 إدارة القنوات", callback_data="admin_channels")
    )
    keyboard.row(
        InlineKeyboardButton(text="💰 شحن رصيد", callback_data="admin_add_balance"),
        InlineKeyboardButton(text="💳 خصم رصيد", callback_data="admin_deduct_balance")
    )
    keyboard.row(
        InlineKeyboardButton(text="📢 رسالة جماعية", callback_data="admin_broadcast"),
        InlineKeyboardButton(text="💬 رسالة خاصة", callback_data="admin_private_message")
    )
    keyboard.row(
        InlineKeyboardButton(text="📦 المخزون", callback_data="admin_inventory"),
        InlineKeyboardButton(text="📊 الإحصائيات", callback_data="admin_stats")
    )
    keyboard.row(
        InlineKeyboardButton(text="⚙️ الإعدادات", callback_data="admin_settings"),
        InlineKeyboardButton(text="🔧 وضع الصيانة", callback_data="admin_maintenance")
    )
    keyboard.row(InlineKeyboardButton(text="🔙 الرئيسية", callback_data="main_menu"))
    return keyboard.as_markup()

async def reserve_number(user_id: int, service_id: int, country_code: str) -> Optional[Reservation]:
    """Reserve a number for user"""
    db = get_db()
    try:
        # Find available number
        available_number = db.query(Number).filter(
            Number.service_id == service_id,
            Number.country_code == country_code,
            Number.status == NumberStatus.AVAILABLE
        ).first()
        
        if not available_number:
            return None
        
        # Create reservation
        expires_at = datetime.now() + timedelta(minutes=RESERVATION_TIMEOUT_MIN)
        reservation = Reservation(
            user_id=user_id,
            service_id=service_id,
            number_id=available_number.id,
            status=ReservationStatus.WAITING_CODE,
            expired_at=expires_at
        )
        
        # Update number status
        available_number.status = NumberStatus.RESERVED
        available_number.reserved_by_user_id = user_id
        available_number.reserved_at = datetime.now()
        available_number.expires_at = expires_at
        
        db.add(reservation)
        db.commit()
        db.refresh(reservation)
        
        return reservation
    finally:
        db.close()

async def complete_reservation_atomic(reservation_id: int, code: str) -> bool:
    """Complete reservation atomically with proper transaction handling"""
    db = get_db()
    try:
        # Begin transaction
        db.begin()
        
        # Lock the reservation for update
        reservation = db.query(Reservation).filter(
            Reservation.id == reservation_id
        ).with_for_update().first()
        
        if not reservation or reservation.status != ReservationStatus.WAITING_CODE:
            db.rollback()
            return False
        
        # Lock related records
        user = db.query(User).filter(
            User.id == reservation.user_id
        ).with_for_update().first()
        
        service = db.query(Service).filter(
            Service.id == reservation.service_id
        ).first()
        
        number = db.query(Number).filter(
            Number.id == reservation.number_id
        ).with_for_update().first()
        
        if not user or not service or not number:
            db.rollback()
            return False
        
        # Calculate price
        price = float(number.price_override or service.default_price)
        
        # Check if user has enough balance
        if float(user.balance or 0) < float(price):
            # Mark reservation as failed due to insufficient balance
            reservation.status = ReservationStatus.EXPIRED
            db.commit()
            
            await bot.send_message(
                str(user.telegram_id),
                f"❌ رصيدك غير كافي!\nالسعر المطلوب: {price}\nرصيدك الحالي: {user.balance}"
            )
            return False
        
        # Complete the transaction atomically
        user.balance = float(user.balance or 0) - price
        reservation.status = ReservationStatus.COMPLETED
        reservation.code_value = code
        reservation.completed_at = datetime.now()
        number.status = NumberStatus.USED
        number.code_received_at = datetime.now()
        
        # Create transaction record
        transaction = Transaction(
            user_id=user.id,
            type=TransactionType.PURCHASE,
            amount=price,
            reason=f"{service.name} {number.phone_number}"
        )
        db.add(transaction)
        
        # Check if this was the last available number for this country/service before committing
        remaining_numbers = db.query(Number).filter(
            Number.service_id == reservation.service_id,
            Number.country_code == number.country_code,
            Number.status == NumberStatus.AVAILABLE,
            Number.id != number.id  # Exclude the current number being used
        ).count()
        
        # Commit all changes
        db.commit()
        
        # Format message with new style
        sms_formatted = format_sms_message(str(number.phone_number), code)
        
        # Notify user
        await bot.send_message(
            str(user.telegram_id),
            f"🎉 وصل الكود!\n\n"
            f"```\n{sms_formatted}\n```\n\n"
            f"تم خصم {price} من رصيدك\n"
            f"رصيدك الحالي: {user.balance}",
            parse_mode="Markdown"
        )
        
        # Check if we need to notify admin about empty stock
        if remaining_numbers == 0:
            # Get country name for notification
            country_name, _ = get_country_name_and_flag(str(number.country_code))
            await notify_admin_low_stock(int(reservation.service_id), str(number.country_code), country_name)
        
        return True
        
    except Exception as e:
        logger.error(f"Error completing reservation atomically: {e}")
        db.rollback()
        return False
    finally:
        db.close()

async def poll_provider_messages():
    """Poll provider APIs for new messages"""
    while True:
        try:
            db = get_db()
            try:
                # Get active providers
                providers = db.query(Provider).filter(
                    Provider.active == True,
                    Provider.mode == ProviderMode.POLL
                ).all()
                
                for provider in providers:
                    try:
                        await process_provider_messages(provider)
                    except Exception as e:
                        logger.error(f"Error processing provider {provider.name}: {e}")
                
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"Error in polling loop: {e}")
        
        await asyncio.sleep(POLL_INTERVAL_SEC)

async def process_provider_messages(provider: Provider):
    """Process messages from a specific provider"""
    try:
        timeout = aiohttp.ClientTimeout(total=PROVIDER_API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            headers = {"Authorization": f"Bearer {provider.api_key}"}
            async with session.get(f"{provider.base_url}/messages", headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    messages = data.get('messages', [])
                    
                    for msg in messages:
                        await process_single_message(provider, msg)
                        
    except Exception as e:
        logger.error(f"Error fetching messages from {provider.name}: {e}")

async def process_single_message(provider: Provider, message: Dict[str, Any]):
    """Process a single message from provider"""
    db = get_db()
    try:
        to_number = normalize_phone_number(message.get('to', ''))
        text = message.get('text', '')
        service_name = message.get('service', '')
        
        # Try to infer service from text if not provided
        if not service_name:
            # Basic service inference (can be improved)
            if 'whatsapp' in text.lower():
                service_name = 'WhatsApp'
            elif 'telegram' in text.lower():
                service_name = 'Telegram'
            # Add more service inference logic here
        
        # Extract code
        code = await extract_code_from_message(text, service_name)
        if not code:
            return
        
        # Find matching reservation
        service = db.query(Service).filter(Service.name == service_name).first()
        if not service:
            return
        
        number = db.query(Number).filter(
            Number.phone_number == to_number,
            Number.service_id == service.id,
            Number.status == NumberStatus.RESERVED
        ).first()
        
        if not number:
            return
        
        reservation = db.query(Reservation).filter(
            Reservation.number_id == number.id,
            Reservation.status == ReservationStatus.WAITING_CODE
        ).first()
        
        if not reservation:
            return
        
        # Store message
        provider_msg = ProviderMessage(
            provider_id=provider.id,
            raw_payload=json.dumps(message)
        )
        db.add(provider_msg)
        db.commit()
        
        # Complete reservation
        await complete_reservation_atomic(reservation.id, code)
        
    finally:
        db.close()

async def check_expired_reservations():
    """Check and expire old reservations"""
    while True:
        try:
            db = get_db()
            try:
                now = datetime.now()
                expired_reservations = db.query(Reservation).filter(
                    Reservation.status == ReservationStatus.WAITING_CODE,
                    Reservation.expired_at < now
                ).all()
                
                for reservation in expired_reservations:
                    # Mark as expired
                    reservation.status = ReservationStatus.EXPIRED
                    
                    # Return number to available
                    number = db.query(Number).filter(Number.id == reservation.number_id).first()
                    if number:
                        number.status = NumberStatus.AVAILABLE
                        number.reserved_by_user_id = None
                        number.reserved_at = None
                        number.expires_at = None
                    
                    # Notify user
                    user = db.query(User).filter(User.id == reservation.user_id).first()
                    if user:
                        keyboard = InlineKeyboardBuilder()
                        keyboard.row(InlineKeyboardButton(text="🔄 احجز رقم جديد", callback_data="main_menu"))
                        
                        await bot.send_message(
                            user.telegram_id,
                            "⏰ انتهت مهلة انتظار الكود\n"
                            "لم يتم خصم أي رسوم من رصيدك\n"
                            "يمكنك حجز رقم جديد",
                            reply_markup=keyboard.as_markup()
                        )
                
                db.commit()
                
            finally:
                db.close()
        
        except Exception as e:
            logger.error(f"Error checking expired reservations: {e}")
        
        await asyncio.sleep(60)  # Check every minute

# Admin handlers for service group management
@dp.callback_query(F.data == "admin_add_service")
async def admin_add_service_handler(callback: CallbackQuery, state: FSMContext):
    """Handle adding new service"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    await state.set_state(AdminStates.waiting_for_service_name)
    await callback.message.edit_text(
        "📝 إضافة خدمة جديدة\n\n"
        "أدخل اسم الخدمة (مثل: WhatsApp, Telegram, Instagram):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin_services")
        ]])
    )

@dp.message(StateFilter(AdminStates.waiting_for_service_name))
async def process_service_name(message: types.Message, state: FSMContext):
    """Process service name input"""
    if not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        return
    
    service_name = message.text.strip()
    if not service_name:
        await message.reply("❌ يرجى إدخال اسم صحيح للخدمة")
        return
    
    await state.update_data(service_name=service_name)
    await state.set_state(AdminStates.waiting_for_service_emoji)
    await message.reply(
        f"✅ اسم الخدمة: {service_name}\n\n"
        "أدخل الإيموجي للخدمة (مثل: 📱, 💬, 📸):"
    )

@dp.message(StateFilter(AdminStates.waiting_for_service_emoji))
async def process_service_emoji(message: types.Message, state: FSMContext):
    """Process service emoji input"""
    if not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        return
    
    emoji = message.text.strip()
    if not emoji:
        emoji = "📱"  # Default emoji
    
    await state.update_data(service_emoji=emoji)
    await state.set_state(AdminStates.waiting_for_service_price)
    await message.reply(
        f"✅ الإيموجي: {emoji}\n\n"
        "أدخل السعر الافتراضي للخدمة (بالوحدات):"
    )

@dp.message(StateFilter(AdminStates.waiting_for_service_price))
async def process_service_price(message: types.Message, state: FSMContext):
    """Process service price input"""
    if not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        return
    
    try:
        price = float(message.text.strip())
        if price < 0:
            await message.reply("❌ السعر يجب أن يكون رقم موجب")
            return
    except ValueError:
        await message.reply("❌ يرجى إدخال رقم صحيح للسعر")
        return
    
    await state.update_data(service_price=price)
    await state.set_state(AdminStates.waiting_for_service_description)
    await message.reply(
        f"✅ السعر: {price} وحدة\n\n"
        "أدخل وصف الخدمة (اختياري - أرسل 'تخطي' للتخطي):"
    )

@dp.message(StateFilter(AdminStates.waiting_for_service_description))
async def process_service_description(message: types.Message, state: FSMContext):
    """Process service description input"""
    if not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        return
    
    description = message.text.strip() if message.text.strip().lower() != 'تخطي' else None
    
    await state.update_data(service_description=description)
    await state.set_state(AdminStates.waiting_for_service_regex)
    await message.reply(
        "📝 أدخل نمط Regex لاستخراج الكود من الرسائل\n\n"
        "أمثلة:\n"
        "• للأكواد من 4-6 أرقام: \\b\\d{4,6}\\b\n"
        "• للأكواد من 5 أرقام فقط: \\b\\d{5}\\b\n"
        "• أرسل 'افتراضي' لاستخدام النمط الافتراضي:"
    )

@dp.message(StateFilter(AdminStates.waiting_for_service_regex))
async def process_service_regex(message: types.Message, state: FSMContext):
    """Process service regex pattern input"""
    if not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        return
    
    regex_pattern = message.text.strip()
    if regex_pattern.lower() == 'افتراضي' or not regex_pattern:
        regex_pattern = r'\\b\\d{4,6}\\b'
    
    # Test regex pattern
    try:
        re.compile(regex_pattern)
    except re.error:
        await message.reply("❌ نمط Regex غير صحيح، يرجى المحاولة مرة أخرى")
        return
    
    await state.update_data(service_regex=regex_pattern)
    await state.set_state(AdminStates.waiting_for_service_group_id)
    await message.reply(
        f"✅ نمط Regex: {regex_pattern}\n\n"
        "📞 أدخل Group ID للجروب/القناة التي ستستقبل الرسائل\n\n"
        "مثال: -1001234567890\n"
        "💡 لمعرفة Group ID، أضف البوت للجروب واستخدم الأمر /chatinfo"
    )

@dp.message(StateFilter(AdminStates.waiting_for_service_group_id))
async def process_service_group_id(message: types.Message, state: FSMContext):
    """Process service group ID input"""
    if not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        return
    
    group_id = message.text.strip()
    
    # Validate group ID format
    try:
        int(group_id)
    except ValueError:
        await message.reply("❌ Group ID يجب أن يكون رقم صحيح")
        return
    
    await state.update_data(service_group_id=group_id)
    await state.set_state(AdminStates.waiting_for_service_secret_token)
    await message.reply(
        f"✅ Group ID: {group_id}\n\n"
        "🔐 أدخل التوكن السري للتحقق من الرسائل\n"
        "(اختياري - أرسل 'تخطي' للتخطي):"
    )

@dp.message(StateFilter(AdminStates.waiting_for_service_secret_token))
async def process_service_secret_token(message: types.Message, state: FSMContext):
    """Process service secret token input"""
    if not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        return
    
    secret_token = message.text.strip() if message.text.strip().lower() != 'تخطي' else None
    
    await state.update_data(service_secret_token=secret_token)
    await state.set_state(AdminStates.waiting_for_service_security_mode)
    
    # Create security mode selection keyboard
    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(text="🔑 Token Only", callback_data="security_token_only"),
        InlineKeyboardButton(text="👑 Admin Only", callback_data="security_admin_only")
    )
    keyboard.row(InlineKeyboardButton(text="🔐 HMAC", callback_data="security_hmac"))
    
    await message.reply(
        f"✅ التوكن السري: {'✅ محدد' if secret_token else '❌ غير محدد'}\n\n"
        "🛡️ اختر وضع الأمان:\n\n"
        "🔑 Token Only: التحقق من التوكن فقط\n"
        "👑 Admin Only: قبول الرسائل من المشرفين فقط\n"
        "🔐 HMAC: تشفير متقدم مع HMAC",
        reply_markup=keyboard.as_markup()
    )

@dp.callback_query(F.data.startswith("security_"))
async def process_security_mode(callback: CallbackQuery, state: FSMContext):
    """Process security mode selection"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    security_mode = callback.data.replace("security_", "")
    security_mode_map = {
        "token_only": SecurityMode.TOKEN_ONLY,
        "admin_only": SecurityMode.ADMIN_ONLY,
        "hmac": SecurityMode.HMAC
    }
    
    selected_mode = security_mode_map.get(security_mode, SecurityMode.TOKEN_ONLY)
    
    # Get all data and create service
    data = await state.get_data()
    
    db = get_db()
    try:
        # Create service
        service = Service(
            name=data['service_name'],
            emoji=data['service_emoji'],
            description=data.get('service_description'),
            default_price=data['service_price'],
            active=True
        )
        db.add(service)
        db.flush()  # Get service ID
        
        # Create service group mapping
        service_group = ServiceGroup(
            service_id=service.id,
            group_chat_id=data['service_group_id'],
            secret_token=data.get('service_secret_token'),
            regex_pattern=data['service_regex'],
            security_mode=selected_mode,
            active=True
        )
        db.add(service_group)
        db.commit()
        
        await state.clear()
        
        # Show summary
        security_mode_text = {
            SecurityMode.TOKEN_ONLY: "🔑 Token Only",
            SecurityMode.ADMIN_ONLY: "👑 Admin Only",
            SecurityMode.HMAC: "🔐 HMAC"
        }
        
        await callback.message.edit_text(
            f"✅ تم إنشاء الخدمة بنجاح!\n\n"
            f"📱 الاسم: {service.name}\n"
            f"🎨 الإيموجي: {service.emoji}\n"
            f"💰 السعر: {service.default_price} وحدة\n"
            f"📞 Group ID: {service_group.group_chat_id}\n"
            f"🔍 Regex: {service_group.regex_pattern}\n"
            f"🛡️ وضع الأمان: {security_mode_text[selected_mode]}\n"
            f"🔐 التوكن: {'✅ محدد' if service_group.secret_token else '❌ غير محدد'}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔗 اختبار الجروب", callback_data=f"test_group_{service.id}"),
                InlineKeyboardButton(text="🔙 إدارة الخدمات", callback_data="admin_services")
            ]])
        )
        
    except Exception as e:
        logger.error(f"Error creating service: {e}")
        await callback.message.edit_text(
            f"❌ خطأ في إنشاء الخدمة: {str(e)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 المحاولة مرة أخرى", callback_data="admin_add_service")
            ]])
        )
        db.rollback()
    finally:
        db.close()

@dp.callback_query(F.data.startswith("test_group_"))
async def test_group_handler(callback: CallbackQuery):
    """Test group connectivity"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    service_id = int(callback.data.split("_")[2])
    
    db = get_db()
    try:
        service_group = db.query(ServiceGroup).filter(
            ServiceGroup.service_id == service_id
        ).first()
        
        if not service_group:
            await callback.answer("❌ لم يتم العثور على الجروب")
            return
        
        try:
            # Try to get chat info
            chat = await bot.get_chat(str(service_group.group_chat_id))
            
            # Try to get bot member status
            bot_member = await bot.get_chat_member(str(service_group.group_chat_id), bot.id)
            
            status_text = {
                'creator': '👑 المؤسس',
                'administrator': '👮‍♂️ مشرف',
                'member': '👤 عضو',
                'restricted': '🚫 مقيد',
                'left': '❌ غير موجود',
                'kicked': '🚫 محظور'
            }
            
            await callback.message.edit_text(
                f"🔍 نتائج اختبار الجروب\n\n"
                f"📞 Group ID: {service_group.group_chat_id}\n"
                f"📝 اسم الجروب: {chat.title or 'غير محدد'}\n"
                f"👥 نوع الجروب: {chat.type}\n"
                f"🤖 حالة البوت: {status_text.get(bot_member.status, bot_member.status)}\n\n"
                "✅ الاتصال بالجروب ناجح!",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔙 إدارة الخدمات", callback_data="admin_services")
                ]])
            )
            
        except Exception as e:
            await callback.message.edit_text(
                f"❌ فشل في الاتصال بالجروب\n\n"
                f"📞 Group ID: {service_group.group_chat_id}\n"
                f"❗ الخطأ: {str(e)}\n\n"
                "تأكد من:\n"
                "• البوت عضو في الجروب\n"
                "• Group ID صحيح\n"
                "• البوت لديه صلاحيات قراءة الرسائل",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔙 إدارة الخدمات", callback_data="admin_services")
                ]])
            )
    finally:
        db.close()

# Command to get chat info (helpful for admins)
@dp.message(Command("chatinfo"))
async def chatinfo_handler(message: types.Message):
    """Get chat information"""
    if not message.chat:
        return
    
    if message.chat.type == 'private':
        await message.reply("هذا الأمر يعمل فقط في الجروبات والقنوات")
        return
    
    # Check if user is admin
    try:
        chat_member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if chat_member.status not in ['creator', 'administrator']:
            await message.reply("هذا الأمر متاح للمشرفين فقط")
            return
    except:
        await message.reply("لا يمكن التحقق من صلاحياتك")
        return
    
    chat_info = (
        f"📊 معلومات الدردشة\n\n"
        f"🆔 Chat ID: `{message.chat.id}`\n"
        f"📝 الاسم: {message.chat.title or 'غير محدد'}\n"
        f"👥 النوع: {message.chat.type}\n"
        f"👤 اليوزر: @{message.chat.username or 'غير محدد'}"
    )
    
    await message.reply(chat_info, parse_mode="Markdown")

@dp.callback_query(F.data == "admin_service_groups")
async def admin_service_groups_handler(callback: CallbackQuery):
    """Handle service groups management"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        service_groups = db.query(ServiceGroup).join(Service).all()
        
        text = "🔗 إدارة ربط الخدمات بالجروبات\n\n"
        
        if service_groups:
            text += "الروابط الحالية:\n"
            for sg in service_groups:
                status = "✅" if sg.active else "❌"
                security_icon = {
                    SecurityMode.TOKEN_ONLY: "🔑",
                    SecurityMode.ADMIN_ONLY: "👑",
                    SecurityMode.HMAC: "🔐"
                }.get(sg.security_mode, "🔑")
                
                text += f"{status} {sg.service.emoji} {sg.service.name}\n"
                text += f"   📞 {sg.group_chat_id} {security_icon}\n\n"
        else:
            text += "لا توجد روابط محددة\n"
        
        keyboard = InlineKeyboardBuilder()
        
        for sg in service_groups:
            status = "✅" if sg.active else "❌"
            security_icon = {
                SecurityMode.TOKEN_ONLY: "🔑",
                SecurityMode.ADMIN_ONLY: "👑", 
                SecurityMode.HMAC: "🔐"
            }.get(sg.security_mode, "🔑")
            
            # Check if bot is admin in the group
            bot_status = await verify_bot_in_group(sg.group_chat_id)
            bot_icon = "🤖✅" if bot_status else "🤖❌"
            
            keyboard.row(InlineKeyboardButton(
                text=f"{status} {sg.service.emoji} {sg.service.name} - {sg.group_chat_id} {security_icon} {bot_icon}",
                callback_data=f"edit_service_group_{sg.id}"
            ))
        
        keyboard.row(
            InlineKeyboardButton(text="➕ ربط خدمة بجروب", callback_data="admin_add_service"),
            InlineKeyboardButton(text="📊 إحصائيات الرسائل", callback_data="admin_messages_stats")
        )
        keyboard.row(InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_messages_stats")
async def admin_messages_stats_handler(callback: CallbackQuery):
    """Handle messages statistics"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        # Get message statistics
        total_messages = db.query(ProviderMessage).count()
        processed_messages = db.query(ProviderMessage).filter(
            ProviderMessage.status == MessageStatus.PROCESSED
        ).count()
        rejected_messages = db.query(ProviderMessage).filter(
            ProviderMessage.status == MessageStatus.REJECTED
        ).count()
        orphan_messages = db.query(ProviderMessage).filter(
            ProviderMessage.status == MessageStatus.ORPHAN
        ).count()
        blocked_messages = db.query(BlockedMessage).count()
        
        # Get recent completed reservations
        recent_completions = db.query(Reservation).filter(
            Reservation.status == ReservationStatus.COMPLETED
        ).order_by(Reservation.completed_at.desc()).limit(5).all()
        
        text = f"📊 إحصائيات الرسائل\n\n"
        text += f"📬 إجمالي الرسائل: {total_messages}\n"
        text += f"✅ معالجة: {processed_messages}\n"
        text += f"❌ مرفوضة: {rejected_messages}\n"
        text += f"🔶 يتيمة: {orphan_messages}\n"
        text += f"🚫 محظورة: {blocked_messages}\n\n"
        
        if recent_completions:
            text += "🎉 آخر الإنجازات:\n"
            for res in recent_completions:
                service = db.query(Service).filter(Service.id == res.service_id).first()
                number = db.query(Number).filter(Number.id == res.number_id).first()
                if service and number:
                    text += f"• {service.emoji} {service.name} - {number.phone_number}\n"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="🗑️ تنظيف الرسائل القديمة", callback_data="admin_cleanup_messages"),
            InlineKeyboardButton(text="🔄 تحديث", callback_data="admin_messages_stats")
        )
        keyboard.row(InlineKeyboardButton(text="🔙 إدارة الخدمات", callback_data="admin_services"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_cleanup_messages")
async def admin_cleanup_messages_handler(callback: CallbackQuery):
    """Cleanup old messages"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        # Delete messages older than 7 days
        cutoff_date = datetime.now() - timedelta(days=7)
        
        deleted_provider = db.query(ProviderMessage).filter(
            ProviderMessage.received_at < cutoff_date
        ).delete()
        
        deleted_blocked = db.query(BlockedMessage).filter(
            BlockedMessage.created_at < cutoff_date
        ).delete()
        
        db.commit()
        
        await callback.answer(
            f"✅ تم حذف {deleted_provider + deleted_blocked} رسالة قديمة",
            show_alert=True
        )
        
        # Refresh the stats
        await admin_messages_stats_handler(callback)
        
    except Exception as e:
        logger.error(f"Error cleaning up messages: {e}")
        await callback.answer(f"❌ خطأ في التنظيف: {str(e)}")
        db.rollback()
    finally:
        db.close()

# Group message processing functions
async def process_incoming_group_message(message: types.Message):
    """Process incoming message from a registered group"""
    if not message.chat or not message.from_user or not message.text:
        return
    
    group_chat_id = str(message.chat.id)
    sender_id = str(message.from_user.id)
    message_text = message.text
    
    db = get_db()
    try:
        # Find service group mapping
        service_group = db.query(ServiceGroup).filter(
            ServiceGroup.group_chat_id == group_chat_id,
            ServiceGroup.active == True
        ).first()
        
        if not service_group:
            logger.info(f"Message from unregistered group: {group_chat_id}")
            return  # Not a registered group
            
        logger.info(f"Processing message from group: {group_chat_id}, service_id: {service_group.service_id}, service: {service_group.service.name if service_group.service else 'Unknown'}")
        
        # Store incoming message for audit
        provider_msg = ProviderMessage(
            service_id=service_group.service_id,
            group_chat_id=group_chat_id,
            sender_id=sender_id,
            message_text=message_text,
            raw_payload=json.dumps({
                'message_id': message.message_id,
                'chat_title': message.chat.title,
                'sender_username': message.from_user.username,
                'date': message.date.isoformat() if message.date else None
            }),
            status=MessageStatus.PENDING
        )
        db.add(provider_msg)
        db.commit()
        
        # Security checks
        security_check_result = await verify_message_security(
            service_group, message_text, sender_id, group_chat_id
        )
        
        if not security_check_result['valid']:
            # Store as blocked message
            blocked_msg = BlockedMessage(
                service_id=service_group.service_id,
                group_chat_id=group_chat_id,
                sender_id=sender_id,
                message_text=message_text,
                reason=security_check_result['reason']
            )
            db.add(blocked_msg)
            
            # Update provider message status
            provider_msg.status = MessageStatus.REJECTED
            db.commit()
            return
        
        # Extract number and code
        number, code = extract_number_and_code(message_text, service_group.regex_pattern)
        
        if not number or not code:
            # Store as blocked - no valid number or code found
            blocked_msg = BlockedMessage(
                service_id=service_group.service_id,
                group_chat_id=group_chat_id,
                sender_id=sender_id,
                message_text=message_text,
                reason="no_number_or_no_code"
            )
            db.add(blocked_msg)
            
            provider_msg.status = MessageStatus.REJECTED
            db.commit()
            return
        
        # Find matching reservation with detailed logging
        logger.info(f"Searching for reservation: number={number}, service_id={service_group.service_id}")
        
        # First check if the number exists
        number_obj = db.query(Number).filter(
            Number.phone_number == number,
            Number.service_id == service_group.service_id
        ).first()
        
        if not number_obj:
            logger.warning(f"Number {number} not found for service_id {service_group.service_id}")
            provider_msg.status = MessageStatus.ORPHAN
            db.commit()
            return
        
        logger.info(f"Found number: id={number_obj.id}, status={number_obj.status}, reserved_by={number_obj.reserved_by_user_id}")
        
        # Check for reservation
        reservation = db.query(Reservation).filter(
            Reservation.number_id == number_obj.id,
            Reservation.status == ReservationStatus.WAITING_CODE
        ).first()
        
        if not reservation:
            # Log more details about why no reservation found
            all_reservations = db.query(Reservation).filter(
                Reservation.number_id == number_obj.id
            ).all()
            logger.warning(f"No WAITING_CODE reservation found for number {number}")
            for res in all_reservations:
                logger.info(f"Found reservation: id={res.id}, status={res.status}, user_id={res.user_id}")
            
            # Mark as orphan - no matching reservation
            provider_msg.status = MessageStatus.ORPHAN
            db.commit()
            return
            
        logger.info(f"Found matching reservation: id={reservation.id}, user_id={reservation.user_id}, status={reservation.status}")
        
        # Complete reservation atomically
        success = await complete_reservation_atomic(reservation.id, code)
        
        if success:
            provider_msg.status = MessageStatus.PROCESSED
            provider_msg.processed_at = datetime.now()
        else:
            provider_msg.status = MessageStatus.REJECTED
            blocked_msg = BlockedMessage(
                service_id=service_group.service_id,
                group_chat_id=group_chat_id,
                sender_id=sender_id,
                message_text=message_text,
                reason="completion_failed"
            )
            db.add(blocked_msg)
        
        db.commit()
        
    except Exception as e:
        logger.error(f"Error processing group message: {e}")
        db.rollback()
    finally:
        db.close()

async def verify_message_security(service_group: ServiceGroup, message_text: str, sender_id: str, group_chat_id: str) -> Dict[str, Any]:
    """Verify message security based on service group settings - Simplified for single user"""
    try:
        # Simplified security - since user owns the group, accept all messages
        if service_group.security_mode == SecurityMode.TOKEN_ONLY:
            # Token is optional now - accept messages with or without token
            return {'valid': True, 'reason': 'simplified_security'}
            
        # Keep other security modes for flexibility
        
        elif service_group.security_mode == SecurityMode.ADMIN_ONLY:
            is_admin = await is_user_admin_in_chat(int(sender_id), group_chat_id)
            if not is_admin:
                return {'valid': False, 'reason': 'not_admin'}
            return {'valid': True, 'reason': 'admin_verified'}
        
        elif service_group.security_mode == SecurityMode.HMAC:
            if not service_group.secret_token:
                return {'valid': False, 'reason': 'no_secret_configured'}
            
            if not verify_hmac_signature(message_text, service_group.secret_token):
                return {'valid': False, 'reason': 'invalid_hmac'}
            return {'valid': True, 'reason': 'hmac_verified'}
        
        return {'valid': False, 'reason': 'unknown_security_mode'}
    
    except Exception as e:
        logger.error(f"Security verification error: {e}")
        return {'valid': False, 'reason': f'verification_error: {str(e)}'}

# Message handlers for group messages
@dp.message(F.chat.type.in_(['group', 'supergroup']))
async def group_message_handler(message: types.Message):
    """Handle messages from groups"""
    await process_incoming_group_message(message)

# Bot handlers
@dp.message(Command("start"))
async def start_handler(message: types.Message, state: FSMContext):
    """Handle /start command"""
    if maintenance_mode and message.from_user and message.from_user.id != ADMIN_ID:
        await message.reply("🚧 البوت تحت الصيانة حالياً، يرجى المحاولة لاحقاً")
        return
    
    if not message.from_user:
        return
        
    user, is_new_user = await get_or_create_user(
        str(message.from_user.id),
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name
    )
    
    await state.clear()
    
    # If new user or no language set, set default to Arabic and show welcome
    if is_new_user or not user.language_code:
        # Set Arabic as default language for new users
        update_user_language(str(message.from_user.id), 'ar')
        user.language_code = 'ar'
        
        # Update bot commands for Arabic
        await set_bot_commands(bot, 'ar')
        
        # Show welcome with language selection option (in Arabic for new users)
        welcome_text = (
            "🌟 مرحباً! أهلاً بك في بوت الأرقام المؤقتة! 🌟\n\n"
            "📱 احصل على أرقام مؤقتة لتفعيل حساباتك على:\n"
            "• واتساب، تليجرام، فيسبوك، إنستجرام وغيرها\n\n"
            "🌐 يمكنك تغيير اللغة من قائمة الإعدادات\n\n"
            "💰 اختر خدمة للبدء:"
        )
        
        await message.reply(welcome_text, reply_markup=await create_main_keyboard(str(message.from_user.id)))
        return
    
    # Get user's language and show main menu with translation
    lang_code = user.language_code or 'ar'
    
    welcome_text = await translator.translate_text(
        "🌟 أهلاً بك في بوت الأرقام المؤقتة! 🌟\n\n"
        "📱 احصل على أرقام مؤقتة لتفعيل حساباتك على:\n"
        "• واتساب، تليجرام، فيسبوك، إنستجرام وغيرها\n\n"
        "💰 اختر خدمة للبدء:",
        lang_code
    )
    
    await message.reply(welcome_text, reply_markup=await create_main_keyboard(str(message.from_user.id)))

# New Command Handlers
@dp.message(Command("balance"))
async def balance_handler(message: types.Message):
    """Handle /balance command"""
    if not message.from_user:
        return
    
    user, _ = await get_or_create_user(
        str(message.from_user.id),
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name
    )
    
    lang_code = get_user_language(str(message.from_user.id))
    balance_text = await translator.translate_text(f"💰 رصيدك الحالي: {user.balance}", lang_code)
    await message.reply(balance_text)

@dp.message(Command("language"))
async def language_handler(message: types.Message):
    """Handle /language command"""
    if not message.from_user:
        return
    
    keyboard = InlineKeyboardBuilder()
    
    # Add language selection buttons (2 per row)
    lang_items = list(SUPPORTED_LANGUAGES.items())
    for i in range(0, len(lang_items), 2):
        row = []
        for j in range(2):
            if i + j < len(lang_items):
                code, name = lang_items[i + j]
                row.append(InlineKeyboardButton(
                    text=name,
                    callback_data=f"set_lang_{code}"
                ))
        keyboard.row(*row)
    
    # Get current user language for back button
    lang_code = get_user_language(str(message.from_user.id))
    back_text = t('main_menu', lang_code)
    
    keyboard.row(InlineKeyboardButton(text=f"🔙 {back_text}", callback_data="main_menu"))
    
    # Get multilingual text for language selection
    selection_text = "🌐 اختر لغتك المفضلة:\nChoose your preferred language:\nElige tu idioma preferido:"
    
    await message.reply(
        selection_text,
        reply_markup=keyboard.as_markup()
    )

@dp.message(Command("services"))
async def services_handler(message: types.Message):
    """Handle /services command"""
    if not message.from_user:
        return
        
    lang_code = get_user_language(str(message.from_user.id))
    services_text = await translator.translate_text("📱 الخدمات المتاحة:", lang_code)
    
    await message.reply(services_text, reply_markup=await create_main_keyboard(str(message.from_user.id)))

@dp.message(Command("history"))
async def history_handler(message: types.Message):
    """Handle /history command"""
    if not message.from_user:
        return
    
    db = get_db()
    try:
        reservations = db.query(Reservation).filter(
            Reservation.user_id == str(message.from_user.id)
        ).order_by(Reservation.created_at.desc()).limit(10).all()
        
        if not reservations:
            lang_code = get_user_language(str(message.from_user.id))
            no_history_text = await translator.translate_text("📋 لا توجد طلبات سابقة", lang_code)
            await message.reply(no_history_text)
            return
        
        lang_code = get_user_language(str(message.from_user.id))
        history_header = await translator.translate_text("📋 آخر 10 طلبات:", lang_code)
        history_text = f"{history_header}\n\n"
        
        for res in reservations:
            status_emoji = {
                ReservationStatus.WAITING_CODE: "⏳",
                ReservationStatus.COMPLETED: "✅", 
                ReservationStatus.EXPIRED: "⏰",
                ReservationStatus.CANCELED: "❌"
            }.get(res.status, "❓")
            
            service_name = await get_text(res.service.name, lang_code)
            history_text += f"{status_emoji} {service_name} - {res.number.phone_number}\n"
            history_text += f"   📅 {res.created_at.strftime('%Y-%m-%d %H:%M')}\n\n"
        
        await message.reply(history_text)
        
    finally:
        db.close()

@dp.message(Command("support"))
async def support_handler(message: types.Message):
    """Handle /support command"""
    if not message.from_user:
        return
    
    lang_code = get_user_language(str(message.from_user.id))
    support_text = await translator.translate_text(
        "🆘 للدعم الفني تواصل مع:\n"
        f"👨‍💼 المدير: @{ADMIN_ID}\n\n"
        "📧 أو أرسل رسالة مباشرة وسيتم الرد عليك قريباً",
        lang_code
    )
    
    await message.reply(support_text)

@dp.message(Command("cancel"))
async def cancel_handler(message: types.Message, state: FSMContext):
    """Handle /cancel command"""
    if not message.from_user:
        return
    
    await state.clear()
    lang_code = get_user_language(str(message.from_user.id))
    cancel_text = await translator.translate_text("❌ تم إلغاء العملية الحالية", lang_code)
    
    await message.reply(cancel_text, reply_markup=await create_main_keyboard(str(message.from_user.id)))

@dp.message(Command("chatinfo"))
async def chatinfo_handler(message: types.Message):
    """Handle /chatinfo command - useful for getting group ID"""
    lang_code = get_user_language(str(message.from_user.id))
    header_text = await translator.translate_text("ℹ️ معلومات المحادثة:", lang_code)
    
    chat_info = f"{header_text}\n\n"
    chat_info += f"🆔 Chat ID: `{message.chat.id}`\n"
    chat_info += f"📝 Type: {message.chat.type}\n"
    
    if message.chat.title:
        chat_info += f"📊 Title: {message.chat.title}\n"
    
    if message.from_user:
        chat_info += f"👤 User ID: `{message.from_user.id}`\n"
    
    await message.reply(chat_info, parse_mode="Markdown")

@dp.message(Command("testreservations"))
async def test_reservations_handler(message: types.Message):
    """Test command to check reservations status"""
    if not is_admin(message.from_user.id):
        return
        
    db = get_db()
    try:
        # Count reservations by status
        waiting_count = db.query(Reservation).filter(Reservation.status == ReservationStatus.WAITING_CODE).count()
        completed_count = db.query(Reservation).filter(Reservation.status == ReservationStatus.COMPLETED).count()
        expired_count = db.query(Reservation).filter(Reservation.status == ReservationStatus.EXPIRED).count()
        
        # Get recent reservations
        recent_reservations = db.query(Reservation).join(Number).order_by(Reservation.created_at.desc()).limit(5).all()
        
        # Since this is an admin command, we can use Arabic or admin's preferred language
        # For now, keeping it in Arabic as it's an admin debug command
        info = f"📊 حالة الحجوزات:\n\n"
        info += f"⏳ في انتظار الكود: {waiting_count}\n"
        info += f"✅ مكتملة: {completed_count}\n"
        info += f"❌ منتهية الصلاحية: {expired_count}\n\n"
        
        if recent_reservations:
            info += "📋 آخر 5 حجوزات:\n"
            for i, res in enumerate(recent_reservations, 1):
                info += f"{i}. {res.number.phone_number} - {res.status.value} - المستخدم: {res.user_id}\n"
        
        await message.reply(info)
    finally:
        db.close()

# Language selection callback handler
@dp.callback_query(F.data.startswith("set_lang_"))
async def set_language_callback(callback: CallbackQuery):
    """Handle language selection"""
    if not callback.from_user:
        return
    
    lang_code = callback.data.split("_")[2]
    
    # Update user language preference
    success = update_user_language(str(callback.from_user.id), lang_code)
    
    if success:
        # Update bot commands for new language
        await set_bot_commands(bot, lang_code)
        
        success_text = await translator.translate_text("✅ تم تغيير اللغة بنجاح!", lang_code)
        await callback.message.edit_text(
            success_text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('main_menu', lang_code), callback_data="main_menu")]
            ])
        )
    else:
        await callback.message.edit_text(
            "❌ خطأ في تغيير اللغة، يرجى المحاولة مرة أخرى"
        )

@dp.callback_query(F.data == "main_menu")
async def main_menu_handler(callback: CallbackQuery, state: FSMContext):
    """Handle main menu callback"""
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "🌟 القائمة الرئيسية 🌟\n\n"
            "📱 اختر خدمة للحصول على رقم مؤقت:",
            reply_markup=await create_main_keyboard()
        )

@dp.callback_query(F.data.startswith("svc_"))
async def service_selected_handler(callback: CallbackQuery, state: FSMContext):
    """Handle service selection"""
    if not callback.data:
        return
    service_id = int(callback.data.split("_")[1])
    
    db = get_db()
    try:
        service = db.query(Service).filter(Service.id == service_id).first()
        if not service:
            await callback.answer("❌ خدمة غير موجودة")
            return
        
        # Check if service has available numbers
        available_count = db.query(Number).filter(
            Number.service_id == service_id,
            Number.status == NumberStatus.AVAILABLE
        ).count()
        
        if available_count == 0:
            await callback.answer("❌ لا توجد أرقام متاحة لهذه الخدمة حالياً")
            return
        
        await state.update_data(service_id=service_id)
        
        if callback.message:
            # Get total available numbers for this service
            total_available = db.query(Number).filter(
                Number.service_id == service_id,
                Number.status == NumberStatus.AVAILABLE
            ).count()
            
            # Get user language
            user_lang = get_user_language(str(callback.from_user.id))
            translated_service_name = await get_text(service.name, user_lang)
            
            await callback.message.edit_text(
                f"🌍 اختر الدولة للخدمة: {service.emoji} {translated_service_name}\n\n"
                f"💰 السعر: {service.default_price} وحدة\n"
                f"📊 إجمالي الأرقام المتاحة: {total_available}",
                reply_markup=create_countries_keyboard(service_id)
            )
        
    finally:
        db.close()

@dp.callback_query(F.data.startswith("cty_"))
async def country_selected_handler(callback: CallbackQuery, state: FSMContext):
    """Handle country selection"""
    if not callback.data:
        return
    parts = callback.data.split("_")
    
    if parts[1] == "page":
        # Handle pagination
        service_id = int(parts[2])
        page = int(parts[3])
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=create_countries_keyboard(service_id, page))
        return
    
    service_id = int(parts[1])
    country_code = parts[2]
    
    # Get user
    user, _ = await get_or_create_user(str(callback.from_user.id))
    
    # Reserve number
    reservation = await reserve_number(int(user.id), service_id, country_code)
    
    if not reservation:
        await callback.answer("❌ لا توجد أرقام متاحة لهذه الدولة حالياً")
        return
    
    db = get_db()
    try:
        number = db.query(Number).filter(Number.id == reservation.number_id).first()
        service = db.query(Service).filter(Service.id == service_id).first()
        
        await state.update_data(reservation_id=reservation.id)
        
        # Start auto search for code in background
        asyncio.create_task(auto_search_for_code(int(reservation.id)))
        
        if callback.message:
            # Get remaining numbers count for this service and country
            remaining_count = db.query(Number).filter(
                Number.service_id == service_id,
                Number.country_code == country_code,
                Number.status == NumberStatus.AVAILABLE
            ).count()
            
            # Get user language and translate service name
            user_lang = get_user_language(str(callback.from_user.id))
            translated_service_name = await get_text(service.name, user_lang)
            
            await callback.message.edit_text(
                f"✅ تم حجز رقمك بنجاح!\n\n"
                f"📱 الرقم: `{number.phone_number}`\n"
                f"الكود: سيظهر هنا تلقائياً\n"
                f"🏷 الخدمة: {service.emoji} {translated_service_name}\n"
                f"🌍 الدولة: {country_code}\n"
                f"💰 السعر: {service.default_price} وحدة\n"
                f"📊 الأرقام المتبقية: {remaining_count}\n\n"
                f"⏱ سيتم البحث عن الكود تلقائياً خلال 15 ثانية\n"
                f"⏰ مهلة الانتظار: {RESERVATION_TIMEOUT_MIN} دقيقة\n"
                f"💳 سيتم الخصم فقط عند وصول الكود",
                parse_mode="Markdown",
                reply_markup=create_number_action_keyboard(int(reservation.id))
            )
        
    finally:
        db.close()

@dp.callback_query(F.data.startswith("change_number_"))
async def change_number_handler(callback: CallbackQuery, state: FSMContext):
    """Handle number change request"""
    reservation_id = int(callback.data.split("_")[2])
    
    db = get_db()
    try:
        reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
        if not reservation or reservation.status != ReservationStatus.WAITING_CODE:
            await callback.answer("❌ حجز غير صالح")
            return
        
        # Release current number
        current_number = db.query(Number).filter(Number.id == reservation.number_id).first()
        if current_number:
            current_number.status = NumberStatus.AVAILABLE
            current_number.reserved_by_user_id = None
            current_number.reserved_at = None
            current_number.expires_at = None
        
        # Find new number
        new_number = db.query(Number).filter(
            Number.service_id == reservation.service_id,
            Number.country_code == current_number.country_code,
            Number.status == NumberStatus.AVAILABLE,
            Number.id != current_number.id
        ).first()
        
        if not new_number:
            # Restore original number
            current_number.status = NumberStatus.RESERVED
            current_number.reserved_by_user_id = reservation.user_id
            current_number.reserved_at = datetime.now()
            current_number.expires_at = reservation.expired_at
            db.commit()
            
            await callback.answer("❌ لا توجد أرقام أخرى متاحة")
            return
        
        # Update reservation
        reservation.number_id = new_number.id
        new_number.status = NumberStatus.RESERVED
        new_number.reserved_by_user_id = reservation.user_id
        new_number.reserved_at = datetime.now()
        new_number.expires_at = reservation.expired_at
        
        db.commit()
        
        service = db.query(Service).filter(Service.id == reservation.service_id).first()
        
        await callback.message.edit_text(
            f"✅ تم تغيير رقمك:\n\n"
            f"📱 الرقم الجديد: `{new_number.phone_number}`\n"
            f"🏷 الخدمة: {service.emoji} {service.name}\n"
            f"🌍 الدولة: {new_number.country_code}\n\n"
            f"⏱ سيتم إرسال كود التحقق هنا فور وصوله\n"
            f"⏰ مهلة الانتظار: {RESERVATION_TIMEOUT_MIN} دقيقة",
            parse_mode="Markdown",
            reply_markup=create_number_action_keyboard(reservation.id)
        )
        
    finally:
        db.close()

@dp.callback_query(F.data.startswith("change_country_"))
async def change_country_handler(callback: CallbackQuery, state: FSMContext):
    """Handle country change request"""
    reservation_id = int(callback.data.split("_")[2])
    
    db = get_db()
    try:
        reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
        if not reservation:
            await callback.answer("❌ حجز غير صالح")
            return
        
        # Release current number
        current_number = db.query(Number).filter(Number.id == reservation.number_id).first()
        if current_number:
            current_number.status = NumberStatus.AVAILABLE
            current_number.reserved_by_user_id = None
            current_number.reserved_at = None
            current_number.expires_at = None
        
        # Delete reservation
        db.delete(reservation)
        db.commit()
        
        await state.update_data(service_id=reservation.service_id)
        
        service = db.query(Service).filter(Service.id == reservation.service_id).first()
        
        await callback.message.edit_text(
            f"🌍 اختر الدولة للخدمة: {service.emoji} {service.name}\n\n"
            f"💰 السعر: {service.default_price} وحدة",
            reply_markup=create_countries_keyboard(reservation.service_id)
        )
        
    finally:
        db.close()


@dp.callback_query(F.data == "my_balance")
async def my_balance_handler(callback: CallbackQuery):
    """Handle balance check"""
    user, _ = await get_or_create_user(str(callback.from_user.id))
    
    db = get_db()
    try:
        # Get recent transactions
        transactions = db.query(Transaction).filter(
            Transaction.user_id == user.id
        ).order_by(Transaction.created_at.desc()).limit(5).all()
        
        text = f"💰 رصيدك الحالي: {user.balance} وحدة\n\n"
        
        if transactions:
            text += "📊 آخر المعاملات:\n"
            for tx in transactions:
                type_emoji = {"add": "➕", "deduct": "➖", "purchase": "🛒", "reward": "🎁"}
                text += f"{type_emoji.get(tx.type.value, '•')} {tx.amount} - {tx.reason} ({tx.created_at.strftime('%Y-%m-%d %H:%M')})\n"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(InlineKeyboardButton(text="🔙 الرئيسية", callback_data="main_menu"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "free_credits")
async def free_credits_handler(callback: CallbackQuery):
    """Handle free credits collection from channels and groups"""
    db = get_db()
    try:
        channels = db.query(Channel).filter(Channel.active == True).all()
        groups = db.query(Group).filter(Group.active == True).all()
        
        if not channels and not groups:
            await callback.answer("❌ لا توجد قنوات أو جروبات متاحة حالياً")
            return
        
        text = "🆓 تجميع رصيد مجاني\n\n" \
               "اشترك في القنوات والجروبات التالية ثم اضغط '✅ تحقق' للحصول على رصيد مجاني:\n\n"
        
        keyboard = InlineKeyboardBuilder()
        
        # Add channels
        if channels:
            text += "📢 القنوات:\n"
            for channel in channels:
                text += f"📢 {channel.title} - {channel.reward_amount} وحدة\n"
                
                # Validate URL before creating button
                channel_url = channel.username_or_link
                if not channel_url.startswith('http'):
                    if channel_url.startswith('@'):
                        channel_url = f"https://t.me/{channel_url[1:]}"
                    else:
                        channel_url = f"https://t.me/{channel_url}"
                
                keyboard.row(
                    InlineKeyboardButton(text="🔗 انضمام", url=channel_url),
                    InlineKeyboardButton(text="✅ تحقق", callback_data=f"verify_channel_{channel.id}")
                )
            text += "\n"
        
        # Add groups
        if groups:
            text += "👥 الجروبات:\n"
            for group in groups:
                text += f"👥 {group.title} - {group.reward_amount} وحدة\n"
                
                # Validate URL before creating button
                group_url = group.username_or_link
                if not group_url.startswith('http'):
                    if group_url.startswith('@'):
                        group_url = f"https://t.me/{group_url[1:]}"
                    else:
                        group_url = f"https://t.me/{group_url}"
                
                keyboard.row(
                    InlineKeyboardButton(text="🔗 انضمام", url=group_url),
                    InlineKeyboardButton(text="✅ تحقق", callback_data=f"verify_group_{group.id}")
                )
        
        # Add verification for all
        nav_buttons = []
        if channels:
            nav_buttons.append(InlineKeyboardButton(text="✅ تحقق من جميع القنوات", callback_data="verify_all_channels"))
        if groups:
            nav_buttons.append(InlineKeyboardButton(text="✅ تحقق من جميع الجروبات", callback_data="verify_all_groups"))
        if nav_buttons:
            keyboard.row(*nav_buttons)
        
        if channels and groups:
            keyboard.row(InlineKeyboardButton(text="✅ تحقق من الكل", callback_data="verify_all"))
        
        keyboard.row(InlineKeyboardButton(text="🔙 الرئيسية", callback_data="main_menu"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data.startswith("verify_channel_"))
async def verify_channel_handler(callback: CallbackQuery):
    """Handle single channel verification"""
    channel_id = int(callback.data.split("_")[2])
    user, _ = await get_or_create_user(str(callback.from_user.id))
    
    db = get_db()
    try:
        channel = db.query(Channel).filter(Channel.id == channel_id).first()
        if not channel:
            await callback.answer("❌ قناة غير موجودة")
            return
        
        # Check if user already received reward
        reward_record = db.query(UserChannelReward).filter(
            UserChannelReward.user_id == user.id,
            UserChannelReward.channel_id == channel_id
        ).first()
        
        if reward_record and reward_record.last_award_at:
            await callback.answer("✅ تم استلام مكافأة هذه القناة من قبل")
            return
        
        # Check membership
        try:
            # Extract channel username from link
            channel_username = channel.username_or_link
            if channel_username.startswith('https://t.me/'):
                channel_username = '@' + channel_username.split('/')[-1]
            elif not channel_username.startswith('@'):
                channel_username = '@' + channel_username
            
            member = await bot.get_chat_member(channel_username, callback.from_user.id)
            if member.status in ['member', 'administrator', 'creator']:
                # Give reward
                user_obj = db.query(User).filter(User.id == user.id).first()
                user_obj.balance += channel.reward_amount
                
                # Create reward record
                if not reward_record:
                    reward_record = UserChannelReward(
                        user_id=user.id,
                        channel_id=channel_id,
                        times_awarded=1
                    )
                    db.add(reward_record)
                else:
                    reward_record.times_awarded += 1
                
                reward_record.last_award_at = datetime.now()
                
                # Create transaction
                transaction = Transaction(
                    user_id=user.id,
                    type=TransactionType.REWARD,
                    amount=channel.reward_amount,
                    reason=f"مكافأة الاشتراك في {channel.title}"
                )
                db.add(transaction)
                
                db.commit()
                
                await callback.answer(f"🎉 تم إضافة {channel.reward_amount} وحدة لرصيدك!")
            else:
                await callback.answer("❌ يجب الاشتراك في القناة أولاً")
                
        except Exception as e:
            logger.error(f"Error checking channel membership: {e}")
            await callback.answer("❌ حدث خطأ في التحقق من الاشتراك")
    
    finally:
        db.close()

@dp.callback_query(F.data.startswith("verify_group_"))
async def verify_group_handler(callback: CallbackQuery):
    """Handle single group verification"""
    group_id = int(callback.data.split("_")[2])
    user, _ = await get_or_create_user(str(callback.from_user.id))
    
    db = get_db()
    try:
        group = db.query(Group).filter(Group.id == group_id).first()
        if not group:
            await callback.answer("❌ جروب غير موجود")
            return
        
        # Check if user already received reward
        reward_record = db.query(UserGroupReward).filter(
            UserGroupReward.user_id == user.id,
            UserGroupReward.group_id == group_id
        ).first()
        
        if reward_record and reward_record.last_award_at:
            await callback.answer("✅ تم استلام مكافأة هذا الجروب من قبل")
            return
        
        # Check membership
        try:
            # For groups, use group_id directly if available, otherwise extract from link
            group_identifier = group.group_id if group.group_id else group.username_or_link
            
            if not group_identifier.startswith('@') and not group_identifier.startswith('-'):
                if group.username_or_link.startswith('https://t.me/'):
                    group_identifier = '@' + group.username_or_link.split('/')[-1]
                elif not group.username_or_link.startswith('@'):
                    group_identifier = '@' + group.username_or_link
            
            member = await bot.get_chat_member(group_identifier, callback.from_user.id)
            if member.status in ['member', 'administrator', 'creator']:
                # Give reward
                user_obj = db.query(User).filter(User.id == user.id).first()
                user_obj.balance += group.reward_amount
                
                # Create reward record
                if not reward_record:
                    reward_record = UserGroupReward(
                        user_id=user.id,
                        group_id=group_id,
                        times_awarded=1
                    )
                    db.add(reward_record)
                else:
                    reward_record.times_awarded += 1
                
                reward_record.last_award_at = datetime.now()
                
                # Create transaction
                transaction = Transaction(
                    user_id=user.id,
                    type=TransactionType.REWARD,
                    amount=group.reward_amount,
                    reason=f"مكافأة الانضمام لجروب {group.title}"
                )
                db.add(transaction)
                
                db.commit()
                
                await callback.answer(f"🎉 تم إضافة {group.reward_amount} وحدة لرصيدك!")
            else:
                await callback.answer("❌ يجب الانضمام للجروب أولاً")
                
        except Exception as e:
            logger.error(f"Error checking group membership: {e}")
            await callback.answer("❌ حدث خطأ في التحقق من الانضمام")
    
    finally:
        db.close()

@dp.callback_query(F.data == "verify_all_channels")
async def verify_all_channels_handler(callback: CallbackQuery):
    """Handle verification of all channels"""
    user, _ = await get_or_create_user(str(callback.from_user.id))
    
    db = get_db()
    try:
        channels = db.query(Channel).filter(Channel.active == True).all()
        total_reward = 0
        verified_channels = []
        
        for channel in channels:
            # Check if user already received reward
            reward_record = db.query(UserChannelReward).filter(
                UserChannelReward.user_id == user.id,
                UserChannelReward.channel_id == channel.id
            ).first()
            
            if reward_record and reward_record.last_award_at:
                continue
            
            # Check membership
            try:
                channel_username = channel.username_or_link
                if channel_username.startswith('https://t.me/'):
                    channel_username = '@' + channel_username.split('/')[-1]
                elif not channel_username.startswith('@'):
                    channel_username = '@' + channel_username
                
                member = await bot.get_chat_member(channel_username, callback.from_user.id)
                if member.status in ['member', 'administrator', 'creator']:
                    verified_channels.append(channel)
                    total_reward += channel.reward_amount
                    
            except Exception as e:
                logger.error(f"Error checking channel {channel.title}: {e}")
                continue
        
        if total_reward > 0:
            # Add balance
            user_obj = db.query(User).filter(User.id == user.id).first()
            user_obj.balance += total_reward
            
            # Create records and transactions
            for channel in verified_channels:
                reward_record = db.query(UserChannelReward).filter(
                    UserChannelReward.user_id == user.id,
                    UserChannelReward.channel_id == channel.id
                ).first()
                
                if not reward_record:
                    reward_record = UserChannelReward(
                        user_id=user.id,
                        channel_id=channel.id,
                        times_awarded=1
                    )
                    db.add(reward_record)
                else:
                    reward_record.times_awarded += 1
                
                reward_record.last_award_at = datetime.now()
                
                transaction = Transaction(
                    user_id=user.id,
                    type=TransactionType.REWARD,
                    amount=channel.reward_amount,
                    reason=f"مكافأة الاشتراك في {channel.title}"
                )
                db.add(transaction)
            
            db.commit()
            
            await callback.answer(f"🎉 تم إضافة {total_reward} وحدة لرصيدك!")
        else:
            await callback.answer("❌ لم يتم العثور على اشتراكات جديدة")
    
    finally:
        db.close()

@dp.callback_query(F.data == "verify_all_groups")
async def verify_all_groups_handler(callback: CallbackQuery):
    """Handle verification of all groups"""
    user, _ = await get_or_create_user(str(callback.from_user.id))
    
    db = get_db()
    try:
        groups = db.query(Group).filter(Group.active == True).all()
        total_reward = 0
        verified_groups = []
        
        for group in groups:
            # Check if user already received reward
            reward_record = db.query(UserGroupReward).filter(
                UserGroupReward.user_id == user.id,
                UserGroupReward.group_id == group.id
            ).first()
            
            if reward_record and reward_record.last_award_at:
                continue
            
            # Check membership
            try:
                group_identifier = group.group_id if group.group_id else group.username_or_link
                
                if not group_identifier.startswith('@') and not group_identifier.startswith('-'):
                    if group.username_or_link.startswith('https://t.me/'):
                        group_identifier = '@' + group.username_or_link.split('/')[-1]
                    elif not group.username_or_link.startswith('@'):
                        group_identifier = '@' + group.username_or_link
                
                member = await bot.get_chat_member(group_identifier, callback.from_user.id)
                if member.status in ['member', 'administrator', 'creator']:
                    verified_groups.append(group)
                    total_reward += group.reward_amount
                    
            except Exception as e:
                logger.error(f"Error checking group {group.title}: {e}")
                continue
        
        if total_reward > 0:
            # Add balance
            user_obj = db.query(User).filter(User.id == user.id).first()
            user_obj.balance += total_reward
            
            # Create records and transactions
            for group in verified_groups:
                reward_record = db.query(UserGroupReward).filter(
                    UserGroupReward.user_id == user.id,
                    UserGroupReward.group_id == group.id
                ).first()
                
                if not reward_record:
                    reward_record = UserGroupReward(
                        user_id=user.id,
                        group_id=group.id,
                        times_awarded=1
                    )
                    db.add(reward_record)
                else:
                    reward_record.times_awarded += 1
                
                reward_record.last_award_at = datetime.now()
                
                transaction = Transaction(
                    user_id=user.id,
                    type=TransactionType.REWARD,
                    amount=group.reward_amount,
                    reason=f"مكافأة الانضمام لجروب {group.title}"
                )
                db.add(transaction)
            
            db.commit()
            
            await callback.answer(f"🎉 تم إضافة {total_reward} وحدة لرصيدك!")
        else:
            await callback.answer("❌ لم يتم العثور على انضمام جديد للجروبات")
    
    finally:
        db.close()

@dp.callback_query(F.data == "verify_all")
async def verify_all_handler(callback: CallbackQuery):
    """Handle verification of all channels and groups"""
    user, _ = await get_or_create_user(str(callback.from_user.id))
    
    db = get_db()
    try:
        total_reward = 0
        verified_items = []
        
        # Check channels
        channels = db.query(Channel).filter(Channel.active == True).all()
        for channel in channels:
            reward_record = db.query(UserChannelReward).filter(
                UserChannelReward.user_id == user.id,
                UserChannelReward.channel_id == channel.id
            ).first()
            
            if reward_record and reward_record.last_award_at:
                continue
            
            try:
                channel_username = channel.username_or_link
                if channel_username.startswith('https://t.me/'):
                    channel_username = '@' + channel_username.split('/')[-1]
                elif not channel_username.startswith('@'):
                    channel_username = '@' + channel_username
                
                member = await bot.get_chat_member(channel_username, callback.from_user.id)
                if member.status in ['member', 'administrator', 'creator']:
                    verified_items.append(('channel', channel))
                    total_reward += channel.reward_amount
                    
            except Exception as e:
                logger.error(f"Error checking channel {channel.title}: {e}")
                continue
        
        # Check groups
        groups = db.query(Group).filter(Group.active == True).all()
        for group in groups:
            reward_record = db.query(UserGroupReward).filter(
                UserGroupReward.user_id == user.id,
                UserGroupReward.group_id == group.id
            ).first()
            
            if reward_record and reward_record.last_award_at:
                continue
            
            try:
                group_identifier = group.group_id if group.group_id else group.username_or_link
                
                if not group_identifier.startswith('@') and not group_identifier.startswith('-'):
                    if group.username_or_link.startswith('https://t.me/'):
                        group_identifier = '@' + group.username_or_link.split('/')[-1]
                    elif not group.username_or_link.startswith('@'):
                        group_identifier = '@' + group.username_or_link
                
                member = await bot.get_chat_member(group_identifier, callback.from_user.id)
                if member.status in ['member', 'administrator', 'creator']:
                    verified_items.append(('group', group))
                    total_reward += group.reward_amount
                    
            except Exception as e:
                logger.error(f"Error checking group {group.title}: {e}")
                continue
        
        if total_reward > 0:
            # Add balance
            user_obj = db.query(User).filter(User.id == user.id).first()
            user_obj.balance += total_reward
            
            # Create records and transactions
            for item_type, item in verified_items:
                if item_type == 'channel':
                    reward_record = db.query(UserChannelReward).filter(
                        UserChannelReward.user_id == user.id,
                        UserChannelReward.channel_id == item.id
                    ).first()
                    
                    if not reward_record:
                        reward_record = UserChannelReward(
                            user_id=user.id,
                            channel_id=item.id,
                            times_awarded=1
                        )
                        db.add(reward_record)
                    else:
                        reward_record.times_awarded += 1
                    
                    reward_record.last_award_at = datetime.now()
                    
                    transaction = Transaction(
                        user_id=user.id,
                        type=TransactionType.REWARD,
                        amount=item.reward_amount,
                        reason=f"مكافأة الاشتراك في {item.title}"
                    )
                    db.add(transaction)
                    
                elif item_type == 'group':
                    reward_record = db.query(UserGroupReward).filter(
                        UserGroupReward.user_id == user.id,
                        UserGroupReward.group_id == item.id
                    ).first()
                    
                    if not reward_record:
                        reward_record = UserGroupReward(
                            user_id=user.id,
                            group_id=item.id,
                            times_awarded=1
                        )
                        db.add(reward_record)
                    else:
                        reward_record.times_awarded += 1
                    
                    reward_record.last_award_at = datetime.now()
                    
                    transaction = Transaction(
                        user_id=user.id,
                        type=TransactionType.REWARD,
                        amount=item.reward_amount,
                        reason=f"مكافأة الانضمام لجروب {item.title}"
                    )
                    db.add(transaction)
            
            db.commit()
            
            await callback.answer(f"🎉 تم إضافة {total_reward} وحدة لرصيدك!")
        else:
            await callback.answer("❌ لم يتم العثور على اشتراكات أو انضمام جديد")
    
    finally:
        db.close()

@dp.callback_query(F.data == "help")
async def help_handler(callback: CallbackQuery):
    """Handle help request"""
    help_text = (
        "ℹ️ كيفية استخدام البوت:\n\n"
        "1️⃣ اختر الخدمة المطلوبة (واتساب، تليجرام، إلخ)\n"
        "2️⃣ اختر الدولة\n"
        "3️⃣ احصل على رقم مؤقت\n"
        "4️⃣ استخدم الرقم في التطبيق المطلوب\n"
        "5️⃣ انتظر وصول كود التحقق هنا\n\n"
        "💰 لزيادة رصيدك:\n"
        "• اشترك في القنوات واحصل على رصيد مجاني\n"
        "• تواصل مع الإدارة لشراء رصيد\n\n"
        "⏰ مهلة انتظار الكود: 20 دقيقة\n"
        "💳 يتم الخصم فقط عند وصول الكود\n\n"
        "📞 للدعم: تواصل مع @admin"
    )
    
    keyboard = InlineKeyboardBuilder()
    keyboard.row(InlineKeyboardButton(text="🔙 الرئيسية", callback_data="main_menu"))
    
    await callback.message.edit_text(help_text, reply_markup=keyboard.as_markup())

@dp.callback_query(F.data == "settings")
async def settings_handler(callback: CallbackQuery):
    """Handle settings menu for regular users"""
    user_id = str(callback.from_user.id)
    lang_code = get_user_language(user_id)
    
    # Get user info
    db = get_db()
    try:
        user = db.query(User).filter(User.telegram_id == user_id).first()
        if not user:
            await callback.answer("❌ خطأ في تحميل البيانات")
            return
        
        current_lang_name = SUPPORTED_LANGUAGES.get(lang_code, "العربية")
        
        settings_text = f"⚙️ الإعدادات\n\n"
        settings_text += f"👤 المستخدم: {callback.from_user.first_name or 'غير محدد'}\n"
        settings_text += f"🆔 ID: {user_id}\n"
        settings_text += f"💰 الرصيد: {user.balance} وحدة\n"
        settings_text += f"🌐 اللغة الحالية: {current_lang_name}\n\n"
        settings_text += "اختر ما تريد تغييره:"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="🌐 تغيير اللغة", callback_data="choose_language"),
            InlineKeyboardButton(text="📋 سجل الطلبات", callback_data="show_history")
        )
        keyboard.row(
            InlineKeyboardButton(text="💰 رصيدي", callback_data="my_balance"),
            InlineKeyboardButton(text="🆓 رصيد مجاني", callback_data="free_credits")
        )
        keyboard.row(InlineKeyboardButton(text="🔙 الرئيسية", callback_data="main_menu"))
        
        await callback.message.edit_text(settings_text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "choose_language")
async def choose_language_handler(callback: CallbackQuery):
    """Handle language selection from settings"""
    keyboard = InlineKeyboardBuilder()
    
    # Add language selection buttons (2 per row)
    lang_items = list(SUPPORTED_LANGUAGES.items())
    for i in range(0, len(lang_items), 2):
        row = []
        for j in range(2):
            if i + j < len(lang_items):
                code, name = lang_items[i + j]
                row.append(InlineKeyboardButton(
                    text=name,
                    callback_data=f"set_lang_{code}"
                ))
        keyboard.row(*row)
    
    keyboard.row(InlineKeyboardButton(text="🔙 الإعدادات", callback_data="settings"))
    
    await callback.message.edit_text(
        "🌐 اختر لغتك المفضلة:\nChoose your preferred language:",
        reply_markup=keyboard.as_markup()
    )

@dp.callback_query(F.data == "show_history")
async def show_history_handler(callback: CallbackQuery):
    """Show user history from settings"""
    user_id = str(callback.from_user.id)
    
    db = get_db()
    try:
        reservations = db.query(Reservation).filter(
            Reservation.user_id == user_id
        ).order_by(Reservation.created_at.desc()).limit(10).all()
        
        if not reservations:
            lang_code = get_user_language(user_id)
            no_history_text = await translator.translate_text("📋 لا توجد طلبات سابقة", lang_code)
            await callback.message.edit_text(
                no_history_text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔙 الإعدادات", callback_data="settings")
                ]])
            )
            return
        
        history_text = "📋 آخر 10 طلبات:\n\n"
        for res in reservations:
            status_emoji = {
                ReservationStatus.WAITING_CODE: "⏳",
                ReservationStatus.COMPLETED: "✅", 
                ReservationStatus.EXPIRED: "⏰",
                ReservationStatus.CANCELED: "❌"
            }.get(res.status, "❓")
            
            history_text += f"{status_emoji} {res.service.name} - {res.number}\n"
            history_text += f"   📅 {res.created_at.strftime('%Y-%m-%d %H:%M')}\n\n"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(InlineKeyboardButton(text="🔙 الإعدادات", callback_data="settings"))
        
        lang_code = get_user_language(user_id)
        translated_text = await translator.translate_text(history_text, lang_code)
        await callback.message.edit_text(translated_text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

# Admin handlers
@dp.callback_query(F.data == "admin")
async def admin_handler(callback: CallbackQuery, state: FSMContext):
    """Handle admin panel access"""
    user_id = callback.from_user.id
    
    if user_id != ADMIN_ID and not is_admin_session_valid(user_id):
        await state.set_state(AdminStates.waiting_for_password)
        lang_code = get_user_language(str(callback.from_user.id)) 
        password_prompt = t('admin_password_prompt', lang_code)
        cancel_text = t('main_menu', lang_code)
        
        await callback.message.edit_text(
            password_prompt,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🔙 {cancel_text}", callback_data="main_menu")]
            ])
        )
        return
    
    lang_code = get_user_language(str(callback.from_user.id))
    admin_panel_text = t('admin_panel', lang_code)
    choose_section_text = t('choose_section', lang_code)
    
    await callback.message.edit_text(
        f"{admin_panel_text}\n\n{choose_section_text}",
        reply_markup=create_admin_keyboard()
    )

@dp.message(AdminStates.waiting_for_password)
async def admin_password_handler(message: types.Message, state: FSMContext):
    """Handle admin password verification"""
    if message.text == ADMIN_PASSWORD:
        admin_sessions[message.from_user.id] = datetime.now()
        await state.clear()
        lang_code = get_user_language(str(message.from_user.id))
        success_text = t('admin_login_success', lang_code)
        admin_panel_text = t('admin_panel', lang_code)
        
        await message.reply(
            f"{success_text}\n\n{admin_panel_text}:",
            reply_markup=create_admin_keyboard()
        )
    else:
        lang_code = get_user_language(str(message.from_user.id))
        failed_text = t('admin_login_failed', lang_code)
        await message.reply(failed_text)

@dp.callback_query(F.data == "admin_services")
async def admin_services_handler(callback: CallbackQuery):
    """Handle admin services management"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    # Show loading indicator
    await callback.answer("🔄 جاري تحميل الخدمات...")
    
    db = get_db()
    try:
        services = db.query(Service).all()
        
        text = "🛠 إدارة الخدمات\n\n"
        if services:
            text += "الخدمات الحالية:\n"
            for service in services:
                status = "✅" if service.active else "❌"
                text += f"{status} {service.emoji} {service.name} - {service.default_price} وحدة\n"
        else:
            text += "لا توجد خدمات مضافة\n"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="➕ إضافة خدمة", callback_data="admin_add_service"),
            InlineKeyboardButton(text="🔗 إدارة الجروبات", callback_data="admin_service_groups")
        )
        keyboard.row(
            InlineKeyboardButton(text="📋 عرض الخدمات", callback_data="admin_list_services"),
            InlineKeyboardButton(text="📊 إحصائيات الرسائل", callback_data="admin_messages_stats")
        )
        keyboard.row(InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_users")
async def admin_users_handler(callback: CallbackQuery):
    """Handle admin users management"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        users_count = db.query(User).count()
        active_users = db.query(User).filter(User.is_banned == False).count()
        banned_users = db.query(User).filter(User.is_banned == True).count()
        
        text = f"👥 إدارة المستخدمين\n\n"
        text += f"📊 الإحصائيات:\n"
        text += f"• إجمالي المستخدمين: {users_count}\n"
        text += f"• المستخدمين النشطين: {active_users}\n"
        text += f"• المستخدمين المحظورين: {banned_users}\n"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="👤 البحث عن مستخدم", callback_data="admin_search_user"),
            InlineKeyboardButton(text="📋 قائمة المستخدمين", callback_data="admin_list_users")
        )
        keyboard.row(InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_add_balance")
async def admin_add_balance_handler(callback: CallbackQuery, state: FSMContext):
    """Handle admin add balance request"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    await state.set_state(AdminStates.waiting_for_user_id_balance)
    await state.update_data(action_type="add")
    
    if callback.message:
        await callback.message.edit_text(
            "💰 شحن رصيد مستخدم\n\n"
            "أرسل ID المستخدم (الرقم الطويل) أو @username:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin")]
            ])
        )

@dp.callback_query(F.data == "admin_deduct_balance")
async def admin_deduct_balance_handler(callback: CallbackQuery, state: FSMContext):
    """Handle admin deduct balance request"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    await state.set_state(AdminStates.waiting_for_user_id_balance)
    await state.update_data(action_type="deduct")
    
    if callback.message:
        await callback.message.edit_text(
            "💳 خصم رصيد مستخدم\n\n"
            "أرسل ID المستخدم (الرقم الطويل) أو @username:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin")]
            ])
        )

@dp.message(AdminStates.waiting_for_user_id_balance)
async def handle_user_id_for_balance(message: types.Message, state: FSMContext):
    """Handle user ID input for balance operations"""
    if not message.from_user or not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        await state.clear()
        return
    
    user_input = message.text
    db = get_db()
    
    try:
        target_user = None
        
        # Try to find user by telegram_id or username
        if user_input.startswith('@'):
            username = user_input[1:]  # Remove @
            target_user = db.query(User).filter(User.username == username).first()
        else:
            # Try as telegram_id
            target_user = db.query(User).filter(User.telegram_id == user_input).first()
        
        if not target_user:
            await message.reply(
                "❌ لم يتم العثور على المستخدم\n"
                "تأكد من أن المستخدم قد استخدم البوت من قبل"
            )
            return
        
        data = await state.get_data()
        action_type = data.get("action_type", "add")
        
        # Handle different action types
        if action_type == "search":
            # Display user information
            status = "✅ نشط" if not target_user.is_banned else "❌ محظور"
            admin_status = "👑 أدمن" if target_user.is_admin else "👤 مستخدم عادي"
            
            text = f"👤 معلومات المستخدم\n\n"
            text += f"📝 الاسم: {target_user.first_name or 'غير محدد'}\n"
            text += f"📱 المعرف: @{target_user.username or 'غير محدد'}\n"
            text += f"🆔 الرقم: {target_user.telegram_id}\n"
            text += f"💰 الرصيد: {target_user.balance} وحدة\n"
            text += f"📊 الحالة: {status}\n"
            text += f"👨‍💼 النوع: {admin_status}\n"
            text += f"📅 تاريخ الانضمام: {target_user.joined_at.strftime('%Y-%m-%d')}\n"
            
            # Add action buttons
            keyboard = InlineKeyboardBuilder()
            if not target_user.is_banned:
                keyboard.row(InlineKeyboardButton(text="🚫 حظر المستخدم", callback_data=f"ban_user_{target_user.id}"))
            else:
                keyboard.row(InlineKeyboardButton(text="✅ إلغاء الحظر", callback_data=f"unban_user_{target_user.id}"))
            
            keyboard.row(
                InlineKeyboardButton(text="💰 شحن رصيد", callback_data=f"quick_add_balance_{target_user.id}"),
                InlineKeyboardButton(text="💳 خصم رصيد", callback_data=f"quick_deduct_balance_{target_user.id}")
            )
            keyboard.row(InlineKeyboardButton(text="🔙 إدارة المستخدمين", callback_data="admin_users"))
            
            await message.reply(text, reply_markup=keyboard.as_markup())
            await state.clear()
            return
            
        elif action_type == "private_message":
            # Store user for private message
            await state.update_data(target_user_id=target_user.id)
            await state.set_state(AdminStates.waiting_for_broadcast_message)  # Reuse this state
            await state.update_data(is_private=True)
            
            await message.reply(
                f"💬 إرسال رسالة خاصة\n\n"
                f"👤 إلى: {target_user.first_name or target_user.username or target_user.telegram_id}\n\n"
                f"أرسل الرسالة:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin")]
                ])
            )
            return
        
        # Balance operations
        await state.update_data(target_user_id=target_user.id)
        await state.set_state(AdminStates.waiting_for_balance_amount)
        
        action_text = "شحن" if action_type == "add" else "خصم"
        emoji = "💰" if action_type == "add" else "💳"
        
        await message.reply(
            f"{emoji} {action_text} رصيد\n\n"
            f"👤 المستخدم: {target_user.first_name or target_user.username or target_user.telegram_id}\n"
            f"💰 رصيده الحالي: {target_user.balance} وحدة\n\n"
            f"أرسل المبلغ المراد {action_text}ه:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin")]
            ])
        )
        
    finally:
        db.close()

@dp.message(AdminStates.waiting_for_balance_amount)
async def handle_balance_amount(message: types.Message, state: FSMContext):
    """Handle balance amount input"""
    if not message.from_user or not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        await state.clear()
        return
    
    try:
        amount = float(message.text)
        if amount <= 0:
            await message.reply("❌ المبلغ يجب أن يكون أكبر من الصفر")
            return
            
    except ValueError:
        await message.reply("❌ الرجاء إدخال رقم صحيح")
        return
    
    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    action_type = data.get("action_type", "add")
    
    db = get_db()
    try:
        target_user = db.query(User).filter(User.id == target_user_id).first()
        if not target_user:
            await message.reply("❌ حدث خطأ، لم يتم العثور على المستخدم")
            await state.clear()
            return
        
        old_balance = float(target_user.balance)
        
        if action_type == "add":
            target_user.balance = old_balance + amount
            transaction_type = TransactionType.ADD
            transaction_reason = f"شحن رصيد بواسطة الأدمن"
            emoji = "💰"
            action_text = "شحن"
        else:
            if old_balance < amount:
                await message.reply(
                    f"❌ رصيد المستخدم غير كافي للخصم\n"
                    f"الرصيد الحالي: {old_balance} وحدة\n"
                    f"المبلغ المطلوب خصمه: {amount} وحدة"
                )
                return
            
            target_user.balance = old_balance - amount
            transaction_type = TransactionType.DEDUCT
            transaction_reason = f"خصم رصيد بواسطة الأدمن"
            emoji = "💳"
            action_text = "خصم"
        
        # Create transaction record
        transaction = Transaction(
            user_id=target_user.id,
            type=transaction_type,
            amount=amount,
            reason=transaction_reason
        )
        db.add(transaction)
        
        db.commit()
        
        new_balance = float(target_user.balance)
        
        # Send success message
        await message.reply(
            f"✅ تم {action_text} الرصيد بنجاح!\n\n"
            f"👤 المستخدم: {target_user.first_name or target_user.username or target_user.telegram_id}\n"
            f"{emoji} المبلغ: {amount} وحدة\n"
            f"💰 الرصيد السابق: {old_balance} وحدة\n"
            f"💰 الرصيد الجديد: {new_balance} وحدة",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin")]
            ])
        )
        
        # Notify the user about balance change
        try:
            await bot.send_message(
                target_user.telegram_id,
                f"{emoji} تم {action_text} رصيدك!\n\n"
                f"💰 المبلغ: {amount} وحدة\n"
                f"💰 رصيدك الجديد: {new_balance} وحدة"
            )
        except Exception as e:
            logger.error(f"Failed to notify user about balance change: {e}")
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Error processing balance operation: {e}")
        await message.reply("❌ حدث خطأ أثناء معالجة العملية")
        db.rollback()
    finally:
        db.close()

@dp.callback_query(F.data == "admin_inventory")
async def admin_inventory_handler(callback: CallbackQuery):
    """Handle admin inventory management"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        # Get inventory statistics
        total_numbers = db.query(Number).count()
        available_numbers = db.query(Number).filter(Number.status == NumberStatus.AVAILABLE).count()
        reserved_numbers = db.query(Number).filter(Number.status == NumberStatus.RESERVED).count()
        used_numbers = db.query(Number).filter(Number.status == NumberStatus.USED).count()
        
        # Get numbers by service
        services = db.query(Service).filter(Service.active == True).all()
        
        text = f"📦 إدارة المخزون\n\n"
        text += f"📊 الإحصائيات العامة:\n"
        text += f"• إجمالي الأرقام: {total_numbers}\n"
        text += f"• ✅ متاحة: {available_numbers}\n"
        text += f"• 🔒 محجوزة: {reserved_numbers}\n"
        text += f"• ❌ مستخدمة: {used_numbers}\n\n"
        
        text += f"📱 الأرقام حسب الخدمة:\n"
        for service in services:
            service_total = db.query(Number).filter(Number.service_id == service.id).count()
            service_available = db.query(Number).filter(
                Number.service_id == service.id,
                Number.status == NumberStatus.AVAILABLE
            ).count()
            
            text += f"{service.emoji} {service.name}: {service_available}/{service_total}\n"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="📊 تفاصيل الخدمات", callback_data="admin_inventory_services"),
            InlineKeyboardButton(text="🌍 تفاصيل الدول", callback_data="admin_inventory_countries")
        )
        keyboard.row(
            InlineKeyboardButton(text="➕ إضافة أرقام", callback_data="admin_add_numbers"),
            InlineKeyboardButton(text="🗑 تنظيف الأرقام", callback_data="admin_cleanup_numbers")
        )
        keyboard.row(InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin"))
        
        if callback.message:
            await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_inventory_services")
async def admin_inventory_services_handler(callback: CallbackQuery):
    """Handle admin inventory by services"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        services = db.query(Service).filter(Service.active == True).all()
        
        text = f"📊 تفاصيل المخزون حسب الخدمات\n\n"
        
        for service in services[:5]:  # Limit to first 5 services for better performance
            text += f"{service.emoji} {service.name}:\n"
            
            # Get countries for this service with limit
            countries = db.query(ServiceCountry).filter(
                ServiceCountry.service_id == service.id,
                ServiceCountry.active == True
            ).limit(5).all()  # Limit countries per service
            
            for country in countries:
                available_count = db.query(Number).filter(
                    Number.service_id == service.id,
                    Number.country_code == country.country_code,
                    Number.status == NumberStatus.AVAILABLE
                ).count()
                
                total_count = db.query(Number).filter(
                    Number.service_id == service.id,
                    Number.country_code == country.country_code
                ).count()
                
                status = "✅" if available_count > 0 else "❌"
                text += f"  {country.flag} {country.country_name}: {status} {available_count}/{total_count}\n"
            
            text += "\n"
        
        if len(services) > 5:
            text += f"... وعرض {len(services) - 5} خدمة أخرى (للأداء الأفضل)\n\n"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(InlineKeyboardButton(text="🔙 المخزون", callback_data="admin_inventory"))
        
        if callback.message:
            await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_inventory_countries")
async def admin_inventory_countries_handler(callback: CallbackQuery):
    """Handle admin inventory by countries"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        # Get all countries with their total numbers
        countries_data = db.query(ServiceCountry.country_name, ServiceCountry.country_code, ServiceCountry.flag).distinct().all()
        
        text = f"🌍 تفاصيل المخزون حسب الدول\n\n"
        
        for country_name, country_code, flag in countries_data:
            total_numbers = db.query(Number).filter(Number.country_code == country_code).count()
            available_numbers = db.query(Number).filter(
                Number.country_code == country_code,
                Number.status == NumberStatus.AVAILABLE
            ).count()
            
            status = "✅" if available_numbers > 0 else "❌"
            text += f"{flag} {country_name} ({country_code}): {status} {available_numbers}/{total_numbers}\n"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(InlineKeyboardButton(text="🔙 المخزون", callback_data="admin_inventory"))
        
        if callback.message:
            await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_numbers")
async def admin_numbers_handler(callback: CallbackQuery):
    """Handle admin numbers management"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    # Show loading indicator
    await callback.answer("🔄 جاري تحميل إحصائيات الأرقام...")
    
    db = get_db()
    try:
        # Get number statistics
        total_numbers = db.query(Number).count()
        available_numbers = db.query(Number).filter(Number.status == NumberStatus.AVAILABLE).count()
        reserved_numbers = db.query(Number).filter(Number.status == NumberStatus.RESERVED).count()
        used_numbers = db.query(Number).filter(Number.status == NumberStatus.USED).count()
        
        text = f"📱 إدارة الأرقام\n\n"
        text += f"📊 الإحصائيات:\n"
        text += f"• إجمالي الأرقام: {total_numbers}\n"
        text += f"• متاحة: {available_numbers}\n"
        text += f"• محجوزة: {reserved_numbers}\n"
        text += f"• مستخدمة: {used_numbers}\n"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="➕ إضافة أرقام", callback_data="admin_add_numbers"),
            InlineKeyboardButton(text="📋 عرض الأرقام", callback_data="admin_list_numbers")
        )
        keyboard.row(
            InlineKeyboardButton(text="🗑 تنظيف الأرقام", callback_data="admin_cleanup_menu"),
            InlineKeyboardButton(text="📊 إحصائيات تفصيلية", callback_data="admin_inventory")
        )
        keyboard.row(InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_channels")
async def admin_channels_handler(callback: CallbackQuery):
    """Handle admin channels management"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        channels = db.query(Channel).all()
        
        text = "📢 إدارة القنوات\n\n"
        if channels:
            text += "القنوات الحالية:\n"
            for channel in channels:
                status = "✅" if channel.active else "❌"
                text += f"{status} {channel.title} - {channel.reward_amount} وحدة\n"
                text += f"   🔗 {channel.username_or_link}\n\n"
        else:
            text += "لا توجد قنوات مضافة\n"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="➕ إضافة قناة", callback_data="admin_add_channel"),
            InlineKeyboardButton(text="📋 عرض القنوات", callback_data="admin_list_channels")
        )
        if channels:
            keyboard.row(
                InlineKeyboardButton(text="🗑 حذف قناة", callback_data="admin_delete_channel"),
                InlineKeyboardButton(text="👥 إدارة الجروبات", callback_data="admin_groups")
            )
        else:
            keyboard.row(InlineKeyboardButton(text="👥 إدارة الجروبات", callback_data="admin_groups"))
        keyboard.row(InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_cleanup_numbers")
async def admin_cleanup_numbers_handler(callback: CallbackQuery):
    """Cleanup old used numbers"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        # Delete used numbers older than 7 days
        cutoff_date = datetime.now() - timedelta(days=7)
        
        deleted_count = db.query(Number).filter(
            Number.status == NumberStatus.USED,
            Number.code_received_at < cutoff_date
        ).delete()
        
        # Reset expired reservations
        expired_reservations = db.query(Reservation).filter(
            Reservation.status == ReservationStatus.WAITING_CODE,
            Reservation.expired_at < datetime.now()
        ).all()
        
        reset_count = 0
        for reservation in expired_reservations:
            # Reset number status
            number = db.query(Number).filter(Number.id == reservation.number_id).first()
            if number:
                number.status = NumberStatus.AVAILABLE
                number.reserved_by_user_id = None
                number.reserved_at = None
                number.expires_at = None
                reset_count += 1
            
            # Update reservation status
            reservation.status = ReservationStatus.EXPIRED
        
        db.commit()
        
        await callback.answer(
            f"✅ تم حذف {deleted_count} رقم قديم وإعادة تعيين {reset_count} حجز منتهي الصلاحية",
            show_alert=True
        )
        
        # Refresh the numbers page
        await admin_numbers_handler(callback)
        
    except Exception as e:
        logger.error(f"Error cleaning up numbers: {e}")
        await callback.answer(f"❌ خطأ في التنظيف: {str(e)}")
        db.rollback()
    finally:
        db.close()

@dp.callback_query(F.data == "admin_cleanup_menu")
async def admin_cleanup_menu_handler(callback: CallbackQuery):
    """Show cleanup options menu"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    lang_code = get_user_language(str(callback.from_user.id))
    
    db = get_db()
    try:
        # Get unique service-country combinations with number counts
        combinations = db.query(
            Service.id, Service.name, Service.emoji,
            ServiceCountry.country_name, ServiceCountry.country_code, ServiceCountry.flag
        ).join(
            ServiceCountry, Service.id == ServiceCountry.service_id
        ).join(
            Number, and_(
                Number.service_id == Service.id,
                Number.country_code == ServiceCountry.country_code
            )
        ).filter(
            Service.active == True,
            ServiceCountry.active == True
        ).distinct().all()
        
        if not combinations:
            await callback.answer("❌ لا توجد أرقام للتنظيف")
            return
        
        text = await translator.translate_text("🗑 اختر ما تريد تنظيفه:", lang_code)
        text += "\n\n"
        
        keyboard = InlineKeyboardBuilder()
        
        # Add service-country combinations
        for service_id, service_name, emoji, country_name, country_code, flag in combinations[:20]:  # Limit to 20 for performance
            # Count numbers for this combination
            used_count = db.query(Number).filter(
                Number.service_id == service_id,
                Number.country_code == country_code,
                Number.status == NumberStatus.USED
            ).count()
            
            if used_count > 0:
                text += f"{emoji} {flag} {await get_text(service_name, lang_code)} - {country_name}: {used_count} رقم مستخدم\n"
                
                button_text = f"{emoji} {flag} {await get_text(service_name, lang_code)[:10]}"
                callback_data = f"cleanup_{service_id}_{country_code}"
                keyboard.row(InlineKeyboardButton(text=button_text, callback_data=callback_data))
        
        # Add general cleanup options
        keyboard.row(
            InlineKeyboardButton(text="🗑 تنظيف شامل (الكل)", callback_data="admin_cleanup_all"),
            InlineKeyboardButton(text="⏰ تنظيف المنتهية فقط", callback_data="admin_cleanup_expired")
        )
        keyboard.row(InlineKeyboardButton(text="🔙 إدارة الأرقام", callback_data="admin_numbers"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data.startswith("cleanup_"))
async def admin_cleanup_specific_handler(callback: CallbackQuery):
    """Handle specific service-country cleanup"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    # Parse callback data: cleanup_service_id_country_code
    parts = callback.data.split("_")
    if len(parts) != 3:
        await callback.answer("❌ خطأ في البيانات")
        return
    
    service_id = int(parts[1])
    country_code = parts[2]
    
    lang_code = get_user_language(str(callback.from_user.id))
    
    db = get_db()
    try:
        # Get service and country info
        service = db.query(Service).filter(Service.id == service_id).first()
        country = db.query(ServiceCountry).filter(
            ServiceCountry.service_id == service_id,
            ServiceCountry.country_code == country_code
        ).first()
        
        if not service or not country:
            await callback.answer("❌ البيانات غير صحيحة")
            return
        
        # Delete used numbers older than 7 days for this specific combination
        cutoff_date = datetime.now() - timedelta(days=7)
        
        deleted_count = db.query(Number).filter(
            Number.service_id == service_id,
            Number.country_code == country_code,
            Number.status == NumberStatus.USED,
            Number.code_received_at < cutoff_date
        ).delete()
        
        # Reset expired reservations for this combination
        expired_reservations = db.query(Reservation).join(Number).filter(
            Number.service_id == service_id,
            Number.country_code == country_code,
            Reservation.status == ReservationStatus.WAITING_CODE,
            Reservation.expired_at < datetime.now()
        ).all()
        
        reset_count = 0
        for reservation in expired_reservations:
            number = db.query(Number).filter(Number.id == reservation.number_id).first()
            if number:
                number.status = NumberStatus.AVAILABLE
                number.reserved_by_user_id = None
                number.reserved_at = None
                number.expires_at = None
                reset_count += 1
            reservation.status = ReservationStatus.EXPIRED
        
        db.commit()
        
        service_name = await get_text(service.name, lang_code)
        success_msg = await translator.translate_text(
            f"✅ تم تنظيف {service_name} - {country.country_name}\n"
            f"🗑 حذف: {deleted_count} رقم قديم\n"
            f"🔄 إعادة تعيين: {reset_count} حجز منتهي",
            lang_code
        )
        
        await callback.answer(success_msg, show_alert=True)
        
        # Return to cleanup menu
        await admin_cleanup_menu_handler(callback)
        
    except Exception as e:
        logger.error(f"Error in specific cleanup: {e}")
        await callback.answer("❌ حدث خطأ أثناء التنظيف")
        db.rollback()
    finally:
        db.close()

@dp.callback_query(F.data == "admin_cleanup_all")
async def admin_cleanup_all_handler(callback: CallbackQuery):
    """Handle complete cleanup (original functionality)"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    # Call the original cleanup function
    await admin_cleanup_numbers_handler(callback)

@dp.callback_query(F.data == "admin_cleanup_expired")
async def admin_cleanup_expired_handler(callback: CallbackQuery):
    """Handle cleanup of only expired reservations"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    lang_code = get_user_language(str(callback.from_user.id))
    
    db = get_db()
    try:
        # Reset expired reservations only
        expired_reservations = db.query(Reservation).filter(
            Reservation.status == ReservationStatus.WAITING_CODE,
            Reservation.expired_at < datetime.now()
        ).all()
        
        reset_count = 0
        for reservation in expired_reservations:
            number = db.query(Number).filter(Number.id == reservation.number_id).first()
            if number:
                number.status = NumberStatus.AVAILABLE
                number.reserved_by_user_id = None
                number.reserved_at = None
                number.expires_at = None
                reset_count += 1
            reservation.status = ReservationStatus.EXPIRED
        
        db.commit()
        
        success_msg = await translator.translate_text(
            f"✅ تم إعادة تعيين {reset_count} حجز منتهي الصلاحية فقط",
            lang_code
        )
        
        await callback.answer(success_msg, show_alert=True)
        
        # Return to cleanup menu
        await admin_cleanup_menu_handler(callback)
        
    except Exception as e:
        logger.error(f"Error cleaning expired reservations: {e}")
        await callback.answer("❌ حدث خطأ أثناء التنظيف")
        db.rollback()
    finally:
        db.close()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats_handler(callback: CallbackQuery):
    """Handle admin statistics"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        # Get general statistics
        total_users = db.query(User).count()
        active_users = db.query(User).filter(User.is_banned == False).count()
        total_services = db.query(Service).count()
        active_services = db.query(Service).filter(Service.active == True).count()
        total_numbers = db.query(Number).count()
        available_numbers = db.query(Number).filter(Number.status == NumberStatus.AVAILABLE).count()
        total_reservations = db.query(Reservation).count()
        completed_reservations = db.query(Reservation).filter(Reservation.status == ReservationStatus.COMPLETED).count()
        total_channels = db.query(Channel).count()
        
        # Get transaction statistics
        total_transactions = db.query(Transaction).count()
        total_revenue = db.query(Transaction).filter(Transaction.type == TransactionType.PURCHASE).count()
        
        text = f"📊 الإحصائيات العامة\n\n"
        text += f"👥 المستخدمين:\n"
        text += f"• إجمالي: {total_users}\n"
        text += f"• نشط: {active_users}\n\n"
        
        text += f"🛠 الخدمات:\n"
        text += f"• إجمالي: {total_services}\n"
        text += f"• نشط: {active_services}\n\n"
        
        text += f"📱 الأرقام:\n"
        text += f"• إجمالي: {total_numbers}\n"
        text += f"• متاح: {available_numbers}\n\n"
        
        text += f"📋 الحجوزات:\n"
        text += f"• إجمالي: {total_reservations}\n"
        text += f"• مكتمل: {completed_reservations}\n\n"
        
        text += f"📢 القنوات: {total_channels}\n"
        text += f"💰 المعاملات: {total_transactions}\n"
        text += f"💳 المبيعات: {total_revenue}\n"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="📊 إحصائيات الرسائل", callback_data="admin_messages_stats"),
            InlineKeyboardButton(text="🔄 تحديث الآن", callback_data="admin_stats_refresh")
        )
        keyboard.row(InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

# Add optimized refresh handler
@dp.callback_query(F.data == "admin_stats_refresh")
async def admin_stats_refresh_handler(callback: CallbackQuery):
    """Handle admin statistics refresh with loading indicator"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    # Show loading
    await callback.answer("🔄 جاري تحديث الإحصائيات...")
    
    # Call the main stats handler
    await admin_stats_handler(callback)

@dp.callback_query(F.data == "admin_search_user")
async def admin_search_user_handler(callback: CallbackQuery, state: FSMContext):
    """Handle search user request"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    await state.set_state(AdminStates.waiting_for_user_id_balance)
    await state.update_data(action_type="search")
    
    await callback.message.edit_text(
        "🔍 البحث عن مستخدم\n\n"
        "أرسل ID المستخدم أو @username للبحث:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin_users")]
        ])
    )

@dp.callback_query(F.data == "admin_list_users")
async def admin_list_users_handler(callback: CallbackQuery):
    """Handle list users request"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        # Optimize user list query with pagination
        users = db.query(User).order_by(User.joined_at.desc()).limit(10).all()
        
        text = "📋 قائمة المستخدمين (آخر 20)\n\n"
        
        for user in users:
            status = "✅" if not user.is_banned else "❌"
            admin_badge = "👑" if user.is_admin else ""
            username = f"@{user.username}" if user.username else "لا يوجد"
            
            text += f"{status}{admin_badge} {user.first_name or 'بدون اسم'}\n"
            text += f"   🆔 الآيدي: {user.telegram_id}\n"
            text += f"   👤 اليوزر: {username}\n"
            text += f"   💰 الرصيد: {user.balance} وحدة\n"
            text += f"   📅 انضم: {user.joined_at.strftime('%Y-%m-%d')}\n\n"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(InlineKeyboardButton(text="🔙 إدارة المستخدمين", callback_data="admin_users"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_handler(callback: CallbackQuery, state: FSMContext):
    """Handle broadcast message request"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    await state.set_state(AdminStates.waiting_for_broadcast_message)
    
    await callback.message.edit_text(
        "📢 إرسال رسالة جماعية\n\n"
        "أرسل الرسالة التي تريد إرسالها لجميع المستخدمين:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin")]
        ])
    )

@dp.message(AdminStates.waiting_for_broadcast_message)
async def handle_broadcast_message(message: types.Message, state: FSMContext):
    """Handle broadcast message input"""
    if not message.from_user or not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        await state.clear()
        return
    
    broadcast_text = message.text
    data = await state.get_data()
    is_private = data.get("is_private", False)
    
    db = get_db()
    try:
        if is_private:
            # Send private message
            target_user_id = data.get("target_user_id")
            target_user = db.query(User).filter(User.id == target_user_id).first()
            
            if not target_user:
                await message.reply("❌ حدث خطأ، لم يتم العثور على المستخدم")
                await state.clear()
                return
            
            try:
                await bot.send_message(int(target_user.telegram_id), broadcast_text)
                await message.reply(
                    f"✅ تم إرسال الرسالة الخاصة!\n\n"
                    f"👤 إلى: {target_user.first_name or target_user.username or target_user.telegram_id}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin")]
                    ])
                )
            except Exception as e:
                logger.error(f"Failed to send private message to {target_user.telegram_id}: {e}")
                await message.reply("❌ فشل في إرسال الرسالة")
        else:
            # Send broadcast message
            users = db.query(User).filter(User.is_banned == False).all()
            
            sent_count = 0
            failed_count = 0
            
            await message.reply(f"⏳ بدء إرسال الرسالة إلى {len(users)} مستخدم...")
            
            for user in users:
                try:
                    await bot.send_message(int(user.telegram_id), broadcast_text)
                    sent_count += 1
                except Exception as e:
                    logger.error(f"Failed to send broadcast to {user.telegram_id}: {e}")
                    failed_count += 1
            
            await message.reply(
                f"✅ تم إرسال الرسالة الجماعية!\n\n"
                f"📤 تم الإرسال إلى: {sent_count} مستخدم\n"
                f"❌ فشل الإرسال إلى: {failed_count} مستخدم",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin")]
                ])
            )
        
        await state.clear()
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_private_message")
async def admin_private_message_handler(callback: CallbackQuery, state: FSMContext):
    """Handle private message request"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    await state.set_state(AdminStates.waiting_for_user_id_balance)
    await state.update_data(action_type="private_message")
    
    await callback.message.edit_text(
        "💬 إرسال رسالة خاصة\n\n"
        "أرسل ID المستخدم أو @username:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin")]
        ])
    )

@dp.callback_query(F.data == "admin_maintenance")
async def admin_maintenance_handler(callback: CallbackQuery):
    """Handle maintenance mode toggle"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    global maintenance_mode
    
    current_status = "🔴 مفعل" if maintenance_mode else "🟢 معطل"
    new_status = "🟢 معطل" if maintenance_mode else "🔴 مفعل"
    
    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(
            text=f"{'🔴 إيقاف الصيانة' if maintenance_mode else '🔧 تفعيل الصيانة'}", 
            callback_data=f"toggle_maintenance_{'off' if maintenance_mode else 'on'}"
        )
    )
    keyboard.row(InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin"))
    
    await callback.message.edit_text(
        f"🔧 وضع الصيانة\n\n"
        f"الحالة الحالية: {current_status}\n\n"
        f"في وضع الصيانة، لن يتمكن المستخدمون من استخدام البوت عدا الأدمن.",
        reply_markup=keyboard.as_markup()
    )

@dp.callback_query(F.data.startswith("toggle_maintenance_"))
async def toggle_maintenance_handler(callback: CallbackQuery):
    """Toggle maintenance mode"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    global maintenance_mode
    action = callback.data.split("_")[-1]
    
    if action == "on":
        maintenance_mode = True
        await callback.answer("🔧 تم تفعيل وضع الصيانة", show_alert=True)
    else:
        maintenance_mode = False
        await callback.answer("🟢 تم إيقاف وضع الصيانة", show_alert=True)
    
    # Refresh the maintenance page
    await admin_maintenance_handler(callback)

@dp.callback_query(F.data == "admin_add_channel")
async def admin_add_channel_handler(callback: CallbackQuery, state: FSMContext):
    """Handle adding new channel"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    await state.set_state(AdminStates.waiting_for_channel_title)
    await callback.message.edit_text(
        "📢 إضافة قناة جديدة\n\n"
        "أدخل عنوان القناة:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin_channels")
        ]])
    )

@dp.callback_query(F.data == "admin_delete_channel")
async def admin_delete_channel_handler(callback: CallbackQuery):
    """Handle delete channel selection"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        channels = db.query(Channel).all()
        
        if not channels:
            await callback.answer("❌ لا توجد قنوات للحذف")
            return
        
        text = "🗑 حذف قناة\n\n"
        text += "اختر القناة التي تريد حذفها:\n\n"
        
        keyboard = InlineKeyboardBuilder()
        
        for channel in channels:
            status = "✅" if channel.active else "❌"
            keyboard.row(InlineKeyboardButton(
                text=f"{status} {channel.title} ({channel.reward_amount} وحدة)",
                callback_data=f"delete_channel_confirm_{channel.id}"
            ))
        
        keyboard.row(InlineKeyboardButton(text="🔙 إدارة القنوات", callback_data="admin_channels"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data.startswith("delete_channel_confirm_"))
async def delete_channel_confirm_handler(callback: CallbackQuery):
    """Handle channel deletion confirmation"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    channel_id = int(callback.data.split("_")[3])
    
    db = get_db()
    try:
        channel = db.query(Channel).filter(Channel.id == channel_id).first()
        if not channel:
            await callback.answer("❌ القناة غير موجودة")
            return
        
        # Delete all related user rewards
        deleted_rewards = db.query(UserChannelReward).filter(
            UserChannelReward.channel_id == channel_id
        ).delete()
        
        # Delete the channel
        channel_title = channel.title
        db.delete(channel)
        db.commit()
        
        await callback.answer(
            f"✅ تم حذف قناة {channel_title}\n"
            f"🗑 محذوف: {deleted_rewards} مكافأة", 
            show_alert=True
        )
        
        # Go back to channels management
        await admin_channels_handler(callback)
        
    except Exception as e:
        logger.error(f"Error deleting channel: {e}")
        await callback.answer("❌ حدث خطأ أثناء الحذف")
        db.rollback()
    finally:
        db.close()

@dp.callback_query(F.data == "admin_groups")
async def admin_groups_handler(callback: CallbackQuery):
    """Handle admin groups management"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        groups = db.query(Group).all()
        
        text = "👥 إدارة الجروبات\n\n"
        if groups:
            text += "الجروبات الحالية:\n"
            for group in groups:
                status = "✅" if group.active else "❌"
                text += f"{status} {group.title} - {group.reward_amount} وحدة\n"
                text += f"   🔗 {group.username_or_link}\n"
                text += f"   🆔 {group.group_id}\n\n"
        else:
            text += "لا توجد جروبات مضافة\n"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="➕ إضافة جروب", callback_data="admin_add_group"),
            InlineKeyboardButton(text="📋 عرض الجروبات", callback_data="admin_list_groups")
        )
        if groups:
            keyboard.row(InlineKeyboardButton(text="🗑 حذف جروب", callback_data="admin_delete_group"))
        keyboard.row(InlineKeyboardButton(text="🔙 إدارة القنوات", callback_data="admin_channels"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_delete_group")
async def admin_delete_group_handler(callback: CallbackQuery):
    """Handle delete group selection"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        groups = db.query(Group).all()
        
        if not groups:
            await callback.answer("❌ لا توجد جروبات للحذف")
            return
        
        text = "🗑 حذف جروب\n\n"
        text += "اختر الجروب الذي تريد حذفه:\n\n"
        
        keyboard = InlineKeyboardBuilder()
        
        for group in groups:
            status = "✅" if group.active else "❌"
            keyboard.row(InlineKeyboardButton(
                text=f"{status} {group.title} ({group.reward_amount} وحدة)",
                callback_data=f"delete_group_confirm_{group.id}"
            ))
        
        keyboard.row(InlineKeyboardButton(text="🔙 إدارة الجروبات", callback_data="admin_groups"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data.startswith("delete_group_confirm_"))
async def delete_group_confirm_handler(callback: CallbackQuery):
    """Handle group deletion confirmation"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    group_id = int(callback.data.split("_")[3])
    
    db = get_db()
    try:
        group = db.query(Group).filter(Group.id == group_id).first()
        if not group:
            await callback.answer("❌ الجروب غير موجود")
            return
        
        # Delete all related user rewards
        deleted_rewards = db.query(UserGroupReward).filter(
            UserGroupReward.group_id == group_id
        ).delete()
        
        # Delete the group
        group_title = group.title
        db.delete(group)
        db.commit()
        
        await callback.answer(
            f"✅ تم حذف جروب {group_title}\n"
            f"🗑 محذوف: {deleted_rewards} مكافأة", 
            show_alert=True
        )
        
        # Go back to groups management
        await admin_groups_handler(callback)
        
    except Exception as e:
        logger.error(f"Error deleting group: {e}")
        await callback.answer("❌ حدث خطأ أثناء الحذف")
        db.rollback()
    finally:
        db.close()

@dp.callback_query(F.data == "admin_list_channels")
async def admin_list_channels_handler(callback: CallbackQuery):
    """Handle list channels request"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        channels = db.query(Channel).all()
        
        text = "📋 قائمة القنوات\n\n"
        
        if channels:
            for channel in channels:
                status = "✅" if channel.active else "❌"
                text += f"{status} {channel.title}\n"
                text += f"   💰 المكافأة: {channel.reward_amount} وحدة\n"
                text += f"   🔗 {channel.username_or_link}\n"
                
                # Check if bot is in the channel
                try:
                    channel_username = channel.username_or_link
                    if channel_username.startswith('https://t.me/'):
                        channel_username = '@' + channel_username.split('/')[-1]
                    elif not channel_username.startswith('@'):
                        channel_username = '@' + channel_username
                    
                    bot_member = await bot.get_chat_member(channel_username, bot.id)
                    if bot_member.status in ['administrator', 'member']:
                        text += f"   🤖 البوت: متواجد\n"
                    else:
                        text += f"   🤖 البوت: غير متواجد ❌\n"
                except:
                    text += f"   🤖 البوت: غير معروف ❓\n"
                
                text += "\n"
        else:
            text += "لا توجد قنوات مضافة"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(InlineKeyboardButton(text="🔙 إدارة القنوات", callback_data="admin_channels"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_list_services")
async def admin_list_services_handler(callback: CallbackQuery):
    """Handle list services with delete/disable options"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    # Show loading indicator
    await callback.answer("🔄 جاري تحميل قائمة الخدمات...")
    
    db = get_db()
    try:
        services = db.query(Service).all()
        
        text = "📋 قائمة الخدمات\n\n"
        
        keyboard = InlineKeyboardBuilder()
        
        for service in services:
            status = "✅" if service.active else "❌"
            text += f"{status} {service.emoji} {service.name} - {service.default_price} وحدة\n"
            
            # Add buttons for each service
            toggle_text = "❌ إيقاف" if service.active else "✅ تفعيل"
            keyboard.row(
                InlineKeyboardButton(text=f"{toggle_text} {service.name}", callback_data=f"toggle_service_{service.id}"),
                InlineKeyboardButton(text=f"✏️ تعديل {service.name}", callback_data=f"edit_service_{service.id}")
            )
            keyboard.row(
                InlineKeyboardButton(text=f"🗑 حذف {service.name}", callback_data=f"delete_service_{service.id}")
            )
        
        text += "\n📝 اختر الإجراء المطلوب للخدمة:"
        
        keyboard.row(InlineKeyboardButton(text="🔙 إدارة الخدمات", callback_data="admin_services"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data.startswith("toggle_service_"))
async def toggle_service_handler(callback: CallbackQuery):
    """Toggle service active status"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    service_id = int(callback.data.split("_")[-1])
    
    db = get_db()
    try:
        service = db.query(Service).filter(Service.id == service_id).first()
        if not service:
            await callback.answer("❌ الخدمة غير موجودة")
            return
        
        service.active = not service.active
        db.commit()
        
        status_text = "تفعيل" if service.active else "إيقاف"
        await callback.answer(f"✅ تم {status_text} خدمة {service.name}")
        
        # Refresh the services list
        await admin_list_services_handler(callback)
        
    finally:
        db.close()

@dp.callback_query(F.data.startswith("delete_service_"))
async def delete_service_handler(callback: CallbackQuery):
    """Delete service"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    service_id = int(callback.data.split("_")[-1])
    
    db = get_db()
    try:
        service = db.query(Service).filter(Service.id == service_id).first()
        if not service:
            await callback.answer("❌ الخدمة غير موجودة")
            return
        
        # Check if service has active numbers
        active_numbers = db.query(Number).filter(
            Number.service_id == service_id,
            Number.status != NumberStatus.USED
        ).count()
        
        # Show confirmation with force delete option if numbers exist
        keyboard = InlineKeyboardBuilder()
        
        if active_numbers > 0:
            # Show warning with force delete option
            keyboard.row(
                InlineKeyboardButton(text="🗑 حذف إجباري (+ الأرقام)", callback_data=f"force_delete_service_{service_id}"),
                InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_list_services")
            )
            
            await callback.message.edit_text(
                f"⚠️ تحذير - الخدمة تحتوي على أرقام\n\n"
                f"🏷️ الخدمة: {service.name}\n"
                f"📱 الأرقام النشطة: {active_numbers} رقم\n\n"
                f"⚠️ الحذف الإجباري سيحذف:\n"
                f"• الخدمة نفسها\n"
                f"• جميع الأرقام المرتبطة بها\n"
                f"• جميع الحجوزات النشطة\n\n"
                f"هذا الإجراء لا يمكن التراجع عنه!",
                reply_markup=keyboard.as_markup()
            )
        else:
            # Normal delete confirmation
            keyboard.row(
                InlineKeyboardButton(text="✅ نعم، احذف", callback_data=f"confirm_delete_service_{service_id}"),
                InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_list_services")
            )
            
            await callback.message.edit_text(
                f"⚠️ تأكيد الحذف\n\n"
                f"هل أنت متأكد من حذف خدمة '{service.name}'؟\n"
                f"هذا الإجراء لا يمكن التراجع عنه!",
                reply_markup=keyboard.as_markup()
            )
        
    finally:
        db.close()

@dp.callback_query(F.data.startswith("confirm_delete_service_"))
async def confirm_delete_service_handler(callback: CallbackQuery):
    """Confirm service deletion"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    service_id = int(callback.data.split("_")[-1])
    
    db = get_db()
    try:
        service = db.query(Service).filter(Service.id == service_id).first()
        if not service:
            await callback.answer("❌ الخدمة غير موجودة")
            return
        
        service_name = service.name
        
        # Delete related data
        db.query(ServiceCountry).filter(ServiceCountry.service_id == service_id).delete()
        db.query(ServiceGroup).filter(ServiceGroup.service_id == service_id).delete()
        db.query(ServiceProviderMap).filter(ServiceProviderMap.service_id == service_id).delete()
        
        # Delete the service
        db.delete(service)
        db.commit()
        
        await callback.answer(f"✅ تم حذف خدمة {service_name}", show_alert=True)
        
        # Go back to services list
        await admin_list_services_handler(callback)
        
    except Exception as e:
        logger.error(f"Error deleting service: {e}")
        await callback.answer("❌ حدث خطأ أثناء الحذف")
        db.rollback()
    finally:
        db.close()

@dp.callback_query(F.data.startswith("force_delete_service_"))
async def force_delete_service_handler(callback: CallbackQuery):
    """Force delete service with all related numbers and reservations"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    service_id = int(callback.data.split("_")[-1])
    
    db = get_db()
    try:
        service = db.query(Service).filter(Service.id == service_id).first()
        if not service:
            await callback.answer("❌ الخدمة غير موجودة")
            return
        
        service_name = service.name
        
        # Delete all related reservations first
        deleted_reservations = db.query(Reservation).filter(
            Reservation.service_id == service_id
        ).delete()
        
        # Delete all numbers for this service
        deleted_numbers = db.query(Number).filter(
            Number.service_id == service_id
        ).delete()
        
        # Delete related service data
        db.query(ServiceCountry).filter(ServiceCountry.service_id == service_id).delete()
        db.query(ServiceGroup).filter(ServiceGroup.service_id == service_id).delete()
        db.query(ServiceProviderMap).filter(ServiceProviderMap.service_id == service_id).delete()
        
        # Delete the service
        db.delete(service)
        db.commit()
        
        await callback.answer(
            f"✅ تم حذف خدمة {service_name}\n"
            f"🗑 محذوف: {deleted_numbers} رقم، {deleted_reservations} حجز", 
            show_alert=True
        )
        
        # Go back to services list
        await admin_list_services_handler(callback)
        
    except Exception as e:
        logger.error(f"Error force deleting service: {e}")
        await callback.answer("❌ حدث خطأ أثناء الحذف الإجباري")
        db.rollback()
    finally:
        db.close()

@dp.callback_query(F.data.startswith("edit_service_"))
async def edit_service_handler(callback: CallbackQuery):
    """Handle service editing"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    service_id = int(callback.data.split("_")[-1])
    
    db = get_db()
    try:
        service = db.query(Service).filter(Service.id == service_id).first()
        if not service:
            await callback.answer("❌ الخدمة غير موجودة")
            return
        
        # Show service details with edit options
        text = f"✏️ تعديل الخدمة\n\n"
        text += f"🏷️ الاسم: {service.name}\n"
        text += f"🎨 الإيموجي: {service.emoji}\n"
        text += f"💰 السعر: {service.default_price} وحدة\n"
        text += f"📝 الوصف: {service.description or 'غير محدد'}\n"
        text += f"🔄 الحالة: {'نشط' if service.active else 'غير نشط'}\n\n"
        text += "اختر ما تريد تعديله:"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="🏷️ تعديل الاسم", callback_data=f"edit_service_name_{service_id}"),
            InlineKeyboardButton(text="🎨 تعديل الإيموجي", callback_data=f"edit_service_emoji_{service_id}")
        )
        keyboard.row(
            InlineKeyboardButton(text="💰 تعديل السعر", callback_data=f"edit_service_price_{service_id}"),
            InlineKeyboardButton(text="📝 تعديل الوصف", callback_data=f"edit_service_desc_{service_id}")
        )
        keyboard.row(InlineKeyboardButton(text="🔙 قائمة الخدمات", callback_data="admin_list_services"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

# Edit service property handlers
@dp.callback_query(F.data.startswith("edit_service_name_"))
async def edit_service_name_handler(callback: CallbackQuery, state: FSMContext):
    """Handle edit service name"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    service_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_service_id=service_id)
    await state.set_state(AdminStates.waiting_for_edit_service_name)
    
    await callback.message.edit_text(
        "🏷️ أدخل الاسم الجديد للخدمة:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 إلغاء", callback_data=f"edit_service_{service_id}")
        ]])
    )

@dp.callback_query(F.data.startswith("edit_service_emoji_"))
async def edit_service_emoji_handler(callback: CallbackQuery, state: FSMContext):
    """Handle edit service emoji"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    service_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_service_id=service_id)
    await state.set_state(AdminStates.waiting_for_edit_service_emoji)
    
    await callback.message.edit_text(
        "🎨 أدخل الإيموجي الجديد للخدمة:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 إلغاء", callback_data=f"edit_service_{service_id}")
        ]])
    )

@dp.callback_query(F.data.startswith("edit_service_price_"))
async def edit_service_price_handler(callback: CallbackQuery, state: FSMContext):
    """Handle edit service price"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    service_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_service_id=service_id)
    await state.set_state(AdminStates.waiting_for_edit_service_price)
    
    await callback.message.edit_text(
        "💰 أدخل السعر الجديد للخدمة (بالوحدات):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 إلغاء", callback_data=f"edit_service_{service_id}")
        ]])
    )

@dp.callback_query(F.data.startswith("edit_service_desc_"))
async def edit_service_desc_handler(callback: CallbackQuery, state: FSMContext):
    """Handle edit service description"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    service_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_service_id=service_id)
    await state.set_state(AdminStates.waiting_for_edit_service_description)
    
    await callback.message.edit_text(
        "📝 أدخل الوصف الجديد للخدمة (أو أرسل 'حذف' لحذف الوصف):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 إلغاء", callback_data=f"edit_service_{service_id}")
        ]])
    )

# Message handlers for editing service properties
@dp.message(StateFilter(AdminStates.waiting_for_edit_service_name))
async def process_edit_service_name(message: types.Message, state: FSMContext):
    """Process edited service name"""
    if not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        return
    
    data = await state.get_data()
    service_id = data.get('edit_service_id')
    new_name = message.text.strip()
    
    if not new_name:
        await message.reply("❌ يرجى إدخال اسم صحيح للخدمة")
        return
    
    db = get_db()
    try:
        service = db.query(Service).filter(Service.id == service_id).first()
        if not service:
            await message.reply("❌ الخدمة غير موجودة")
            return
        
        # Check if name already exists
        existing = db.query(Service).filter(
            Service.name == new_name,
            Service.id != service_id
        ).first()
        
        if existing:
            await message.reply("❌ اسم الخدمة موجود مسبقاً")
            return
        
        old_name = service.name
        service.name = new_name
        db.commit()
        
        await state.clear()
        await message.reply(
            f"✅ تم تغيير اسم الخدمة\n"
            f"من: {old_name}\n"
            f"إلى: {new_name}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 قائمة الخدمات", callback_data="admin_list_services")
            ]])
        )
        
    finally:
        db.close()

@dp.message(StateFilter(AdminStates.waiting_for_edit_service_emoji))
async def process_edit_service_emoji(message: types.Message, state: FSMContext):
    """Process edited service emoji"""
    if not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        return
    
    data = await state.get_data()
    service_id = data.get('edit_service_id')
    new_emoji = message.text.strip()
    
    if not new_emoji:
        new_emoji = "📱"  # Default emoji
    
    db = get_db()
    try:
        service = db.query(Service).filter(Service.id == service_id).first()
        if not service:
            await message.reply("❌ الخدمة غير موجودة")
            return
        
        old_emoji = service.emoji
        service.emoji = new_emoji
        db.commit()
        
        await state.clear()
        await message.reply(
            f"✅ تم تغيير إيموجي الخدمة {service.name}\n"
            f"من: {old_emoji}\n"
            f"إلى: {new_emoji}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 قائمة الخدمات", callback_data="admin_list_services")
            ]])
        )
        
    finally:
        db.close()

@dp.message(StateFilter(AdminStates.waiting_for_edit_service_price))
async def process_edit_service_price(message: types.Message, state: FSMContext):
    """Process edited service price"""
    if not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        return
    
    data = await state.get_data()
    service_id = data.get('edit_service_id')
    
    try:
        new_price = float(message.text.strip())
        if new_price < 0:
            await message.reply("❌ السعر يجب أن يكون رقم موجب")
            return
    except ValueError:
        await message.reply("❌ يرجى إدخال رقم صحيح للسعر")
        return
    
    db = get_db()
    try:
        service = db.query(Service).filter(Service.id == service_id).first()
        if not service:
            await message.reply("❌ الخدمة غير موجودة")
            return
        
        old_price = service.default_price
        service.default_price = new_price
        db.commit()
        
        await state.clear()
        await message.reply(
            f"✅ تم تغيير سعر الخدمة {service.name}\n"
            f"من: {old_price} وحدة\n"
            f"إلى: {new_price} وحدة",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 قائمة الخدمات", callback_data="admin_list_services")
            ]])
        )
        
    finally:
        db.close()

@dp.message(StateFilter(AdminStates.waiting_for_edit_service_description))
async def process_edit_service_description(message: types.Message, state: FSMContext):
    """Process edited service description"""
    if not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        return
    
    data = await state.get_data()
    service_id = data.get('edit_service_id')
    new_description = message.text.strip()
    
    # Allow deletion of description
    if new_description.lower() in ['حذف', 'delete', 'remove']:
        new_description = None
    
    db = get_db()
    try:
        service = db.query(Service).filter(Service.id == service_id).first()
        if not service:
            await message.reply("❌ الخدمة غير موجودة")
            return
        
        old_description = service.description or "غير محدد"
        service.description = new_description
        db.commit()
        
        await state.clear()
        
        new_desc_text = new_description or "تم حذف الوصف"
        await message.reply(
            f"✅ تم تغيير وصف الخدمة {service.name}\n"
            f"من: {old_description}\n"
            f"إلى: {new_desc_text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 قائمة الخدمات", callback_data="admin_list_services")
            ]])
        )
        
    finally:
        db.close()

# Additional handlers for user management actions
@dp.callback_query(F.data.startswith("ban_user_"))
async def ban_user_handler(callback: CallbackQuery):
    """Ban a user"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    user_id = int(callback.data.split("_")[-1])
    
    db = get_db()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            await callback.answer("❌ المستخدم غير موجود")
            return
        
        user.is_banned = True
        db.commit()
        
        await callback.answer(f"✅ تم حظر المستخدم {user.first_name or user.username}")
        
        # Notify the user
        try:
            await bot.send_message(int(user.telegram_id), "❌ تم حظرك من استخدام البوت")
        except:
            pass
        
    finally:
        db.close()

@dp.callback_query(F.data.startswith("unban_user_"))
async def unban_user_handler(callback: CallbackQuery):
    """Unban a user"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    user_id = int(callback.data.split("_")[-1])
    
    db = get_db()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            await callback.answer("❌ المستخدم غير موجود")
            return
        
        user.is_banned = False
        db.commit()
        
        await callback.answer(f"✅ تم إلغاء حظر المستخدم {user.first_name or user.username}")
        
        # Notify the user
        try:
            await bot.send_message(int(user.telegram_id), "✅ تم إلغاء حظرك، يمكنك الآن استخدام البوت")
        except:
            pass
        
    finally:
        db.close()

@dp.callback_query(F.data.startswith("quick_add_balance_"))
async def quick_add_balance_handler(callback: CallbackQuery, state: FSMContext):
    """Quick add balance"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    user_id = int(callback.data.split("_")[-1])
    
    await state.set_state(AdminStates.waiting_for_balance_amount)
    await state.update_data(action_type="add", target_user_id=user_id)
    
    await callback.message.edit_text(
        "💰 شحن رصيد سريع\n\n"
        "أرسل المبلغ المراد إضافته:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin_users")]
        ])
    )

@dp.callback_query(F.data.startswith("quick_deduct_balance_"))
async def quick_deduct_balance_handler(callback: CallbackQuery, state: FSMContext):
    """Quick deduct balance"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    user_id = int(callback.data.split("_")[-1])
    
    await state.set_state(AdminStates.waiting_for_balance_amount)
    await state.update_data(action_type="deduct", target_user_id=user_id)
    
    await callback.message.edit_text(
        "💳 خصم رصيد سريع\n\n"
        "أرسل المبلغ المراد خصمه:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin_users")]
        ])
    )

# Improved group verification for service groups
async def verify_bot_in_group(group_chat_id: str) -> bool:
    """Verify if bot is admin in the group"""
    try:
        # Check if bot is admin in the group
        bot_member = await bot.get_chat_member(str(group_chat_id), bot.id)
        return bot_member.status in ['administrator', 'creator']
    except Exception as e:
        logger.error(f"Error checking bot admin status in group {group_chat_id}: {e}")
        return False

@dp.callback_query(F.data == "admin_countries")
async def admin_countries_handler(callback: CallbackQuery):
    """Handle admin countries management"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        countries = db.query(Country).all()
        
        text = "🌍 إدارة الدول\n\n"
        
        if countries:
            text += "الدول المتاحة:\n"
            for country in countries:
                text += f"🏳️ {country.name} ({country.code})\n"
        else:
            text += "لا توجد دول مضافة\n"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="➕ إضافة دولة", callback_data="admin_add_country"),
            InlineKeyboardButton(text="📋 عرض الدول", callback_data="admin_list_countries")
        )
        keyboard.row(InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_add_country")
async def admin_add_country_handler(callback: CallbackQuery, state: FSMContext):
    """Handle adding new country"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    await state.set_state(AdminStates.waiting_for_country_name)
    await callback.message.edit_text(
        "🌍 إضافة دولة جديدة\n\n"
        "أدخل اسم الدولة:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin_countries")
        ]])
    )

@dp.callback_query(F.data == "admin_list_countries")
async def admin_list_countries_handler(callback: CallbackQuery):
    """Handle list countries request"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        countries = db.query(Country).all()
        
        text = "📋 قائمة الدول\n\n"
        
        keyboard = InlineKeyboardBuilder()
        
        for country in countries:
            text += f"🏳️ {country.name} ({country.code})\n"
            keyboard.row(
                InlineKeyboardButton(text=f"🗑 حذف {country.name}", callback_data=f"delete_country_{country.id}")
            )
        
        if not countries:
            text += "لا توجد دول مضافة"
        
        keyboard.row(InlineKeyboardButton(text="🔙 إدارة الدول", callback_data="admin_countries"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_settings")
async def admin_settings_handler(callback: CallbackQuery):
    """Handle admin settings"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    text = "⚙️ إعدادات النظام\n\n"
    text += f"🤖 البوت: نشط\n"
    text += f"🔧 وضع الصيانة: {'مفعل' if maintenance_mode else 'معطل'}\n"
    text += f"👑 أدمن ID: {ADMIN_ID}\n"
    
    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(text="🔧 تغيير وضع الصيانة", callback_data="admin_maintenance"),
        InlineKeyboardButton(text="📊 إحصائيات النظام", callback_data="admin_stats")
    )
    keyboard.row(
        InlineKeyboardButton(text="🔄 إعادة تشغيل البوت", callback_data="admin_restart_bot"),
        InlineKeyboardButton(text="📄 تصدير البيانات", callback_data="admin_export_data")
    )
    keyboard.row(InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin"))
    
    await callback.message.edit_text(text, reply_markup=keyboard.as_markup())

@dp.callback_query(F.data == "admin_messages_stats")
async def admin_messages_stats_handler(callback: CallbackQuery):
    """Handle admin messages statistics"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        # Get message statistics from service groups
        service_groups = db.query(ServiceGroup).all()
        
        text = "📊 إحصائيات الرسائل\n\n"
        
        if service_groups:
            text += "📱 حسب الخدمات:\n"
            for sg in service_groups:
                # Count received messages (you can add a messages table to track this)
                text += f"{sg.service.emoji} {sg.service.name}:\n"
                text += f"   📞 جروب: {sg.group_chat_id}\n"
                text += f"   📊 الحالة: {'نشط' if sg.active else 'معطل'}\n\n"
        else:
            text += "لا توجد خدمات مربوطة بجروبات\n"
        
        # Get general message stats
        total_reservations = db.query(Reservation).count()
        completed_reservations = db.query(Reservation).filter(
            Reservation.status == ReservationStatus.COMPLETED
        ).count()
        
        text += f"📈 إحصائيات عامة:\n"
        text += f"• إجمالي الطلبات: {total_reservations}\n"
        text += f"• طلبات مكتملة: {completed_reservations}\n"
        text += f"• معدل النجاح: {(completed_reservations/total_reservations*100):.1f}%" if total_reservations > 0 else "• معدل النجاح: 0%\n"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="🔄 تحديث", callback_data="admin_messages_stats"),
            InlineKeyboardButton(text="📊 إحصائيات عامة", callback_data="admin_stats")
        )
        keyboard.row(InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

@dp.callback_query(F.data == "admin_add_numbers")
async def admin_add_numbers_handler(callback: CallbackQuery, state: FSMContext):
    """Handle adding new numbers"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        services = db.query(Service).filter(Service.active == True).all()
        
        if not services:
            await callback.answer("❌ لا توجد خدمات نشطة لإضافة أرقام لها")
            return
        
        text = "➕ إضافة أرقام جديدة\n\n"
        text += "اختر الخدمة:\n"
        
        keyboard = InlineKeyboardBuilder()
        
        for service in services:
            keyboard.row(InlineKeyboardButton(
                text=f"{service.emoji} {service.name}",
                callback_data=f"add_numbers_service_{service.id}"
            ))
        
        keyboard.row(InlineKeyboardButton(text="🔙 إدارة الأرقام", callback_data="admin_numbers"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

# Additional handlers for channel management
@dp.message(AdminStates.waiting_for_channel_title)
async def handle_channel_title(message: types.Message, state: FSMContext):
    """Handle channel title input"""
    if not message.from_user or not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        await state.clear()
        return
    
    channel_title = message.text
    await state.update_data(channel_title=channel_title)
    await state.set_state(AdminStates.waiting_for_channel_username)
    
    await message.reply(
        f"📢 إضافة قناة: {channel_title}\n\n"
        "أدخل معرف القناة أو رابطها:\n"
        "مثال: @channel_name أو https://t.me/channel_name",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin_channels")
        ]])
    )

@dp.message(AdminStates.waiting_for_channel_username)
async def handle_channel_username(message: types.Message, state: FSMContext):
    """Handle channel username input"""
    if not message.from_user or not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        await state.clear()
        return
    
    channel_username = message.text
    await state.update_data(channel_username=channel_username)
    await state.set_state(AdminStates.waiting_for_channel_reward)
    
    await message.reply(
        f"💰 مكافأة القناة\n\n"
        f"أدخل مقدار المكافأة بالوحدات:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin_channels")
        ]])
    )

@dp.message(AdminStates.waiting_for_channel_reward)
async def handle_channel_reward(message: types.Message, state: FSMContext):
    """Handle channel reward input"""
    if not message.from_user or not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        await state.clear()
        return
    
    try:
        reward_amount = int(message.text)
        if reward_amount <= 0:
            await message.reply("❌ يجب أن تكون المكافأة أكبر من 0")
            return
        
        data = await state.get_data()
        channel_title = data.get('channel_title')
        channel_username = data.get('channel_username')
        
        # Add channel to database
        db = get_db()
        try:
            new_channel = Channel(
                title=channel_title,
                username_or_link=channel_username,
                reward_amount=reward_amount,
                active=True
            )
            db.add(new_channel)
            db.commit()
            
            await message.reply(
                f"✅ تم إضافة القناة بنجاح!\n\n"
                f"📢 العنوان: {channel_title}\n"
                f"🔗 الرابط: {channel_username}\n"
                f"💰 المكافأة: {reward_amount} وحدة",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔙 إدارة القنوات", callback_data="admin_channels")
                ]])
            )
            
        finally:
            db.close()
            
        await state.clear()
        
    except ValueError:
        await message.reply("❌ يرجى إدخال رقم صحيح للمكافأة")

# Country management handlers
@dp.message(AdminStates.waiting_for_country_name)
async def handle_country_name(message: types.Message, state: FSMContext):
    """Handle country name input"""
    if not message.from_user or not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        await state.clear()
        return
    
    country_name = message.text
    await state.update_data(country_name=country_name)
    await state.set_state(AdminStates.waiting_for_country_code)
    
    await message.reply(
        f"🌍 إضافة دولة: {country_name}\n\n"
        "أدخل رمز الدولة (مثال: SA, EG, AE):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin_countries")
        ]])
    )

@dp.message(AdminStates.waiting_for_country_code)
async def handle_country_code(message: types.Message, state: FSMContext):
    """Handle country code input"""
    if not message.from_user or not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        await state.clear()
        return
    
    country_code = message.text.upper()
    
    if len(country_code) != 2:
        await message.reply("❌ رمز الدولة يجب أن يكون حرفين فقط")
        return
    
    data = await state.get_data()
    country_name = data.get('country_name')
    
    db = get_db()
    try:
        # Check if country already exists
        existing = db.query(Country).filter(
            (Country.name == country_name) | (Country.code == country_code)
        ).first()
        
        if existing:
            await message.reply("❌ الدولة موجودة بالفعل")
            return
        
        # Add new country
        new_country = Country(
            name=country_name,
            code=country_code
        )
        db.add(new_country)
        db.commit()
        
        await message.reply(
            f"✅ تم إضافة الدولة بنجاح!\n\n"
            f"🏳️ الاسم: {country_name}\n"
            f"🔤 الرمز: {country_code}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 إدارة الدول", callback_data="admin_countries")
            ]])
        )
        
    finally:
        db.close()
    
    await state.clear()

# Additional settings handlers
@dp.callback_query(F.data == "admin_restart_bot")
async def admin_restart_bot_handler(callback: CallbackQuery):
    """Handle bot restart request"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    await callback.answer("🔄 إعادة تشغيل البوت...")
    await callback.message.edit_text(
        "🔄 جاري إعادة تشغيل البوت...\n\n"
        "سيتم إعادة تشغيل البوت خلال ثوانٍ"
    )
    
    # Exit the application (systemd or process manager will restart it)
    import sys
    sys.exit(0)

@dp.callback_query(F.data == "admin_export_data")
async def admin_export_data_handler(callback: CallbackQuery):
    """Handle data export request"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    db = get_db()
    try:
        # Get basic statistics for export summary
        users_count = db.query(User).count()
        services_count = db.query(Service).count()
        numbers_count = db.query(Number).count()
        reservations_count = db.query(Reservation).count()
        
        text = f"📄 تصدير البيانات\n\n"
        text += f"📊 ملخص البيانات:\n"
        text += f"• المستخدمين: {users_count}\n"
        text += f"• الخدمات: {services_count}\n"
        text += f"• الأرقام: {numbers_count}\n"
        text += f"• الحجوزات: {reservations_count}\n\n"
        text += f"💾 يمكنك تصدير البيانات كملف CSV"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="👥 تصدير المستخدمين", callback_data="export_users"),
            InlineKeyboardButton(text="📱 تصدير الأرقام", callback_data="export_numbers")
        )
        keyboard.row(
            InlineKeyboardButton(text="📋 تصدير الحجوزات", callback_data="export_reservations"),
            InlineKeyboardButton(text="💰 تصدير المعاملات", callback_data="export_transactions")
        )
        keyboard.row(InlineKeyboardButton(text="🔙 الإعدادات", callback_data="admin_settings"))
        
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        
    finally:
        db.close()

# Additional handlers for adding numbers
@dp.callback_query(F.data.startswith("add_numbers_service_"))
async def add_numbers_service_handler(callback: CallbackQuery, state: FSMContext):
    """Handle adding numbers for specific service"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    service_id = int(callback.data.split("_")[-1])
    
    db = get_db()
    try:
        service = db.query(Service).filter(Service.id == service_id).first()
        if not service:
            await callback.answer("❌ الخدمة غير موجودة")
            return
        
        await state.update_data(service_id=service_id)
        await state.set_state(AdminStates.waiting_for_numbers_input)
        
        await callback.message.edit_text(
            f"➕ إضافة أرقام لخدمة {service.emoji} {service.name}\n\n"
            f"أدخل الأرقام (رقم واحد في كل سطر):\n"
            f"مثال:\n"
            f"+966501234567\n"
            f"+966507654321\n"
            f"+966555123456",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin_add_numbers")
            ]])
        )
        
    finally:
        db.close()

@dp.message(AdminStates.waiting_for_numbers_input)
async def handle_numbers_input(message: types.Message, state: FSMContext):
    """Handle numbers input for adding"""
    if not message.from_user or not is_admin_session_valid(message.from_user.id):
        await message.reply("❌ انتهت صلاحية الجلسة")
        await state.clear()
        return
    
    numbers_text = message.text
    numbers = [line.strip() for line in numbers_text.split('\n') if line.strip()]
    
    if not numbers:
        await message.reply("❌ لم يتم إدخال أي أرقام")
        return
    
    data = await state.get_data()
    service_id = data.get('service_id')
    
    db = get_db()
    try:
        service = db.query(Service).filter(Service.id == service_id).first()
        if not service:
            await message.reply("❌ الخدمة غير موجودة")
            return
        
        added_count = 0
        duplicate_count = 0
        invalid_count = 0
        
        for number in numbers:
            # Normalize the number
            normalized_number = normalize_phone_number(number)
            
            # Basic validation
            if not normalized_number.startswith('+') or len(normalized_number) < 10:
                invalid_count += 1
                continue
            
            # Check if number already exists
            existing = db.query(Number).filter(Number.phone_number == normalized_number).first()
            if existing:
                duplicate_count += 1
                continue
            
            # Detect country code from the phone number
            detected_country_code = detect_country_code(normalized_number)
            
            # Ensure ServiceCountry exists for this country code
            ensure_service_country_exists(service_id, detected_country_code, db)
            
            # Add new number with detected country code
            new_number = Number(
                phone_number=normalized_number,
                service_id=service_id,
                country_code=detected_country_code,
                status=NumberStatus.AVAILABLE
            )
            db.add(new_number)
            added_count += 1
        
        db.commit()
        
        result_text = f"✅ تم إضافة الأرقام!\n\n"
        result_text += f"📱 تم إضافة: {added_count} رقم\n"
        if duplicate_count > 0:
            result_text += f"🔄 مكرر: {duplicate_count} رقم\n"
        if invalid_count > 0:
            result_text += f"❌ غير صالح: {invalid_count} رقم\n"
        
        await message.reply(
            result_text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 إدارة الأرقام", callback_data="admin_numbers")
            ]])
        )
        
    finally:
        db.close()
    
    await state.clear()

# Country deletion handler
@dp.callback_query(F.data.startswith("delete_country_"))
async def delete_country_handler(callback: CallbackQuery):
    """Handle country deletion"""
    if not is_admin_session_valid(callback.from_user.id):
        await callback.answer("❌ انتهت صلاحية الجلسة")
        return
    
    country_id = int(callback.data.split("_")[-1])
    
    db = get_db()
    try:
        country = db.query(Country).filter(Country.id == country_id).first()
        if not country:
            await callback.answer("❌ الدولة غير موجودة")
            return
        
        # Check if country is used in any service
        used_services = db.query(ServiceCountry).filter(ServiceCountry.country_id == country_id).count()
        if used_services > 0:
            await callback.answer(
                f"❌ لا يمكن حذف الدولة لأنها مربوطة بـ {used_services} خدمة",
                show_alert=True
            )
            return
        
        country_name = country.name
        db.delete(country)
        db.commit()
        
        await callback.answer(f"✅ تم حذف دولة {country_name}")
        
        # Refresh the countries list
        await admin_list_countries_handler(callback)
        
    finally:
        db.close()

# Initialize database
def init_db():
    """Initialize database tables"""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables created successfully")
        
        # Add default data
        db = get_db()
        try:
            # Add default admin user
            admin_user = db.query(User).filter(User.telegram_id == str(ADMIN_ID)).first()
            if not admin_user:
                admin_user = User(
                    telegram_id=str(ADMIN_ID),
                    username="admin",
                    first_name="Admin",
                    is_admin=True,
                    balance=1000
                )
                db.add(admin_user)
            
            # Add default services
            services_data = [
                {"name": "WhatsApp", "emoji": "📱", "default_price": 10},
                {"name": "Telegram", "emoji": "✈️", "default_price": 8},
                {"name": "Facebook", "emoji": "📘", "default_price": 12},
                {"name": "Instagram", "emoji": "📷", "default_price": 12},
                {"name": "Twitter", "emoji": "🐦", "default_price": 10},
            ]
            
            for service_data in services_data:
                existing = db.query(Service).filter(Service.name == service_data["name"]).first()
                if not existing:
                    service = Service(**service_data)
                    db.add(service)
            
            # Add default countries
            countries_data = [
                {"country_name": "مصر", "country_code": "+20", "flag": "🇪🇬"},
                {"country_name": "السعودية", "country_code": "+966", "flag": "🇸🇦"},
                {"country_name": "الإمارات", "country_code": "+971", "flag": "🇦🇪"},
                {"country_name": "الكويت", "country_code": "+965", "flag": "🇰🇼"},
                {"country_name": "قطر", "country_code": "+974", "flag": "🇶🇦"},
            ]
            
            services = db.query(Service).all()
            for service in services:
                for country_data in countries_data:
                    existing = db.query(ServiceCountry).filter(
                        ServiceCountry.service_id == service.id,
                        ServiceCountry.country_code == country_data["country_code"]
                    ).first()
                    if not existing:
                        country = ServiceCountry(
                            service_id=service.id,
                            **country_data
                        )
                        db.add(country)
            
            db.commit()
            logger.info("Default data added successfully")
            
        finally:
            db.close()
            
    except Exception as e:
        logger.error(f"Error initializing database: {e}")

async def main():
    """Main function"""
    # Initialize database
    init_db()
    
    # Set bot commands menu
    await set_bot_commands(bot)
    
    # Start background tasks
    asyncio.create_task(poll_provider_messages())
    asyncio.create_task(check_expired_reservations())
    
    # Start bot
    logger.info("Starting bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
