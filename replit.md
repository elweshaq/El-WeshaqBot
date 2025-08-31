# replit.md

## Overview

This is a Telegram bot application that provides SMS number services for users. The bot allows users to purchase and use phone numbers from various countries and providers for receiving SMS messages. Key features include:

- User balance management and transactions
- Phone number reservations with timeout mechanisms
- Multi-provider integration with polling and webhook support
- Admin panel for service management
- Channel reward system for user engagement
- Real-time SMS code retrieval and delivery

## User Preferences

Preferred communication style: Simple, everyday language.

### Recent Updates (August 2025)
- **Token System Removed**: Simplified security for single-user groups - no longer requires secret tokens
- **Multi-Language Support**: Added 19 language support with Google Translate integration (Hebrew removed)
- **Quick Commands**: Added command menu for faster bot interaction (/balance, /language, /services, etc.)
- **User Language Preferences**: Bot remembers and uses user's preferred language for responses
- **Auto Country Detection**: Phone numbers now automatically detect and set country codes when added
- **Default Arabic Language**: New users automatically get Arabic as default language with option to change
- **Improved Localization**: Main menu buttons and messages properly localized based on user language
- **Bug Fixes**: Fixed various database interaction issues and tuple handling errors

## System Architecture

### Core Application Structure
- **Bot Framework**: Built using aiogram (Telegram Bot API wrapper) with FSM (Finite State Machine) for conversation flow management
- **Database Layer**: SQLAlchemy ORM with PostgreSQL backend for data persistence
- **Session Management**: Memory-based storage for bot states and admin authentication tracking
- **Asynchronous Design**: Full async/await pattern for handling concurrent user requests and external API calls

### Data Models
- **User System**: Comprehensive user management with balance tracking, admin privileges, and ban status
- **Service Management**: Hierarchical service-country-provider mapping for flexible number allocation
- **Reservation System**: Time-limited number reservations with automatic expiry and status tracking
- **Transaction System**: Complete audit trail for all balance operations (add, deduct, purchase, reward)
- **Provider Integration**: Support for multiple SMS providers with configurable polling/webhook modes

### Key Architectural Decisions
- **Scoped Sessions**: Database sessions are scoped to prevent connection leaks in long-running bot processes
- **Enum-based Status Management**: Type-safe status tracking for numbers, reservations, and transactions
- **Timeout-based Reservations**: Automatic cleanup of expired reservations to prevent number hoarding
- **Provider Abstraction**: Generic provider interface allowing easy integration of new SMS services
- **Admin Authentication**: Session-based admin access with configurable timeouts for security

### External API Integration
- **HTTP Client**: aiohttp for async external provider API communication
- **Polling Mechanism**: Configurable interval-based SMS checking for providers without webhook support
- **Error Handling**: Comprehensive exception handling for provider API failures and timeouts

## External Dependencies

### Core Infrastructure
- **PostgreSQL**: Primary database for persistent data storage
- **Telegram Bot API**: Core messaging platform integration via aiogram library
- **Environment Configuration**: dotenv for secure credential management

### Python Libraries
- **aiogram**: Telegram Bot API framework with FSM support
- **SQLAlchemy**: Database ORM with relationship mapping
- **aiohttp**: Async HTTP client for external API calls
- **python-dotenv**: Environment variable management

### SMS Provider APIs
- **Multiple Provider Support**: Generic interface for integrating various SMS service providers
- **Configurable Endpoints**: Provider-specific API configurations with timeout controls
- **Webhook/Polling Modes**: Flexible message retrieval methods based on provider capabilities

### Optional Integrations
- **CSV Export**: User data and transaction export functionality
- **JSON Configuration**: Dynamic service and provider configuration management
- **Channel Integration**: Telegram channel-based reward system for user engagement