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
