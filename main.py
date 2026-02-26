import os
import logging
import random
import json
import hmac
import hashlib
from datetime import datetime
from typing import Optional, List, Dict
from contextlib import asynccontextmanager

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn
from sqlalchemy import create_engine, Column, Integer, String, BigInteger, Boolean, Float, ForeignKey, TIMESTAMP, Text, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from sqlalchemy.pool import StaticPool
from pydantic import BaseModel
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "momnetk")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bot.db")
PORT = int(os.getenv("PORT", 8000))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# Настройки экономики
SELL_PERCENT = 0.7  # Продажа NFT за 70% от цены

# Инициализация базы данных
engine = create_engine(
    DATABASE_URL, 
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    poolclass=StaticPool if "sqlite" in DATABASE_URL else None
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Модели базы данных
class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(String)
    first_name = Column(String)
    last_name = Column(String)
    stars_balance = Column(BigInteger, default=0)
    total_spent_stars = Column(BigInteger, default=0)
    total_cases_opened = Column(Integer, default=0)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)

class NFT(Base):
    __tablename__ = "nfts"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text)
    rarity = Column(String, nullable=False)
    price = Column(Integer, nullable=False)
    image_url = Column(String)
    is_active = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

class Case(Base):
    __tablename__ = "cases"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text)
    price_stars = Column(Integer, nullable=False)
    image_url = Column(String)
    is_active = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

class CaseNFT(Base):
    __tablename__ = "case_nfts"
    
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"))
    nft_id = Column(Integer, ForeignKey("nfts.id", ondelete="CASCADE"))
    chance = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True)

class UserNFT(Base):
    __tablename__ = "user_nfts"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    nft_id = Column(Integer, ForeignKey("nfts.id", ondelete="CASCADE"))
    is_sold = Column(Boolean, default=False)
    sold_price = Column(Integer)
    opened_from_case_id = Column(Integer, ForeignKey("cases.id", ondelete="SET NULL"))
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

class OpeningHistory(Base):
    __tablename__ = "opening_history"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"))
    nft_id = Column(Integer, ForeignKey("nfts.id", ondelete="CASCADE"))
    stars_spent = Column(Integer, nullable=False)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

# Создание таблиц
Base.metadata.create_all(bind=engine)

# Pydantic модели для API
class NFTSchema(BaseModel):
    id: int
    name: str
    description: Optional[str]
    rarity: str
    price: int
    image_url: Optional[str]
    
    class Config:
        from_attributes = True

class CaseSchema(BaseModel):
    id: int
    name: str
    description: Optional[str]
    price_stars: int
    image_url: Optional[str]
    nfts: List[dict] = []
    
    class Config:
        from_attributes = True

class OpenCaseRequest(BaseModel):
    case_id: int
    init_data: str

# Сервисы
class CaseService:
    @staticmethod
    def open_case(case_nfts: List[dict]) -> dict:
        """Открытие кейса с учетом шансов выпадения"""
        if not case_nfts:
            return None
        
        items = []
        weights = []
        for item in case_nfts:
            items.append(item)
            weights.append(item['chance'])
        
        # Используем random.choices для честного выпадения
        selected = random.choices(items, weights=weights, k=1)[0]
        return selected
    
    @staticmethod
    def get_case_nfts(db: Session, case_id: int):
        """Получение всех NFT в кейсе с шансами"""
        case_nfts = db.query(
            CaseNFT, NFT
        ).join(
            NFT, CaseNFT.nft_id == NFT.id
        ).filter(
            CaseNFT.case_id == case_id,
            CaseNFT.is_active == True,
            NFT.is_active == True
        ).all()
        
        result = []
        for case_nft, nft in case_nfts:
            result.append({
                'id': nft.id,
                'name': nft.name,
                'description': nft.description,
                'rarity': nft.rarity,
                'price': nft.price,
                'image_url': nft.image_url,
                'chance': case_nft.chance
            })
        
        return result

