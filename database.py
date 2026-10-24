import os
from datetime import datetime, timezone, timedelta
import aiosqlite
from contextlib import asynccontextmanager

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS students (
 telegram_id INTEGER PRIMARY KEY, full_name TEXT NOT NULL,
 group_name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 started_bot INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lessons (
 id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL,
 group_name TEXT NOT NULL, opened_at TEXT NOT NULL, closes_at TEXT NOT NULL,
 closed_at TEXT, created_by INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS attendance (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 lesson_id INTEGER NOT NULL REFERENCES lessons(id),
 student_id INTEGER NOT NULL REFERENCES students(telegram_id),
 status TEXT NOT NULL, marked_at TEXT, updated_at TEXT NOT NULL,
 UNIQUE(lesson_id, student_id)
);
CREATE TABLE IF NOT EXISTS schedule_days (
 group_name TEXT NOT NULL, day TEXT NOT NULL, content TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(group_name, day)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, group_name TEXT NOT NULL,
 subject TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', due_date TEXT NOT NULL,
 due_time TEXT NOT NULL DEFAULT '', created_by INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_done (
 event_id INTEGER NOT NULL, student_id INTEGER NOT NULL, PRIMARY KEY(event_id, student_id)
);
CREATE TABLE IF NOT EXISTS reminders_sent (
 event_id INTEGER NOT NULL, offset_days INTEGER NOT NULL, PRIMARY KEY(event_id, offset_days)
);
CREATE TABLE IF NOT EXISTS sync_queue (
 id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL,
 entity_id INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 last_error TEXT, updated_at TEXT NOT NULL,
 UNIQUE(entity_type, entity_id)
);
"""

def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

class Database:
    def __init__(self, path):
        self.path = path

    @asynccontextmanager
    async def connect(self):
        """Open each SQLite connection exactly once and always close it."""
        db = aiosqlite.connect(self.path)
        async with db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            yield db

    async def init(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        async with self.connect() as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def import_students(self, rows):
        async with self.connect() as db:
            for tid, name, group in rows:
                await db.execute("""
                INSERT INTO students(telegram_id,full_name,group_name,created_at)
                VALUES(?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET
                full_name=excluded.full_name, group_name=excluded.group_name, active=1
                """, (tid, name, group, utcnow()))
                await db.execute(
                    "INSERT OR REPLACE INTO sync_queue(entity_type,entity_id,updated_at) VALUES('student',?,?)",
                    (tid, utcnow())
                )
            await db.commit()
        return len(rows)

    async def student(self, tid):
        async with self.connect() as db:
            r = await (await db.execute(
                "SELECT * FROM students WHERE telegram_id=? AND active=1", (tid,)
            )).fetchone()
            return dict(r) if r else None

    async def any_student(self, tid):
        """Студент независимо от статуса (в том числе ожидающий подтверждения)."""
        async with self.connect() as db:
            r = await (await db.execute("SELECT * FROM students WHERE telegram_id=?", (tid,))).fetchone()
            return dict(r) if r else None

    async def groups(self):
        async with self.connect() as db:
            rows = await (await db.execute(
                "SELECT DISTINCT group_name FROM students WHERE active=1 ORDER BY group_name"
            )).fetchall()
            return [r["group_name"] for r in rows]

    async def request_registration(self, tid, name, group):
        """Создаёт заявку (active=0). 'exists' — студент уже подтверждён, 'pending' — заявка ждёт старосту."""
        async with self.connect() as db:
            row = await (await db.execute("SELECT active FROM students WHERE telegram_id=?", (tid,))).fetchone()
            if row and row["active"]:
                return "exists"
            if row:
                await db.execute(
                    "UPDATE students SET full_name=?, group_name=?, started_bot=1 WHERE telegram_id=?",
                    (name, group, tid))
            else:
                await db.execute(
                    "INSERT INTO students(telegram_id,full_name,group_name,active,started_bot,created_at) VALUES(?,?,?,0,1,?)",
                    (tid, name, group, utcnow()))
            await db.commit()
        return "pending"

    async def pending_students(self):
        async with self.connect() as db:
            rows = await (await db.execute("SELECT * FROM students WHERE active=0 ORDER BY created_at")).fetchall()
            return [dict(r) for r in rows]

    async def approve_student(self, tid):
        """Возвращает (студент, была_ли_заявка). Если студента нет — (None, False)."""
        stamp = utcnow()
        async with self.connect() as db:
            row = await (await db.execute("SELECT * FROM students WHERE telegram_id=?", (tid,))).fetchone()
            if not row:
                return None, False
            was_pending = not row["active"]
            if was_pending:
                await db.execute("UPDATE students SET active=1 WHERE telegram_id=?", (tid,))
                await db.execute(
                    "INSERT OR REPLACE INTO sync_queue(entity_type,entity_id,updated_at) VALUES('student',?,?)",
                    (tid, stamp))
                await db.commit()
            return dict(row), was_pending

    async def reject_student(self, tid):
        async with self.connect() as db:
            row = await (await db.execute("SELECT * FROM students WHERE telegram_id=? AND active=0", (tid,))).fetchone()
            if not row:
                return None
            await db.execute("DELETE FROM students WHERE telegram_id=? AND active=0", (tid,))
            await db.commit()
            return dict(row)

    async def mark_started(self, tid):
        async with self.connect() as db:
            await db.execute("UPDATE students SET started_bot=1 WHERE telegram_id=?", (tid,))
            await db.commit()
        return await self.student(tid)

    async def group_students(self, group):
        async with self.connect() as db:
            rows = await (await db.execute(
                "SELECT * FROM students WHERE active=1 AND group_name=? ORDER BY full_name", (group,)
            )).fetchall()
            return [dict(r) for r in rows]

    async def all_students(self):
        async with self.connect() as db:
            rows = await (await db.execute(
                "SELECT * FROM students WHERE active=1 ORDER BY group_name, full_name")).fetchall()
            return [dict(r) for r in rows]

    async def delete_student(self, tid):
        """Удаляет подтверждённого студента вместе с его отметками. Возвращает данные студента или None."""
        async with self.connect() as db:
            row = await (await db.execute("SELECT * FROM students WHERE telegram_id=? AND active=1", (tid,))).fetchone()
            if not row:
                return None
            await db.execute("DELETE FROM sync_queue WHERE entity_type='attendance' AND entity_id IN "
                             "(SELECT id FROM attendance WHERE student_id=?)", (tid,))
            await db.execute("DELETE FROM sync_queue WHERE entity_type='student' AND entity_id=?", (tid,))
            await db.execute("DELETE FROM attendance WHERE student_id=?", (tid,))
            await db.execute("DELETE FROM event_done WHERE student_id=?", (tid,))
            await db.execute("DELETE FROM students WHERE telegram_id=?", (tid,))
            await db.commit()
            return dict(row)

    async def create_lesson(self, subject, group, minutes, admin_id):
        opened = datetime.now(timezone.utc)
        closes = opened + timedelta(minutes=minutes)
        async with self.connect() as db:
            # Do not silently discard an open lesson; require the admin to close it first.
            active = await (await db.execute("SELECT id FROM lessons WHERE status='open' LIMIT 1")).fetchone()
            if active:
                raise ValueError(f"Занятие #{active['id']} ещё открыто. Сначала закрой его.")
            cur = await db.execute(
                "INSERT INTO lessons(subject,group_name,opened_at,closes_at,created_by,status) VALUES(?,?,?,?,?,'open')",
                (subject, group, opened.isoformat(timespec="seconds"), closes.isoformat(timespec="seconds"), admin_id)
            )
            lid = cur.lastrowid
            await db.execute("INSERT OR REPLACE INTO sync_queue(entity_type,entity_id,updated_at) VALUES('lesson',?,?)", (lid, utcnow()))
            await db.commit()
        return lid, opened.isoformat(timespec="seconds"), closes.isoformat(timespec="seconds")

    async def expired_open_lessons(self):
        """Занятия, у которых окно отметки уже истекло, но статус ещё 'open'."""
        async with self.connect() as db:
            rows = await (await db.execute("SELECT * FROM lessons WHERE status='open'")).fetchall()
        now = datetime.now(timezone.utc)
        return [dict(r) for r in rows if datetime.fromisoformat(r["closes_at"]) <= now]

    async def lesson(self, lid):
        async with self.connect() as db:
            r = await (await db.execute("SELECT * FROM lessons WHERE id=?", (lid,))).fetchone()
            return dict(r) if r else None

    async def current_lesson(self):
        async with self.connect() as db:
            r = await (await db.execute("SELECT * FROM lessons WHERE status='open' ORDER BY id DESC LIMIT 1")).fetchone()
            return dict(r) if r else None

    async def mark_attendance(self, lid, tid):
        stamp = utcnow()
        async with self.connect() as db:
            lesson = await (await db.execute("SELECT * FROM lessons WHERE id=?", (lid,))).fetchone()
            student = await (await db.execute("SELECT * FROM students WHERE telegram_id=? AND active=1", (tid,))).fetchone()
            if not lesson or not student:
                return "not_found"
            if lesson["status"] != "open":
                return "closed"
            if datetime.now(timezone.utc) > datetime.fromisoformat(lesson["closes_at"]):
                return "expired"
            if student["group_name"] != lesson["group_name"]:
                return "wrong_group"
            try:
                cur = await db.execute(
                    "INSERT INTO attendance(lesson_id,student_id,status,marked_at,updated_at) VALUES(?,?,'present',?,?)",
                    (lid, tid, stamp, stamp)
                )
            except Exception as exc:
                if "UNIQUE" in str(exc):
                    return "duplicate"
                raise
            aid = cur.lastrowid
            await db.execute("INSERT OR REPLACE INTO sync_queue(entity_type,entity_id,updated_at) VALUES('attendance',?,?)", (aid, stamp))
            await db.commit()
        return "ok"

    async def close_lesson(self, lid):
        stamp = utcnow()
        async with self.connect() as db:
            lesson = await (await db.execute("SELECT * FROM lessons WHERE id=?", (lid,))).fetchone()
            if not lesson:
                return None
            if lesson["status"] == "closed":
                return {"already_closed": True, "added_absent": 0}
            students = await (await db.execute(
                "SELECT telegram_id FROM students WHERE active=1 AND group_name=?", (lesson["group_name"],)
            )).fetchall()
            added = 0
            for s in students:
                cur = await db.execute("""
                INSERT OR IGNORE INTO attendance(lesson_id,student_id,status,marked_at,updated_at)
                VALUES(?,?,'absent',NULL,?)
                """, (lid, s["telegram_id"], stamp))
                if cur.rowcount:
                    added += 1
                    row = await (await db.execute(
                        "SELECT id FROM attendance WHERE lesson_id=? AND student_id=?", (lid, s["telegram_id"])
                    )).fetchone()
                    await db.execute("INSERT OR REPLACE INTO sync_queue(entity_type,entity_id,updated_at) VALUES('attendance',?,?)", (row["id"], stamp))
            await db.execute("UPDATE lessons SET status='closed',closed_at=? WHERE id=?", (stamp, lid))
            await db.execute("INSERT OR REPLACE INTO sync_queue(entity_type,entity_id,updated_at) VALUES('lesson',?,?)", (lid, stamp))
            await db.commit()
        return {"already_closed": False, "added_absent": added}

    async def counts(self, lid):
        async with self.connect() as db:
            rows = await (await db.execute("SELECT status,COUNT(*) n FROM attendance WHERE lesson_id=? GROUP BY status", (lid,))).fetchall()
            return {r["status"]: r["n"] for r in rows}

    async def attendance_rows(self, lid):
        async with self.connect() as db:
            rows = await (await db.execute("""
            SELECT s.full_name,a.status,a.marked_at FROM attendance a
            JOIN students s ON s.telegram_id=a.student_id WHERE a.lesson_id=? ORDER BY s.full_name
            """, (lid,))).fetchall()
            return [dict(r) for r in rows]

    async def export_rows(self):
        async with self.connect() as db:
            rows = await (await db.execute("""
            SELECT l.id AS lesson_id, l.opened_at, l.subject, l.group_name,
                   s.full_name, a.status, a.marked_at
            FROM attendance a
            JOIN lessons l ON l.id=a.lesson_id
            JOIN students s ON s.telegram_id=a.student_id
            ORDER BY l.id, s.full_name
            """)).fetchall()
            return [dict(r) for r in rows]

    async def reset_data(self, wipe_students=False):
        """Стирает занятия и отметки (и, если нужно, студентов). Порядок важен из-за внешних ключей."""
        async with self.connect() as db:
            await db.execute("DELETE FROM attendance")
            await db.execute("DELETE FROM lessons")
            await db.execute("DELETE FROM sync_queue WHERE entity_type IN ('lesson','attendance')")
            names = ["attendance", "lessons"]
            if wipe_students:
                await db.execute("DELETE FROM sync_queue")
                await db.execute("DELETE FROM students")
                for table in ("event_done", "reminders_sent", "events", "schedule_days"):
                    await db.execute(f"DELETE FROM {table}")
                names.append("events")
            # нумерация занятий снова начнётся с 1
            await db.execute("DELETE FROM sqlite_sequence WHERE name IN (%s)" % ",".join("?" * len(names)), names)
            await db.commit()

    # ---------- расписание ----------
    async def schedule_day(self, group, day):
        async with self.connect() as db:
            r = await (await db.execute(
                "SELECT content FROM schedule_days WHERE group_name=? AND day=?", (group, day))).fetchone()
            return r["content"] if r else None

    async def schedule_days(self, group, from_day, limit=14):
        async with self.connect() as db:
            rows = await (await db.execute(
                "SELECT day, content FROM schedule_days WHERE group_name=? AND day>=? ORDER BY day LIMIT ?",
                (group, from_day, limit))).fetchall()
            return [dict(r) for r in rows]

    async def save_schedule(self, group, days):
        """days: {iso_дата: текст}. Пустой текст удаляет расписание на этот день."""
        stamp = utcnow()
        async with self.connect() as db:
            for day, content in days.items():
                if content:
                    await db.execute(
                        "INSERT INTO schedule_days(group_name,day,content,updated_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(group_name,day) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at",
                        (group, day, content, stamp))
                else:
                    await db.execute("DELETE FROM schedule_days WHERE group_name=? AND day=?", (group, day))
            await db.commit()

    # ---------- домашки и контрольные ----------
    async def add_event(self, kind, group, subject, details, due_date, due_time, admin_id):
        async with self.connect() as db:
            cur = await db.execute(
                "INSERT INTO events(kind,group_name,subject,details,due_date,due_time,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (kind, group, subject, details, due_date, due_time, admin_id, utcnow()))
            await db.commit()
            return cur.lastrowid

    async def event(self, eid):
        async with self.connect() as db:
            r = await (await db.execute("SELECT * FROM events WHERE id=?", (eid,))).fetchone()
            return dict(r) if r else None

    async def active_events(self, from_day, group=None, kind=None):
        sql, params = "SELECT * FROM events WHERE due_date>=?", [from_day]
        if group:
            sql += " AND group_name=?"; params.append(group)
        if kind:
            sql += " AND kind=?"; params.append(kind)
        sql += " ORDER BY due_date, due_time, id"
        async with self.connect() as db:
            rows = await (await db.execute(sql, params)).fetchall()
            return [dict(r) for r in rows]

    async def update_event(self, eid, details=None, due_date=None, due_time=""):
        async with self.connect() as db:
            if details is not None:
                await db.execute("UPDATE events SET details=? WHERE id=?", (details, eid))
            if due_date is not None:
                await db.execute("UPDATE events SET due_date=?, due_time=? WHERE id=?", (due_date, due_time, eid))
            await db.commit()

    async def delete_event(self, eid):
        async with self.connect() as db:
            await db.execute("DELETE FROM event_done WHERE event_id=?", (eid,))
            await db.execute("DELETE FROM reminders_sent WHERE event_id=?", (eid,))
            await db.execute("DELETE FROM events WHERE id=?", (eid,))
            await db.commit()

    async def mark_done(self, eid, sid):
        async with self.connect() as db:
            await db.execute("INSERT OR IGNORE INTO event_done(event_id,student_id) VALUES(?,?)", (eid, sid))
            await db.commit()

    async def done_ids(self, sid):
        async with self.connect() as db:
            rows = await (await db.execute("SELECT event_id FROM event_done WHERE student_id=?", (sid,))).fetchall()
            return {r["event_id"] for r in rows}

    async def done_students(self, eid):
        async with self.connect() as db:
            rows = await (await db.execute("SELECT student_id FROM event_done WHERE event_id=?", (eid,))).fetchall()
            return {r["student_id"] for r in rows}

    async def reminder_sent(self, eid, offset):
        async with self.connect() as db:
            r = await (await db.execute(
                "SELECT 1 FROM reminders_sent WHERE event_id=? AND offset_days=?", (eid, offset))).fetchone()
            return bool(r)

    async def mark_reminders(self, eid, offsets):
        async with self.connect() as db:
            for off in offsets:
                await db.execute("INSERT OR IGNORE INTO reminders_sent(event_id,offset_days) VALUES(?,?)", (eid, off))
            await db.commit()

    async def clear_reminders(self, eid):
        async with self.connect() as db:
            await db.execute("DELETE FROM reminders_sent WHERE event_id=?", (eid,))
            await db.commit()

    async def purge_old(self, before_day):
        """Удаляет расписание, домашки и контрольные, дата которых раньше before_day."""
        async with self.connect() as db:
            await db.execute("DELETE FROM schedule_days WHERE day<?", (before_day,))
            for table in ("event_done", "reminders_sent"):
                await db.execute(f"DELETE FROM {table} WHERE event_id IN (SELECT id FROM events WHERE due_date<?)", (before_day,))
            await db.execute("DELETE FROM events WHERE due_date<?", (before_day,))
            await db.commit()

    async def history(self, tid):
        async with self.connect() as db:
            rows = await (await db.execute("""
            SELECT l.subject,l.opened_at,a.status FROM attendance a
            JOIN lessons l ON l.id=a.lesson_id WHERE a.student_id=? ORDER BY l.id DESC LIMIT 10
            """, (tid,))).fetchall()
            return [dict(r) for r in rows]

    async def pending(self):
        async with self.connect() as db:
            rows = await (await db.execute("SELECT * FROM sync_queue ORDER BY id LIMIT 100")).fetchall()
            return [dict(r) for r in rows]

    async def payload(self, kind, eid):
        async with self.connect() as db:
            if kind == "lesson":
                r = await (await db.execute("SELECT * FROM lessons WHERE id=?", (eid,))).fetchone()
            elif kind == "student":
                r = await (await db.execute("SELECT * FROM students WHERE telegram_id=?", (eid,))).fetchone()
            else:
                r = await (await db.execute("""
                SELECT a.*,s.full_name,s.group_name,l.subject,l.opened_at,l.id AS lesson_id
                FROM attendance a JOIN students s ON s.telegram_id=a.student_id
                JOIN lessons l ON l.id=a.lesson_id WHERE a.id=?
                """, (eid,))).fetchone()
            return dict(r) if r else None

    async def sync_done(self, jid):
        async with self.connect() as db:
            await db.execute("DELETE FROM sync_queue WHERE id=?", (jid,))
            await db.commit()

    async def sync_error(self, jid, error):
        async with self.connect() as db:
            await db.execute("UPDATE sync_queue SET attempts=attempts+1,last_error=?,updated_at=? WHERE id=?", (str(error)[:1000], utcnow(), jid))
            await db.commit()
