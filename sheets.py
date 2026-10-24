import asyncio
import json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

class SheetsSync:
    def __init__(self, spreadsheet_id, key_file, students_tab, lessons_tab, attendance_tab, key_json=""):
        self.spreadsheet_id = spreadsheet_id
        self.key_file = key_file
        self.key_json = key_json
        self.students_tab = students_tab
        self.lessons_tab = lessons_tab
        self.attendance_tab = attendance_tab
        self._api = None

    def api(self):
        if self._api is None:
            if self.key_json:  # ключ передан переменной окружения — файл на сервере не нужен
                creds = Credentials.from_service_account_info(json.loads(self.key_json), scopes=SCOPES)
            else:
                creds = Credentials.from_service_account_file(self.key_file, scopes=SCOPES)
            self._api = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return self._api

    async def run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    def ensure_headers_sync(self):
        api = self.api()
        headers = {
            self.students_tab: ["telegram_id", "full_name", "group_name", "active"],
            self.lessons_tab: ["lesson_id", "subject", "group_name", "opened_at_utc", "closes_at_utc", "closed_at_utc", "status"],
            self.attendance_tab: ["record_id", "lesson_id", "date_utc", "subject", "telegram_id", "full_name", "group_name", "status", "marked_at_utc", "updated_at_utc"],
        }
        meta = api.spreadsheets().get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties.title").execute()
        existing = {sh["properties"]["title"] for sh in meta.get("sheets", [])}
        missing = [t for t in headers if t not in existing]
        if missing:  # создаём недостающие вкладки, иначе все записи будут падать с ошибкой
            api.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": t}}} for t in missing]},
            ).execute()
        for tab, row in headers.items():
            current = api.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range=f"'{tab}'!A1:Z1").execute().get("values", [])
            if not current:
                api.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id, range=f"'{tab}'!A1",
                    valueInputOption="RAW", body={"values": [row]}
                ).execute()

    async def ensure_headers(self):
        await self.run(self.ensure_headers_sync)

    def upsert_sync(self, tab, key, row):
        api = self.api()
        values = api.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id, range=f"'{tab}'!A:A"
        ).execute().get("values", [])
        target = None
        for i, val in enumerate(values[1:], start=2):
            if val and str(val[0]) == str(key):
                target = i
                break
        if target is None:
            target = max(2, len(values) + 1)
        api.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id, range=f"'{tab}'!A{target}",
            valueInputOption="RAW", body={"values": [row]}
        ).execute()

    async def upsert_lesson(self, p):
        row = [p["id"], p["subject"], p["group_name"], p["opened_at"], p["closes_at"], p.get("closed_at") or "", p["status"]]
        await self.run(self.upsert_sync, self.lessons_tab, p["id"], row)

    async def upsert_attendance(self, p):
        row = [p["id"], p["lesson_id"], p["opened_at"], p["subject"], p["student_id"], p["full_name"], p["group_name"], p["status"], p.get("marked_at") or "", p["updated_at"]]
        await self.run(self.upsert_sync, self.attendance_tab, p["id"], row)

    async def upsert_student(self, p):
        row = [p["telegram_id"], p["full_name"], p["group_name"], p["active"]]
        await self.run(self.upsert_sync, self.students_tab, p["telegram_id"], row)
