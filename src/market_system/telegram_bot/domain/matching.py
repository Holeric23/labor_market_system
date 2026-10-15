from datetime import datetime, timedelta

from .models import Post, SearchFilter, WorkFormat, utcnow


def matches(post: Post, filter: SearchFilter, now: datetime | None = None) -> bool:
    """Reference semantics shared by search and subscription matching.

    Salary compares the known lower bound, without guessing currency conversions.
    All selected skills must be present; specialty is a case-insensitive substring.
    Unknown values do not satisfy an explicit condition.
    """
    if post.post_type != filter.post_type:
        return False
    if filter.specialty and filter.specialty.casefold() not in post.specialty.casefold():
        return False
    if filter.salary_min is not None and (post.salary_min is None or post.salary_min < filter.salary_min):
        return False
    if filter.work_format != WorkFormat.ANY and post.work_format != filter.work_format:
        return False
    if filter.relocation is not None and post.is_relocation_possible is not filter.relocation:
        return False
    if not set(filter.skills).issubset({s.strip().casefold() for s in post.skills}):
        return False
    if filter.published_within_days is not None:
        if post.parsed_date is None:
            return False
        now = now or utcnow()
        if not now - timedelta(days=filter.published_within_days) <= post.parsed_date <= now:
            return False
    return True
