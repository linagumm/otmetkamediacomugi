import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from attendance_bot.config import load_settings
from attendance_bot.database import Database
from attendance_bot.keyboards import admin_menu, student_menu, attendance_button, cancel_keyboard
from attendance_bot.sheets import SheetsSync

settings = load_settings()
db = Database(settings.database_path)
sheets = SheetsSync(settings.sheets_id, settings.service_account_file, settings.students_tab,
                    settings.lessons_tab, settings.attendance_tab) if settings.sheets_id else None
bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

class NewLesson(StatesGroup):
    subject = State()
    group = State()

def admin(uid):
    return uid in settings.admin_ids

def display_time(value):
    return datetime.fromisoformat(value).astimezone(ZoneInfo(settings.timezone)).strftime("%d.%m.%Y %H:%M")

async def sync_worker():
    if not sheets:
        return
    try:
        await sheets.ensure_headers()
    except Exception as e:
        print("[sheets] Header setup failed:", e)
    while True:
        for job in await db.pending():
            try:
                p = await db.payload(job["entity_type"], job["entity_id"])
                if p:
                    if job["entity_type"] == "lesson":
                        await sheets.upsert_lesson(p)
                    elif job["entity_type"] == "student":
                        await sheets.upsert_student(p)
                    else:
                        await sheets.upsert_attendance(p)
                await db.sync_done(job["id"])
            except Exception as e:
                await db.sync_error(job["id"], e)
                print(f"[sheets] Job {job['id']} failed; will retry: {e}")
        await asyncio.sleep(settings.sync_interval)

@dp.message(CommandStart())
async def start(m: Message):
    s = await db.mark_started(m.from_user.id)
    if not s:
        msg = "Твой Telegram ID пока не найден в списке группы. Попроси старосту добавить тебя в CSV, затем отправь /start ещё раз."
    else:
        msg = f"Привет, {s['full_name']}! Ты зарегистрирована/зарегистрирован. Когда староста откроет отметку, я пришлю сообщение сюда."
    if admin(m.from_user.id):
        msg += "\n\nПанель старосты: /admin"
    await m.answer(msg, reply_markup=student_menu())

@dp.message(Command("id"))
async def get_id(m: Message):
    await m.answer(f"Твой Telegram ID: <code>{m.from_user.id}</code>")

@dp.message(Command("admin"))
async def admin_panel(m: Message):
    if not admin(m.from_user.id):
        await m.answer("Нет прав администратора.")
        return
    await m.answer("Панель старосты", reply_markup=admin_menu())

@dp.callback_query(F.data == "admin:create")
async def create_start(c: CallbackQuery, state: FSMContext):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await state.set_state(NewLesson.subject)
    await c.message.answer("Введи название предмета:", reply_markup=cancel_keyboard())
    await c.answer()

@dp.message(NewLesson.subject)
async def get_subject(m: Message, state: FSMContext):
    if not admin(m.from_user.id): return
    if m.text == "Отмена":
        await state.clear(); await m.answer("Отменено.", reply_markup=student_menu()); return
    subject = (m.text or "").strip()
    if not subject or len(subject) > 120:
        await m.answer("Введи название от 1 до 120 символов."); return
    await state.update_data(subject=subject)
    await state.set_state(NewLesson.group)
    await m.answer("Введи точное название группы, как в CSV (например, МК-201):")

@dp.message(NewLesson.group)
async def get_group(m: Message, state: FSMContext):
    if not admin(m.from_user.id): return
    if m.text == "Отмена":
        await state.clear(); await m.answer("Отменено.", reply_markup=student_menu()); return
    group = (m.text or "").strip()
    students = await db.group_students(group)
    if not students:
        await m.answer("В этой группе нет импортированных студентов. Проверь group_name в CSV."); return
    data = await state.get_data()
    try:
        lid, opened, closes = await db.create_lesson(data["subject"], group, settings.window_minutes, m.from_user.id)
    except ValueError as e:
        await m.answer(str(e), reply_markup=admin_menu()); await state.clear(); return
    await state.clear()
    sent = failed = 0
    for s in students:
        if not s["started_bot"]:
            failed += 1
            continue
        try:
            await bot.send_message(s["telegram_id"],
                f"<b>Открыта отметка посещаемости</b>\nПредмет: {data['subject']}\nГруппа: {group}\n"
                f"Закрытие: {display_time(closes)}\n\nНажми кнопку, только если ты присутствуешь на занятии.",
                reply_markup=attendance_button(lid))
            sent += 1
        except Exception as e:
            failed += 1
            print(f"DM failed for {s['telegram_id']}: {e}")
    await m.answer(f"Занятие #{lid} открыто.\nПредмет: {data['subject']}\nГруппа: {group}\n"
                   f"Окно: {settings.window_minutes} минут.\nЛичных сообщений отправлено: {sent}; не доставлено или не запущен бот: {failed}.",
                   reply_markup=admin_menu())

