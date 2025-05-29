import logging
import os
import datetime
import json
import re
import asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, MessageHandler, filters, ContextTypes, CommandHandler,
    ChatMemberHandler, ConversationHandler, CallbackQueryHandler
)
# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    filename='bot.log',  # Добавляем запись логов в файл
    filemode='a'
)
logger = logging.getLogger(__name__)
# Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE_PATH", "credentials_telegram.json")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

if not TELEGRAM_BOT_TOKEN:
    logger.critical("TELEGRAM_BOT_TOKEN not set. Bot cannot start.")
    exit()
if not GOOGLE_SHEET_ID:
    logger.warning("GOOGLE_SHEET_ID not set. Google Sheets functionality impaired.")
if not OPENAI_API_KEY and OPENAI_LIB_AVAILABLE:
    logger.warning("OPENAI_API_KEY not set. GPT-4o functionality disabled.")
# Attempt to import external libraries
try:
    import gspread
    from google.oauth2.service_account import Credentials
    GOOGLE_LIBS_AVAILABLE = True
except ImportError:
    GOOGLE_LIBS_AVAILABLE = False
    logging.warning("Google Sheets libraries not found. Google Sheets functionality disabled.")

try:
    import openai
    OPENAI_LIB_AVAILABLE = True
except ImportError:
    OPENAI_LIB_AVAILABLE = False
    logging.warning("OpenAI library not found. GPT-4o functionality disabled.")

# Initialize OpenAI client
openai_client = None
if OPENAI_LIB_AVAILABLE and OPENAI_API_KEY:
    try:
        openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
        logger.info("OpenAI client initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize OpenAI client: {e}")
# In-memory storage
BOT_DATA = {
    "sysadmin_telegram_id": None,
    "sysadmin_telegram_username": None,
    "task_type_deadlines": {
        "default": 24,
        "📨 Почта / Office / Outlook / Teams": 8,
        "🖨 Принтер / Сканер / Картриджи": 4,
        "💾 Программы (1С, AutoCAD, др.)": 8,
        "🔧 Компьютеры и ноутбуки": 12,
        "🌐 Интернет / Сеть / Кабели": 6,
        "🚪 Пропуска / СКУД / Видеонаблюдение": 4,
        "👤 Доступы / Учетки / Замена сотрудников": 2
    },
    "user_violations": {}
}

