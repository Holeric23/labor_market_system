from market_system.sqlite import connect as sqlite_connect
from market_system.paths import local_path, DATABASE_PATH
"""Read-only adapter for the actual research schema; no ML inference or imports."""
import asyncio
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from market_system.telegram_bot.domain.models import Post, SearchFilter, utcnow
from market_system.telegram_bot.domain.ports import UnsupportedFilter


def telegram_reference(url: str):
    parsed = urlsplit(url)
    if parsed.hostname not in ("t.me", "telegram.me", "www.t.me"):
        return None, None, None
    parts = parsed.path.strip("/").split("/")
    if parts and parts[0] == "s":
        parts = parts[1:]
    if len(parts) == 3 and parts[0] == "c" and parts[1].isdigit() and parts[2].isdigit():
        return -(10**12 + int(parts[1])), None, int(parts[2]) or None
    if len(parts) == 2 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", parts[0]) and parts[1].isdigit():
        return None, "@" + parts[0], int(parts[1]) or None
    return None, None, None


def parse_date(value):
    if not value:
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # SQLite julianday also interprets dates without a timezone as UTC.
        return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)
    except ValueError:
        return None


class ResearchPostRepository:
    """Select one extraction run and its classifier version; never mix runs.

    Profiles and skills are selected from compatible classifier versions.
    """

    def __init__(self, path: Path, run_id: str | None = None, profile_run_id: str | None = None):
        self.path = local_path(path)
        with self.connect() as db:
            conditions, params = [], []
            if run_id:
                conditions.append("s.run_id=?")
                params.append(run_id)
            if profile_run_id:
                conditions.append("p.run_id=?")
                params.append(profile_run_id)
            query = """SELECT s.run_id,s.model_version,p.run_id FROM skill_extraction_runs s
                JOIN profile_extraction_runs p ON p.model_version=s.model_version"""
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            row = db.execute(query + " ORDER BY p.created_at DESC,s.imported_at DESC,p.run_id DESC,s.run_id DESC LIMIT 1", params).fetchone()
            if row is None:
                raise ValueError("No compatible skills/profile runs; check RESEARCH_DB_PATH / SKILLS_RUN_ID / PROFILES_RUN_ID")
            self.run_id, self.model_version, self.profile_run_id = row
            # Fail at startup if the wrong database/schema was selected.
            db.execute(self._base() + " WHERE 0").fetchall()

    @contextmanager
    def connect(self):
        db = sqlite_connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.create_function("casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _base(columns="p.id,p.url,p.published_at,c.text,c.text_hash,l.final_label,ep.*"):
        return f"""SELECT {columns} FROM publications p
            JOIN contents c ON c.id=p.content_id
            JOIN labeled_contents l ON l.text_hash=c.text_hash
            JOIN extracted_profiles ep ON ep.text_hash=c.text_hash AND ep.model_version=l.model_version
        """

    def _where(self, filter: SearchFilter):
        if filter.relocation is not None:
            raise UnsupportedFilter("В БД нет отдельного результата модели о переезде. Выберите «Переезд: Не важно».")
        clauses = ["l.model_version=?", "ep.run_id=?", "l.final_label=?", "ep.post_type=l.final_label", "trim(c.text)<>''",
                   "(p.url LIKE 'https://%' OR p.url LIKE 'http://%')"]
        values = [self.model_version, self.profile_run_id, filter.post_type.value.lower()]
        if filter.specialty:
            clauses.append("instr(casefold((SELECT group_concat(value,' / ') FROM json_each(ep.occupation_names_json))),?)>0")
            values.append(filter.specialty.casefold())
        if filter.salary_min is not None:
            clauses.append("ep.salary_from>=?")
            values.append(filter.salary_min)
        if filter.work_format != "ANY":
            clauses.append("ep.work_format=?")
            values.append({"REMOTE": "remote", "OFFICE": "onsite", "HYBRID": "hybrid"}[filter.work_format])
        if filter.published_within_days:
            now = utcnow()
            clauses.append("julianday(p.published_at) BETWEEN julianday(?) AND julianday(?)")
            values += [(now - timedelta(days=filter.published_within_days)).isoformat(), now.isoformat()]
        for skill in filter.skills:
            clauses.append("""EXISTS (SELECT 1 FROM extracted_skill_mentions m
                JOIN extracted_skill_dictionary d ON d.run_id=m.run_id AND d.skill_id=m.skill_id
                WHERE m.run_id=? AND m.text_hash=c.text_hash AND casefold(d.name)=?
                AND ((l.final_label='vacancy' AND m.role IN ('required','preferred'))
                  OR (l.final_label='resume' AND m.role='possessed')))""")
            values += [self.run_id, skill]
        return " WHERE " + " AND ".join(clauses), values

    def _post(self, db, row):
        roles = ("required", "preferred") if row["final_label"] == "vacancy" else ("possessed",)
        skills = db.execute(f"""SELECT DISTINCT d.name FROM extracted_skill_mentions m
            JOIN extracted_skill_dictionary d ON d.run_id=m.run_id AND d.skill_id=m.skill_id
            WHERE m.run_id=? AND m.text_hash=? AND m.role IN ({','.join('?' for _ in roles)})
            ORDER BY d.name""", (self.run_id, row["text_hash"], *roles)).fetchall()
        channel, username, message = telegram_reference(row["url"])
        occupations = json.loads(row["occupation_names_json"])
        return Post(id=uuid5(NAMESPACE_URL, row["url"]), post_type=row["final_label"].upper(),
                    tg_channel_id=channel, tg_channel_username=username, tg_message_id=message,
                    tg_post_link=row["url"], raw_text=row["text"], parsed_date=parse_date(row["published_at"]),
                    skills=[r[0] for r in skills], specialty=" / ".join(occupations) or "Не указана",
                    salary_min=row["salary_from"], salary_max=row["salary_to"], salary_currency=row["currency"],
                    salary_period=row["salary_period"], tax_mode=row["tax_mode"],
                    work_format={"remote": "REMOTE", "onsite": "OFFICE", "hybrid": "HYBRID"}.get(row["work_format"], "ANY"),
                    schedule=row["schedule"], employment_type=row["employment_type"],
                    working_hours=row["working_hours"], hours_per_week=row["hours_per_week"])

    async def search_posts(self, filter, limit, offset):
        if not 1 <= limit <= 10 or offset < 0:
            raise ValueError("Invalid pagination")
        where, values = self._where(filter)

        def search():
            with self.connect() as db:
                db.execute("BEGIN")
                rows = db.execute(self._base() + where +
                    " ORDER BY julianday(p.published_at) DESC,p.id DESC LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
                return [self._post(db, row) for row in rows]
        return await asyncio.to_thread(search)

    async def count_posts(self, filter):
        where, values = self._where(filter)

        def count():
            with self.connect() as db:
                return db.execute(self._base("count(*)") + where, values).fetchone()[0]
        return await asyncio.to_thread(count)

    async def get_publication(self, publication_id: int) -> Post | None:
        """Resolve an ML event using exactly the same mapping as interactive search."""
        if type(publication_id) is not int or publication_id <= 0:
            raise ValueError("publication_id must be a positive integer")

        def get():
            with self.connect() as db:
                db.execute("BEGIN")
                row = db.execute(self._base() + """ WHERE p.id=? AND l.model_version=? AND ep.run_id=?
                    AND l.final_label IN ('vacancy','resume') AND ep.post_type=l.final_label""",
                    (publication_id, self.model_version, self.profile_run_id)).fetchone()
                return self._post(db, row) if row else None
        return await asyncio.to_thread(get)
