import asyncio
import csv
import html
import io
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import load_settings
from database import Database
from keyboards import (admin_menu, student_menu, attendance_button, cancel_keyboard,
                       approve_keyboard, group_keyboard, reset_keyboard)
from sheets import SheetsSync

settings = load_settings()
db = Database(settings.database_path)
sheets = SheetsSync(settings.sheets_id, settings.service_account_file, settings.students_tab,
                    settings.lessons_tab, settings.attendance_tab,
                    key_json=settings.service_account_json) if settings.sheets_id else None
bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

class NewLesson(StatesGroup):
    subject = State()
    group = State()

class Register(StatesGroup):
    name = State()
    group = State()

def admin(uid):
    return uid in settings.admin_ids

STATUS_RU = {"present": "присутствовал(а)", "absent": "отсутствовал(а)"}

def esc(value):
    """Экранирует текст для parse_mode=HTML (названия предметов и ФИО могут содержать < > &)."""
    return html.escape(str(value), quote=False)

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
        try:
            jobs = await db.pending()
        except Exception as e:
            print("[sheets] Cannot read sync queue:", e)
            jobs = []
        for job in jobs:
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
                try:
                    await db.sync_error(job["id"], e)
                except Exception:
                    pass
                print(f"[sheets] Job {job['id']} failed; will retry: {e}")
        await asyncio.sleep(settings.sync_interval)

@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    s = await db.mark_started(uid)
    if s:
        await m.answer(f"Привет, {esc(s['full_name'])}! Ты зарегистрирована/зарегистрирован. "
                       f"Когда староста откроет отметку, я пришлю сообщение сюда.",
                       reply_markup=student_menu())
        return
    if await db.any_student(uid):
        await m.answer("Твоя заявка уже отправлена старосте и ждёт подтверждения. Я напишу, когда её одобрят.",
                       reply_markup=student_menu())
        return
    await state.set_state(Register.name)
    await m.answer("Привет! Тебя пока нет в списке группы — давай зарегистрируемся.\n\n"
                   "Введи ФИО полностью (например, Иванов Иван Иванович):",
                   reply_markup=cancel_keyboard())

@dp.message(Register.name)
async def reg_name(m: Message, state: FSMContext):
    if m.text == "Отмена":
        await state.clear()
        await m.answer("Отменено. Чтобы зарегистрироваться, отправь /start.", reply_markup=student_menu())
        return
    name = " ".join((m.text or "").split())
    if len(name) < 3 or len(name) > 120:
        await m.answer("Введи ФИО текстом, от 3 до 120 символов.")
        return
    await state.update_data(name=name)
    await state.set_state(Register.group)
    await m.answer("Теперь введи название своей группы (например, МК-201).",
                   reply_markup=group_keyboard(await db.groups()))

@dp.message(Register.group)
async def reg_group(m: Message, state: FSMContext):
    if m.text == "Отмена":
        await state.clear()
        await m.answer("Отменено. Чтобы зарегистрироваться, отправь /start.", reply_markup=student_menu())
        return
    group = " ".join((m.text or "").split())
    if not group or len(group) > 50:
        await m.answer("Введи название группы текстом, до 50 символов.")
        return
    # если такая группа уже есть, берём её написание (МК-201 и мк-201 — одна группа)
    group = next((g for g in await db.groups() if g.casefold() == group.casefold()), group)
    name = (await state.get_data()).get("name")
    await state.clear()
    if not name:
        await m.answer("Что-то пошло не так. Отправь /start и начни заново.", reply_markup=student_menu())
        return
    uid = m.from_user.id
    result = await db.request_registration(uid, name, group)
    if result == "exists":
        await m.answer("Ты уже зарегистрирована/зарегистрирован.", reply_markup=student_menu())
        return
    if admin(uid):  # заявка старосты подтверждается автоматически
        await db.approve_student(uid)
        await m.answer(f"Готово: {esc(name)}, группа {esc(group)}.", reply_markup=student_menu())
        return
    await m.answer("Заявка отправлена старосте. Я напишу, когда её подтвердят.", reply_markup=student_menu())
    username = f"@{m.from_user.username}" if m.from_user.username else "без username"
    text = (f"<b>Новая заявка на регистрацию</b>\n{esc(name)}\nГруппа: {esc(group)}\n"
            f"Telegram: {esc(username)} · ID <code>{uid}</code>")
    for aid in settings.admin_ids:
        try:
            await bot.send_message(aid, text, reply_markup=approve_keyboard(uid))
        except Exception as e:
            print(f"Cannot notify admin {aid}: {e}")

