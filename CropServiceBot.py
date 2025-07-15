import re
import os
import asyncio
import logging
from datetime import datetime, timedelta
from pymongo import MongoClient # Імпортуємо модуль для роботи з MongoDB
from pymongo.errors import ConnectionFailure, OperationFailure

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from aiogram.utils import executor
from aiogram.utils.exceptions import BadRequest, TelegramAPIError, MessageNotModified, MessageToDeleteNotFound

# ======== Налаштування ========
API_TOKEN = os.getenv('API_TOKEN')
if not API_TOKEN:
    print("❌ API_TOKEN не заданий. Будь ласка, встановіть змінну середовища API_TOKEN.")
    exit(1)

# Налаштування MongoDB URI
# Railway часто надає MONGO_URL або DATABASE_URL для MongoDB
MONGO_URI = os.getenv('MONGO_URL') or os.getenv('DATABASE_URL')
if not MONGO_URI:
    print("❌ MONGO_URL або DATABASE_URL не заданий. Будь ласка, встановіть змінну середовища для підключення до MongoDB.")
    exit(1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger().addHandler(logging.StreamHandler())

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

# ======== Файли (видаляємо JSON, додаємо DB) ========
ANALYTICS_FILE = 'analytics.log' # Залишаємо для логів аналітики

# ======== Підключення до MongoDB ========
mongo_client = None
db = None
posts_collection = None

def init_mongo_db():
    """Ініціалізує з'єднання з MongoDB та отримує колекцію posts."""
    global mongo_client, db, posts_collection
    try:
        mongo_client = MongoClient(MONGO_URI)
        db = mongo_client.get_default_database() # Отримуємо базу даних з URI
        posts_collection = db.posts # Отримуємо колекцію "posts"
        # Перевірка з'єднання
        mongo_client.admin.command('ping') 
        logging.info("Successfully connected to MongoDB!")
    except ConnectionFailure as e:
        logging.error(f"Could not connect to MongoDB: {e}")
        exit(1)
    except Exception as e:
        logging.error(f"An unexpected error occurred during MongoDB initialization: {e}")
        exit(1)

# Викликаємо ініціалізацію БД при запуску бота
init_mongo_db()

# ======== Категорії ========
CATEGORIES = [
    ("🔨 Ремонт і будівництво", "Ремомент і будівництво"),
    ("🧹 Прибирання і побутова допомога", "Прибирання і побутова допомога"),
    ("🚚 Кур’єрські та транспортні послуги", "Кур’єрські та транспортні послуги"),
    ("🐾 Допомога з тваринами", "Допомога з тваринами"),
    ("💻 IT та цифрові послуги", "IT та цифрові послуги"),
    ("🎓 Освітні послуги", "Освітні послуги"),
    ("💅 Краса та здоров’я", "Краса та здоров’я"),
    ("🎉 Події та допомога на заходах", "Події та допомога на заходах"),
    ("❄️/🌿 Сезонна/разова робота", "Сезонна/разова робота"),
    ("📦 Інше", "Інше"),
]

# Створення словника для швидкого доступу до емоджі категорії за назвою
CATEGORY_EMOJIS = {name: emoji for emoji, name in CATEGORIES}

# Тематичні емоджі для типу "Робота" та "Послуга"
TYPE_EMOJIS = {
    "робота": "💼",
    "послуга": "🤝"
}


# ======== FSM стани ========
class AppStates(StatesGroup):
    MAIN_MENU = State() # Головне меню
    ADD_TYPE = State()
    ADD_CAT = State()
    ADD_DESC = State()
    ADD_CONT = State()
    ADD_CONFIRM = State()

    VIEW_CAT = State()
    VIEW_LISTING = State() # Цей стан тепер знову для пагінації загальних оголошень
    
    MY_POSTS_VIEW = State()
    EDIT_DESC = State()


# ======== Утиліти для роботи з даними (MongoDB) ========

def log_event(text):
    """Логує події у файл аналітики."""
    try:
        with open(ANALYTICS_FILE, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().isoformat()}] {text}\n")
    except Exception as e:
        logging.error(f"Error logging event to {ANALYTICS_FILE}: {e}")

# Функції для роботи з постами в MongoDB
def add_post_to_db(post_data):
    """Додає нове оголошення до бази даних MongoDB."""
    try:
        result = posts_collection.insert_one(post_data)
        return str(result.inserted_id) # Повертаємо ID як рядок
    except OperationFailure as e:
        logging.error(f"MongoDB operation failed when adding post: {e}")
        return None
    except Exception as e:
        logging.error(f"Error adding post to MongoDB: {e}")
        return None

def get_posts_from_db():
    """Отримує всі оголошення з бази даних MongoDB."""
    try:
        posts = list(posts_collection.find().sort("created_at", -1))
        # Перетворюємо ObjectId на рядок для сумісності
        for post in posts:
            if '_id' in post:
                post['id'] = str(post['_id'])
        return posts
    except OperationFailure as e:
        logging.error(f"MongoDB operation failed when getting posts: {e}")
        return []
    except Exception as e:
        logging.error(f"Error getting posts from MongoDB: {e}")
        return []

