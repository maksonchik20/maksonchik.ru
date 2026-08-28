#!/usr/bin/env python3
import os
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "maksonchik.settings")
django.setup()

from webhook_tg.models import UserTg
from webhook_tg.telegram import tg_send_message

TEXT = """<b>WhoUpdate обновился</b>

Бот стал работать стабильнее: быстрее ловит удаления и правки, надёжнее доставляет уведомления.

<b>Что нового</b>

1. <b>Одноразовые фото и видео (view once)</b>
Не открывайте сообщение. Сделайте reply любым текстом — WhoUpdate пришлёт копию вам в чат с ботом.

2. <b>Команда /mute @username</b>
Можно глушить собеседника на 10 минут, 1 час, 3 часа, 1 день или навсегда.
На первое его сообщение уходит предупреждение от вашего имени, дальше сообщения удаляются у обоих.
По желанию — получать копии этих сообщений в боте (кнопка «Да» при настройке).

<b>Как настроить mute</b>
В личке с ботом: <code>/mute @username</code> → выберите срок → хотите ли получать сообщения в боте.

Нужны права бота в «Автоматизация чатов»: удаление сообщений и ответы от вашего имени.

Вопросы — пишите сюда.

@who_update_bot"""


def label(u: UserTg) -> str:
    uname = u.username or "-"
    fname = u.first_name or "-"
    return f"@{uname} | {fname} | chat_id={u.chat_id} | user_id={u.user_id}"


def main() -> None:
    users = list(
        UserTg.objects.exclude(chat_id__isnull=True).exclude(chat_id=1).order_by("id")
    )
    seen = set()
    recipients = []
    for u in users:
        if u.chat_id in seen:
            continue
        seen.add(u.chat_id)
        recipients.append(u)

    print(f"RECIPIENTS={len(recipients)}")
    ok_list = []
    fail_list = []

    for i, u in enumerate(recipients, 1):
        lab = label(u)
        try:
            ok = tg_send_message(u.chat_id, TEXT, timeout=15)
            err = ""
        except Exception as exc:
            ok = False
            err = str(exc)
        if ok:
            ok_list.append(lab)
            print(f"OK {i}/{len(recipients)} {lab}")
        else:
            fail_list.append(f"{lab} | {err}")
            print(f"FAIL {i}/{len(recipients)} {lab} {err}")
        time.sleep(0.35)

    print("====OK====")
    for x in ok_list:
        print(x)
    print("====FAIL====")
    for x in fail_list:
        print(x)
    print(f"SUMMARY ok={len(ok_list)} fail={len(fail_list)} total={len(recipients)}")


if __name__ == "__main__":
    main()
