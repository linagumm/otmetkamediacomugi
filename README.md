# Telegram-бот посещаемости — исправленная версия для FadeHost

## FadeHost settings
- **Branch:** `main`
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `python bot.py`

Все Python-файлы (`bot.py`, `config.py`, `database.py`, `keyboards.py`, `sheets.py`, `import_students.py`) должны находиться в корне репозитория рядом с `requirements.txt`.

## Переменные окружения
В настройках проекта FadeHost добавьте:
- `BOT_TOKEN` — новый токен бота от @BotFather
- `ADMIN_IDS` — Telegram ID старосты (число; если несколько, разделите запятыми)
- `TIMEZONE` — например `Asia/Yekaterinburg`
- `DATABASE_PATH` — `data/attendance.sqlite3`
- `ATTENDANCE_WINDOW_MINUTES` — `30`

Google Sheets необязателен. Для включения синхронизации также задайте `GOOGLE_SHEETS_ID`, `GOOGLE_SERVICE_ACCOUNT_FILE` и обеспечьте наличие JSON-ключа сервисного аккаунта на сервере. Если эти настройки не заданы, бот работает с SQLite без синхронизации.

## Первоначальная настройка
1. Вставьте файлы в корень GitHub-репозитория, заменив старые версии.
2. Проверьте, что `requirements.txt` тоже в корне.
3. В FadeHost проверьте команды сборки/запуска выше и переменные окружения.
4. Сделайте Commit changes в GitHub. FadeHost должен выполнить новый deploy.
5. В Telegram отправьте боту `/start`. Для проверки ID используйте `/id`.
6. Добавьте реальных студентов в CSV `students.csv` с колонками `telegram_id,full_name,group_name` и импортируйте его командой `python import_students.py students.csv` только если есть доступ к консоли с нужным окружением и базой.

**Важно:** SQLite-файл на хостинге должен сохраняться между перезапусками, иначе записи могут потеряться. Если платформа не предоставляет постоянный диск, потребуется внешняя база данных.

Не публикуйте токен бота или JSON-ключ Google в репозитории. Если токен уже попадал на скриншоты, отзовите его через @BotFather и сохраните новый только в переменных окружения FadeHost.
