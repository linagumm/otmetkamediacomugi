import asyncio, csv, sys
from attendance_bot.config import load_settings
from attendance_bot.database import Database

async def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m attendance_bot.import_students students.csv")
    rows = []
    with open(sys.argv[1], newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"telegram_id", "full_name", "group_name"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise SystemExit("CSV headers must be telegram_id,full_name,group_name")
        for line, r in enumerate(reader, start=2):
            try:
                tid = int(r["telegram_id"].strip())
            except (ValueError, AttributeError):
                raise SystemExit(f"Invalid telegram_id on CSV line {line}")
            name, group = (r["full_name"] or "").strip(), (r["group_name"] or "").strip()
            if not name or not group:
                raise SystemExit(f"Missing full_name/group_name on CSV line {line}")
            rows.append((tid, name, group))
    cfg = load_settings()
    db = Database(cfg.database_path)
    await db.init()
    print(f"Imported/updated {await db.import_students(rows)} student records.")

if __name__ == "__main__":
    asyncio.run(main())
