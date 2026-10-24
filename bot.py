import asyncio
import csv
import html
import io
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, CallbackQuery, BufferedInputFile,
                           InlineKeyboardMarkup, InlineKeyboardButton)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import load_settings
from database import Database
from keyboards import (admin_menu, student_menu, attendance_button, cancel_keyboard,
                       approve_keyboard, group_keyboard, reset_keyboard, deputy_menu)
from sheets import SheetsSync
from utils import (HW_OFFSETS, TEST_OFFSETS, fmt_day, left_text, parse_schedule, parse_when)

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

def can_open(uid):
    """Открывать отметку может староста или заместитель; всё остальное — только староста."""
    return uid in settings.admin_ids or uid in settings.deputy_ids

def menu_for(uid):
    return admin_menu() if admin(uid) else deputy_menu()

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
    if not can_open(m.from_user.id):
        await m.answer("Нет прав администратора.")
        return
    await m.answer("Панель старосты" if admin(m.from_user.id) else "Панель заместителя",
                   reply_markup=menu_for(m.from_user.id))

@dp.callback_query(F.data == "admin:create")
async def create_start(c: CallbackQuery, state: FSMContext):
    if not can_open(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await state.set_state(NewLesson.subject)
    await c.message.answer("Введи название предмета:", reply_markup=cancel_keyboard())
    await c.answer()

@dp.message(NewLesson.subject)
async def get_subject(m: Message, state: FSMContext):
    if not can_open(m.from_user.id): return
    if m.text == "Отмена":
        await state.clear(); await m.answer("Отменено.", reply_markup=student_menu()); return
    subject = (m.text or "").strip()
    if not subject or len(subject) > 120:
        await m.answer("Введи название от 1 до 120 символов."); return
    await state.update_data(subject=subject)
    await state.set_state(NewLesson.group)
    await m.answer("Введи точное название группы (например, МК-201) или выбери из списка:",
                   reply_markup=group_keyboard(await db.groups()))

@dp.message(NewLesson.group)
async def get_group(m: Message, state: FSMContext):
    if not can_open(m.from_user.id): return
    if m.text == "Отмена":
        await state.clear(); await m.answer("Отменено.", reply_markup=student_menu()); return
    group = (m.text or "").strip()
    students = await db.group_students(group)
    if not students:
        await m.answer("В этой группе нет зарегистрированных студентов. Проверь название группы."); return
    data = await state.get_data()
    for old in await db.expired_open_lessons():  # прошлое занятие с истёкшим окном закрываем автоматически
        await finish_lesson(old, auto=True)
    try:
        lid, opened, closes = await db.create_lesson(data["subject"], group, settings.window_minutes, m.from_user.id)
    except ValueError as e:
        await state.clear()
        await m.answer(esc(e), reply_markup=student_menu())
        await m.answer("Панель", reply_markup=menu_for(m.from_user.id))
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
                   reply_markup=menu_for(m.from_user.id))
    if not admin(m.from_user.id):  # староста должен знать, что заместитель открыл отметку
        who = esc(m.from_user.full_name)
        for aid in settings.admin_ids:
            try:
                await bot.send_message(aid, f"Заместитель {who} открыл занятие #{lid}: {esc(data['subject'])}, {esc(group)}.")
            except Exception as e:
                print(f"Cannot notify admin {aid}: {e}")

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

async def finish_lesson(lesson, auto=False):
    """Закрывает занятие, проставляет отсутствующих и возвращает текст итога."""
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
    title = "закрыто автоматически (время отметки истекло)" if auto else "закрыто"
    return (f"<b>Занятие #{lesson['id']} {title}.</b>\nПрисутствовали: {counts.get('present',0)}\n"
            f"Отсутствовали: {counts.get('absent',0)}\n\n{summary or 'Нет записей'}")

