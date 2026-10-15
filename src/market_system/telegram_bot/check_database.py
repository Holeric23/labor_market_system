from market_system.paths import DATABASE_PATH
"""Read-only connection check; no bot token or Telegram requests required."""
import argparse
import asyncio
import json
from pathlib import Path

from market_system.telegram_bot.domain.models import SearchFilter
from market_system.telegram_bot.infrastructure.research import ResearchPostRepository


async def check(path, run_id=None, skill=None):
    repository = ResearchPostRepository(path, run_id)
    result = {"database": str(repository.path), "skills_run_id": repository.run_id,
              "profiles_run_id": repository.profile_run_id,
              "model_version": repository.model_version, "post_types": {}}
    for kind in ("VACANCY", "RESUME"):
        filter = SearchFilter(post_type=kind, skills=[skill] if skill else [])
        total = await repository.count_posts(filter)
        posts = await repository.search_posts(filter, 3, 0)
        result["post_types"][kind] = {"total": total, "page_size": len(posts),
            "sample_skill_counts": [len(post.skills) for post in posts]}
    print(json.dumps(result, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DATABASE_PATH)
    parser.add_argument("--run-id")
    parser.add_argument("--skill")
    args = parser.parse_args()
    asyncio.run(check(args.db, args.run_id, args.skill))


if __name__ == "__main__":
    from market_system.runtime import enable_guard
    enable_guard()
    main()

