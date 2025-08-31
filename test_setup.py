#!/usr/bin/env python3
"""
Test setup script for the Telegram bot with Group ID functionality
This script adds sample data to test the system
"""

import asyncio
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import (
    Base, User, Service, ServiceCountry, Number, ServiceGroup,
    NumberStatus, SecurityMode
)
from config import DATABASE_URL

# Database setup
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)

def add_test_data():
    """Add test data to the database"""
    db = SessionLocal()
    
    try:
        # Create test service
        test_service = Service(
            name="WhatsApp Test",
            emoji="📱",
            description="Test WhatsApp service for Group ID functionality",
            default_price=Decimal('5.00'),
            active=True
        )
        db.add(test_service)
        db.flush()  # Get service ID
        
        # Create test service country
        test_country = ServiceCountry(
            service_id=test_service.id,
            country_name="مصر",
            country_code="+20",
            flag="🇪🇬",
            active=True
        )
        db.add(test_country)
        
        # Add test numbers
        test_numbers = [
            Number(
                service_id=test_service.id,
                country_code="+20",
                phone_number="+201234567890",
                status=NumberStatus.AVAILABLE
            ),
            Number(
                service_id=test_service.id,
                country_code="+20", 
                phone_number="+201234567891",
                status=NumberStatus.AVAILABLE
            ),
            Number(
                service_id=test_service.id,
                country_code="+20",
                phone_number="+201234567892", 
                status=NumberStatus.AVAILABLE
            )
        ]
        
        for number in test_numbers:
            db.add(number)
        
        # Create test service group (this would normally be done through admin interface)
        # Note: Replace -1001234567890 with your actual test group ID
        test_group = ServiceGroup(
            service_id=test_service.id,
            group_chat_id="-1001234567890",  # ⚠️ PLACEHOLDER - يجب تغييره لـ Group ID حقيقي
            group_title="Test Group",
            secret_token="TEST_TOKEN_123",
            regex_pattern=r'\b\d{4,6}\b',
            security_mode=SecurityMode.TOKEN_ONLY,
            active=True
        )
        db.add(test_group)
        
        db.commit()
        print("✅ Test data added successfully!")
        print(f"📱 Service: {test_service.name} (ID: {test_service.id})")
        print(f"🌍 Country: {test_country.country_name} {test_country.flag}")
        print(f"📞 Numbers added: {len(test_numbers)}")
        print(f"🔗 Group mapping: {test_group.group_chat_id}")
        print(f"🔑 Security mode: {test_group.security_mode.value}")
        print(f"📝 Regex pattern: {test_group.regex_pattern}")
        print(f"🔐 Secret token: {test_group.secret_token}")
        print()
        print("📋 To test the system:")
        print("1. Add the bot to your test group")
        print("2. Update the group_chat_id in the database with your actual group ID")
        print("3. Send a test message like: 'to:+201234567890 code:123456 token:TEST_TOKEN_123'")
        print("4. Check that the bot processes the message and completes any matching reservations")
        
    except Exception as e:
        print(f"❌ Error adding test data: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    add_test_data()