def get_post_by_id_from_db(post_id):
    """Отримує оголошення за ID."""
    from bson.objectid import ObjectId # Імпортуємо тут, щоб уникнути кругових залежностей
    try:
        post = posts_collection.find_one({"_id": ObjectId(post_id)})
        if post:
            post['id'] = str(post['_id']) # Перетворюємо ObjectId на рядок
        return post
    except OperationFailure as e:
        logging.error(f"MongoDB operation failed when getting post by ID: {e}")
        return None
    except Exception as e:
        logging.error(f"Error getting post by ID from MongoDB: {e}")
        return None

def update_post_description_in_db(post_id, new_description):
    """Оновлює опис оголошення за ID."""
    from bson.objectid import ObjectId
    try:
        result = posts_collection.update_one(
            {"_id": ObjectId(post_id)},
            {"$set": {"description": new_description}}
        )
        return result.modified_count > 0
    except OperationFailure as e:
        logging.error(f"MongoDB operation failed when updating post description: {e}")
        return False
    except Exception as e:
        logging.error(f"Error updating post description in MongoDB: {e}")
        return False

def delete_post_from_db(post_id, user_id):
    """Видаляє оголошення за ID, перевіряючи user_id."""
    from bson.objectid import ObjectId
    try:
        result = posts_collection.delete_one(
            {"_id": ObjectId(post_id), "user_id": user_id}
        )
        return result.deleted_count > 0
    except OperationFailure as e:
        logging.error(f"MongoDB operation failed when deleting post: {e}")
        return False
    except Exception as e:
        logging.error(f"Error deleting post from MongoDB: {e}")
        return False

def can_edit(post):
    created = datetime.fromisoformat(post['created_at'])
    return datetime.utcnow() - created < timedelta(minutes=15)

# ======== Допоміжна функція для екранування MarkdownV2 символів ========
def escape_markdown_v2(text: str) -> str:
    """
    Екранує спеціальні символи MarkdownV2 в тексті,
    які не повинні інтерпретуватися як форматування.
    """
    if not isinstance(text, str):
        text = str(text) 
    
    # Список всіх спеціальних символів MarkdownV2, які потребують екранування
    # https://core.telegram.org/bots/api#markdownv2-style
    special_chars = r'_*[]()~`>#+-=|{}.!' 
    
    for char in special_chars:
        text = text.replace(char, '\\' + char)
    return text

# ======== Кнопки ========
def main_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ Додати", callback_data="add_post"),
        InlineKeyboardButton("🔍 Пошук", callback_data="view_posts"),
        InlineKeyboardButton("🗂️ Мої", callback_data="my_posts"),
        InlineKeyboardButton("❓ Допомога", callback_data="help"),
    )
    return kb

def back_kb():
    kb = InlineKeyboardMarkup()
    # Кнопка "Назад" завжди внизу
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="go_back_to_prev_step")) 
    return kb

def type_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Робота", callback_data="type_work"),
        InlineKeyboardButton("Послуга", callback_data="type_service"),
    )
    # Кнопка "Назад до головного меню" внизу
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="go_back_to_main_menu"))
    return kb

def cat_kb(is_post_creation=True):
    kb = InlineKeyboardMarkup(row_width=1)
    for i, (lbl, _) in enumerate(CATEGORIES):
        cb = f"{'post' if is_post_creation else 'view'}_cat_{i}"
        kb.add(InlineKeyboardButton(lbl, callback_data=cb))
    # Кнопка "Назад" завжди внизу
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="go_back_to_prev_step")) 
    return kb

def contact_kb():
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("❎ Пропустити", callback_data="skip_cont"),
    )
    # Кнопка "Назад" завжди внизу
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="go_back_to_prev_step"))
    return kb

# ======== Старт ========
WELCOME_MESSAGE = (
    "👋 Привіт\\! Я CropServiceBot — допомагаю знаходити підробітки або виконавців у твоєму місті\\.\n\n"
    "➕ \\— Додати оголошення\n"
    "🔍 \\— Пошук оголошень\n"
    "🗂️ \\— Мої оголошення\n"
    "❓ \\— Допомога"
)

# Кількість оголошень на сторінці для "Мої оголошення"
MY_POSTS_PER_PAGE = 5 
# Кількість оголошень на сторінці для "Перегляд усіх оголошень"
VIEW_POSTS_PER_PAGE = 5 # Можете змінити це значення, наприклад, на 7 або 10