class UserService:
    @staticmethod
    def get_or_create_user(db: Session, telegram_id: int, username: str = None, first_name: str = None, last_name: str = None):
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user:
            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                stars_balance=1000  # Начальный баланс для теста
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        return user
    
    @staticmethod
    def add_nft_to_inventory(db: Session, user_id: int, nft_id: int, case_id: int):
        user_nft = UserNFT(
            user_id=user_id,
            nft_id=nft_id,
            opened_from_case_id=case_id
        )
        db.add(user_nft)
        db.commit()
        return user_nft
    
    @staticmethod
    def get_user_nfts(db: Session, user_id: int):
        return db.query(
            UserNFT, NFT
        ).join(
            NFT, UserNFT.nft_id == NFT.id
        ).filter(
            UserNFT.user_id == user_id,
            UserNFT.is_sold == False
        ).all()
    
    @staticmethod
    def sell_nft(db: Session, user_nft_id: int, user_id: int):
        user_nft = db.query(UserNFT).filter(
            UserNFT.id == user_nft_id,
            UserNFT.user_id == user_id,
            UserNFT.is_sold == False
        ).first()
        
        if not user_nft:
            return None
        
        nft = db.query(NFT).filter(NFT.id == user_nft.nft_id).first()
        sell_price = int(nft.price * SELL_PERCENT)
        
        user_nft.is_sold = True
        user_nft.sold_price = sell_price
        
        user = db.query(User).filter(User.id == user_id).first()
        user.stars_balance += sell_price
        
        db.commit()
        return sell_price