@dp.callback_query(F.data.startswith("attend:"))
async def attendance(c: CallbackQuery):
    try:
        lid = int(c.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await c.answer("Некорректная отметка.", show_alert=True); return
    if not await db.student(c.from_user.id):
        await c.answer("Тебя нет в списке студентов. Обратись к старосте.", show_alert=True); return
    result = await db.mark_attendance(lid, c.from_user.id)
    text = {
        "ok": "Посещение отмечено.",
        "duplicate": "Ты уже отметилась/отметился.",
        "closed": "Отметка закрыта.",
        "expired": "Время отметки истекло.",
        "wrong_group": "Эта отметка для другой группы.",
        "not_found": "Занятие или профиль не найден.",
    }.get(result, "Не удалось отметить посещение.")
    await c.answer(text, show_alert=True)

@dp.callback_query(F.data == "admin:current")
async def current(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    lesson = await db.current_lesson()
    if not lesson:
        await c.message.answer("Нет открытого занятия.")
    else:
        counts = await db.counts(lesson["id"])
        await c.message.answer(f"<b>Занятие #{lesson['id']}</b>\n{lesson['subject']} · {lesson['group_name']}\n"
                               f"Закрытие: {display_time(lesson['closes_at'])}\nОтметились: {counts.get('present', 0)}")
    await c.answer()

@dp.callback_query(F.data == "admin:close")
async def close(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    lesson = await db.current_lesson()
    if not lesson:
        await c.message.answer("Нет открытого занятия."); await c.answer(); return
    await db.close_lesson(lesson["id"])
    counts = await db.counts(lesson["id"])
    rows = await db.attendance_rows(lesson["id"])
    summary = "\n".join(f"{'✓' if r['status']=='present' else '—'} {r['full_name']}: {r['status']}" for r in rows)
    await c.message.answer(f"<b>Занятие #{lesson['id']} закрыто.</b>\nПрисутствовали: {counts.get('present',0)}\n"
                           f"Отсутствовали: {counts.get('absent',0)}\n\n{summary[:3000] or 'Нет записей'}",
                           reply_markup=admin_menu())
    await c.answer("Закрыто")

@dp.callback_query(F.data == "admin:sync")
async def sync_status(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await c.message.answer("Google Sheets синхронизация " + ("включена; очередь обрабатывается в фоне." if sheets else "не настроена. Заполни GOOGLE_SHEETS_ID в .env."))
    await c.answer()

@dp.message(F.text == "Мой профиль")
async def profile(m: Message):
    s = await db.student(m.from_user.id)
    if not s:
        await m.answer("Твой аккаунт не найден в списке группы."); return
    await m.answer(f"<b>{s['full_name']}</b>\nГруппа: {s['group_name']}\nTelegram ID: <code>{s['telegram_id']}</code>")

@dp.message(F.text == "Мои посещения")
async def history(m: Message):
    if not await db.student(m.from_user.id):
        await m.answer("Твой аккаунт не найден в списке группы."); return
    rows = await db.history(m.from_user.id)
    if not rows:
        await m.answer("Пока нет записей о посещениях."); return
    lines = [f"{display_time(r['opened_at'])} — {r['subject']}: {r['status']}" for r in rows]
    await m.answer("<b>Последние посещения</b>\n" + "\n".join(lines))

async def main():
    await db.init()
    if sheets:
        asyncio.create_task(sync_worker())
    print("Attendance bot started.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