# ==== Функція для надсилання/оновлення повідомлення інтерфейсу ====
async def update_or_send_interface_message(chat_id: int, state: FSMContext, text: str, reply_markup: InlineKeyboardMarkup = None, parse_mode: str = None, disable_web_page_preview: bool = False):
    """
    Редагує існуюче повідомлення інтерфейсу або надсилає нове, якщо його немає.
    Зберігає message_id останнього повідомлення бота у стані.
    """
    data = await state.get_data()
    last_bot_message_id = data.get('last_bot_message_id')

    try:
        if last_bot_message_id:
            # Спроба відредагувати існуюче повідомлення
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=last_bot_message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview
            )
            logging.info(f"Edited existing interface message for user {chat_id}. Message ID: {last_bot_message_id}")
        else:
            # Надсилання нового повідомлення, якщо немає існуючого
            new_msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview
            )
            await state.update_data(last_bot_message_id=new_msg.message_id)
            logging.info(f"Sent new interface message for user {chat_id}. Message ID: {new_msg.message_id}")
    except MessageNotModified:
        # Це нормальна ситуація, якщо текст або клавіатура не змінилися
        logging.info(f"Message not modified for user {chat_id}. Message ID: {last_bot_message_id}")
    except (MessageToDeleteNotFound, BadRequest) as e:
        # Якщо повідомлення було видалено або є інші помилки редагування, надсилаємо нове
        logging.warning(f"Failed to edit message {last_bot_message_id} for user {chat_id} (Error: {e}). Sending new message.")
        new_msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview
        )
        await state.update_data(last_bot_message_id=new_msg.message_id)
        logging.info(f"Sent new interface message (after edit failure) for user {chat_id}. Message ID: {new_msg.message_id}")
    except Exception as e:
        logging.error(f"Unexpected error in update_or_send_interface_message for user {chat_id}: {e}", exc_info=True)
        # У випадку будь-якої іншої непередбаченої помилки, спробуйте надіслати нове повідомлення як останній варіант
        new_msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview
        )
        await state.update_data(last_bot_message_id=new_msg.message_id)
        logging.info(f"Sent new interface message (after unexpected error) for user {chat_id}. Message ID: {new_msg.message_id}")


# ==== Функції переходу ====
async def go_to_main_menu(chat_id: int, state: FSMContext):
    """Повертає до головного меню, оновлюючи існуюче повідомлення."""
    logging.info(f"User {chat_id} going to main menu.")
    await update_or_send_interface_message(chat_id, state, WELCOME_MESSAGE, main_kb(), parse_mode='MarkdownV2')
    await state.set_state(AppStates.MAIN_MENU)


@dp.message_handler(commands=['start'], state="*")
async def on_start(msg: types.Message, state: FSMContext):
    logging.info(f"User {msg.from_user.id} started bot.")
    
    try:
        # Видаляємо команду /start, якщо вона була відправлена
        await msg.delete() 
    except Exception as e:
        logging.warning(f"Failed to delete /start command: {e}")
    
    # Скидаємо last_bot_message_id при старті, щоб гарантовано надіслати нове вітальне повідомлення
    await state.update_data(last_bot_message_id=None)
    await go_to_main_menu(msg.chat.id, state)


@dp.callback_query_handler(lambda c: c.data == 'go_back_to_main_menu', state='*')
async def on_back_to_main(call: CallbackQuery, state: FSMContext):
    logging.info(f"User {call.from_user.id} pressed 'Go Back to Main Menu'.")
    await call.answer()
    await go_to_main_menu(call.message.chat.id, state)


@dp.callback_query_handler(lambda c: c.data == 'go_back_to_prev_step', state='*')
async def on_back_to_prev_step(call: CallbackQuery, state: FSMContext):
    logging.info(f"User {call.from_user.id} pressed 'Go Back to Previous Step'.")
    await call.answer()
    current_state = await state.get_state()
    chat_id = call.message.chat.id
    
    if current_state == AppStates.ADD_TYPE.state:
        await go_to_main_menu(chat_id, state)
    elif current_state == AppStates.ADD_CAT.state:
        await update_or_send_interface_message(chat_id, state, "🔹 Виберіть тип оголошення:", type_kb())
        await state.set_state(AppStates.ADD_TYPE)
    elif current_state == AppStates.ADD_DESC.state:
        await update_or_send_interface_message(chat_id, state, "🗂️ Виберіть категорію:", cat_kb(is_post_creation=True))
        await state.set_state(AppStates.ADD_CAT)
    elif current_state == AppStates.ADD_CONT.state:
        await update_or_send_interface_message(chat_id, state, "✏️ Введіть опис (до 300 символів):", back_kb())
        await state.set_state(AppStates.ADD_DESC)
    elif current_state == AppStates.ADD_CONFIRM.state:
        await update_or_send_interface_message(chat_id, state, "📞 Введіть контакт (необов’язково):", contact_kb())
        await state.set_state(AppStates.ADD_CONT)
    elif current_state == AppStates.VIEW_CAT.state:
        await go_to_main_menu(chat_id, state)
    elif current_state == AppStates.VIEW_LISTING.state:
        # Для VIEW_LISTING повертаємось до вибору категорії
        await update_or_send_interface_message(chat_id, state, "🔎 Оберіть категорію:", cat_kb(is_post_creation=False))
        await state.set_state(AppStates.VIEW_CAT)
    elif current_state == AppStates.MY_POSTS_VIEW.state:
        await go_to_main_menu(chat_id, state) 
    elif current_state == AppStates.EDIT_DESC.state:
        data = await state.get_data()
        await show_my_posts_page(chat_id, state, data.get('offset', 0)) # Повернутися до тієї ж сторінки
        await state.set_state(AppStates.MY_POSTS_VIEW)
    else:
        await go_to_main_menu(chat_id, state)

# ======== Додавання оголошень ========
@dp.callback_query_handler(lambda c: c.data == 'add_post', state=[AppStates.MAIN_MENU, AppStates.MY_POSTS_VIEW])
async def add_start(call: CallbackQuery, state: FSMContext):
    logging.info(f"User {call.from_user.id} initiated 'Add Post'.")
    await call.answer()
    await update_or_send_interface_message(call.message.chat.id, state, "🔹 Виберіть тип оголошення:", type_kb())
    await state.set_state(AppStates.ADD_TYPE)