class AuthService:
    @staticmethod
    def verify_telegram_init_data(init_data: str) -> bool:
        """Проверка подлинности данных от Telegram"""
        try:
            # Парсим данные
            data_pairs = []
            hash_value = None
            
            for pair in init_data.split('&'):
                key, value = pair.split('=')
                if key == 'hash':
                    hash_value = value
                else:
                    data_pairs.append((key, value))
            
            if not hash_value:
                return False
            
            # Сортируем и формируем строку для проверки
            data_pairs.sort(key=lambda x: x[0])
            data_check_string = '\n'.join(f"{k}={v}" for k, v in data_pairs)
            
            # Создаем секретный ключ из токена бота
            secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
            
            # Вычисляем HMAC
            h = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256)
            computed_hash = h.hexdigest()
            
            return computed_hash == hash_value
        except Exception as e:
            logger.error(f"Auth error: {e}")
            return False

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# FastAPI приложение
@asynccontextmanager
async def lifespan(app: FastAPI):
    # При запуске
    webhook_url = f"{WEBHOOK_URL}/webhook"
    await bot.set_webhook(url=webhook_url, allowed_updates=dp.resolve_used_update_types())
    logger.info(f"Webhook set to {webhook_url}")
    yield
    # При завершении
    await bot.delete_webhook()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# HTML шаблон Mini App
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Gift Battle - Кейсы с NFT</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
        }
        
        body {
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: #fff;
            min-height: 100vh;
            padding: 16px;
        }
        
        .container {
            max-width: 600px;
            margin: 0 auto;
        }
        
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            padding: 16px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 16px;
            backdrop-filter: blur(10px);
        }
        
        .balance {
            font-size: 20px;
            font-weight: bold;
            color: #ffd700;
        }
        
        .stars-icon {
            font-size: 24px;
            margin-right: 8px;
        }
        
        .tabs {
            display: flex;
            gap: 8px;
            margin-bottom: 24px;
        }
        
        .tab {
            flex: 1;
            padding: 12px;
            text-align: center;
            background: rgba(255, 255, 255, 0.1);
            border: none;
            color: #fff;
            border-radius: 12px;
            font-size: 16px;
            cursor: pointer;
            transition: all 0.3s;
        }
        
        .tab.active {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
        }
        
        .cases-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }
        
        .case-card {
            background: rgba(255, 255, 255, 0.05);
            border-radius: 16px;
            padding: 16px;
            cursor: pointer;
            transition: transform 0.3s, box-shadow 0.3s;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        
        .case-card:hover {
            transform: translateY(-4px);
            box-shadow: 0 8px 25px rgba(0, 0, 0, 0.3);
            background: rgba(255, 255, 255, 0.1);
        }
        
        .case-image {
            width: 100%;
            aspect-ratio: 1;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-radius: 12px;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 40px;
        }
        
        .case-name {
            font-size: 18px;
            font-weight: bold;
            margin-bottom: 8px;
        }
        
        .case-price {
            color: #ffd700;
            font-weight: bold;
        }
        
        .nft-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 12px;
        }
        
        .nft-card {
            background: rgba(255, 255, 255, 0.05);
            border-radius: 12px;
            padding: 12px;
            position: relative;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        
        .nft-rarity {
            position: absolute;
            top: 8px;
            right: 8px;
            padding: 4px 8px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
        }
        
        .rarity-common { background: #808080; }
        .rarity-rare { background: #4169e1; }
        .rarity-epic { background: #9400d3; }
        .rarity-legendary { background: #ffd700; color: #000; }
        
        .nft-image {
            width: 100%;
            aspect-ratio: 1;
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            border-radius: 8px;
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 24px;
        }
        
        .nft-name {
            font-size: 14px;
            font-weight: bold;
            margin-bottom: 4px;
        }
        
        .nft-price {
            font-size: 12px;
            color: #ffd700;
        }
        
        .sell-button {
            width: 100%;
            padding: 8px;
            margin-top: 8px;
            background: linear-gradient(135deg, #00b09b, #96c93d);
            border: none;
            color: white;
            border-radius: 8px;
            cursor: pointer;
            font-size: 12px;
        }
        
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.8);
            z-index: 1000;
            align-items: center;
            justify-content: center;
            padding: 16px;
        }
        
        .modal.active {
            display: flex;
        }
        
        .modal-content {
            background: #1a1a2e;
            border-radius: 24px;
            padding: 24px;
            max-width: 400px;
            width: 100%;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        
        .case-detail-image {
            width: 200px;
            height: 200px;
            margin: 0 auto 24px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-radius: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 60px;
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0% { transform: scale(1); }
            50% { transform: scale(1.05); }
            100% { transform: scale(1); }
        }
        
        .prize-list {
            margin: 20px 0;
            max-height: 300px;
            overflow-y: auto;
        }
        
        .prize-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 8px;
            margin-bottom: 8px;
        }
        
        .prize-chance {
            color: #4caf50;
            font-weight: bold;
        }
        
        .open-button {
            width: 100%;
            padding: 16px;
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            border: none;
            color: white;
            border-radius: 16px;
            font-size: 18px;
            font-weight: bold;
            cursor: pointer;
            margin-top: 20px;
        }
        
        .opening-animation {
            text-align: center;
            padding: 40px;
        }
        
        .spinner {
            width: 50px;
            height: 50px;
            border: 5px solid #f3f3f3;
            border-top: 5px solid #3498db;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 20px;
        }
        
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        
        .result-nft {
            text-align: center;
        }
        
        .result-nft-image {
            width: 150px;
            height: 150px;
            margin: 20px auto;
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            border-radius: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 50px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="balance">
                <span class="stars-icon">⭐</span>
                <span id="balance">0</span>
            </div>
            <div>Gift Battle</div>
        </div>
        
        <div class="tabs">
            <button class="tab active" onclick="switchTab('cases')">Кейсы</button>
            <button class="tab" onclick="switchTab('inventory')">Инвентарь</button>
        </div>
        
        <div id="cases-tab" class="tab-content">
            <div class="cases-grid" id="cases-list"></div>
        </div>
        
        <div id="inventory-tab" class="tab-content" style="display: none;">
            <div class="nft-grid" id="inventory-list"></div>
        </div>
    </div>
    
    <!-- Модальное окно кейса -->
    <div class="modal" id="case-modal">
        <div class="modal-content">
            <div id="case-detail"></div>
        </div>
    </div>
    
    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();
        tg.ready();
        
        let userData = tg.initDataUnsafe?.user;
        let currentCase = null;
        let userBalance = 0;
        
        // Загрузка данных
        async function loadCases() {
            try {
                let response = await fetch('/api/cases');
                let cases = await response.json();
                displayCases(cases);
            } catch (error) {
                console.error('Error loading cases:', error);
            }
        }
        
        async function loadInventory() {
          
