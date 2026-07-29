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
    os.getenv("TELEGRAM_MEDIA_GROUP_QUIET_PERIOD_SECONDS", "2.5")
)
# A separate Telegram text message may be bound to exactly one active album in
# the same chat/thread only during this short transport-level window.
TELEGRAM_MEDIA_GROUP_TEXT_JOIN_WINDOW_SECONDS = float(
    os.getenv("TELEGRAM_MEDIA_GROUP_TEXT_JOIN_WINDOW_SECONDS", "10")
)
# Forwarded text updates can overtake earlier forwarded album updates because
# webhook processing is concurrent. Only forwarded text may wait briefly for
# that exact earlier album to become active; ordinary text remains immediate.
TELEGRAM_FORWARDED_TEXT_JOIN_WAIT_SECONDS = float(
    os.getenv("TELEGRAM_FORWARDED_TEXT_JOIN_WAIT_SECONDS", "1.5")
)
# Emergency ceiling for one Telegram album workflow. Expiry never commits a
# partial batch; the group ends with a transport-level error instead.
TELEGRAM_MEDIA_GROUP_MAX_LIFETIME_SECONDS = float(
    os.getenv("TELEGRAM_MEDIA_GROUP_MAX_LIFETIME_SECONDS", "300")
)
if TELEGRAM_MEDIA_GROUP_QUIET_PERIOD_SECONDS <= 0:
    raise ValueError("TELEGRAM_MEDIA_GROUP_QUIET_PERIOD_SECONDS must be positive")
if TELEGRAM_MEDIA_GROUP_TEXT_JOIN_WINDOW_SECONDS <= 0:
    raise ValueError("TELEGRAM_MEDIA_GROUP_TEXT_JOIN_WINDOW_SECONDS must be positive")
if TELEGRAM_FORWARDED_TEXT_JOIN_WAIT_SECONDS < 0:
    raise ValueError(
        "TELEGRAM_FORWARDED_TEXT_JOIN_WAIT_SECONDS must not be negative"
    )
if TELEGRAM_MEDIA_GROUP_MAX_LIFETIME_SECONDS <= 0:
    raise ValueError("TELEGRAM_MEDIA_GROUP_MAX_LIFETIME_SECONDS must be positive")
if (
    TELEGRAM_MEDIA_GROUP_MAX_LIFETIME_SECONDS
    < TELEGRAM_MEDIA_GROUP_QUIET_PERIOD_SECONDS
):
    raise ValueError(
        "TELEGRAM_MEDIA_GROUP_MAX_LIFETIME_SECONDS must not be shorter than "
        "TELEGRAM_MEDIA_GROUP_QUIET_PERIOD_SECONDS"
    )
if (
    TELEGRAM_MEDIA_GROUP_TEXT_JOIN_WINDOW_SECONDS
    > TELEGRAM_MEDIA_GROUP_MAX_LIFETIME_SECONDS
):
    raise ValueError(
        "TELEGRAM_MEDIA_GROUP_TEXT_JOIN_WINDOW_SECONDS must not exceed "
        "TELEGRAM_MEDIA_GROUP_MAX_LIFETIME_SECONDS"
    )
if (
    TELEGRAM_FORWARDED_TEXT_JOIN_WAIT_SECONDS
    > TELEGRAM_MEDIA_GROUP_TEXT_JOIN_WINDOW_SECONDS
):
    raise ValueError(
        "TELEGRAM_FORWARDED_TEXT_JOIN_WAIT_SECONDS must not exceed "
        "TELEGRAM_MEDIA_GROUP_TEXT_JOIN_WINDOW_SECONDS"
    )

TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES = int(
    os.getenv("TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES", str(8 * 1024 * 1024))
)
if TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES <= 0:
    raise ValueError("TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES must be positive")

# Process-local v0.4 outbox pump. The minimum age prevents a recovered worker
# from racing the ordinary synchronous response path for freshly committed
# batches. Durable claim ownership still arbitrates multiple replicas.
TELEGRAM_READY_OUTBOX_POLL_SECONDS = float(
    os.getenv("TELEGRAM_READY_OUTBOX_POLL_SECONDS", "15")
)
TELEGRAM_READY_OUTBOX_MINIMUM_AGE_SECONDS = float(
    os.getenv("TELEGRAM_READY_OUTBOX_MINIMUM_AGE_SECONDS", "30")
)
TELEGRAM_READY_OUTBOX_BATCH_LIMIT = int(
    os.getenv("TELEGRAM_READY_OUTBOX_BATCH_LIMIT", "50")
)
if TELEGRAM_READY_OUTBOX_POLL_SECONDS <= 0:
    raise ValueError("TELEGRAM_READY_OUTBOX_POLL_SECONDS must be positive")
if not 0 <= TELEGRAM_READY_OUTBOX_MINIMUM_AGE_SECONDS <= 3600:
    raise ValueError(
        "TELEGRAM_READY_OUTBOX_MINIMUM_AGE_SECONDS must be between 0 and 3600"
    )
if not 1 <= TELEGRAM_READY_OUTBOX_BATCH_LIMIT <= 500:
    raise ValueError("TELEGRAM_READY_OUTBOX_BATCH_LIMIT must be between 1 and 500")

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
