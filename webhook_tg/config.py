START_PHOTO_ID = "AgACAgIAAxkDAAEKAyRqJJYFv4D0Ix7NOIF7wjy8Jaq7ZgACKBhrG6BCKEmQv3TUzQcFxgEAAwIAA3gAAzsE"
START_TEXT = (
    "<b>Добро пожаловать!</b>\n\n"
    "<b>Этот бот помогает контролировать\n"
    "переписку.</b>\n\n"
    "Возможности:\n"
    "• Мгновенно уведомит, если собеседник\n"
    "изменит или удалит сообщение.\n"
    "• Пришлёт исходный текст/копию\n"
    "удалённого сообщения.\n"
    "• Одноразовые фото/видео: не открывайте,\n"
    "ответьте (reply) любым текстом —\n"
    "бот пришлёт копию.\n"
    "• <code>/mute @username</code> — удалять его\n"
    "сообщения у обоих (срок и уведомления\n"
    "выбираются кнопками).\n\n"
    "Как подключить бота — смотрите\n"
    "инструкцию на картинке сверху.\n\n"
    f"<b>@who_update_bot</b>"
)
DEMO_CALLBACK_DATA = "who_update_demo"
START_REPLY_MARKUP = {
    "inline_keyboard": [
        [
            {
                "text": "🎬 Демонстрация работы бота",
                "callback_data": DEMO_CALLBACK_DATA,
            }
        ]
    ]
}
DEMO_VIDEO_CAPTIONS = (
    (
            "<b>1. Сохранение скрытого медиа</b>\n\n"
            "Показываем, как WhoUpdate сохраняет одноразовое фото или видео: "
            "достаточно ответить на сообщение, не открывая медиа."
    ),
    (
            "<b>2. Изменение сообщения</b>\n\n"
            "WhoUpdate замечает редактирование и присылает исходный текст сообщения."
    ),
    (
            "<b>3. Удаление сообщения</b>\n\n"
            "После удаления WhoUpdate отправляет сохранённую копию сообщения или файла."
    ),
)
try:
    from .demo_media_local import DEMO_VIDEO_FILE_IDS
except ImportError:
    DEMO_VIDEO_FILE_IDS = ()

DEMO_VIDEOS = tuple(
    {"file_id": file_id, "caption": caption}
    for file_id, caption in zip(DEMO_VIDEO_FILE_IDS, DEMO_VIDEO_CAPTIONS)
)
BOT_ACTIVATED_TEXT = (
    "<b>WhoUpdate успешно активирован</b>\n\n"
    "Бот подключён к автоматизации чатов и готов к работе.\n"
    "Теперь будут приходить уведомления об удалениях, правках "
    "и одноразовых медиа в разрешённых чатах.\n\n"
    "Полезные команды:\n"
    "• <code>/mute @username</code> — глушить собеседника\n"
    "• <code>/mutelist</code> — список mute\n"
    "• <code>/unmute @username</code> — снять mute"
)
BOT_DEACTIVATED_TEXT = (
    "<b>WhoUpdate отключён</b>\n\n"
    "Автоматизация чатов снята. Уведомления больше не приходят.\n"
    "Чтобы снова включить — подключите бота в настройках профиля."
)
CONNECTION_REMINDER_TEXT = (
    "<b>WhoUpdate пока не подключён</b>\n\n"
    "Вы запустили бота, но ещё не завершили подключение. "
    "Без этого бот не сможет отслеживать удалённые и изменённые сообщения.\n\n"
    "<b>Как подключить:</b>\n"
    "1. Откройте настройки Telegram.\n"
    "2. Перейдите в раздел <b>Telegram для бизнеса → Чат-боты</b>.\n"
    "3. Добавьте <b>@who_update_bot</b>.\n"
    "4. Разрешите боту доступ к сообщениям и сохраните настройки.\n\n"
    "Подробная инструкция показана на картинке выше."
)
OWNER_CHAT_ID = "1394340082"

# IndexNow (Яндекс): ключ в корне https://maksonchik.ru/{INDEXNOW_KEY}.txt
INDEXNOW_KEY = "qIXnCp99XqCIbkmFQv6mWaNweY2n1fio"
INDEXNOW_HOST = "maksonchik.ru"
INDEXNOW_ENDPOINT = "https://yandex.com/indexnow"

# chat_id пользователей, которым разрешены команды /send_photo, /send_audio, /send_video
ALLOWED_SEND_CHAT_IDS = [
    1394340082,  # maksonchik200
    870546616,   # angelinatam
]