@dp.callback_query_handler(lambda c: c.data.startswith('type_'), state=AppStates.ADD_TYPE)
async def add_type(call: CallbackQuery, state: FSMContext):
    logging.info(f"User {call.from_user.id} selected post type: {call.data}.")
    await call.answer()
    typ = 'робота' if call.data == 'type_work' else 'послуга'
    await state.update_data(type=typ)
    await update_or_send_interface_message(call.message.chat.id, state, "🗂️ Виберіть категорію:", cat_kb(is_post_creation=True))
    await state.set_state(AppStates.ADD_CAT)

@dp.callback_query_handler(lambda c: c.data.startswith('post_cat_'), state=AppStates.ADD_CAT)
async def add_cat(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split('_')[2])
    _, cat = CATEGORIES[idx]
    logging.info(f"User {call.from_user.id} selected category: {cat}.")
    await call.answer()
    await state.update_data(category=cat)
    await update_or_send_interface_message(call.message.chat.id, state, "✏️ Введіть опис (до 300 символів):", back_kb())
    await state.set_state(AppStates.ADD_DESC)

@dp.message_handler(state=AppStates.ADD_DESC)
async def add_desc(msg: types.Message, state: FSMContext):
    logging.info(f"User {msg.from_user.id} entered description.")
    text = msg.text.strip()
    
    # Видаляємо повідомлення користувача, якщо це не заважає візуально
    try:
        await msg.delete()
    except MessageToDeleteNotFound:
        pass

    if not text:
        return await update_or_send_interface_message(msg.chat.id, state, "❌ Опис не може бути порожнім\\. Введіть опис (до 300 символів):", back_kb(), parse_mode='MarkdownV2')
    if len(text) > 300:
        return await update_or_send_interface_message(msg.chat.id, state, f"❌ Занадто довгий \\({len(text)}/300\\)\\. Введіть опис (до 300 символів):", back_kb(), parse_mode='MarkdownV2')
    
    await state.update_data(desc=text)
    await update_or_send_interface_message(msg.chat.id, state, "📞 Введіть контакт (необов’язково):", contact_kb())
    await state.set_state(AppStates.ADD_CONT)

@dp.callback_query_handler(lambda c: c.data == 'skip_cont', state=AppStates.ADD_CONT)
async def skip_cont(call: CallbackQuery, state: FSMContext):
    logging.info(f"User {call.from_user.id} skipped contact info.")
    await call.answer()
    await state.update_data(cont="")
    data = await state.get_data()
    
    type_emoji = TYPE_EMOJIS.get(data['type'], '') # Отримуємо емоджі для типу
    
    summary = (
        f"🔎 \\*Перевірте:\\*\n"
        f"{escape_markdown_v2(type_emoji)} **{escape_markdown_v2(data['type'].capitalize())}** \\| **{escape_markdown_v2(data['category'])}**\n"
        f"🔹 {escape_markdown_v2(data['desc'])}\n"
        f"📞 \\_немає\\_"
    )
    kb = InlineKeyboardMarkup(row_width=2).add(
        InlineKeyboardButton("✅ Підтвердити", callback_data="confirm_add_post"),
    )
    # Кнопка "Назад" завжди внизу
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="go_back_to_prev_step"))
    await update_or_send_interface_message(call.message.chat.id, state, summary, kb, parse_mode='MarkdownV2')
    await state.set_state(AppStates.ADD_CONFIRM)

@dp.message_handler(state=AppStates.ADD_CONT)
async def add_cont(msg: types.Message, state: FSMContext):
    logging.info(f"User {msg.from_user.id} entered contact info.")
    text = msg.text.strip()
    
    # Видаляємо повідомлення користувача, якщо це не заважає візуально
    try:
        await msg.delete()
    except MessageToDeleteNotFound:
        pass

    if not text:
        return await update_or_send_interface_message(msg.chat.id, state, "❌ Контакт не може бути порожнім\\. Введіть номер телефону або пропустіть\\.", contact_kb(), parse_mode='MarkdownV2')
    
    # Регулярний вираз для валідації номера телефону
    # Дозволяє формати:
    #   - 0XXXXXXXXX (10 цифр, починається з 0)
    #   - +380XXXXXXXXX (12 цифр, починається з +380)
    #   - @username (username Telegram)
    phone_pattern = r'^(?:0\d{9}|\+380\d{9}|@[a-zA-Z0-9_]{5,32})$'

    if not re.fullmatch(phone_pattern, text):
        logging.warning(f"User {msg.from_user.id} entered invalid contact format: '{text}'")
        return await update_or_send_interface_message(
            msg.chat.id, state,
            "❌ Невірний формат\\. Будь ласка, введіть номер телефону у форматі \\+380XXXXXXXXX або 0XXXXXXXXX, або username Telegram \\(@username\\)\\.",
            contact_kb(), parse_mode='MarkdownV2'
        )
    
    await state.update_data(cont=text)
    data = await state.get_data()
    
    type_emoji = TYPE_EMOJIS.get(data['type'], '') # Отримуємо емоджі для типу

    summary = (
        f"🔎 \\*Перевірте:\\*\n"
        f"{escape_markdown_v2(type_emoji)} **{escape_markdown_v2(data['type'].capitalize())}** \\| **{escape_markdown_v2(data['category'])}**\n"
        f"🔹 {escape_markdown_v2(data['desc'])}\n"
        f"📞 {escape_markdown_v2(data['cont'])}"
    )
    kb = InlineKeyboardMarkup(row_width=2).add(
        InlineKeyboardButton("✅ Підтвердити", callback_data="confirm_add_post"),
    )
    # Кнопка "Назад" завжди внизу
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="go_back_to_prev_step"))
    await update_or_send_interface_message(msg.chat.id, state, summary, kb, parse_mode='MarkdownV2')
    await state.set_state(AppStates.ADD_CONFIRM)