@dp.callback_query(F.data.startswith("reg:"))
async def reg_decision(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    try:
        _, action, tid = c.data.split(":")
        tid = int(tid)
    except ValueError:
        await c.answer("Некорректная заявка.", show_alert=True); return
    if action == "ok":
        s, was_pending = await db.approve_student(tid)
        if not s or not was_pending:
            await c.answer("Заявка уже обработана или не найдена.", show_alert=True); return
        result_text = f"✅ Подтверждено: {esc(s['full_name'])} · {esc(s['group_name'])}"
        student_text = "Староста подтвердил(а) твою заявку. Теперь ты в списке группы — жди сообщения об отметке."
    else:
        s = await db.reject_student(tid)
        if not s:
            await c.answer("Заявка уже обработана или не найдена.", show_alert=True); return
        result_text = f"❌ Отклонено: {esc(s['full_name'])} · {esc(s['group_name'])}"
        student_text = "Заявку отклонили. Проверь ФИО и группу и отправь /start, чтобы подать заново."
    try:
        await c.message.edit_text(result_text)
    except Exception:
        await c.message.answer(result_text)
    try:
        await bot.send_message(tid, student_text, reply_markup=student_menu())
    except Exception as e:
        print(f"Cannot notify student {tid}: {e}")
    await c.answer("Готово")

@dp.callback_query(F.data == "admin:pending")
async def pending_list(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    rows = await db.pending_students()
    if not rows:
        await c.message.answer("Новых заявок нет.")
    for r in rows[:20]:
        await c.message.answer(
            f"<b>Заявка</b>\n{esc(r['full_name'])}\nГруппа: {esc(r['group_name'])}\nID <code>{r['telegram_id']}</code>",
            reply_markup=approve_keyboard(r["telegram_id"]))
    await c.answer()

@dp.message(Command("id"))
async def get_id(m: Message):
    await m.answer(f"Твой Telegram ID: <code>{m.from_user.id}</code>")

def csv_safe(value):
    """Защита от формул в Excel/Sheets: ФИО вводят сами студенты."""
    value = "" if value is None else str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value

async def send_export(chat_id):
    rows = await db.export_rows()
    if not rows:
        await bot.send_message(chat_id, "Пока нет записей о посещаемости.")
        return
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Занятие №", "Дата и время", "Предмет", "Группа", "ФИО", "Статус", "Время отметки"])
    for r in rows:
        w.writerow([
            r["lesson_id"], display_time(r["opened_at"]), csv_safe(r["subject"]), csv_safe(r["group_name"]),
            csv_safe(r["full_name"]), "был(а)" if r["status"] == "present" else "не был(а)",
            display_time(r["marked_at"]) if r["marked_at"] else "",
        ])
    # utf-8-sig, чтобы Excel правильно открыл кириллицу
    data = buf.getvalue().encode("utf-8-sig")
    name = f"attendance_{datetime.now(ZoneInfo(settings.timezone)).strftime('%Y-%m-%d_%H-%M')}.csv"
    await bot.send_document(chat_id, BufferedInputFile(data, filename=name),
                            caption=f"Выгрузка посещаемости: {len(rows)} записей.")

@dp.message(Command("export"))
async def export_cmd(m: Message):
    if not admin(m.from_user.id):
        await m.answer("Нет прав администратора.")
        return
    await send_export(m.chat.id)

@dp.callback_query(F.data == "admin:export")
async def export_btn(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await send_export(c.message.chat.id)
    await c.answer()

@dp.message(Command("reset"))
async def reset_cmd(m: Message):
    if not admin(m.from_user.id):
        await m.answer("Нет прав администратора.")
        return
    await m.answer("<b>Сброс данных</b>\nПеред стиранием я пришлю CSV-файл с текущими отметками, чтобы копия осталась у тебя. "
                   "Действие необратимо. Что стереть?", reply_markup=reset_keyboard())

@dp.callback_query(F.data.startswith("reset:"))
async def reset_do(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    action = c.data.split(":", 1)[1]
    if action == "cancel":
        await c.message.edit_text("Сброс отменён.")
        await c.answer(); return
    if action not in ("marks", "all"):
        await c.answer("Некорректная команда.", show_alert=True); return
    await send_export(c.message.chat.id)  # резервная копия перед стиранием
    await db.reset_data(wipe_students=(action == "all"))
    text = ("Готово: все занятия и отметки стёрты, студенты остались в списке."
            if action == "marks" else
            "Готово: стёрты занятия, отметки и список студентов. Студентам нужно заново отправить боту /start.")
    await c.message.edit_text(text)
    await c.answer("Сброшено")

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
        await state.clear()
        await m.answer(esc(e), reply_markup=student_menu())
        await m.answer("Панель старосты", reply_markup=admin_menu())
        return
    await state.clear()
    # убираем клавиатуру «Отмена», которая осталась после ввода данных
    await m.answer("Отметка создана.", reply_markup=student_menu())
    sent = failed = 0
    for s in students:
        # Пробуем отправить всем: студент мог нажать /start или /id ещё до импорта CSV,
        # тогда started_bot=0, но личные сообщения ему всё равно доставляются.
        try:
            await bot.send_message(s["telegram_id"],
                f"<b>Открыта отметка посещаемости</b>\nПредмет: {esc(data['subject'])}\nГруппа: {esc(group)}\n"
                f"Закрытие: {display_time(closes)}\n\nНажми кнопку, только если ты присутствуешь на занятии.",
                reply_markup=attendance_button(lid))
            sent += 1
        except Exception as e:
            failed += 1
            print(f"DM failed for {s['telegram_id']}: {e}")
    await m.answer(f"Занятие #{lid} открыто.\nПредмет: {esc(data['subject'])}\nГруппа: {esc(group)}\n"
                   f"Окно: {settings.window_minutes} минут.\nЛичных сообщений отправлено: {sent}; не доставлено (студент не запускал бота): {failed}.",
                   reply_markup=admin_menu())

@dp.callback_query(F.data.startswith("attend:"))
async def attendance(c: CallbackQuery):
    try:
        lid = int(c.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await c.answer("Некорректная отметка.", show_alert=True); return
    if not await db.student(c.from_user.id):
        await c.answer("Тебя нет в списке студентов. Отправь боту /start для регистрации.", show_alert=True); return
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
        await c.message.answer(f"<b>Занятие #{lesson['id']}</b>\n{esc(lesson['subject'])} · {esc(lesson['group_name'])}\n"
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
    lines = [f"{'✓' if r['status']=='present' else '—'} {esc(r['full_name'])}: {STATUS_RU.get(r['status'], r['status'])}" for r in rows]
    summary = ""
    for line in lines:  # обрезаем по строкам, чтобы не разрезать HTML-сущность и уложиться в лимит Telegram
        if len(summary) + len(line) + 1 > 3000:
            summary += "\n…"
            break
        summary += ("\n" if summary else "") + line
    await c.message.answer(f"<b>Занятие #{lesson['id']} закрыто.</b>\nПрисутствовали: {counts.get('present',0)}\n"
                           f"Отсутствовали: {counts.get('absent',0)}\n\n{summary or 'Нет записей'}",
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
        await m.answer("Твой аккаунт не найден в списке группы. Отправь /start, чтобы зарегистрироваться."); return
    await m.answer(f"<b>{esc(s['full_name'])}</b>\nГруппа: {esc(s['group_name'])}\nTelegram ID: <code>{s['telegram_id']}</code>")

@dp.message(F.text == "Мои посещения")
async def history(m: Message):
    if not await db.student(m.from_user.id):
        await m.answer("Твой аккаунт не найден в списке группы. Отправь /start, чтобы зарегистрироваться."); return
    rows = await db.history(m.from_user.id)
    if not rows:
        await m.answer("Пока нет записей о посещениях."); return
    lines = [f"{display_time(r['opened_at'])} — {esc(r['subject'])}: {STATUS_RU.get(r['status'], r['status'])}" for r in rows]
    await m.answer("<b>Последние посещения</b>\n" + "\n".join(lines))

async def main():
    await db.init()
    sync_task = asyncio.create_task(sync_worker()) if sheets else None  # держим ссылку, иначе задачу может собрать GC
    print("Attendance bot started; polling Telegram updates.", flush=True)
    try:
        await bot.delete_webhook()  # если раньше стоял webhook, polling не заработает
        await dp.start_polling(bot)
    finally:
        if sync_task:
            sync_task.cancel()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
