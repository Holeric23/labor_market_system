from datetime import datetime, timezone
from enum import Enum
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PostType(str, Enum):
    VACANCY = "VACANCY"
    RESUME = "RESUME"


class WorkFormat(str, Enum):
    REMOTE = "REMOTE"
    OFFICE = "OFFICE"
    HYBRID = "HYBRID"
    ANY = "ANY"


class ExperienceGrade(str, Enum):
    JUNIOR = "JUNIOR"
    MIDDLE = "MIDDLE"
    SENIOR = "SENIOR"
    ANY = "ANY"


class Role(str, Enum):
    ADMIN = "ADMIN"
    USER = "USER"


class State(str, Enum):
    IDLE = "STATE_IDLE"
    SEARCH_SETUP = "STATE_SEARCH_SETUP"
    VIEWING_RESULTS = "STATE_VIEWING_RESULTS"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


Money = Annotated[int, Field(strict=True, ge=0, le=10**12)]
Amount = Money | Annotated[float, Field(strict=True, ge=0, le=10**12, allow_inf_nan=False)]
ShortText = Annotated[str, Field(max_length=160)]


class SearchFilter(Model):
    post_type: PostType
    specialty: ShortText | None = None
    salary_min: Money | None = None
    work_format: WorkFormat = WorkFormat.ANY
    relocation: bool | None = Field(default=None, strict=True)
    skills: list[ShortText] = Field(default_factory=list, max_length=20)
    published_within_days: int | None = None
    limit: int = Field(default=3, strict=True, ge=1, le=10)
    offset: int = Field(default=0, strict=True, ge=0)

    @field_validator("specialty")
    @classmethod
    def clean_specialty(cls, value):
        return (value.strip() or None) if value is not None else None

    @field_validator("skills")
    @classmethod
    def clean_skills(cls, values):
        return list(dict.fromkeys(v.strip().casefold() for v in values if v.strip()))

    @field_validator("published_within_days", mode="before")
    @classmethod
    def valid_days(cls, value):
        if value is not None and (type(value) is not int or value not in (3, 7, 30)):
            raise ValueError("Допустимо: 3, 7, 30 дней или null")
        return value

    def subscription_payload(self) -> dict:
        return self.model_dump(mode="json", exclude={"limit", "offset"})


class Post(Model):
    id: UUID = Field(default_factory=uuid4)
    post_type: PostType
    # Missing Telegram metadata is allowed for non-Telegram sources: use fallback.
    tg_channel_id: int | None = Field(default=None, strict=True, lt=0)
    tg_channel_username: str | None = Field(default=None, pattern=r"^@[A-Za-z][A-Za-z0-9_]{3,31}$")
    tg_message_id: int | None = Field(default=None, strict=True, gt=0)
    tg_post_link: str = Field(max_length=2048)
    raw_text: str = Field(min_length=1, max_length=100_000)
    parsed_date: datetime | None
    specialty: str = Field(default="Не указана", max_length=2000)
    salary_min: Amount | None = None
    salary_max: Amount | None = None
    salary_currency: str | None = Field(default=None, max_length=12)
    salary_period: ShortText | None = None
    tax_mode: ShortText | None = None
    work_format: WorkFormat = WorkFormat.ANY
    # None means ML did not determine relocation; it must not become "no".
    is_relocation_possible: bool | None = Field(default=None, strict=True)
    skills: list[ShortText] = Field(default_factory=list, max_length=100)
    grade: ExperienceGrade | None = None
    domain: ShortText | None = None
    employment_type: ShortText | None = None
    schedule: ShortText | None = None
    working_hours: ShortText | None = None
    hours_per_week: int | None = Field(default=None, ge=0, le=168)

    @field_validator("tg_post_link")
    @classmethod
    def safe_link(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username:
            raise ValueError("Нужна ссылка http(s) на оригинал")
        return value

    @field_validator("parsed_date")
    @classmethod
    def aware_date(cls, value):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Дата должна содержать часовой пояс")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def salary_range(self):
        if self.salary_min is not None and self.salary_max is not None and self.salary_min > self.salary_max:
            raise ValueError("salary_min > salary_max")
        return self


class User(Model):
    user_id: int = Field(strict=True, gt=0)
    role: Role = Role.USER
    created_at: datetime = Field(default_factory=utcnow)
    current_state: State = State.IDLE
    last_menu_message_id: int | None = None

    @field_validator("role", mode="before")
    @classmethod
    def legacy_guest(cls, value):
        return "USER" if value == "GUEST" else value


class Subscription(Model):
    id: UUID = Field(default_factory=uuid4)
    user_id: int
    # Explicit payload excludes session pagination fields.
    filter: dict
    created_at: datetime = Field(default_factory=utcnow)
    is_active: bool = True

    @field_validator("filter")
    @classmethod
    def valid_filter(cls, value):
        return SearchFilter.model_validate(value).subscription_payload()


class PostCreated(Model):
    event_id: UUID
    post: Post


class PublicationCreated(Model):
    event_id: UUID
    publication_id: int = Field(strict=True, gt=0)
