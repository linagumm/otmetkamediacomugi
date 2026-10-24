import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: set[int]
    timezone: str
    database_path: str
    sheets_id: str
    service_account_file: str
    service_account_json: str
    students_tab: str
    lessons_tab: str
    attendance_tab: str
    window_minutes: int
    sync_interval: int

def load_settings():
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is missing; copy .env.example to .env and fill it in.")
    admins = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
    if not admins:
        raise RuntimeError("Set ADMIN_IDS to the numeric Telegram ID of the group representative.")
    return Settings(
        bot_token=token,
        admin_ids=admins,
        timezone=os.getenv("TIMEZONE", "Asia/Yekaterinburg"),
        database_path=os.getenv("DATABASE_PATH", "data/attendance.sqlite3"),
        sheets_id=os.getenv("GOOGLE_SHEETS_ID", "").strip(),
        service_account_file=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "secrets/google-service-account.json"),
        service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip(),
        students_tab=os.getenv("SHEETS_STUDENTS_TAB", "Students"),
        lessons_tab=os.getenv("SHEETS_LESSONS_TAB", "Lessons"),
        attendance_tab=os.getenv("SHEETS_ATTENDANCE_TAB", "Attendance"),
        window_minutes=max(1, int(os.getenv("ATTENDANCE_WINDOW_MINUTES", "30"))),
        sync_interval=max(10, int(os.getenv("SHEETS_SYNC_INTERVAL_SECONDS", "30"))),
    )
