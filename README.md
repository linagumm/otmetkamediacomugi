# Telegram-бот посещаемости

Python 3.11+, aiogram 3, SQLite и Google Sheets API. Отметка отправляется студентам в личные сообщения.

## Возможности
- студенты регистрируются по заранее импортированному Telegram ID;
- староста создаёт занятие и указывает предмет и группу;
- бот отправляет личные сообщения с кнопкой «Я присутствую»;
- окно отметки по умолчанию 30 минут;
- проверка группы, времени и повторных отметок;
- закрытие занятия добавляет отсутствующих для всех не отметившихся;
- история посещений;
- фоновая синхронизация Google Sheets с повторными попытками;
- стабильные ID записей используются для обновления строк при повторной синхронизации.

## 1. Установка

Нужен Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
```

Заполните `.env`: `BOT_TOKEN`, `ADMIN_IDS`, `TIMEZONE`, `GOOGLE_SHEETS_ID` и `GOOGLE_SERVICE_ACCOUNT_FILE`.

Токен Telegram получите у `@BotFather` через `/newbot`. Не публикуйте токен.

## 2. Google Sheets API

1. Создайте таблицу Google Sheets.
2. В Google Cloud Console создайте проект и включите **Google Sheets API**.
3. Создайте Service Account и сгенерируйте JSON-ключ.
4. Храните JSON на сервере, например `secrets/google-service-account.json`, с ограниченными правами доступа.
5. Скопируйте поле `client_email` из JSON и предоставьте этому адресу доступ «Редактор» к таблице.
6. Скопируйте ID таблицы из URL `https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit` в `GOOGLE_SHEETS_ID`.
7. Создайте вкладки `Students`, `Lessons`, `Attendance` (или укажите другие названия в `.env`).

Если первая строка вкладки пуста, бот создаст заголовки автоматически.

**Students:** `telegram_id, full_name, group_name, active`

**Lessons:** `lesson_id, subject, group_name, opened_at_utc, closes_at_utc, closed_at_utc, status`

**Attendance:** `record_id, lesson_id, date_utc, subject, telegram_id, full_name, group_name, status, marked_at_utc, updated_at_utc`

SQLite является основным хранилищем. Импортированные студенты, занятия и отметки синхронизируются в Sheets. При временной ошибке задача остаётся в очереди для повтора.

## 3. Импорт списка студентов

Создайте CSV в UTF-8 с заголовками:

```csv
telegram_id,full_name,group_name
123456789,Иванов Иван Иванович,МК-201
987654321,Петрова Анна Сергеевна,МК-201
```

Строки выше — только примеры формата; замените их реальными данными. Узнать Telegram ID можно командой `/id` после запуска бота.

Перед импортом студент должен открыть бота и нажать `/start`: Telegram не разрешает боту первым писать пользователю. Импорт:

```bash
python -m attendance_bot.import_students students.csv
```

Импорт обновляет записи с совпадающим Telegram ID.

## 4. Запуск

```bash
python -m attendance_bot.bot
```

Староста отправляет `/admin`, нажимает «Открыть отметку», вводит предмет и точное название группы. Название группы должно совпадать со столбцом `group_name`.

## 5. Команды и кнопки

**Студенты:** `/start`, `/id`, «Мой профиль», «Мои посещения».

**Староста:** `/admin`, «Открыть отметку», «Текущее занятие», «Закрыть отметку», «Статус таблицы».

## 6. Постоянный запуск на Linux через systemd

Создайте отдельного пользователя Linux `attendance`, разместите проект в `/opt/attendance_bot`, настройте `.env`, JSON-ключ и права на каталог. Создайте `/etc/systemd/system/attendance-bot.service`:

```ini
[Unit]
Description=Telegram Attendance Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=attendance
WorkingDirectory=/opt/attendance_bot
EnvironmentFile=/opt/attendance_bot/.env
ExecStart=/opt/attendance_bot/.venv/bin/python -m attendance_bot.bot
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Затем:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now attendance-bot
sudo systemctl status attendance-bot
journalctl -u attendance-bot -f
```

## Ограничения перед реальным использованием

- Каждый студент обязан заранее нажать `/start`; иначе Telegram не позволит отправить ему личное сообщение.
- Кнопка подтверждает взаимодействие с ботом, но не доказывает физическое присутствие.
- В текущей версии староста закрывает занятие вручную. Если он не закроет занятие, бот не сформирует список отсутствующих автоматически; это можно добавить отдельным планировщиком.
- Данные времени хранятся в UTC, а отображаются в часовом поясе из `.env`.
- Не запускайте несколько экземпляров с одной SQLite-базой.
- Делайте резервные копии `data/attendance.sqlite3`.
- Не публикуйте `.env` и JSON-ключ Google в Git. Ограничьте доступ к таблице.