@dp.callback_query_handler(lambda c: c.data == 'confirm_add_post', state=AppStates.ADD_CONFIRM)
async def add_confirm(call: CallbackQuery, state: FSMContext):
    logging.info(f"User {call.from_user.id} confirmed post creation.")
    await call.answer()
    d = await state.get_data()
    
    usr = call.from_user.username or str(call.from_user.id)

    # Валідація номеру телефону перед збереженням (повторна перевірка на всяк випадок)
    contact_info = d.get('cont', "")
    if contact_info:
        phone_pattern = r'^(?:0\d{9}|\+380\d{9}|@[a-zA-Z0-9_]{5,32})$'
        if not re.fullmatch(phone_pattern, contact_info):
            logging.error(f"Invalid contact format somehow slipped through for user {call.from_user.id}: {contact_info}")
            await update_or_send_interface_message(
                call.message.chat.id, state,
                "❌ Помилка: Невірний формат контакту\\. Будь ласка, спробуйте ще раз\\.",
                main_kb(), parse_mode='MarkdownV2'
            )
            await state.set_state(AppStates.MAIN_MENU)
            return

    post_data = {
        'user_id': call.from_user.id, 
        'username': usr,
        'type': d['type'], 
        'category': d['category'], 
        'description': d['desc'],
        'contacts': contact_info, 
        'created_at': datetime.utcnow().isoformat()
    }
    
    pid = add_post_to_db(post_data) # Зберігаємо в БД
    if pid:
        log_event(f"Added {pid}")
        await update_or_send_interface_message(call.message.chat.id, state, "✅ Оголошення успішно додано\\!", parse_mode='MarkdownV2') 
        await show_my_posts_page(call.message.chat.id, state, 0) # Повертаємось на першу сторінку моїх оголошень
        await state.set_state(AppStates.MY_POSTS_VIEW)
    else:
        await update_or_send_interface_message(call.message.chat.id, state, "❌ Помилка при додаванні оголошення\\. Спробуйте ще раз\\.", main_kb(), parse_mode='MarkdownV2')
        await state.set_state(AppStates.MAIN_MENU)


# ======== Перегляд оголошень (Повернення до пагінації) ========
@dp.callback_query_handler(lambda c: c.data == 'view_posts', state=AppStates.MAIN_MENU)
async def view_start(call: CallbackQuery, state: FSMContext):
    logging.info(f"User {call.from_user.id} initiated 'View Posts'.")
    await call.answer()
    await update_or_send_interface_message(call.message.chat.id, state, "🔎 Оберіть категорію:", cat_kb(is_post_creation=False))
    await state.set_state(AppStates.VIEW_CAT)

@dp.callback_query_handler(lambda c: c.data.startswith('view_cat_'), state=AppStates.VIEW_CAT)
async def view_cat(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split('_')[2])
    cat_name = CATEGORIES[idx][1]
    logging.info(f"User {call.from_user.id} selected view category: {cat_name}.")
    await call.answer()
    
    await state.update_data(current_view_category=cat_name, current_category_idx=idx)
    await show_view_posts_page(call.message.chat.id, state, 0) # Починаємо з першої сторінки
    await state.set_state(AppStates.VIEW_LISTING)


