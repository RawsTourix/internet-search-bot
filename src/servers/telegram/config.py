import os
from dotenv import load_dotenv

# Загрузка переменных из .env
load_dotenv()

# Токен телеграмм бота
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Настройки вебхука
WEBHOOK_DOMAIN = os.getenv("WEBHOOK_DOMAIN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# Настройка API
TELEGRAM_API_KEY = os.getenv("TELEGRAM_API_KEY")

# Получение Gateway URL
GATEWAY_URL = os.getenv("GATEWAY_URL")

TELEGRAM_PROGRESS_CALLBACK_URL = os.getenv(
    "TELEGRAM_PROGRESS_CALLBACK_URL",
    "http://127.0.0.1:8001/internal/progress",
)
TELEGRAM_PROGRESS_CALLBACK_TOKEN = os.getenv(
    "TELEGRAM_PROGRESS_CALLBACK_TOKEN",
    "",
) or WEBHOOK_SECRET or ""

# Закрытый endpoint, через который Gateway потоково забирает Telegram file_id.
TELEGRAM_FILE_PROVIDER_TOKEN = os.getenv(
    "TELEGRAM_FILE_PROVIDER_TOKEN",
    "",
) or WEBHOOK_SECRET or ""
TELEGRAM_BOT_INSTANCE_ID = os.getenv(
    "TELEGRAM_BOT_INSTANCE_ID",
    "default",
).strip() or "default"

# Quiet period only closes the known album composition. Active uploads are
# tracked separately and must finish before the commit callback can run.
TELEGRAM_MEDIA_GROUP_QUIET_PERIOD_SECONDS = float(
    os.getenv(
        "TELEGRAM_MEDIA_GROUP_QUIET_PERIOD_SECONDS",
        os.getenv("TELEGRAM_MEDIA_GROUP_COMMIT_DELAY_SECONDS", "2.5"),
    )
)
# Compatibility alias for existing deployments and imports.
TELEGRAM_MEDIA_GROUP_COMMIT_DELAY_SECONDS = (
    TELEGRAM_MEDIA_GROUP_QUIET_PERIOD_SECONDS
)

TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES = int(
    os.getenv("TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES", str(8 * 1024 * 1024))
)

PROGRESS_EDIT_MIN_INTERVAL = float(
    os.getenv("PROGRESS_EDIT_MIN_INTERVAL", "1.2")
)
PROGRESS_MAX_TEXT_LENGTH = int(
    os.getenv("PROGRESS_MAX_TEXT_LENGTH", "1000")
)
TELEGRAM_FINAL_EDIT_MAX_LENGTH = int(
    os.getenv("TELEGRAM_FINAL_EDIT_MAX_LENGTH", "3500")
)
TELEGRAM_FINAL_DELIVERY_MODE = os.getenv(
    "TELEGRAM_FINAL_DELIVERY_MODE",
    "send_new",
)