async def auto_close_worker():
    """Раз в 20 секунд закрывает занятия с истёкшим окном и присылает итог старосте."""
    while True:
        try:
            for lesson in await db.expired_open_lessons():
                text = await finish_lesson(lesson, auto=True)
                for aid in settings.admin_ids:
                    try:
                        await bot.send_message(aid, text)
                    except Exception as e:
                        print(f"Cannot send summary to admin {aid}: {e}")
        except Exception as e:
            print("[auto-close] error:", e)
        await asyncio.sleep(20)

@dp.callback_query(F.data == "admin:close")
async def close(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    lesson = await db.current_lesson()
    if not lesson:
        await c.message.answer("Нет открытого занятия."); await c.answer(); return
    await c.message.answer(await finish_lesson(lesson), reply_markup=admin_menu())
    await c.answer("Закрыто")

@dp.callback_query(F.data == "admin:sync")
async def sync_status(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await c.message.answer("Google Sheets синхронизация " + ("включена; очередь обрабатывается в фоне." if sheets else "не настроена. Заполни GOOGLE_SHEETS_ID в .env."))
    await c.answer()

# ---------- удаление студентов ----------

async def list_students_admin(msg):
    students = await db.all_students()
    if not students:
        await msg.answer("Список студентов пуст.")
        return
    by_group = {}
    for s in students:
        by_group.setdefault(s["group_name"], []).append(s)
    for group, items in by_group.items():
        for i in range(0, len(items), 40):  # у Telegram лимит кнопок на сообщение
            chunk = items[i:i + 40]
            rows = [[InlineKeyboardButton(text=f"🗑 {s['full_name'][:40]}", callback_data=f"stdel:{s['telegram_id']}")]
                    for s in chunk]
            await msg.answer(f"<b>Группа {esc(group)}</b>: {len(items)} чел.\nНажми на студента, чтобы удалить его из списка.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.message(Command("students"))
async def students_cmd(m: Message):
    if not admin(m.from_user.id):
        await m.answer("Нет прав администратора."); return
    await list_students_admin(m)

@dp.callback_query(F.data == "admin:students")
async def students_btn(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await list_students_admin(c.message)
    await c.answer()

@dp.callback_query(F.data.startswith("stdel:"))
async def student_delete_ask(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    try:
        tid = int(c.data.split(":")[1])
    except ValueError:
        await c.answer("Некорректная команда.", show_alert=True); return
    s = await db.student(tid)
    if not s:
        await c.answer("Студент уже удалён.", show_alert=True); return
    rows = [[InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"stdelok:{tid}")],
            [InlineKeyboardButton(text="Отмена", callback_data="evcancel")]]
    await c.message.answer(f"Удалить <b>{esc(s['full_name'])}</b> ({esc(s['group_name'])}) из списка?\n"
                           "Его отметки посещаемости тоже будут стёрты, это нельзя отменить.",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()

@dp.callback_query(F.data.startswith("stdelok:"))
async def student_delete_do(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    try:
        tid = int(c.data.split(":")[1])
    except ValueError:
        await c.answer("Некорректная команда.", show_alert=True); return
    s = await db.delete_student(tid)
    if not s:
        await c.answer("Студент уже удалён.", show_alert=True); return
    await finish(c, f"Удалён: {esc(s['full_name'])} ({esc(s['group_name'])}).")

# ===================== РАСПИСАНИЕ, ДОМАШКИ, КОНТРОЛЬНЫЕ =====================

class Sched(StatesGroup):
    group = State()
    text = State()

class NewEvent(StatesGroup):
    group = State()
    subject = State()
    when = State()
    details = State()

class EditEvent(StatesGroup):
    value = State()

SCHEDULE_HELP = (
    "Пришли расписание группы {group} одним сообщением, например:\n\n"
    "24.10 суббота\n09:00 Математика, ауд. 201\n10:45 Физика, ауд. 305\n\n"
    "26.10 понедельник\n09:00 Химия, ауд. 110\n\n"
    "Можно сразу на неделю или месяц. Чтобы поменять один день, пришли только его: он полностью заменит старое. "
    "Дата без пар удалит расписание на этот день."
)

def today_local():
    return datetime.now(ZoneInfo(settings.timezone)).date()

def days_left(ev):
    return (date.fromisoformat(ev["due_date"]) - today_local()).days

def when_text(ev):
    s = fmt_day(ev["due_date"])
    return f"{s} в {ev['due_time']}" if ev["due_time"] else s

def event_text(ev, header):
    left = left_text(days_left(ev))
    if ev["kind"] == "hw":
        return (f"📚 <b>{header}</b>\nПредмет: {esc(ev['subject'])}\nДедлайн: {when_text(ev)} ({left})\n\n"
                f"{esc(ev['details'])}")
    room = f"\nАудитория: {esc(ev['details'])}" if ev["details"] else ""
    return f"📝 <b>{header}</b>\nПредмет: {esc(ev['subject'])}\nКогда: {when_text(ev)} ({left}){room}"

def done_keyboard(ev):
    if ev["kind"] != "hw":
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Сделано", callback_data=f"evdone:{ev['id']}")]])

async def resolve_group(text):
    t = " ".join((text or "").split()).casefold()
    return next((g for g in await db.groups() if g.casefold() == t), None)

async def cancelled(m, state):
    if m.text == "Отмена":
        await state.clear()
        await m.answer("Отменено.", reply_markup=student_menu())
        return True
    return False

async def finish(c, text):
    """Убирает кнопки у сообщения с вопросом и возвращает обычную клавиатуру."""
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await c.message.answer(text, reply_markup=student_menu())
    await c.answer()

async def broadcast(group, text, markup=None, skip=()):
    sent = failed = 0
    for s in await db.group_students(group):
        if s["telegram_id"] in skip:
            continue
        try:
            await bot.send_message(s["telegram_id"], text, reply_markup=markup)
            sent += 1
        except Exception as e:
            failed += 1
            print(f"Broadcast failed for {s['telegram_id']}: {e}")
        await asyncio.sleep(0.05)  # не упираемся в лимиты Telegram
    return sent, failed

# ---------- напоминания и уборка ----------

async def reminder_worker():
    last_cleanup = None
    while True:
        try:
            now = datetime.now(ZoneInfo(settings.timezone))
            today = now.date()
            if last_cleanup != today:
                await db.purge_old((today - timedelta(days=14)).isoformat())
                last_cleanup = today
            if now.hour >= settings.reminder_hour:  # если бот перезапустили позже — напоминание всё равно уйдёт
                for ev in await db.active_events(today.isoformat()):
                    left = days_left(ev)
                    offsets = HW_OFFSETS if ev["kind"] == "hw" else TEST_OFFSETS
                    if left in offsets and not await db.reminder_sent(ev["id"], left):
                        await db.mark_reminders(ev["id"], [left])  # отмечаем до отправки, чтобы не слать дважды
                        header = "Напоминание о домашке" if ev["kind"] == "hw" else "Напоминание о контрольной"
                        done = await db.done_students(ev["id"]) if ev["kind"] == "hw" else set()
                        await broadcast(ev["group_name"], event_text(ev, header), done_keyboard(ev), skip=done)
        except Exception as e:
            print("[reminders] error:", e)
        await asyncio.sleep(60)

# ---------- расписание: староста ----------

async def begin_schedule(msg, state):
    groups = await db.groups()
    if not groups:
        await msg.answer("Пока нет зарегистрированных студентов, расписание добавлять некому.")
        return
    await state.clear()
    if len(groups) == 1:
        await state.update_data(group=groups[0])
        await state.set_state(Sched.text)
        await msg.answer(SCHEDULE_HELP.format(group=esc(groups[0])), reply_markup=cancel_keyboard())
    else:
        await state.set_state(Sched.group)
        await msg.answer("Для какой группы расписание?", reply_markup=group_keyboard(groups))

@dp.message(Command("setschedule"))
async def setschedule_cmd(m: Message, state: FSMContext):
    if not admin(m.from_user.id):
        await m.answer("Нет прав администратора."); return
    await begin_schedule(m, state)

@dp.callback_query(F.data == "admin:setsch")
async def setschedule_btn(c: CallbackQuery, state: FSMContext):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await begin_schedule(c.message, state)
    await c.answer()

@dp.message(Sched.group)
async def sched_group(m: Message, state: FSMContext):
    if not admin(m.from_user.id): return
    if await cancelled(m, state): return
    group = await resolve_group(m.text)
    if not group:
        await m.answer("Выбери группу из списка."); return
    await state.update_data(group=group)
    await state.set_state(Sched.text)
    await m.answer(SCHEDULE_HELP.format(group=esc(group)), reply_markup=cancel_keyboard())

@dp.message(Sched.text)
async def sched_text(m: Message, state: FSMContext):
    if not admin(m.from_user.id): return
    if await cancelled(m, state): return
    days, err = parse_schedule(m.text or "", today_local())
    if err:
        await m.answer(esc(err) + "\n\nИсправь и пришли ещё раз."); return
    group = (await state.get_data())["group"]
    plan = []
    for iso in sorted(days):
        new, old = days[iso], await db.schedule_day(group, iso)
        if old == new or (not new and old is None):
            continue
        status = "new" if old is None else ("removed" if not new else "changed")
        plan.append({"day": iso, "status": status, "old": old, "new": new})
    if not plan:
        await state.clear()
        await m.answer("Ничего не изменилось: такое расписание уже сохранено.", reply_markup=student_menu()); return
    label = {"new": "новое", "changed": "изменено", "removed": "будет удалено"}
    lines = [f"• {fmt_day(p['day'])} — {label[p['status']]}"
             + (f", пар: {len(p['new'].splitlines())}" if p["new"] else "") for p in plan]
    unchanged = len(days) - len(plan)
    if unchanged:
        lines.append(f"Без изменений: {unchanged} дн.")
    today = today_local().isoformat()
    notify = any(p["status"] != "new" and p["day"] >= today for p in plan)
    await state.update_data(plan=plan)
    await state.set_state(None)
    rows = []
    if notify:
        rows.append([InlineKeyboardButton(text="💾 Сохранить и уведомить студентов", callback_data="schsave:1")])
        rows.append([InlineKeyboardButton(text="💾 Сохранить без уведомления", callback_data="schsave:0")])
    else:
        rows.append([InlineKeyboardButton(text="💾 Сохранить", callback_data="schsave:0")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="evcancel")])
    await m.answer(f"<b>Группа {esc(group)}. Вот что я понял:</b>\n" + "\n".join(lines),
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(F.data.startswith("schsave:"))
async def sched_save(c: CallbackQuery, state: FSMContext):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    data = await state.get_data()
    plan, group = data.get("plan"), data.get("group")
    if not plan or not group:
        await c.answer("Нечего сохранять.", show_alert=True); return
    notify = c.data.endswith(":1")
    await state.clear()
    await db.save_schedule(group, {p["day"]: p["new"] for p in plan})
    await finish(c, "Расписание сохранено.")
    if notify:
        today = today_local().isoformat()
        sent = failed = 0
        for p in plan:
            if p["status"] == "new" or p["day"] < today:
                continue
            text = (f"✏️ <b>Изменение в расписании на {fmt_day(p['day'])}</b>\n\n<b>Было:</b>\n{esc(p['old'])}\n\n"
                    f"<b>Стало:</b>\n{esc(p['new']) if p['new'] else 'пар нет'}")
            s_, f_ = await broadcast(group, text)
            sent, failed = sent + s_, failed + f_
        await c.message.answer(f"Уведомления отправлены: {sent}; не доставлено: {failed}.")

# ---------- домашки и контрольные: староста ----------

async def begin_event(msg, state, kind):
    groups = await db.groups()
    if not groups:
        await msg.answer("Пока нет зарегистрированных студентов.")
        return
    await state.clear()
    await state.update_data(kind=kind)
    if len(groups) == 1:
        await state.update_data(group=groups[0])
        await state.set_state(NewEvent.subject)
        await msg.answer("Введи название предмета:", reply_markup=cancel_keyboard())
    else:
        await state.set_state(NewEvent.group)
        await msg.answer("Для какой группы?", reply_markup=group_keyboard(groups))

@dp.message(Command("addhw"))
async def addhw_cmd(m: Message, state: FSMContext):
    if not admin(m.from_user.id):
        await m.answer("Нет прав администратора."); return
    await begin_event(m, state, "hw")

@dp.message(Command("addtest"))
async def addtest_cmd(m: Message, state: FSMContext):
    if not admin(m.from_user.id):
        await m.answer("Нет прав администратора."); return
    await begin_event(m, state, "test")

@dp.callback_query(F.data.in_({"admin:addhw", "admin:addtest"}))
async def addevent_btn(c: CallbackQuery, state: FSMContext):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await begin_event(c.message, state, "hw" if c.data == "admin:addhw" else "test")
    await c.answer()

@dp.message(NewEvent.group)
async def event_group(m: Message, state: FSMContext):
    if not admin(m.from_user.id): return
    if await cancelled(m, state): return
    group = await resolve_group(m.text)
    if not group:
        await m.answer("Выбери группу из списка."); return
    await state.update_data(group=group)
    await state.set_state(NewEvent.subject)
    await m.answer("Введи название предмета:", reply_markup=cancel_keyboard())

@dp.message(NewEvent.subject)
async def event_subject(m: Message, state: FSMContext):
    if not admin(m.from_user.id): return
    if await cancelled(m, state): return
    subject = " ".join((m.text or "").split())
    if not subject or len(subject) > 120:
        await m.answer("Введи название от 1 до 120 символов."); return
    await state.update_data(subject=subject)
    await state.set_state(NewEvent.when)
    kind = (await state.get_data())["kind"]
    await m.answer("Введи дедлайн, например: 30.10" if kind == "hw"
                   else "Введи дату и время контрольной, например: 30.10 10:30")

@dp.message(NewEvent.when)
async def event_when(m: Message, state: FSMContext):
    if not admin(m.from_user.id): return
    if await cancelled(m, state): return
    kind = (await state.get_data())["kind"]
    due_date, due_time, err = parse_when(m.text, kind, today_local())
    if err:
        await m.answer(err); return
    await state.update_data(due_date=due_date, due_time=due_time)
    await state.set_state(NewEvent.details)
    await m.answer("Введи задание (текстом):" if kind == "hw"
                   else "Введи аудиторию (или «-», если пока неизвестна):")

@dp.message(NewEvent.details)
async def event_details(m: Message, state: FSMContext):
    if not admin(m.from_user.id): return
    if await cancelled(m, state): return
    data = await state.get_data()
    kind = data["kind"]
    details = (m.text or "").strip()
    if kind == "test" and details in ("-", "—", "нет"):
        details = ""
    elif not details or len(details) > 1500:
        await m.answer("Введи текст от 1 до 1500 символов."); return
    await state.clear()
    eid = await db.add_event(kind, data["group"], data["subject"], details, data["due_date"], data["due_time"], m.from_user.id)
    ev = await db.event(eid)
    offsets = HW_OFFSETS if kind == "hw" else TEST_OFFSETS
    left = days_left(ev)
    await db.mark_reminders(eid, [o for o in offsets if o >= left])  # эти напоминания уже «покрыты» сообщением о новой записи
    await m.answer("Сохранено. Рассылаю студентам…", reply_markup=student_menu())
    header = "Новая домашка" if kind == "hw" else "Контрольная"
    sent, failed = await broadcast(ev["group_name"], event_text(ev, header), done_keyboard(ev))
    await m.answer(f"Готово. Отправлено: {sent}; не доставлено (студент не запускал бота): {failed}.")

# ---------- домашки и контрольные: список, правка, удаление ----------

async def list_events_admin(msg):
    events = await db.active_events(today_local().isoformat())
    if not events:
        await msg.answer("Активных домашек и контрольных нет.")
        return
    for e in events[:20]:
        header = "Домашка" if e["kind"] == "hw" else "Контрольная"
        rows = [[
            InlineKeyboardButton(text="✏️ Задание" if e["kind"] == "hw" else "✏️ Аудитория", callback_data=f"evedit:details:{e['id']}"),
            InlineKeyboardButton(text="📅 Дата", callback_data=f"evedit:when:{e['id']}"),
        ], [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"evdel:{e['id']}")]]
        await msg.answer(event_text(e, header) + f"\nГруппа: {esc(e['group_name'])}",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.message(Command("events"))
async def events_cmd(m: Message):
    if not admin(m.from_user.id):
        await m.answer("Нет прав администратора."); return
    await list_events_admin(m)

@dp.callback_query(F.data == "admin:events")
async def events_btn(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await list_events_admin(c.message)
    await c.answer()

@dp.callback_query(F.data.startswith("evedit:"))
async def event_edit_start(c: CallbackQuery, state: FSMContext):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    try:
        _, field, eid = c.data.split(":")
        eid = int(eid)
    except ValueError:
        await c.answer("Некорректная команда.", show_alert=True); return
    ev = await db.event(eid)
    if not ev:
        await c.answer("Запись уже удалена.", show_alert=True); return
    await state.clear()
    await state.update_data(event_id=eid, field=field)
    await state.set_state(EditEvent.value)
    if field == "when":
        prompt = ("Введи новый дедлайн, например: 30.10" if ev["kind"] == "hw"
                  else "Введи новую дату и время, например: 30.10 10:30")
    else:
        prompt = ("Введи новый текст задания:" if ev["kind"] == "hw"
                  else "Введи новую аудиторию (или «-», чтобы убрать):")
    await c.message.answer(prompt, reply_markup=cancel_keyboard())
    await c.answer()

@dp.message(EditEvent.value)
async def event_edit_value(m: Message, state: FSMContext):
    if not admin(m.from_user.id): return
    if await cancelled(m, state): return
    data = await state.get_data()
    ev = await db.event(data.get("event_id"))
    if not ev:
        await state.clear()
        await m.answer("Запись уже удалена.", reply_markup=student_menu()); return
    if data["field"] == "when":
        due_date, due_time, err = parse_when(m.text, ev["kind"], today_local())
        if err:
            await m.answer(err); return
        pending = {"due_date": due_date, "due_time": due_time}
        preview = f"Новая дата: {fmt_day(due_date)}" + (f" в {due_time}" if due_time else "")
    else:
        value = (m.text or "").strip()
        if ev["kind"] == "test" and value in ("-", "—", "нет"):
            value = ""
        elif not value or len(value) > 1500:
            await m.answer("Введи текст от 1 до 1500 символов."); return
        pending = {"details": value}
        preview = ("Новый текст: " if ev["kind"] == "hw" else "Новая аудитория: ") + (esc(value) or "не указана")
    await state.update_data(pending=pending)
    await state.set_state(None)
    rows = [[InlineKeyboardButton(text="💾 Сохранить и уведомить студентов", callback_data="evapply:1")],
            [InlineKeyboardButton(text="💾 Сохранить без уведомления", callback_data="evapply:0")],
            [InlineKeyboardButton(text="Отмена", callback_data="evcancel")]]
    await m.answer(f"<b>{esc(ev['subject'])}</b> ({esc(ev['group_name'])})\n{preview}",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(F.data.startswith("evapply:"))
async def event_apply(c: CallbackQuery, state: FSMContext):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    data = await state.get_data()
    eid, pending = data.get("event_id"), data.get("pending")
    if not eid or not pending or not await db.event(eid):
        await c.answer("Нечего сохранять.", show_alert=True); return
    notify = c.data.endswith(":1")
    await state.clear()
    await db.update_event(eid, details=pending.get("details"), due_date=pending.get("due_date"),
                          due_time=pending.get("due_time", ""))
    ev = await db.event(eid)
    if "due_date" in pending:  # дата поменялась — напоминания пересчитываем заново
        await db.clear_reminders(eid)
        offsets = HW_OFFSETS if ev["kind"] == "hw" else TEST_OFFSETS
        left = days_left(ev)
        await db.mark_reminders(eid, [o for o in offsets if o >= left])
    await finish(c, "Изменения сохранены.")
    if notify:
        sent, failed = await broadcast(ev["group_name"], event_text(ev, "Изменение"), done_keyboard(ev))
        await c.message.answer(f"Уведомления отправлены: {sent}; не доставлено: {failed}.")

@dp.callback_query(F.data.startswith("evdel:"))
async def event_delete_ask(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    eid = int(c.data.split(":")[1])
    ev = await db.event(eid)
    if not ev:
        await c.answer("Запись уже удалена.", show_alert=True); return
    rows = [[InlineKeyboardButton(text="🗑 Удалить и уведомить студентов", callback_data=f"evdelok:{eid}:1")],
            [InlineKeyboardButton(text="🗑 Удалить без уведомления", callback_data=f"evdelok:{eid}:0")],
            [InlineKeyboardButton(text="Отмена", callback_data="evcancel")]]
    await c.message.answer(f"Удалить «{esc(ev['subject'])}» ({fmt_day(ev['due_date'])})?",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()

@dp.callback_query(F.data.startswith("evdelok:"))
async def event_delete_do(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    _, eid, flag = c.data.split(":")
    ev = await db.event(int(eid))
    if not ev:
        await c.answer("Запись уже удалена.", show_alert=True); return
    await db.delete_event(ev["id"])
    await finish(c, "Удалено.")
    if flag == "1":
        header = "Домашка отменена" if ev["kind"] == "hw" else "Контрольная отменена"
        sent, failed = await broadcast(ev["group_name"], f"❌ <b>{header}</b>\nПредмет: {esc(ev['subject'])}\n"
                                       f"Было: {when_text(ev)}")
        await c.message.answer(f"Уведомления отправлены: {sent}; не доставлено: {failed}.")

@dp.callback_query(F.data == "evcancel")
async def event_cancel(c: CallbackQuery, state: FSMContext):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await state.clear()
    await finish(c, "Отменено.")

# ---------- студенты: расписание, домашки, контрольные ----------

async def need_student(m):
    s = await db.student(m.from_user.id)
    if not s:
        await m.answer("Твой аккаунт не найден в списке группы. Отправь /start, чтобы зарегистрироваться.")
    return s

async def show_schedule_menu(m):
    s = await need_student(m)
    if not s: return
    days = await db.schedule_days(s["group_name"], today_local().isoformat(), 14)
    if not days:
        await m.answer("Расписание пока не добавлено."); return
    buttons = [InlineKeyboardButton(text=fmt_day(d["day"]), callback_data=f"day:{d['day']}") for d in days]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    await m.answer("Выбери день:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

async def show_schedule_day(m, iso):
    s = await need_student(m)
    if not s: return
    content = await db.schedule_day(s["group_name"], iso)
    if not content:
        await m.answer(f"На {fmt_day(iso)} расписания нет."); return
    await m.answer(f"<b>Расписание на {fmt_day(iso)}</b>\n\n{esc(content)}")

@dp.callback_query(F.data.startswith("day:"))
async def schedule_day_btn(c: CallbackQuery):
    iso = c.data.split(":", 1)[1]
    try:
        date.fromisoformat(iso)
    except ValueError:
        await c.answer("Некорректная дата.", show_alert=True); return
    s = await db.student(c.from_user.id)
    if not s:
        await c.answer("Тебя нет в списке студентов. Отправь боту /start.", show_alert=True); return
    content = await db.schedule_day(s["group_name"], iso)
    await c.message.answer(f"<b>Расписание на {fmt_day(iso)}</b>\n\n{esc(content)}" if content
                           else f"На {fmt_day(iso)} расписания нет.")
    await c.answer()

@dp.message(Command("schedule"))
async def schedule_cmd(m: Message):
    await show_schedule_menu(m)

@dp.message(Command("today"))
async def today_cmd(m: Message):
    await show_schedule_day(m, today_local().isoformat())

@dp.message(Command("tomorrow"))
async def tomorrow_cmd(m: Message):
    await show_schedule_day(m, (today_local() + timedelta(days=1)).isoformat())

async def show_events_student(m, kind, filt=None):
    s = await need_student(m)
    if not s: return
    events = await db.active_events(today_local().isoformat(), group=s["group_name"], kind=kind)
    if filt:
        events = [e for e in events if filt.casefold() in e["subject"].casefold()]
    if not events:
        await m.answer("Подходящих домашек нет." if kind == "hw" else "Ближайших контрольных нет."); return
    done = await db.done_ids(m.from_user.id) if kind == "hw" else set()
    parts, rows = [], []
    for e in events:
        left = left_text(days_left(e))
        if kind == "hw":
            mark = "✅ " if e["id"] in done else ""
            parts.append(f"{mark}<b>{esc(e['subject'])}</b> — до {when_text(e)} ({left})\n{esc(e['details'])}")
            if e["id"] not in done and len(rows) < 10:
                rows.append([InlineKeyboardButton(text=f"✅ Сделано: {e['subject'][:25]}", callback_data=f"evdone:{e['id']}")])
        else:
            room = f"\nАудитория: {esc(e['details'])}" if e["details"] else ""
            parts.append(f"<b>{esc(e['subject'])}</b> — {when_text(e)} ({left}){room}")
    text, shown = "", 0
    for p in parts:
        if len(text) + len(p) + 2 > 3800:
            text += "\n\n…и другие. Уточни предмет: /hw название"
            break
        text += ("\n\n" if text else "") + p
        shown += 1
    await m.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)

@dp.message(Command("hw"))
async def hw_cmd(m: Message, command: CommandObject):
    await show_events_student(m, "hw", (command.args or "").strip() or None)

@dp.message(Command("tests"))
async def tests_cmd(m: Message):
    await show_events_student(m, "test")

@dp.callback_query(F.data.startswith("evdone:"))
async def event_done_btn(c: CallbackQuery):
    s = await db.student(c.from_user.id)
    if not s:
        await c.answer("Тебя нет в списке студентов.", show_alert=True); return
    try:
        eid = int(c.data.split(":")[1])
    except ValueError:
        await c.answer("Некорректная команда.", show_alert=True); return
    ev = await db.event(eid)
    if not ev or ev["group_name"] != s["group_name"] or ev["kind"] != "hw":
        await c.answer("Эта домашка уже недоступна.", show_alert=True); return
    await db.mark_done(eid, c.from_user.id)
    kb = c.message.reply_markup
    rows = [r for r in (kb.inline_keyboard if kb else []) if not any(b.callback_data == c.data for b in r)]
    try:
        await c.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
    except Exception:
        pass
    await c.answer("Отмечено ✅ Напоминаний по этой домашке больше не будет.")

# кнопки нижнего меню (регистрируются после всех состояний, чтобы не перехватывать ввод)
@dp.message(F.text == "Расписание")
async def menu_schedule(m: Message):
    await show_schedule_menu(m)

@dp.message(F.text == "Домашка")
async def menu_hw(m: Message):
    await show_events_student(m, "hw")

@dp.message(F.text == "Контрольные")
async def menu_tests(m: Message):
    await show_events_student(m, "test")

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
    close_task = asyncio.create_task(auto_close_worker())
    remind_task = asyncio.create_task(reminder_worker())
    print("Attendance bot started; polling Telegram updates.", flush=True)
    try:
        await bot.delete_webhook()  # если раньше стоял webhook, polling не заработает
        await dp.start_polling(bot)
    finally:
        close_task.cancel()
        remind_task.cancel()
        if sync_task:
            sync_task.cancel()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