# Conversation states
CONFIRM_TASK = 0
# Google Sheets Manager
class GoogleSheetsManager:
    def __init__(self, credentials_file_path, spreadsheet_id):
        self.spreadsheet_id = spreadsheet_id
        self.creds = None
        self.client = None
        self.sheet = None
        self.header = ["ID Задачи", "Описание Задачи", "Дата Постановки", "Категория",
                       "Срок Выполнения (план)", "Статус", "Исполнитель (ID)",
                       "Дата Факт. Выполнения", "ID Сообщения Задачи",
                       "ID Постановщика Задачи", "Комментарии"]
        self.offtopic_header = ["Дата", "ID Пользователя", "Имя Пользователя", "Текст Сообщения"]
        self.complaints_header = ["Дата", "ID Пользователя", "Имя Пользователя", "Текст Жалобы", "Связанное Сообщение"]

        if not GOOGLE_LIBS_AVAILABLE or not spreadsheet_id:
            logger.warning("Google Sheets libraries or Spreadsheet ID missing.")
            return

        try:
            if not os.path.exists(credentials_file_path):
                logger.error(f"Credentials file {credentials_file_path} not found.")
                return

            scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            self.creds = Credentials.from_service_account_file(credentials_file_path, scopes=scopes)
            self.client = gspread.authorize(self.creds)
            self.sheet = self.client.open_by_key(spreadsheet_id)

            # Инициализация листов
            self.tasks_sheet = self.sheet.worksheet("Tasks") if "Tasks" in [ws.title for ws in self.sheet.worksheets()] else self.sheet.add_worksheet("Tasks", 1000, 20)
            self.offtopic_sheet = self.sheet.worksheet("OfftopicLog") if "OfftopicLog" in [ws.title for ws in self.sheet.worksheets()] else self.sheet.add_worksheet("OfftopicLog", 1000, 10)
            self.complaints_sheet = self.sheet.worksheet("Complaints") if "Complaints" in [ws.title for ws in self.sheet.worksheets()] else self.sheet.add_worksheet("Complaints", 1000, 10)

            # Проверка заголовков
            if not self.tasks_sheet.row_values(1):
                self.tasks_sheet.append_row(self.header)
            if not self.offtopic_sheet.row_values(1):
                self.offtopic_sheet.append_row(self.offtopic_header)
            if not self.complaints_sheet.row_values(1):
                self.complaints_sheet.append_row(self.complaints_header)

            logger.info("Connected to Google Sheets API.")
        except Exception as e:
            logger.error(f"Failed to connect to Google Sheets: {e}")
            self.sheet = None

    def _get_next_task_id(self):
        if not self.tasks_sheet:
            return 1
        try:
            records = self.tasks_sheet.get_all_records()
            return max((int(r.get("ID Задачи", 0)) for r in records), default=0) + 1
        except Exception as e:
            logger.error(f"Error getting next task ID: {e}")
            return 1
    def add_task(self, task_description, task_category, deadline_plan, message_id, user_id, user_name):
        if not self.tasks_sheet:
            logger.warning("Google Sheets not available. Task not added.")
            return None, False
        try:
            task_id = self._get_next_task_id()
            date_created = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            status = "Новая"
            row = [task_id, task_description, date_created, task_category, deadline_plan, status,
                   BOT_DATA.get("sysadmin_telegram_id", "Не назначен"), "", str(message_id),
                   f"{user_name} ({user_id})", ""]
            self.tasks_sheet.append_row(row)
            logger.info(f"Task {task_id} added: {task_description}")
            return task_id, True
        except Exception as e:
            logger.error(f"Error adding task: {e}")
            return None, False

    def find_task_row(self, task_id_str):
        if not self.tasks_sheet:
            return None
        try:
            task_id = int(task_id_str.strip())
            records = self.tasks_sheet.get_all_records()
            for i, record in enumerate(records, 2):
                if int(record.get("ID Задачи", 0)) == task_id:
                    return i
            logger.warning(f"Task ID {task_id} not found.")
            return None
        except ValueError:
            logger.warning(f"Invalid task ID format: {task_id_str}")
            return None

    def update_task_status(self, task_id_str, new_status, sysadmin_id_on_done=None):
        if not self.tasks_sheet:
            return False
        row_index = self.find_task_row(task_id_str)
        if not row_index:
            return False
        try:
            self.tasks_sheet.update_cell(row_index, 6, new_status)
            if new_status == "Выполнена" and sysadmin_id_on_done:
                self.tasks_sheet.update_cell(row_index, 8, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                executor = self.tasks_sheet.cell(row_index, 7).value
                if not executor or executor == "Не назначен":
                    self.tasks_sheet.update_cell(row_index, 7, str(sysadmin_id_on_done))
            logger.info(f"Task {task_id_str} status updated to {new_status}")
            return True
        except Exception as e:
            logger.error(f"Error updating task {task_id_str}: {e}")
            return False
    def get_task_info(self, task_id_str):
        if not self.tasks_sheet:
            return None
        row_index = self.find_task_row(task_id_str)
        if not row_index:
            return None
        try:
            return dict(zip(self.header, self.tasks_sheet.row_values(row_index)))
        except Exception as e:
            logger.error(f"Error getting task {task_id_str} info: {e}")
            return None

    def get_active_tasks(self):
        if not self.tasks_sheet:
            return []
        try:
            return [r for r in self.tasks_sheet.get_all_records()
                    if r.get("Статус") not in ["Выполнена", "Отменена"]]
        except Exception as e:
            logger.error(f"Error getting active tasks: {e}")
            return []

    def calculate_remaining_time(self, task_id_str):
        task = self.get_task_info(task_id_str)
        if not task or not task.get("Срок Выполнения (план)"):
            return None
        deadline = datetime.datetime.strptime(task["Срок Выполнения (план)"], "%Y-%m-%d %H:%M:%S")
        now = datetime.datetime.now()
        time_diff = deadline - now
        if time_diff.total_seconds() <= 0:
            return "Время истекло"
        hours = int(time_diff.total_seconds() / 3600)
        minutes = int((time_diff.total_seconds() % 3600) / 60)
        return f"{hours} часов {minutes} минут"

    def log_offtopic_message(self, user_id, user_name, message_text):
        if not self.offtopic_sheet:
            logger.warning("Google Sheets not available. Offtopic message not logged.")
            return
        try:
            row = [datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, user_name, message_text]
            self.offtopic_sheet.append_row(row)
            logger.info(f"Offtopic message logged: {message_text}")
        except Exception as e:
            logger.error(f"Error logging offtopic message: {e}")

    def log_complaint(self, user_id, user_name, complaint_text, related_message=None):
        if not self.complaints_sheet:
            logger.warning("Google Sheets not available. Complaint not logged.")
            return
        try:
            row = [datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, user_name, complaint_text, related_message or ""]
            self.complaints_sheet.append_row(row)
            logger.info(f"Complaint logged: {complaint_text}")
        except Exception as e:
            logger.error(f"Error logging complaint: {e}")

sheets_manager = GoogleSheetsManager(CREDENTIALS_FILE, GOOGLE_SHEET_ID)
# Command Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    await update.message.reply_text(
        f"Привет, {user.full_name}! Я бот-администратор IT-отдела.\n"
        "Используйте /help для просмотра доступных команд."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    user = update.effective_user
    help_text = (
        "Доступные команды:\n"
        "/start - Начать работу с ботом\n"
        "/help - Показать это сообщение\n"
        "/task <описание> - Создать новую задачу\n"
        "/status <ID> - Проверить статус задачи\n"
        "/tasks - Показать список активных задач"
    )
    if BOT_DATA.get("sysadmin_telegram_id") and user.id == BOT_DATA["sysadmin_telegram_id"]:
        help_text += "\n\nКоманды для сисадмина:\n/done <ID> - Отметить задачу выполненной"
    if await check_is_admin(update, context, user.id):
        help_text += "\n\nКоманды для администратора:\n/set_sysadmin <ID или @username> - Назначить системного администратора"
    await update.message.reply_text(help_text)

async def task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /task"""
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Использование: /task <описание задачи>")
        return

    description = " ".join(context.args)
    analysis = await get_gpt_analysis(description)
    category = analysis.get("category", "📌 Другое / Не по теме")

    context.user_data["task_description"] = description
    context.user_data["task_category"] = category
    await update.message.reply_text(
        f"Сообщение классифицировано как: *{category}*.\nПодтвердите создание задачи или уточните детали:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["Подтвердить", "Уточнить", "Отмена"]], resize_keyboard=True)
    )
    return CONFIRM_TASK

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /status"""
    if not context.args:
        await update.message.reply_text("Использование: /status <ID задачи>")
        return

    task_id = context.args[0]
    task = sheets_manager.get_task_info(task_id)
    if not task:
        await update.message.reply_text("❌ Задача не найдена.")
        return

    remaining_time = sheets_manager.calculate_remaining_time(task_id)
    response = (f"📋 Задача ID {task_id}:\n"
                f"*{task['Описание Задачи']}*\n\n"
                f"Категория: {task['Категория']}\n"
                f"Статус: {task['Статус']}\n"
                f"Осталось времени: {remaining_time or 'Не указано'}")
    await update.message.reply_text(response, parse_mode="Markdown")

async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /tasks"""
    tasks = sheets_manager.get_active_tasks()
    if not tasks:
        await update.message.reply_text("📋 Активных задач нет.", parse_mode="Markdown")
        return

    response = "📋 *Активные задачи:*\n\n"
    for task in tasks:
        task_id = task["ID Задачи"]
        remaining_time = sheets_manager.calculate_remaining_time(str(task_id))
        response += f"• ID: {task_id} - {task['Описание Задачи']}\n"
        response += f"  Категория: {task['Категория']}, Осталось: {remaining_time or 'Не указано'}\n\n"
    await update.message.reply_text(response, parse_mode="Markdown")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /done"""
    user = update.effective_user
    if not BOT_DATA.get("sysadmin_telegram_id") or user.id != BOT_DATA["sysadmin_telegram_id"]:
        await update.message.reply_text("❌ Эта команда доступна только системному администратору.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /done <ID задачи>")
        return

    task_id = context.args[0]
    if sheets_manager.update_task_status(task_id, "Выполнена", user.id):
        await update.message.reply_text(f"✅ Задача ID {task_id} отмечена как выполненная.", parse_mode="Markdown")
        try:
            task = sheets_manager.get_task_info(task_id)
            if task:
                creator_id_match = re.search(r'\((\d+)\)$', task["ID Постановщика Задачи"])
                if creator_id_match:
                    creator_id = int(creator_id_match.group(1))
                    await context.bot.send_message(
                        creator_id,
                        f"✅ Ваша задача ID {task_id} выполнена!\n\n*{task['Описание Задачи']}*",
                        parse_mode="Markdown"
                    )
        except Exception as e:
            logger.error(f"Failed to notify creator for task {task_id}: {e}")
    else:
        await update.message.reply_text("❌ Ошибка при обновлении задачи.", parse_mode="Markdown")

async def set_sysadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /set_sysadmin"""
    user = update.effective_user
    if not await check_is_admin(update, context, user.id):
        await update.message.reply_text("❌ Эта команда доступна только администраторам группы.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /set_sysadmin <ID или @username>")
        return

    target = context.args[0]
    if target.startswith('@'):
        # Сохраняем username, ID будет получен позже
        BOT_DATA["sysadmin_telegram_username"] = target[1:]
        await update.message.reply_text(
            f"✅ @{target[1:]} установлен как системный администратор.\n"
            f"Для завершения настройки, пользователь должен отправить /iam_sysadmin"
        )
    else:
        try:
            sysadmin_id = int(target)
            BOT_DATA["sysadmin_telegram_id"] = sysadmin_id
            await update.message.reply_text(f"✅ Пользователь с ID {sysadmin_id} установлен как системный администратор.")
        except ValueError:
            await update.message.reply_text("❌ Неверный формат ID. Используйте числовой ID или @username.")

async def iam_sysadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /iam_sysadmin"""
    user = update.effective_user
    if user.username and user.username.lower() == BOT_DATA.get("sysadmin_telegram_username", "").lower():
        BOT_DATA["sysadmin_telegram_id"] = user.id
        BOT_DATA["sysadmin_telegram_username"] = user.username
        await update.message.reply_text("✅ Вы успешно установлены как системный администратор.")
    else:
        await update.message.reply_text("❌ Вы не назначены системным администратором.")
# Helper functions
async def check_is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        chat_member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
        return chat_member.status in ["creator", "administrator"]
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return False

async def get_rules_text():
    return (
        "📜 *Правила чата IT Oiltech*\n\n"
        "Разрешены только ИТ-сообщения, связанные с:\n"
        "- 📨 Почта / Office / Outlook / Teams\n"
        "- 🖨 Принтер / Сканер / Картриджи\n"
        "- 💾 Программы (1С, AutoCAD, др.)\n"
        "- 🔧 Компьютеры и ноутбуки\n"
        "- 🌐 Интернет / Сеть / Кабели\n"
        "- 🚪 Пропуска / СКУД / Видеонаблюдение\n"
        "- 👤 Доступы / Учетки / Замена сотрудников\n\n"
        "Запрещено:\n"
        "- Разговоры не по теме (погода, личные обсуждения, мемы)\n"
        "- Жалобы, флуд, голосовые сообщения\n\n"
        "Для вопросов о правилах напишите 'Какие темы можно обсуждать?'"
    )

async def notify_admins(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message: str):
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        for admin in admins:
            await context.bot.send_message(admin.user.id, message, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to notify admins: {e}")

async def get_gpt_analysis(text_message: str) -> dict:
    if not openai_client:
        return {"category": "📌 Другое / Не по теме", "action": "offtopic"}

    prompt = """
    Ты — интеллектуальный агент, виртуальный администратор группы IT Oiltech компании SRL Oiltech.
    Твоя цель: контролировать тематическую чистоту чата (разрешены только вопросы, связанные с технической поддержкой, ИТ-инфраструктурой и обслуживанием), определять категорию сообщения и действие, которое нужно выполнить.

    Как ты работаешь:
    1. Получаешь сообщение из группы.
    2. Извлекаешь суть, игнорируя имена, обращения и ненужные детали.
    3. Определяешь категорию сообщения из списка ниже.
    4. Определяешь действие на основе контекста:
       - "create_task": Создать задачу для ИТ-тематики.
       - "check_status": Проверить статус задач (если запрашивается, например, "Задача выполнена" или "ID 123 готово").
       - "mark_done": Отметить задачу как выполненную (если указано, например, "Задача выполнена" или "ID 123 готово").
       - "offtopic": Сообщение не по теме, удалить и уведомить пользователя.
       - "show_rules": Показать правила чата (если запрошены, например, "Какие темы можно обсуждать?").
       - "complain": Обработать жалобу (если содержит "жалоба", "проблема с модерацией").
    5. Возвращаешь JSON с категорией и действием, например:
       {"category": "🖨 Принтер / Сканер / Картриджи", "action": "create_task", "entities": {"description": "Принтер не печатает"}}
       {"category": "📌 Другое / Не по теме", "action": "offtopic"}
       {"category": "📌 Другое / Не по теме", "action": "show_rules"}
       {"category": "📌 Другое / Не по теме", "action": "complain", "entities": {"complaint_text": "Жалоба на модерацию"}}

    Запрещено в чате:
    - Разговоры не по теме (погода, обсуждения, конфликты, жалобы, личные разговоры).
    - Мемы, шутки, флуд, голосовые сообщения.
    - Просьбы без ИТ-содержания.

    Категории:
    - 📨 Почта / Office / Outlook / Teams (почта, Teams, Office365)
    - 🖨 Принтер / Сканер / Картриджи (проблемы с принтерами, сканерами, картриджами)
    - 💾 Программы (1С, AutoCAD, др.) (установка, обновление, ошибки)
    - 🔧 Компьютеры и ноутбуки (устройства, драйверы, мониторы, периферия)
    - 🌐 Интернет / Сеть / Кабели (интернет, Wi-Fi, кабели, порты)
    - 🚪 Пропуска / СКУД / Видеонаблюдение (доступ, камеры, турникеты)
    - 👤 Доступы / Учетки / Замена сотрудников (учётные записи, пароли)
    - 📌 Другое / Не по теме (флуд, личные вопросы, оффтоп)
    """
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": text_message}],
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"OpenAI error for '{text_message}': {e}")
        return {"category": "📌 Другое / Не по теме", "action": "offtopic"}

# Keyboard Helpers
def get_main_keyboard(is_sysadmin=False, is_admin=False):
    keyboard = [[KeyboardButton("📋 Список задач")]]
    if is_sysadmin:
        keyboard.append([KeyboardButton("✅ Отметить выполненной")])
    if is_admin:
        keyboard.append([KeyboardButton("⚙️ Настройки")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# Message Handlers
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE, transcribed_text: str = None):
    text = transcribed_text or update.message.text
    if not text:
        return

    user = update.effective_user
    logger.info(f"Message from {user.full_name} ({user.id}): {text}")

    # Проверка, является ли пользователь админом
    is_admin = await check_is_admin(update, context, user.id)
    is_sysadmin = BOT_DATA.get("sysadmin_telegram_id") and user.id == BOT_DATA["sysadmin_telegram_id"]

    # Обработка уточнения задачи
    if context.user_data.get("awaiting_task_description"):
        if text == "Отмена":
            context.user_data.clear()
            await update.message.reply_text(
                "❌ Создание задачи отменено.",
                reply_markup=get_main_keyboard(is_sysadmin, is_admin)
            )
            return
        context.user_data.pop("awaiting_task_description")
        context.user_data["task_description"] = text
        await update.message.reply_text(
            f"Сообщение классифицировано как: *{context.user_data.get('task_category')}*.\nПодтвердите создание задачи или уточните детали:",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["Подтвердить", "Уточнить", "Отмена"]], resize_keyboard=True)
        )
        return CONFIRM_TASK

    # Анализ сообщения через GPT-4o
    analysis = await get_gpt_analysis(text)
    category = analysis.get("category", "📌 Другое / Не по теме")
    action = analysis.get("action", "offtopic")
    entities = analysis.get("entities", {})

    # Отслеживание нарушений
    if category == "📌 Другое / Не по теме" and not is_admin:
        BOT_DATA["user_violations"][user.id] = BOT_DATA["user_violations"].get(user.id, 0) + 1
        sheets_manager.log_offtopic_message(user.id, user.full_name, text)
        try:
            if (await context.bot.get_chat_member(update.effective_chat.id, context.bot.id)).can_delete_messages:
                await update.message.delete()
            await context.bot.send_message(
                user.id,
                f"@{user.username}, ваше сообщение удалено, так как оно не относится к ИТ-тематике. Пожалуйста, пишите только о технических вопросах.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📜 Правила чата", callback_data="show_rules")]
                ])
            )
            if BOT_DATA["user_violations"][user.id] >= 3:
                await context.bot.send_message(user.id, await get_rules_text(), parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to handle offtopic message: {e}")
        return

    # Обработка действий
    if action == "create_task":
        description = entities.get("description", text)
        context.user_data["task_description"] = description
        context.user_data["task_category"] = category
        await update.message.reply_text(
            f"Сообщение классифицировано как: *{category}*.\nПодтвердите создание задачи или уточните детали:",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["Подтвердить", "Уточнить", "Отмена"]], resize_keyboard=True)
        )
        return CONFIRM_TASK

    elif action == "check_status" and is_admin:
        tasks = sheets_manager.get_active_tasks()
        if not tasks:
            await update.message.reply_text("📋 Активных задач нет.", parse_mode="Markdown")
            return
        response = "📋 *Активные задачи:*\n\n"
        for task in tasks:
            task_id = task["ID Задачи"]
            remaining_time = sheets_manager.calculate_remaining_time(str(task_id))
            response += f"• ID: {task_id} - {task['Описание Задачи']}\n"
            response += f"  Категория: {task['Категория']}, Осталось: {remaining_time or 'Не указано'}\n\n"
        await update.message.reply_text(response, parse_mode="Markdown")
        return

    elif action == "mark_done" and is_sysadmin:
        task_id = entities.get("task_id")
        if task_id and sheets_manager.update_task_status(task_id, "Выполнена", user.id):
            await update.message.reply_text(f"✅ Задача ID {task_id} отмечена как выполненная.", parse_mode="Markdown")
            try:
                task = sheets_manager.get_task_info(task_id)
                if task:
                    creator_id_match = re.search(r'\((\d+)\)$', task["ID Постановщика Задачи"])
                    if creator_id_match:
                        creator_id = int(creator_id_match.group(1))
                        await context.bot.send_message(
                            creator_id,
                            f"✅ Ваша задача ID {task_id} выполнена!\n\n*{task['Описание Задачи']}*",
                            parse_mode="Markdown"
                        )
            except Exception as e:
                logger.error(f"Failed to notify creator for task {task_id}: {e}")
        else:
            await update.message.reply_text("❌ Ошибка при обновлении задачи.", parse_mode="Markdown")
        return

    elif action == "show_rules":
        await update.message.reply_text(await get_rules_text(), parse_mode="Markdown")
        return

    elif action == "complain":
        complaint_text = entities