async def show_view_posts_page(chat_id: int, state: FSMContext, offset: int = 0):
    logging.info(f"Showing view posts page for user {chat_id}, offset {offset}")
    try:
        data = await state.get_data()
        cat = data.get('current_view_category')

        if not cat:
            logging.error(f"Category not found in state for user {chat_id}")
            return await go_to_main_menu(chat_id, state)

        all_posts = get_posts_from_db() # Отримуємо пости з БД
        all_posts_in_category = [p for p in all_posts if p['category'] == cat]
        # Сортуємо за датою створення, найновіші перші (MongoDB вже повертає відсортовані)
        user_posts = all_posts_in_category

        if not user_posts:
            logging.info(f"No posts found for category '{cat}' for user {chat_id}")
            kb = InlineKeyboardMarkup(row_width=1).add(
                InlineKeyboardButton("⬅️ Назад до категорій", callback_data="go_back_to_prev_step"),
                InlineKeyboardButton("🏠 Головне меню", callback_data="go_back_to_main_menu")
            )
            text_to_send = f"У категорії «{escape_markdown_v2(cat)}» поки що немає оголошень\\."
            return await update_or_send_interface_message(
                chat_id, state,
                text_to_send,
                kb, parse_mode='MarkdownV2'
            )

        await state.update_data(offset=offset)
        
        page_posts = user_posts[offset : offset + VIEW_POSTS_PER_PAGE] # Використовуємо VIEW_POSTS_PER_PAGE
        total_posts = len(user_posts)
        total_pages = (total_posts + VIEW_POSTS_PER_PAGE - 1) // VIEW_POSTS_PER_PAGE
        current_page = offset // VIEW_POSTS_PER_PAGE + 1
        
        full_text = (f"📋 **{escape_markdown_v2(cat)}** \\(Сторінка {escape_markdown_v2(current_page)}/{escape_markdown_v2(total_pages)}\\)\n\n")
        
        combined_keyboard = InlineKeyboardMarkup(row_width=1) 

        for i, p in enumerate(page_posts):
            type_emoji = TYPE_EMOJIS.get(p['type'], '') 
            
            post_block = (f"ID: {escape_markdown_v2(p['id'])}\n" # Тут залишаємо реальний ID
                         f"{escape_markdown_v2(type_emoji)} **{escape_markdown_v2(p['type'].capitalize())}**\n"
                         f"🔹 {escape_markdown_v2(p['description'])}\n") 
            
            if p['username']:
                if str(p['username']).isdigit(): # Перевіряємо, чи username є просто ID користувача
                    post_block += f"👤 Автор: \\_Приватний користувач\\_\n"
                else:
                    post_block += f"👤 Автор: \\@{escape_markdown_v2(p['username'])}\n"
            
            contact_info = p.get('contacts', '')
            if contact_info:
                post_block += f"📞 Контакт: {escape_markdown_v2(contact_info)}\n"
            
            full_text += post_block
            
            if i < len(page_posts) - 1:
                full_text += "\n—\n\n" 

        # Додаємо кнопки пагінації
        nav_row_page_footer = []
        if offset > 0:
            nav_row_page_footer.append(InlineKeyboardButton("⬅️ Попередня", callback_data=f"viewpage_{offset - VIEW_POSTS_PER_PAGE}"))
        if offset + VIEW_POSTS_PER_PAGE < total_posts:
            nav_row_page_footer.append(InlineKeyboardButton("Наступна ➡️", callback_data=f"viewpage_{offset + VIEW_POSTS_PER_PAGE}"))
        
        if nav_row_page_footer:
            combined_keyboard.row(*nav_row_page_footer)
            
        combined_keyboard.add(InlineKeyboardButton("⬅️ Назад до категорій", callback_data="go_back_to_prev_step"))
        combined_keyboard.add(InlineKeyboardButton("🏠 Головне меню", callback_data="go_back_to_main_menu"))
        
        await update_or_send_interface_message(chat_id, state, full_text, combined_keyboard, parse_mode='MarkdownV2', disable_web_page_preview=True)

    except Exception as e:
        logging.error(f"Error in show_view_posts_page for user {chat_id}: {e}", exc_info=True)
        await update_or_send_interface_message(chat_id, state, "Вибачте, сталася неочікувана помилка при перегляді оголошень\\. Спробуйте ще раз\\.", main_kb(), parse_mode='MarkdownV2')
        await state.set_state(AppStates.MAIN_MENU)
    
@dp.callback_query_handler(lambda c: c.data.startswith('viewpage_'), state=AppStates.VIEW_LISTING)
async def view_paginate(call: CallbackQuery, state: FSMContext):
    logging.info(f"User {call.from_user.id} paginating view posts to offset {call.data.split('_')[1]}.")
    await call.answer()
    offset = int(call.data.split('_')[1])
    await show_view_posts_page(call.message.chat.id, state, offset)


# ======== Мої оголошення ========
@dp.callback_query_handler(lambda c: c.data=='my_posts', state=AppStates.MAIN_MENU)
async def my_posts_start(call: CallbackQuery, state: FSMContext):
    logging.info(f"User {call.from_user.id} pressed 'My Posts'.")
    await call.answer()
    await show_my_posts_page(call.message.chat.id, state, 0) # Починаємо з першої сторінки
    await state.set_state(AppStates.MY_POSTS_VIEW)


