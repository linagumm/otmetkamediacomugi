from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

def student_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Мой профиль")],
        [KeyboardButton(text="Мои посещения")],
    ], resize_keyboard=True)

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Открыть отметку", callback_data="admin:create")],
        [InlineKeyboardButton(text="📋 Текущее занятие", callback_data="admin:current")],
        [InlineKeyboardButton(text="🔒 Закрыть отметку", callback_data="admin:close")],
        [InlineKeyboardButton(text="👥 Заявки на регистрацию", callback_data="admin:pending")],
        [InlineKeyboardButton(text="📊 Статус таблицы", callback_data="admin:sync")],
    ])

def attendance_button(lid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Я присутствую", callback_data=f"attend:{lid}")]
    ])

def cancel_keyboard():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)

def approve_keyboard(tid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"reg:ok:{tid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reg:no:{tid}"),
    ]])

def group_keyboard(groups):
    rows = [[KeyboardButton(text=g)] for g in groups[:20]]
    rows.append([KeyboardButton(text="Отмена")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
