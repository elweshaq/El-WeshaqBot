"""
Translation system with support for 20 languages
Supports both static translations and dynamic Google Translate API
"""

from googletrans import Translator
import asyncio
from functools import lru_cache
from typing import Dict, Optional

# Supported languages with their codes and names
SUPPORTED_LANGUAGES = {
    'ar': '🇸🇦 العربية',
    'en': '🇺🇸 English', 
    'es': '🇪🇸 Español',
    'fr': '🇫🇷 Français',
    'de': '🇩🇪 Deutsch',
    'it': '🇮🇹 Italiano',
    'pt': '🇵🇹 Português',
    'ru': '🇷🇺 Русский',
    'zh': '🇨🇳 中文',
    'ja': '🇯🇵 日本語',
    'ko': '🇰🇷 한국어',
    'tr': '🇹🇷 Türkçe',
    'hi': '🇮🇳 हिन्दी',
    'ur': '🇵🇰 اردو',
    'fa': '🇮🇷 فارسی',
    'id': '🇮🇩 Bahasa Indonesia',
    'ms': '🇲🇾 Bahasa Melayu',
    'th': '🇹🇭 ไทย',
    'vi': '🇻🇳 Tiếng Việt'
}

# Static translations for common phrases
STATIC_TRANSLATIONS = {
    # Main Menu
    'main_menu': {
        'ar': '🏠 القائمة الرئيسية',
        'en': '🏠 Main Menu',
        'es': '🏠 Menú Principal',
        'fr': '🏠 Menu Principal',
        'de': '🏠 Hauptmenü',
        'it': '🏠 Menu Principale',
        'pt': '🏠 Menu Principal',
        'ru': '🏠 Главное меню',
        'zh': '🏠 主菜单',
        'ja': '🏠 メインメニュー',
        'ko': '🏠 메인 메뉴',
        'tr': '🏠 Ana Menü',
        'hi': '🏠 मुख्य मेनू',
        'ur': '🏠 مین مینو',
        'fa': '🏠 منوی اصلی',
        'id': '🏠 Menu Utama',
        'ms': '🏠 Menu Utama',
        'th': '🏠 เมนูหลัก',
        'vi': '🏠 Menu Chính'
    },
    
    # Balance
    'my_balance': {
        'ar': '💰 رصيدي',
        'en': '💰 My Balance',
        'es': '💰 Mi Saldo',
        'fr': '💰 Mon Solde',
        'de': '💰 Mein Guthaben',
        'it': '💰 Il Mio Saldo',
        'pt': '💰 Meu Saldo',
        'ru': '💰 Мой баланс',
        'zh': '💰 我的余额',
        'ja': '💰 残高',
        'ko': '💰 내 잔액',
        'tr': '💰 Bakiyem',
        'hi': '💰 मेरा शेष',
        'ur': '💰 میرا بیلنس',
        'fa': '💰 موجودی من',
        'id': '💰 Saldo Saya',
        'ms': '💰 Baki Saya',
        'th': '💰 ยอดเงินของฉัน',
        'vi': '💰 Số Dư Của Tôi'
    },
    
    # Free Credits
    'free_credits': {
        'ar': '🆓 رصيد مجاني',
        'en': '🆓 Free Credits',
        'es': '🆓 Créditos Gratuitos',
        'fr': '🆓 Crédits Gratuits',
        'de': '🆓 Kostenlose Credits',
        'it': '🆓 Crediti Gratuiti',
        'pt': '🆓 Créditos Grátis',
        'ru': '🆓 Бесплатные кредиты',
        'zh': '🆓 免费积分',
        'ja': '🆓 無料クレジット',
        'ko': '🆓 무료 크레딧',
        'tr': '🆓 Ücretsiz Kredi',
        'hi': '🆓 मुफ्त क्रेडिट',
        'ur': '🆓 مفت کریڈٹ',
        'fa': '🆓 اعتبار رایگان',
        'id': '🆓 Kredit Gratis',
        'ms': '🆓 Kredit Percuma',
        'th': '🆓 เครดิตฟรี',
        'vi': '🆓 Tín Dụng Miễn Phí'
    },
    
    # Language
    'choose_language': {
        'ar': '🌐 اختر اللغة',
        'en': '🌐 Choose Language',
        'es': '🌐 Elegir Idioma',
        'fr': '🌐 Choisir la Langue',
        'de': '🌐 Sprache Wählen',
        'it': '🌐 Scegli Lingua',
        'pt': '🌐 Escolher Idioma',
        'ru': '🌐 Выбрать язык',
        'zh': '🌐 选择语言',
        'ja': '🌐 言語を選択',
        'ko': '🌐 언어 선택',
        'tr': '🌐 Dil Seç',
        'hi': '🌐 भाषा चुनें',
        'ur': '🌐 زبان منتخب کریں',
        'fa': '🌐 انتخاب زبان',
        'id': '🌐 Pilih Bahasa',
        'ms': '🌐 Pilih Bahasa',
        'th': '🌐 เลือกภาษา',
        'vi': '🌐 Chọn Ngôn Ngữ'
    },
    
    # Help
    'help': {
        'ar': 'ℹ️ المساعدة',
        'en': 'ℹ️ Help',
        'es': 'ℹ️ Ayuda',
        'fr': 'ℹ️ Aide',
        'de': 'ℹ️ Hilfe',
        'it': 'ℹ️ Aiuto',
        'pt': 'ℹ️ Ajuda',
        'ru': 'ℹ️ Помощь',
        'zh': 'ℹ️ 帮助',
        'ja': 'ℹ️ ヘルプ',
        'ko': 'ℹ️ 도움말',
        'tr': 'ℹ️ Yardım',
        'hi': 'ℹ️ सहायता',
        'ur': 'ℹ️ مدد',
        'fa': 'ℹ️ راهنما',
        'id': 'ℹ️ Bantuan',
        'ms': 'ℹ️ Bantuan',
        'th': 'ℹ️ ช่วยเหลือ',
        'vi': 'ℹ️ Trợ Giúp'
    },
    
    # Settings
    'settings': {
        'ar': '⚙️ الإعدادات',
        'en': '⚙️ Settings',
        'es': '⚙️ Configuración',
        'fr': '⚙️ Paramètres',
        'de': '⚙️ Einstellungen',
        'it': '⚙️ Impostazioni',
        'pt': '⚙️ Configurações',
        'ru': '⚙️ Настройки',
        'zh': '⚙️ 设置',
        'ja': '⚙️ 設定',
        'ko': '⚙️ 설정',
        'tr': '⚙️ Ayarlar',
        'hi': '⚙️ सेटिंग्स',
        'ur': '⚙️ سیٹنگز',
        'fa': '⚙️ تنظیمات',
        'id': '⚙️ Pengaturan',
        'ms': '⚙️ Tetapan',
        'th': '⚙️ การตั้งค่า',
        'vi': '⚙️ Cài Đặt'
    },
    
    # Help
    'help': {
        'ar': 'ℹ️ مساعدة',
        'en': 'ℹ️ Help',
        'es': 'ℹ️ Ayuda',
        'fr': 'ℹ️ Aide',
        'de': 'ℹ️ Hilfe',
        'it': 'ℹ️ Aiuto',
        'pt': 'ℹ️ Ajuda',
        'ru': 'ℹ️ Помощь',
        'zh': 'ℹ️ 帮助',
        'ja': 'ℹ️ ヘルプ',
        'ko': 'ℹ️ 도움말',
        'tr': 'ℹ️ Yardım',
        'hi': 'ℹ️ सहायता',
        'ur': 'ℹ️ مدد',
        'fa': 'ℹ️ راهنما',
        'id': 'ℹ️ Bantuan',
        'ms': 'ℹ️ Bantuan',
        'th': 'ℹ️ ช่วยเหลือ',
        'vi': 'ℹ️ Trợ Giúp'
    },
    
    # Settings
    'settings': {
        'ar': '⚙️ الإعدادات',
        'en': '⚙️ Settings',
        'es': '⚙️ Configuración',
        'fr': '⚙️ Paramètres',
        'de': '⚙️ Einstellungen',
        'it': '⚙️ Impostazioni',
        'pt': '⚙️ Configurações',
        'ru': '⚙️ Настройки',
        'zh': '⚙️ 设置',
        'ja': '⚙️ 設定',
        'ko': '⚙️ 설정',
        'tr': '⚙️ Ayarlar',
        'hi': '⚙️ सेटिंग्स',
        'ur': '⚙️ سیٹنگز',
        'fa': '⚙️ تنظیمات',
        'id': '⚙️ Pengaturan',
        'ms': '⚙️ Tetapan',
        'th': '⚙️ การตั้งค่า',
        'vi': '⚙️ Cài Đặt'
    },
    
    # Language Selection
    'choose_language': {
        'ar': '🌐 اختر اللغة',
        'en': '🌐 Choose Language',
        'es': '🌐 Elegir Idioma',
        'fr': '🌐 Choisir la Langue',
        'de': '🌐 Sprache Wählen',
        'it': '🌐 Scegli Lingua',
        'pt': '🌐 Escolher Idioma',
        'ru': '🌐 Выбрать язык',
        'zh': '🌐 选择语言',
        'ja': '🌐 言語を選択',
        'ko': '🌐 언어 선택',
        'tr': '🌐 Dil Seç',
        'hi': '🌐 भाषा चुनें',
        'ur': '🌐 زبان منتخب کریں',
        'fa': '🌐 انتخاب زبان',
        'id': '🌐 Pilih Bahasa',
        'ms': '🌐 Pilih Bahasa',
        'th': '🌐 เลือกภาษา',
        'vi': '🌐 Chọn Ngôn Ngữ'
    },
    
    # Admin Panel
    'admin_panel': {
        'ar': '👨‍💼 لوحة الإدارة',
        'en': '👨‍💼 Admin Panel',
        'es': '👨‍💼 Panel de Administración',
        'fr': '👨‍💼 Panneau d\'Administration',
        'de': '👨‍💼 Admin-Panel',
        'it': '👨‍💼 Pannello Admin',
        'pt': '👨‍💼 Painel Admin',
        'ru': '👨‍💼 Панель администратора',
        'zh': '👨‍💼 管理面板',
        'ja': '👨‍💼 管理パネル',
        'ko': '👨‍💼 관리자 패널',
        'tr': '👨‍💼 Yönetici Paneli',
        'hi': '👨‍💼 एडमिन पैनल',
        'ur': '👨‍💼 ایڈمن پینل',
        'fa': '👨‍💼 پنل مدیریت',
        'id': '👨‍💼 Panel Admin',
        'ms': '👨‍💼 Panel Admin',
        'th': '👨‍💼 แผงผู้ดูแลระบบ',
        'vi': '👨‍💼 Bảng Quản Trị'
    },
    
    # Admin Login
    'admin_password_prompt': {
        'ar': '🔐 أدخل كلمة مرور الإدارة:',
        'en': '🔐 Enter admin password:',
        'es': '🔐 Ingrese la contraseña de administrador:',
        'fr': '🔐 Entrez le mot de passe administrateur:',
        'de': '🔐 Admin-Passwort eingeben:',
        'it': '🔐 Inserisci la password admin:',
        'pt': '🔐 Digite a senha do administrador:',
        'ru': '🔐 Введите пароль администратора:',
        'zh': '🔐 输入管理员密码:',
        'ja': '🔐 管理者パスワードを入力:',
        'ko': '🔐 관리자 비밀번호 입력:',
        'tr': '🔐 Yönetici şifresini girin:',
        'hi': '🔐 एडमिन पासवर्ड दर्ज करें:',
        'ur': '🔐 ایڈمن پاس ورڈ داخل کریں:',
        'fa': '🔐 رمز عبور مدیر را وارد کنید:',
        'id': '🔐 Masukkan password admin:',
        'ms': '🔐 Masukkan kata laluan admin:',
        'th': '🔐 ป้อนรหัสผ่านแอดมิน:',
        'vi': '🔐 Nhập mật khẩu quản trị:'
    },
    
    'admin_login_success': {
        'ar': '✅ تم تسجيل الدخول بنجاح',
        'en': '✅ Login successful',
        'es': '✅ Inicio de sesión exitoso',
        'fr': '✅ Connexion réussie',
        'de': '✅ Anmeldung erfolgreich',
        'it': '✅ Accesso riuscito',
        'pt': '✅ Login bem-sucedido',
        'ru': '✅ Вход выполнен успешно',
        'zh': '✅ 登录成功',
        'ja': '✅ ログイン成功',
        'ko': '✅ 로그인 성공',
        'tr': '✅ Giriş başarılı',
        'hi': '✅ लॉगिन सफल',
        'ur': '✅ لاگ ان کامیاب',
        'fa': '✅ ورود موفقیت‌آمیز',
        'id': '✅ Login berhasil',
        'ms': '✅ Log masuk berjaya',
        'th': '✅ เข้าสู่ระบบสำเร็จ',
        'vi': '✅ Đăng nhập thành công'
    },
    
    'admin_login_failed': {
        'ar': '❌ كلمة مرور خاطئة',
        'en': '❌ Wrong password',
        'es': '❌ Contraseña incorrecta',
        'fr': '❌ Mot de passe incorrect',
        'de': '❌ Falsches Passwort',
        'it': '❌ Password sbagliata',
        'pt': '❌ Senha incorreta',
        'ru': '❌ Неверный пароль',
        'zh': '❌ 密码错误',
        'ja': '❌ パスワードが間違っています',
        'ko': '❌ 잘못된 비밀번호',
        'tr': '❌ Yanlış şifre',
        'hi': '❌ गलत पासवर्ड',
        'ur': '❌ غلط پاس ورڈ',
        'fa': '❌ رمز عبور اشتباه',
        'id': '❌ Password salah',
        'ms': '❌ Kata laluan salah',
        'th': '❌ รหัสผ่านผิด',
        'vi': '❌ Mật khẩu sai'
    },
    
    'choose_section': {
        'ar': 'اختر القسم المطلوب:',
        'en': 'Choose section:',
        'es': 'Elige la sección:',
        'fr': 'Choisissez la section:',
        'de': 'Bereich wählen:',
        'it': 'Scegli la sezione:',
        'pt': 'Escolha a seção:',
        'ru': 'Выберите раздел:',
        'zh': '选择部分:',
        'ja': 'セクションを選択:',
        'ko': '섹션 선택:',
        'tr': 'Bölüm seçin:',
        'hi': 'सेक्शन चुनें:',
        'ur': 'سیکشن منتخب کریں:',
        'fa': 'بخش را انتخاب کنید:',
        'id': 'Pilih bagian:',
        'ms': 'Pilih bahagian:',
        'th': 'เลือกส่วน:',
        'vi': 'Chọn phần:'
    },
    
    # Command descriptions
    'services': {
        'ar': 'خدمات الأرقام',
        'en': 'Phone Services',
        'es': 'Servicios Telefónicos',
        'fr': 'Services Téléphoniques',
        'de': 'Telefondienste',
        'it': 'Servizi Telefonici',
        'pt': 'Serviços Telefônicos',
        'ru': 'Телефонные услуги',
        'zh': '电话服务',
        'ja': '電話サービス',
        'ko': '전화 서비스',
        'tr': 'Telefon Hizmetleri',
        'hi': 'फोन सेवाएं',
        'ur': 'فون سروسز',
        'fa': 'سرویس‌های تلفن',
        'id': 'Layanan Telepon',
        'ms': 'Perkhidmatan Telefon',
        'th': 'บริการโทรศัพท์',
        'vi': 'Dịch Vụ Điện Thoại'
    },
    
    'history': {
        'ar': 'سجل الطلبات',
        'en': 'Order History',
        'es': 'Historial de Pedidos',
        'fr': 'Historique des Commandes',
        'de': 'Bestellverlauf',
        'it': 'Cronologia Ordini',
        'pt': 'Histórico de Pedidos',
        'ru': 'История заказов',
        'zh': '订单历史',
        'ja': '注文履歴',
        'ko': '주문 내역',
        'tr': 'Sipariş Geçmişi',
        'hi': 'ऑर्डर इतिहास',
        'ur': 'آرڈر کی تاریخ',
        'fa': 'تاریخچه سفارشات',
        'id': 'Riwayat Pesanan',
        'ms': 'Sejarah Pesanan',
        'th': 'ประวัติการสั่งซื้อ',
        'vi': 'Lịch Sử Đặt Hàng'
    },
    
    'support': {
        'ar': 'الدعم الفني',
        'en': 'Technical Support',
        'es': 'Soporte Técnico',
        'fr': 'Support Technique',
        'de': 'Technischer Support',
        'it': 'Supporto Tecnico',
        'pt': 'Suporte Técnico',
        'ru': 'Техническая поддержка',
        'zh': '技术支持',
        'ja': 'テクニカルサポート',
        'ko': '기술 지원',
        'tr': 'Teknik Destek',
        'hi': 'तकनीकी सहायता',
        'ur': 'تکنیکی سپورٹ',
        'fa': 'پشتیبانی فنی',
        'id': 'Dukungan Teknis',
        'ms': 'Sokongan Teknikal',
        'th': 'การสนับสนุนทางเทคนิค',
        'vi': 'Hỗ Trợ Kỹ Thuật'
    },
    
    'cancel': {
        'ar': 'إلغاء العملية',
        'en': 'Cancel Operation',
        'es': 'Cancelar Operación',
        'fr': 'Annuler l\'Opération',
        'de': 'Vorgang Abbrechen',
        'it': 'Annulla Operazione',
        'pt': 'Cancelar Operação',
        'ru': 'Отменить операцию',
        'zh': '取消操作',
        'ja': '操作をキャンセル',
        'ko': '작업 취소',
        'tr': 'İşlemi İptal Et',
        'hi': 'ऑपरेशन रद्द करें',
        'ur': 'آپریشن منسوخ کریں',
        'fa': 'لغو عملیات',
        'id': 'Batalkan Operasi',
        'ms': 'Batal Operasi',
        'th': 'ยกเลิกการดำเนินการ',
        'vi': 'Hủy Thao Tác'
    },
    
    'group_info': {
        'ar': 'معلومات الجروب',
        'en': 'Group Info',
        'es': 'Información del Grupo',
        'fr': 'Informations du Groupe',
        'de': 'Gruppeninfo',
        'it': 'Info Gruppo',
        'pt': 'Informações do Grupo',
        'ru': 'Информация о группе',
        'zh': '群组信息',
        'ja': 'グループ情報',
        'ko': '그룹 정보',
        'tr': 'Grup Bilgisi',
        'hi': 'समूह जानकारी',
        'ur': 'گروپ کی معلومات',
        'fa': 'اطلاعات گروه',
        'id': 'Info Grup',
        'ms': 'Maklumat Kumpulan',
        'th': 'ข้อมูลกลุ่ม',
        'vi': 'Thông Tin Nhóm'
    }
}

