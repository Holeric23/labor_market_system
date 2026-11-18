"""Conservative salary baseline. Candidates are evidence, not verified facts.

Only standard-library dependencies; offsets refer to the original text.
This first version deliberately leaves implicit units and ambiguous amounts unresolved.
"""

import re

EXTRACTOR_VERSION = "salary_rules_v2"
NUMBER = r"(?:\d{1,3}(?:[ \u00a0\u202f.]\d{3})+(?!\d)|\d+(?:[.,]\d+)?)"
SCALE = r"(?:т\.(?=р\.)|(?:тысяч\w*|тыс\.?|млн\.?|кк|kk|[кkт]\.?)(?!\w))"
CURRENCY = r"(?:руб(?:л\w*)?\.?|rub\b|rur\b|usd\b|eur\b|доллар\w*|евро\b|₽|\$|€|р\.(?!\w))"
MONEY = re.compile(
    rf"(?<![\w.,])(?:(?P<bound>от|до|не\s+менее|не\s+более)\s+)?"
    rf"(?P<prefix>{CURRENCY})?\s*(?P<n1>{NUMBER})\s*(?P<s1>{SCALE})?\s*(?P<c1>{CURRENCY})?"
    rf"(?:\s*(?:[-–—]|до)\s*(?P<n2>{NUMBER})\s*(?P<s2>{SCALE})?)?"
    rf"\s*(?P<currency>{CURRENCY})?(?!\w)", re.I)
PAY = re.compile(r"зарплат\w*|заработн\w*\s+плат\w*|\bз\s*/?\s*п\b|оклад\w*|оплат\w*|\bдоход(?:а|ов|ы|ом|у|е)?\b|вознагражден\w*|salary|компенсаци\w*", re.I)
NON_PAY = re.compile(r"бюджет\w*|оборот\w*|выручк\w*|чек\w*|стоимость\w*|стоит|цен[ауы]\b|штраф\w*|стоил\w*", re.I)
PERIODS = {
    "month": r"(?:в|за|/)\s*мес(?:яц\w*|\.)?|ежемесяч\w*|per\s+month|/mo\b",
    "hour": r"(?:в|за|/)\s*час\w*|per\s+hour|/h\b",
    "day": r"(?:в|за|/)\s*(?:день|сутки)|per\s+day",
    "shift": r"(?:за|/)\s*смен\w*",
    "year": r"(?:в|за|/)\s*год\w*|per\s+year",
    "project": r"за\s+(?:проект\w*|макет\w*|ролик\w*|статью|текст\w*|задач\w*)",
}


def _amount(number, scale):
    number = re.sub(r"[ \u00a0\u202f]", "", number)
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", number):
        number = number.replace(".", "")
    value = float(number.replace(",", "."))
    if scale:
        value *= 1_000_000 if scale.lower().startswith(("млн", "кк", "kk")) else 1000
    return int(value) if value.is_integer() else value


def _currency(token):
    if not token:
        return None
    token = token.lower()
    if token.startswith(("руб", "rub", "rur", "р.")) or token == "₽":
        return "RUB"
    if token.startswith(("usd", "доллар")) or token == "$":
        return "USD"
    return "EUR"


