from market_system.telegram_bot.application.services import FORMATS
from market_system.telegram_bot.domain.models import SearchFilter


def describe_filter(filter: SearchFilter) -> str:
    skills = ", ".join(filter.skills)
    skills_preview = skills[:900] + ("…" if len(skills) > 900 else "")
    return (
        f"Тип: {'Вакансия' if filter.post_type == 'VACANCY' else 'Резюме'}\n"
        f"Специальность: {filter.specialty or 'Не важно'}\n"
        f"ЗП от: {filter.salary_min if filter.salary_min is not None else 'Не важно'}\n"
        f"Формат: {FORMATS[filter.work_format] if filter.work_format != 'ANY' else 'Не важно'}\n"
        f"Переезд: { {True: 'Да', False: 'Нет', None: 'Не важно'}[filter.relocation]}\n"
        f"Навыки (все): {skills_preview or 'Не важно'}\n"
        f"Дата: {'За ' + str(filter.published_within_days) + ' дней' if filter.published_within_days else 'Не важно'}"
    )


def home():
    return "Поиск вакансий и резюме", [
        [("🔎 Найти вакансию", "type:VACANCY"), ("📄 Найти резюме", "type:RESUME")],
        [("🔔 Мои подписки", "subscriptions")],
    ]


def filter_menu(filter):
    return "Текущие настройки поиска:\n\n" + describe_filter(filter), [
        [("Тип", "edit:post_type"), ("Специальность", "edit:specialty")],
        [("Зарплата", "edit:salary_min"), ("Формат", "edit:work_format")],
        [("Навыки", "edit:skills"), ("Дата", "edit:published_within_days")],
        [("Переезд", "edit:relocation")],
        [("❌ Сбросить", "reset"), ("✅ Применить и найти", "search")],
        [("Главное меню", "home")],
    ]


CHOICES = {
    "post_type": [("Вакансия", "VACANCY"), ("Резюме", "RESUME")],
    "work_format": [("Удалённо", "REMOTE"), ("Офис", "OFFICE"), ("Гибрид", "HYBRID"), ("Не важно", "ANY")],
    "relocation": [("Да", "yes"), ("Нет", "no"), ("Не важно", "any")],
    "published_within_days": [("3 дня", "3"), ("7 дней", "7"), ("30 дней", "30"), ("Не важно", "any")],
}
PROMPTS = {
    "specialty": "Введите специальность (до 160 символов).",
    "salary_min": "Введите минимальную зарплату целым числом. Можно с пробелами.\n"
                  "Суммы сравниваются без пересчёта валют; валюта показана в каждой публикации.",
    "skills": "Введите навыки через запятую (до 20). В публикации должны быть все выбранные навыки.",
}


def editor(field):
    if field in CHOICES:
        return "Выберите значение:", [[(label, f"set:{field}:{value}")] for label, value in CHOICES[field]] + [
            [("Назад", "filter")]]
    return PROMPTS[field], [[("Не важно", f"clear:{field}"), ("Назад", "filter")]]