class TranslationManager:
    def __init__(self):
        try:
            self.translator = Translator()
        except Exception as e:
            print(f"Failed to initialize Google Translator: {e}")
            self.translator = None
        
    @lru_cache(maxsize=1000)
    def get_static_text(self, key: str, lang_code: str = 'ar') -> str:
        """Get static translation for common phrases"""
        if key in STATIC_TRANSLATIONS:
            # Try to get the requested language first
            if lang_code in STATIC_TRANSLATIONS[key]:
                return STATIC_TRANSLATIONS[key][lang_code]
            # If not found, try English as fallback
            elif 'en' in STATIC_TRANSLATIONS[key]:
                return STATIC_TRANSLATIONS[key]['en']
            # Last resort: Arabic
            else:
                return STATIC_TRANSLATIONS[key]['ar']
        return key
    
    async def translate_text(self, text: str, target_lang: str = 'ar', source_lang: str = 'auto') -> str:
        """Translate text using Google Translate API"""
        try:
            # If target language is the same as source, return original text    
            if target_lang == source_lang:
                return text
            
            # Don't auto-skip Arabic translation - let Google Translate handle it
            # This ensures proper translation even when target is Arabic
                
            # If translator is not available, return original text
            if not self.translator:
                print("Google Translator not available, returning original text")
                return text
                
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, 
                lambda: self.translator.translate(text, dest=target_lang, src=source_lang) if self.translator else None
            )
            
            if result and hasattr(result, 'text') and result.text:
                return result.text
            else:
                print("Translation result is empty or invalid")
                return text
                
        except Exception as e:
            print(f"Translation error: {e}")
            # Fallback: try to reinitialize translator
            try:
                self.translator = Translator()
                print("Translator reinitialized successfully")
            except:
                print("Failed to reinitialize translator")
            return text  # Return original text if translation fails
    
    def get_language_name(self, lang_code: str) -> str:
        """Get language name with flag"""
        return SUPPORTED_LANGUAGES.get(lang_code, '🇸🇦 العربية')
    
    def get_language_codes(self) -> Dict[str, str]:
        """Get all supported language codes"""
        return SUPPORTED_LANGUAGES

# Global translator instance
translator = TranslationManager()

def t(key: str, lang_code: str = 'ar') -> str:
    """Quick function to get static translations"""
    return translator.get_static_text(key, lang_code)

async def translate(text: str, lang_code: str = 'ar') -> str:
    """Quick function to translate dynamic text"""
    return await translator.translate_text(text, lang_code)