"""Чистые функции без зависимостей от aiogram: разбор дат и расписания, форматирование."""
import difflib
import random
import re
from datetime import date, timedelta

WEEKDAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WEEKDAY_WORDS = {
    "пн", "вт", "ср", "чт", "пт", "сб", "вс", "пон", "вто", "сре", "чет", "пят", "суб", "вос",
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
}
HW_OFFSETS = (7, 3, 2, 1)     # за сколько дней напоминать о домашке
TEST_OFFSETS = (7, 1, 0)      # за сколько дней напоминать о контрольной (0 — утром в день контрольной)

DATE_RE = re.compile(r"^\s*(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?![\d:])(.*)$")
WHEN_RE = re.compile(r"^\s*(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?:\s+(\d{1,2})[:.](\d{2}))?\s*$")


def make_date(d, m, y, today):
    try:
        if y:
            y = int(y)
            if y < 100:
                y += 2000
            return date(y, m, d)
        res = date(today.year, m, d)
        if res < today - timedelta(days=180):  # «01.02» в декабре — это следующий год
            res = date(today.year + 1, m, d)
        return res
    except ValueError:
        return None


def parse_schedule(text, today):
    """Возвращает ({iso_дата: 'строки занятий'}, None) или (None, текст_ошибки)."""
    days = {}
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = DATE_RE.match(line)
        if m:
            rest = re.sub(r"[^\w]", "", m.group(4).casefold())
            # строка-заголовок: дата и, возможно, день недели. «11.10 Физика» — это уже пара, а не дата
            if rest == "" or rest in WEEKDAY_WORDS:
                d = make_date(int(m.group(1)), int(m.group(2)), m.group(3), today)
                if d is None:
                    return None, f"Не понял дату: «{line[:30]}»"
                iso = d.isoformat()
                if iso in days:
                    return None, f"Дата {d:%d.%m} указана дважды."
                days[iso] = []
                current = iso
                continue
        if current is None:
            return None, "Сначала укажи дату, например: 24.10 суббота"
        days[current].append(line)
    if not days:
        return None, "Не нашёл ни одной даты. Начни с даты, например: 24.10 суббота"
    if len(days) > 62:
        return None, "Слишком много дней за раз (максимум 62)."
    out = {}
    for iso, lines in days.items():
        content = "\n".join(lines)
        if len(content) > 1500:
            return None, f"Слишком длинное расписание на {date.fromisoformat(iso):%d.%m}."
        out[iso] = content
    return out, None


def parse_when(text, kind, today):
    """kind: 'hw' — только дата, 'test' — дата и время. Возвращает (iso_дата, 'ЧЧ:ММ' или '', ошибка)."""
    example = "30.10" if kind == "hw" else "30.10 10:30"
    m = WHEN_RE.match(text or "")
    if not m:
        return None, None, f"Не понял. Пример: {example}"
    d = make_date(int(m.group(1)), int(m.group(2)), m.group(3), today)
    if d is None:
        return None, None, "Такой даты не существует."
    if d < today:
        return None, None, "Эта дата уже прошла."
    time_s = ""
    if kind == "test":
        if m.group(4) is None:
            return None, None, "Для контрольной нужно и время, например: 30.10 10:30"
        h, mi = int(m.group(4)), int(m.group(5))
        if h > 23 or mi > 59:
            return None, None, "Такого времени не бывает."
        time_s = f"{h:02d}:{mi:02d}"
    return d.isoformat(), time_s, None


def fmt_day(iso):
    d = date.fromisoformat(iso)
    return f"{WEEKDAY_SHORT[d.weekday()]}, {d:%d.%m}"


def left_text(days):
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "завтра"
    return f"через {days} дн."


# ---------- подготовка к контрольным (карточки) ----------
LETTERS = "АБВГ"
CARD_DASH_RE = re.compile(r"^(.+?)\s+[—–-]\s+(.+)$")
CARD_SEP_RE = re.compile(r"^(.+?)\s*[=;\t]\s*(.+)$")


def parse_cards(text):
    """Строки «вопрос — ответ» -> ([(вопрос, ответ)], [непонятные строки])."""
    cards, bad = [], []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = CARD_DASH_RE.match(line) or CARD_SEP_RE.match(line)
        if not m:
            bad.append(line)
            continue
        q, a = m.group(1).strip(), m.group(2).strip()
        if not q or not a or len(q) > 300 or len(a) > 300:
            bad.append(line)
            continue
        cards.append((q, a))
    return cards, bad


def normalize(s):
    s = (s or "").casefold().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def is_correct(user, correct):
    """Возвращает (верно, засчитано_с_опечаткой)."""
    a, b = normalize(user), normalize(correct)
    if not a:
        return False, False
    if a == b:
        return True, False
    if any(ch.isdigit() for ch in a + b):  # с числами и датами опечатки не прощаем
        return False, False
    if len(a) >= 5 and len(b) >= 5 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.88:
        return True, True
    return False, False


def make_options(cards, cid, rng=random):
    """4 варианта ответа (правильный + до 3 чужих ответов из набора). None, если не из чего выбирать."""
    card = next(c for c in cards if c["id"] == cid)
    correct = card["answer"]
    seen, pool = {normalize(correct)}, []
    for c in cards:
        if c["id"] == cid:
            continue
        n = normalize(c["answer"])
        if n in seen:
            continue
        seen.add(n)
        pool.append(c["answer"])
    if not pool:
        return None
    opts = rng.sample(pool, min(3, len(pool))) + [correct]
    rng.shuffle(opts)
    return opts, opts.index(correct)


def pct(ok, total):
    return round(100 * ok / total) if total else 0