def extract_salary(text):
    """Return all plausible money mentions, retaining uncertainty and source spans.

    `not_found` means the rules found no candidate, not that salary is absent.
    Candidate values are promoted to scalar fields only for a single unambiguous
    mention. Missing currency/period/tax information remains None.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    # Replacing Markdown markers preserves indices, unlike deleting characters.
    scan = text.replace("*", " ").replace("_", " ")
    mentions = []
    for match in MONEY.finditer(scan):
        start, end = match.span()
        while start < end and scan[start].isspace():
            start += 1
        while end > start and scan[end - 1].isspace():
            end -= 1
        line_start = scan.rfind("\n", 0, start) + 1
        line_end = scan.find("\n", end)
        if line_end < 0:
            line_end = len(scan)
        before = scan[max(line_start, start - 100):start]
        after = scan[end:min(line_end, end + 70)]
        # Nearby negative context wins unless a later explicit pay cue follows it.
        pay_before = list(PAY.finditer(before))
        negatives = list(NON_PAY.finditer(before))
        if negatives and (not pay_before or negatives[-1].start() > pay_before[-1].start()):
            continue
        if re.match(r"\s*%|\s*[/.:]\s*\d", after):
            continue
        cue = bool(pay_before or PAY.search(after[:45]))
        if not cue and not before.strip():
            previous = scan[max(0, line_start - 65):line_start].strip()
            cue = bool(PAY.search(previous) and len(previous) < 60 and not NON_PAY.search(previous))
        s1, s2 = match['s1'], match['s2']
        currency = _currency(match['currency'] or match['c1'] or match['prefix'])
        n1 = _amount(match['n1'], s1 or (s2 if match['n2'] else None))
        n2 = _amount(match['n2'], s2 or s1) if match['n2'] else None
        if not (s1 or s2 or currency) and (not cue or n1 < 100):
            continue
        if not cue and not currency:
            continue
        issues = []
        if not cue:
            issues.append("no_explicit_salary_context")
        if n1 <= 0 or (n2 is not None and n2 <= 0):
            issues.append("nonpositive_amount")
        if n2 is not None and n1 > n2:
            issues.append("reversed_range")
        if not (s1 or s2 or currency) and n1 < 1000:
            issues.append("ambiguous_scale")
        if n2 is not None and not (s1 or s2) and min(n1,n2) < 1000 <= max(n1,n2):
            issues.append("possibly_omitted_multiplier")
        currencies = {_currency(match[key]) for key in ['prefix','c1','currency'] if match[key]}
        if len(currencies) > 1:
            issues.append("conflicting_currency")
        bound = (match['bound'] or '').lower()
        lower, upper = (n1, n2) if n2 is not None else (n1, n1)
        if n2 is None and bound in ("от", "не менее"):
            upper = None
        elif n2 is None and bound in ("до", "не более"):
            lower = None
        # Limit qualifiers to this amount's immediate suffix and pay prefix.
        context = before + ' ' + after
        periods = [key for key, pattern in PERIODS.items() if re.search(pattern, context, re.I)]
        if len(periods) > 1:
            issues.append("conflicting_period")
        net = bool(re.search(r"\bnet\b|на\s+руки|после\s+вычета", context, re.I))
        gross = bool(re.search(r"\bgross\b|до\s+вычета|до\s+уплаты\s+налог", context, re.I))
        if net and gross:
            issues.append("conflicting_tax_mode")
        if re.search(r"бонус\w*|преми\w*|kpi|процент\w*|%|открыти\w*|перспектив\w*", context, re.I):
            issues.append("additional_conditions")
        if re.search(r"раньше|предыдущ\w*|получал\w*|зарабатывал\w*|вывел\w*|выводил\w*", before, re.I):
            issues.append("possibly_historical_amount")
        if re.search(r"от\s*$", before, re.I) and not bound:
            issues.append("unparsed_lower_bound")
        mentions.append({
            "raw": text[start:end], "start": start, "end": end,
            "context": text[line_start:line_end],
            "salary_from": lower, "salary_to": upper, "currency": currency,
            "salary_period": periods[0] if len(periods) == 1 else None,
            "tax_mode": "net" if net and not gross else "gross" if gross and not net else None,
            "issues": issues, "rule_id": "money_with_context_v1",
        })
    result = {"extractor_version": EXTRACTOR_VERSION, "status": "not_found",
              "salary_from": None, "salary_to": None, "currency": None,
              "salary_period": None, "tax_mode": None, "mentions": mentions}
    if len(mentions) == 1 and not mentions[0]['issues']:
        result.update({key: mentions[0][key] for key in ("salary_from", "salary_to", "currency", "salary_period", "tax_mode")})
        result['status'] = 'extracted'
    elif mentions:
        result['status'] = 'needs_review'
    return result