async def show_my_posts_page(chat_id: int, state: FSMContext, offset: int = 0):
    logging.info(f"Showing my posts page for user {chat_id}, offset {offset}")
    try:
        all_posts = get_posts_from_db() # Отримуємо пости з БД
        user_posts = sorted([p for p in all_posts if p['user_id'] == chat_id], key=lambda x: datetime.fromisoformat(x['created_at']), reverse=True)
        
        await state.update_data(offset=offset) # Зберігаємо offset для подальшого використання

        if not user_posts:
            logging.info(f"No posts found for user {chat_id}")
            kb_no_posts = InlineKeyboardMarkup(row_width=1).add(
                InlineKeyboardButton("➕ Додати оголошення", callback_data="add_post"), 
                InlineKeyboardButton("🏠 Головне меню", callback_data="go_back_to_main_menu")
            )
            return await update_or_send_interface_message(chat_id, state, "🧐 У вас немає оголошень\\.", kb_no_posts, parse_mode='MarkdownV2')

        page_posts = user_posts[offset : offset + MY_POSTS_PER_PAGE] 
        total_posts = len(user_posts)
        total_pages = (total_posts + MY_POSTS_PER_PAGE - 1) // MY_POSTS_PER_PAGE
        current_page = offset // MY_POSTS_PER_PAGE + 1
        
        full_text = f"🗂️ **Мої оголошення** \\(Сторінка {escape_markdown_v2(current_page)}/{escape_markdown_v2(total_pages)}\\)\n\n"
        
        combined_keyboard = InlineKeyboardMarkup(row_width=2) 

        for i, p in enumerate(page_posts):
            type_emoji = TYPE_EMOJIS.get(p['type'], '') 
            
            local_post_num = offset + i + 1
            
            post_block = (f"№ {escape_markdown_v2(local_post_num)}\n" 
                         f"{escape_markdown_v2(type_emoji)} **{escape_markdown_v2(p['type'].capitalize())}**\n"
                         f"🔹 {escape_markdown_v2(p['description'])}\n")
            
            if p['username']:
                if str(p['username']).isdigit(): # Перевіряємо, чи username є просто ID користувача
                    post_block += f"👤 Автор: \\_Приватний користувач\\_\n"
                else:
                    post_block += f"👤 Автор: \\@{escape_markdown_v2(p['username'])}\n"
            
            if p.get('contacts'):
                 post_block += f"📞 Контакт: {escape_markdown_v2(p['contacts'])}\n"
            
            full_text += post_block
            
            post_kb_row = []
            if can_edit(p):
                post_kb_row.append(InlineKeyboardButton(f"✏️ Редагувати № {local_post_num}", callback_data=f"edit_{p['id']}")) 
            post_kb_row.append(InlineKeyboardButton(f"🗑️ Видалити № {local_post_num}", callback_data=f"delete_{p['id']}")) 
            
            combined_keyboard.row(*post_kb_row)

            if i < len(page_posts) - 1:
                full_text += "\n—\n\n"

        nav_row_page_footer = []
        if offset > 0:
            nav_row_page_footer.append(InlineKeyboardButton("⬅️ Попередня", callback_data=f"mypage_{offset - MY_POSTS_PER_PAGE}"))
        if offset + MY_POSTS_PER_PAGE < total_posts: 
            nav_row_page_footer.append(InlineKeyboardButton("Наступна ➡️", callback_data=f"mypage_{offset + MY_POSTS_PER_PAGE}"))
        
        if nav_row_page_footer: 
            combined_keyboard.row(*nav_row_page_footer)
            
        combined_keyboard.add(InlineKeyboardButton("🏠 Головне меню", callback_data="go_back_to_main_menu"))
        
        await update_or_send_interface_message(chat_id, state, full_text, combined_keyboard, parse_mode='MarkdownV2', disable_web_page_preview=True)


    except Exception as e:
        logging.error(f"Error in show_my_posts_page for user {chat_id}: {e}", exc_info=True)
        await update_or_send_interface_message(chat_id, state, "Вибачте, сталася неочікувана помилка при завантаженні ваших оголошень\\. Спробуйте ще раз\\.", main_kb(), parse_mode='MarkdownV2')
        await state.set_state(AppStates.MAIN_MENU)

@dp.callback_query_handler(lambda c: c.data.startswith('mypage_'), state=AppStates.MY_POSTS_VIEW)
async def my_posts_paginate(call: CallbackQuery, state: FSMContext):
    logging.info(f"User {call.from_user.id} paginating my posts to offset {call.data.split('_')[1]}.")
    await call.answer()
    offset = int(call.data.split('_')[1])
    await show_my_posts_page(call.message.chat.id, state, offset)


# ======== Редагування ========
@dp.callback_query_handler(lambda c: c.data.startswith('edit_'), state=AppStates.MY_POSTS_VIEW)
async def edit_start(call: CallbackQuery, state: FSMContext):
    logging.info(f"User {call.from_user.id} initiated edit for post {call.data.split('_')[1]}.")
    await call.answer()
    pid = call.data.split('_')[1] # ID тепер рядок
    post = get_post_by_id_from_db(pid) # Отримуємо пост з БД
    
    if not post or not can_edit(post):
        logging.warning(f"User {call.from_user.id} tried to edit expired or non-existent post {pid}.")
        return await call.answer("⏰ Час редагування (15 хв) вичерпано\\.", show_alert=True, parse_mode='MarkdownV2') 
        
    await state.update_data(edit_pid=pid)
    
    await update_or_send_interface_message(call.message.chat.id, state, "✏️ Введіть новий опис (до 300 символів):", back_kb())
    await state.set_state(AppStates.EDIT_DESC)

