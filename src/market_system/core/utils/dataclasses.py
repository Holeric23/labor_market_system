from dataclasses import dataclass
from datetime import datetime


@dataclass
class RawPublication:
    """Runtime DTO passed from a parser to the importer."""

    source_type_name: str
    source_name: str
    text: str
    url: str
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_type_name, str) or not self.source_type_name.strip():
            raise ValueError("source_type_name must be a non-empty string")

        if not isinstance(self.source_name, str) or not self.source_name.strip():
            raise ValueError("source_name must be a non-empty string")

        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text must be a non-empty string")

        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("url must be a non-empty string")

        if self.published_at is not None and not isinstance(self.published_at, datetime):
            raise TypeError("published_at must be datetime or None")