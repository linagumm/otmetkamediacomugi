from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

def student_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Расписание"), KeyboardButton(text="Домашка")],
        [KeyboardButton(text="Контрольные"), KeyboardButton(text="Подготовка")],
        [KeyboardButton(text="Мой профиль"), KeyboardButton(text="Мои посещения")],
    ], resize_keyboard=True)

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Открыть отметку", callback_data="admin:create")],
        [InlineKeyboardButton(text="📋 Текущее занятие", callback_data="admin:current")],
        [InlineKeyboardButton(text="🔒 Закрыть отметку", callback_data="admin:close")],
        [InlineKeyboardButton(text="👥 Заявки на регистрацию", callback_data="admin:pending")],
        [InlineKeyboardButton(text="📅 Загрузить расписание", callback_data="admin:setsch")],
        [InlineKeyboardButton(text="📚 Добавить домашку", callback_data="admin:addhw")],
        [InlineKeyboardButton(text="📝 Добавить контрольную", callback_data="admin:addtest")],
        [InlineKeyboardButton(text="📋 Домашки и контрольные", callback_data="admin:events")],
        [InlineKeyboardButton(text="👤 Студенты (удаление)", callback_data="admin:students")],
        [InlineKeyboardButton(text="📥 Выгрузка в CSV", callback_data="admin:export")],
        [InlineKeyboardButton(text="📊 Статус таблицы", callback_data="admin:sync")],
    ])

def deputy_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Открыть отметку", callback_data="admin:create")],
    ])

def attendance_button(lid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Я присутствую", callback_data=f"attend:{lid}")]
    ])

def cancel_keyboard():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)

def reset_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Стереть отметки (студенты останутся)", callback_data="reset:marks")],
        [InlineKeyboardButton(text="⚠️ Стереть всё, включая студентов", callback_data="reset:all")],
        [InlineKeyboardButton(text="Отмена", callback_data="reset:cancel")],
    ])

def approve_keyboard(tid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"reg:ok:{tid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reg:no:{tid}"),
    ]])

def group_keyboard(groups):
    rows = [[KeyboardButton(text=g)] for g in groups[:20]]
    rows.append([KeyboardButton(text="Отмена")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
