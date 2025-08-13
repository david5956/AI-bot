import sqlite3
import os
import logging
from logging.handlers import RotatingFileHandler
import requests
from dotenv import load_dotenv
import telebot
from telebot import types
from datetime import datetime
import threading
from functools import lru_cache

# Инициализация окружения
load_dotenv()

# Настройка логирования
def setup_logging():
    os.makedirs('logs', exist_ok=True)
    
    log_format = '%(asctime)s - %(name)s - %(levelname)s - ChatID: %(chat_id)s - %(message)s'
    formatter = logging.Formatter(log_format)

    file_handler = RotatingFileHandler(
        'logs/bot.log',
        maxBytes=5*1024*1024,
        backupCount=3,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    
    return logger

logger = setup_logging()

# Класс для управления БД
class DatabaseManager:
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init_db()
        return cls._instance
    
    def _init_db(self):
        self.connections = {}
        self._setup_database()
    
    def _setup_database(self):
        """Инициализация структуры БД"""
        conn = sqlite3.connect('dialog_history.db', check_same_thread=False)
        cursor = conn.cursor()
        
        # Оптимизация БД
        cursor.execute('PRAGMA journal_mode = WAL')
        cursor.execute('PRAGMA synchronous = NORMAL')
        cursor.execute('PRAGMA cache_size = -10000')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_id ON messages(chat_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON messages(timestamp)')
        
        cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS cleanup_old_messages
        AFTER INSERT ON messages
        BEGIN
            DELETE FROM messages 
            WHERE timestamp < datetime('now', '-7 days');
        END''')
        
        cursor.execute('PRAGMA optimize')
        conn.commit()
        conn.close()
    
    def get_connection(self):
        """Получение соединения для текущего потока"""
        thread_id = threading.get_ident()
        if thread_id not in self.connections:
            conn = sqlite3.connect(
                'dialog_history.db',
                check_same_thread=False
            )
            conn.execute('PRAGMA foreign_keys = ON')
            self.connections[thread_id] = conn
        return self.connections[thread_id]
    
    def close_all(self):
        """Закрытие всех соединений"""
        for conn in self.connections.values():
            conn.close()
        self.connections.clear()

# Инициализация менеджера БД
db_manager = DatabaseManager()

# Инициализация бота
try:
    bot = telebot.TeleBot(os.getenv('TELEGRAM_AI'))
    logger.info("Бот инициализирован", extra={'chat_id': 'SYSTEM'})
except Exception as e:
    logger.error(f"Ошибка инициализации бота: {str(e)}", extra={'chat_id': 'SYSTEM'})
    exit()

# Конфигурация YandexGPT API
YC_API_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
HEADERS = {
    "Authorization": f"Api-Key {os.getenv('YC_API_KEY')}",
    "x-folder-id": os.getenv('YC_FOLDER_ID'),
    "Content-Type": "application/json"
}

def filter_response(text: str) -> str:
    """Фильтрация ненужных фраз в ответах"""
    unwanted_phrases = [
        "как искусственный интеллект",
        "я обученная модель",
        "насколько я понимаю",
        "вот развернутый ответ",
        "как языковая модель"
    ]
    
    for phrase in unwanted_phrases:
        text = text.replace(phrase, "")
    
    return text.strip()

@lru_cache(maxsize=500)
def get_cached_response(chat_id: int, prompt: str) -> str:
    """Кеширование ответов с учетом chat_id"""
    return ask_yandex_gpt(prompt, chat_id)

def get_dialog_history(chat_id: int, max_messages: int = 7) -> list:
    """Получение истории диалога"""
    conn = db_manager.get_connection()
    cursor = conn.cursor()
    cursor.execute('''
    SELECT role, content FROM messages 
    WHERE chat_id = ? 
    ORDER BY timestamp DESC 
    LIMIT ?
    ''', (chat_id, max_messages))
    return cursor.fetchall()[::-1]

def save_message(chat_id: int, role: str, content: str) -> None:
    """Сохранение сообщения в историю"""
    conn = db_manager.get_connection()
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO messages (chat_id, role, content) 
    VALUES (?, ?, ?)
    ''', (chat_id, role, content))
    conn.commit()

def ask_yandex_gpt(prompt: str, chat_id: int) -> str:
    """Запрос к YandexGPT с историей диалога"""
    history = get_dialog_history(chat_id)
    
    messages = [{
        "role": "system",
        "text": "Ты - точный AI-ассистент. Отвечай кратко и по делу. Избегай вводных фраз."
    }]
    
    for role, content in history:
        messages.append({"role": role, "text": content})
    
    messages.append({"role": "user", "text": prompt})
    
    data = {
        "modelUri": f"gpt://{os.getenv('YC_FOLDER_ID')}/yandexgpt",
        "completionOptions": {
            "stream": False,
            "temperature": 0.2,
            "maxTokens": 400
        },
        "messages": messages
    }

    try:
        response = requests.post(YC_API_URL, json=data, headers=HEADERS, timeout=15)
        response.raise_for_status()
        result = response.json()["result"]["alternatives"][0]["message"]["text"]
        result = filter_response(result)
        
        save_message(chat_id, "user", prompt)
        save_message(chat_id, "assistant", result)
        
        return result
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка запроса к YandexGPT: {str(e)}", extra={'chat_id': chat_id})
        return "⚠️ Ошибка обработки запроса"
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {str(e)}", extra={'chat_id': chat_id})
        return "⚠️ Системная ошибка"

# Обработчики команд
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    try:
        welcome_text = (
            "👋 Привет! Я полезный ассистент\n\n"
            "Отвечаю кратко и по делу.\n"
            "Доступные команды:\n"
            "/clear - очистить историю диалога\n\n"
            "Просто напиши свой вопрос!"
        )
        bot.reply_to(message, welcome_text)
        logger.info("Пользователь запустил бота", extra={'chat_id': message.chat.id})
    except Exception as e:
        logger.error(f"Ошибка в send_welcome: {str(e)}", extra={'chat_id': message.chat.id})

@bot.message_handler(commands=['clear'])
def clear_history(message):
    try:
        conn = db_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM messages WHERE chat_id = ?', (message.chat.id,))
        conn.commit()
        get_cached_response.cache_clear()  # Очищаем кеш
        bot.reply_to(message, "🗑️ История диалога очищена!")
        logger.info(f"История очищена для chat_id: {message.chat.id}")
    except Exception as e:
        logger.error(f"Ошибка очистки истории: {str(e)}", extra={'chat_id': message.chat.id})
        bot.reply_to(message, "❌ Не удалось очистить историю")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    try:
        # Проверка простых команд без API
        lower_text = message.text.lower().strip()
        if lower_text in ["привет", "здравствуй"]:
            return bot.reply_to(message, "Привет! Чем помочь?")
            
        if len(message.text) > 500:
            return bot.reply_to(message, "⚠️ Сообщение слишком длинное. Максимум 500 символов.")
        
        bot.send_chat_action(message.chat.id, 'typing')
        response = get_cached_response(message.chat.id, message.text[:300])  # Кешируем
        bot.reply_to(message, response)
        logger.info(f"Ответ отправлен", extra={'chat_id': message.chat.id})
        
    except Exception as e:
        logger.error(f"Ошибка обработки: {str(e)}", extra={'chat_id': message.chat.id})
        bot.reply_to(message, "❌ Ошибка обработки. Попробуйте позже.")

if __name__ == '__main__':
    try:
        logger.info("----- Бот запущен -----", extra={'chat_id': 'SYSTEM'})
        bot.infinity_polling()
    except KeyboardInterrupt:
        logger.info("----- Бот остановлен -----", extra={'chat_id': 'SYSTEM'})
    except Exception as e:
        logger.critical(f"Критическая ошибка: {str(e)}", extra={'chat_id': 'SYSTEM'})
    finally:
        db_manager.close_all()
        logger.info("----- Ресурсы освобождены -----", extra={'chat_id': 'SYSTEM'})