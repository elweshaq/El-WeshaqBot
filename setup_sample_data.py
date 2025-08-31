#!/usr/bin/env python3
"""Setup sample data for testing"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from decimal import Decimal

from models import (
    Base, User, Service, ServiceCountry, Number, Provider, ServiceProviderMap,
    NumberStatus, ProviderMode
)
from config import DATABASE_URL, ADMIN_ID

def setup_sample_data():
    """Setup initial sample data"""
    engine = create_engine(DATABASE_URL, echo=False)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    
    try:
        # Create admin user
        admin_user = db.query(User).filter(User.telegram_id == str(ADMIN_ID)).first()
        if not admin_user:
            admin_user = User(
                telegram_id=str(ADMIN_ID),
                username="admin",
                first_name="Admin",
                balance=Decimal("1000.00"),
                is_admin=True
            )
            db.add(admin_user)
        
        # Create sample services
        services_data = [
            {"name": "WhatsApp", "emoji": "📱", "default_price": Decimal("10.00")},
            {"name": "Telegram", "emoji": "💬", "default_price": Decimal("8.00")},
            {"name": "Facebook", "emoji": "📘", "default_price": Decimal("12.00")},
            {"name": "Instagram", "emoji": "📷", "default_price": Decimal("15.00")},
            {"name": "Twitter", "emoji": "🐦", "default_price": Decimal("9.00")}
        ]
        
        for service_data in services_data:
            service = db.query(Service).filter(Service.name == service_data["name"]).first()
            if not service:
                service = Service(**service_data)
                db.add(service)
                print(f"Created service: {service_data['name']}")
        
        db.commit()
        
        # Get services for adding countries and numbers
        whatsapp = db.query(Service).filter(Service.name == "WhatsApp").first()
        telegram = db.query(Service).filter(Service.name == "Telegram").first()
        
        if whatsapp:
            # Add countries for WhatsApp
            countries_data = [
                {"service_id": whatsapp.id, "country_name": "مصر", "country_code": "+20", "flag": "🇪🇬"},
                {"service_id": whatsapp.id, "country_name": "السعودية", "country_code": "+966", "flag": "🇸🇦"},
                {"service_id": whatsapp.id, "country_name": "الإمارات", "country_code": "+971", "flag": "🇦🇪"},
                {"service_id": whatsapp.id, "country_name": "الكويت", "country_code": "+965", "flag": "🇰🇼"}
            ]
            
            for country_data in countries_data:
                country = db.query(ServiceCountry).filter(
                    ServiceCountry.service_id == country_data["service_id"],
                    ServiceCountry.country_code == country_data["country_code"]
                ).first()
                if not country:
                    country = ServiceCountry(**country_data)
                    db.add(country)
                    print(f"Created country: {country_data['country_name']}")
        
        if telegram:
            # Add countries for Telegram
            countries_data = [
                {"service_id": telegram.id, "country_name": "مصر", "country_code": "+20", "flag": "🇪🇬"},
                {"service_id": telegram.id, "country_name": "السعودية", "country_code": "+966", "flag": "🇸🇦"}
            ]
            
            for country_data in countries_data:
                country = db.query(ServiceCountry).filter(
                    ServiceCountry.service_id == country_data["service_id"],
                    ServiceCountry.country_code == country_data["country_code"]
                ).first()
                if not country:
                    country = ServiceCountry(**country_data)
                    db.add(country)
        
        db.commit()
        
        # Add sample numbers
        if whatsapp:
            sample_numbers = [
                "+201234567890", "+201234567891", "+201234567892",
                "+966501234567", "+966501234568", "+971501234567"
            ]
            
            for phone_number in sample_numbers:
                country_code = phone_number[:4] if phone_number.startswith("+966") or phone_number.startswith("+971") else phone_number[:3]
                
                number = db.query(Number).filter(
                    Number.phone_number == phone_number,
                    Number.service_id == whatsapp.id
                ).first()
                if not number:
                    number = Number(
                        service_id=whatsapp.id,
                        country_code=country_code,
                        phone_number=phone_number,
                        status=NumberStatus.AVAILABLE
                    )
                    db.add(number)
                    print(f"Created number: {phone_number}")
        
        db.commit()
        print("Sample data setup completed successfully!")
        
    except Exception as e:
        print(f"Error setting up sample data: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    setup_sample_data()