@dp.message_handler(state=AppStates.EDIT_DESC)
async def process_edit(msg: types.Message, state: FSMContext):
    logging.info(f"User {msg.from_user.id} submitting new description for edit.")
    text = msg.text.strip()
    
    # Видаляємо повідомлення користувача
    try:
        await msg.delete()
    except MessageToDeleteNotFound:
        pass

    if not text or len(text) > 300:
        return await update_or_send_interface_message(msg.chat.id, state, f"❌ Неприпустимий опис \\(1\\-300 символів\\)\\.", back_kb(), parse_mode='MarkdownV2') 
        
    data = await state.get_data()
    pid = data['edit_pid']
    
    updated = update_post_description_in_db(pid, text) # Оновлюємо опис в БД
    if updated:
        log_event(f"Edited {pid}")
        await update_or_send_interface_message(msg.chat.id, state, "✅ Опис оголошення оновлено\\!", parse_mode='MarkdownV2')
        # Повертаємось до списку "Мої оголошення"
        await show_my_posts_page(msg.chat.id, state, data.get('offset', 0))
        await state.set_state(AppStates.MY_POSTS_VIEW)
    else:
        await update_or_send_interface_message(msg.chat.id, state, "❌ Помилка при оновленні оголошення\\. Спробуйте ще раз\\.", main_kb(), parse_mode='MarkdownV2')
        await state.set_state(AppStates.MAIN_MENU)


# ======== Видалення ========
@dp.callback_query_handler(lambda c: c.data.startswith('delete_'), state=AppStates.MY_POSTS_VIEW)
async def delete_post_handler(call: CallbackQuery, state: FSMContext): # Перейменовано, щоб уникнути конфлікту
    logging.info(f"User {call.from_user.id} initiating delete for post {call.data.split('_')[1]}.")
    
    pid = call.data.split('_')[1] # ID тепер рядок
    user_id = call.from_user.id
    
    deleted = delete_post_from_db(pid, user_id) # Видаляємо з БД, перевіряючи user_id
    
    if not deleted:
        logging.warning(f"User {call.from_user.id} tried to delete non-existent or unauthorized post {pid}.")
        await call.answer("🚫 Не вдалося видалити оголошення або воно вже було видалене.", show_alert=True) 
        # Якщо оголошення не знайдене або не належить користувачу, просто оновлюємо список
        await show_my_posts_page(call.message.chat.id, state, (await state.get_data()).get('offset', 0))
        return

    log_event(f"Deleted {pid}")
    await call.answer("✅ Оголошення видалено.", show_alert=True) 

    data = await state.get_data()
    current_offset = data.get('offset', 0)
    
    # Після видалення, отримуємо оновлений список оголошень користувача
    all_user_posts_after_delete = [p for p in get_posts_from_db() if p['user_id'] == user_id]
    
    # Визначаємо новий offset
    new_offset = current_offset
    
    # Якщо поточний offset тепер більший або дорівнює загальній кількості оголошень
    # і при цьому offset не нульовий, переміщаємось на попередню сторінку
    if new_offset >= len(all_user_posts_after_delete) and new_offset > 0:
        new_offset = max(0, new_offset - MY_POSTS_PER_PAGE)
    
    # Завжди оновлюємо сторінку після видалення
    await update_or_send_interface_message(call.message.chat.id, state, "🗑️ Оголошення успішно видалено\\!", parse_mode='MarkdownV2')
    await show_my_posts_page(call.message.chat.id, state, new_offset)
    await state.set_state(AppStates.MY_POSTS_VIEW)


# ======== Допомога ========
@dp.callback_query_handler(lambda c: c.data=='help', state=AppStates.MAIN_MENU)
async def help_handler(call: CallbackQuery, state: FSMContext):
    logging.info(f"User {call.from_user.id} requested help.")
    await call.answer()
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("Написати @VILARSO18", url="https://t.me/VILARSO18"))
    # Кнопка "Назад" завжди внизу
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="go_back_to_main_menu"))
    await update_or_send_interface_message(call.message.chat.id, state, "💬 Для співпраці або допомоги пишіть \\@VILARSO18", kb, parse_mode='MarkdownV2') 
    await state.set_state(AppStates.MAIN_MENU) 

# ======== Глобальний хендлер помилок ========
@dp.errors_handler()
async def err_handler(update, exception):
    logging.error(f"Update: {update} caused error: {exception}", exc_info=True)
    
    # Визначаємо chat_id для відправки повідомлення про помилку
    chat_id = None
    if update.callback_query and update.callback_query.message:
        chat_id = update.callback_query.message.chat.id
    elif update.message:
        chat_id = update.message.chat.id

    if chat_id:
        if isinstance(exception, (BadRequest, TelegramAPIError)):
            if "Can't parse entities" in str(exception):
                logging.error("Markdown parse error detected. Ensure all user-supplied text is escaped.")
                await update_or_send_interface_message(chat_id, dp.current_state(), "Вибачте, сталася помилка з відображенням тексту\\\\. Можливо, в описі є некоректні символи\\\\.", main_kb(), parse_mode='MarkdownV2')
                await dp.current_state().set_state(AppStates.MAIN_MENU)
                return True
            elif "Text must be non-empty" in str(exception):
                logging.error("Message text is empty error detected.")
                await update_or_send_interface_message(chat_id, dp.current_state(), "Вибачте, сталася внутрішня помилка\\\\. Спробуйте ще раз\\\\.", main_kb())
                await dp.current_state().set_state(AppStates.MAIN_MENU)
                return True
            elif "message is not modified" in str(exception): 
                logging.info("Message was not modified, skipping update.")
                return True 
    
    logging.critical(f"Unhandled error: {exception}", exc_info=True)
    return True

if __name__ == '__main__':
    logging.info("Starting bot...")
    executor.start_polling(dp, skip_updates=True)
