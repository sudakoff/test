"""Извлечение структурированных данных из текста одного документа.

Подход: правила + регулярные выражения. Никаких предзашитых ответов —
все значения берутся из загруженного текста и сопровождаются ссылкой
на абзац-источник (Evidence).

Основные шаги для одного документа:
  1. Разбить текст на абзацы (с сохранением смещений).
  2. Определить тип документа (договор / допсоглашение / письмо / неизвестно),
     признак «проект» и статус подписания.
  3. Найти собственный номер документа и ссылки на договоры.
  4. Найти дату документа и дату вступления в силу.
  5. Найти поставщика.
  6. Найти значения полей (сумма, срок поставки) — через «скоринг» абзацев:
     ключевые слова дают плюс, слова-«шум» (аванс, неустойка, оплата) — минус.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional

# ---------------------------------------------------------------------------
# Даты
# ---------------------------------------------------------------------------

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_MONTH_ALT = "|".join(MONTHS)

DATE_RE = re.compile(
    rf"(?:«\s*)?(?P<d>\d{{1,2}})(?:\s*»)?\s+(?P<m>{_MONTH_ALT})\s+(?P<y>\d{{4}})(?:\s*(?:г\.|года|г\b))?"
    r"|(?P<d2>\d{2})\.(?P<m2>\d{2})\.(?P<y2>\d{4})",
    re.IGNORECASE,
)

# Дата, перед которой стоит «от» — это ссылка на реквизиты другого документа
# («Договор № STN-101 от 15 января 2026 г.»), а не срок.
_PRECEDED_BY_OT = re.compile(r"\bот\s*$", re.IGNORECASE)
# Ссылка вида «Договор(а/у) поставки № XXX от <дата>» — для определения даты самого документа
_CONTRACT_REF_BEFORE_DATE = re.compile(r"(договор|соглашени|доверенност)\w*(\s+поставки)?\s*№?\s*\S*\s+от\s*$", re.IGNORECASE)


@dataclass
class DateMatch:
    value: date
    start: int
    end: int
    text: str
    after_ot: bool          # дата стоит после «от …» (реквизит другого документа)
    in_contract_ref: bool   # дата внутри «№ … от <дата>»


def find_dates(text: str) -> list[DateMatch]:
    out: list[DateMatch] = []
    for m in DATE_RE.finditer(text):
        try:
            if m.group("d"):
                d = date(int(m.group("y")), MONTHS[m.group("m").lower()], int(m.group("d")))
            else:
                d = date(int(m.group("y2")), int(m.group("m2")), int(m.group("d2")))
        except ValueError:
            continue
        before = text[: m.start()]
        out.append(DateMatch(
            value=d, start=m.start(), end=m.end(), text=m.group(0).strip(),
            after_ot=bool(_PRECEDED_BY_OT.search(before)),
            in_contract_ref=bool(_CONTRACT_REF_BEFORE_DATE.search(before)),
        ))
    return out


# ---------------------------------------------------------------------------
# Суммы
# ---------------------------------------------------------------------------

AMOUNT_RE = re.compile(
    r"(?<![\d,.])(?P<int>\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)"
    r"(?:[.,](?P<frac>\d{2}))?"
    r"\s*(?:\([^)]{0,200}\)\s*)?"               # (прописью)
    r"(?:руб(?:лей|ля|ль|\.)?|₽|RUB)"
    r"(?:\s*(?P<kop>\d{1,2})\s*коп(?:еек|ейки|ейка|\.)?)?",
    re.IGNORECASE,
)


@dataclass
class AmountMatch:
    value: float
    start: int
    end: int
    text: str


def find_amounts(text: str) -> list[AmountMatch]:
    out = []
    for m in AMOUNT_RE.finditer(text):
        whole = int(re.sub(r"\D", "", m.group("int")))
        frac = 0
        if m.group("frac"):
            frac = int(m.group("frac"))
        elif m.group("kop"):
            frac = int(m.group("kop"))
        out.append(AmountMatch(value=whole + frac / 100, start=m.start(), end=m.end(), text=m.group(0).strip()))
    return out


# ---------------------------------------------------------------------------
# Абзацы и свидетельства
# ---------------------------------------------------------------------------

@dataclass
class Paragraph:
    index: int
    text: str
    start: int  # смещение в исходном тексте


_CLAUSE_START = re.compile(r"^\s*(\d+(?:\.\d+)*\.?\s+|«\s*\d+(?:\.\d+)*\.)")


_CONTINUATION = re.compile(r"^[a-zа-яё]")


def split_paragraphs(text: str) -> list[Paragraph]:
    """Абзац = непустая строка. Исключение — «жёсткие переносы»: если строка идёт сразу
    за предыдущей (без пустой строки) и начинается со строчной буквы, она считается
    продолжением предыдущего абзаца. Пункты договора обычно занимают одну строку,
    поэтому подсветка абзаца для пользователя получается точной."""
    paragraphs: list[Paragraph] = []
    pos = 0
    prev_blank = True
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped:
            if paragraphs and not prev_blank and _CONTINUATION.match(stripped):
                paragraphs[-1].text += " " + stripped
            else:
                start = pos + (len(line) - len(line.lstrip()))
                paragraphs.append(Paragraph(index=len(paragraphs), text=stripped, start=start))
            prev_blank = False
        else:
            prev_blank = True
        pos += len(line)
    return paragraphs


@dataclass
class Evidence:
    paragraph_index: int
    quote: str          # точная цитата — текст абзаца
    fragment: str       # ключевой фрагмент внутри абзаца (дата/сумма)
    doc_id: str = ""
    filename: str = ""


@dataclass
class FieldResult:
    """Результат извлечения одного поля из одного документа."""
    status: str                      # ok | no_data | check
    value: Optional[object] = None   # float для суммы, date для срока
    display: str = "нет данных"
    evidence: Optional[Evidence] = None
    note: str = ""                   # пояснение (почему check/no_data)


# ---------------------------------------------------------------------------
# Скоринг абзацев для полей
# ---------------------------------------------------------------------------

# Сумма
AMOUNT_STRONG = re.compile(
    r"(общ\w*\s+)?(сумм\w*|цен\w*|стоимост\w*)\s+(настоящего\s+)?(договора|контракта|соглашения|поставки)"
    r"|(договора|контракта)\s+составля",
    re.IGNORECASE,
)
AMOUNT_WEAK = re.compile(r"сумм|цен[аыуе]\b|стоимост", re.IGNORECASE)
AMOUNT_NEG = re.compile(
    r"аванс|предоплат|неустойк|штраф|\bпен[иья]\b|оплат|платеж|возмещ|возврат|убытк|задолженност|не\s+изменя",
    re.IGNORECASE,
)

# Срок поставки
DELIV_STRONG = re.compile(
    r"срок\w*\s+поставки|срок\w*\s+(передачи|отгрузки)\s+товар"
    r"|поставить\s+[^.;]{0,80}?(в\s+срок|не\s+позднее|до\s+«?\d)"
    r"|поставк\w+\s+[^.;]{0,60}?(осуществля|производ)\w*\s+[^.;]{0,40}?(до\s+«?\d|не\s+позднее)"
    r"|перенести\s+срок|поставить\s+[^.;]{0,80}?\d{1,2}\s+(" + _MONTH_ALT + ")",
    re.IGNORECASE,
)
DELIV_WEAK = re.compile(r"поставк|поставить|отгруз", re.IGNORECASE)
DELIV_NEG = re.compile(
    r"оплат|платеж|расчет|неустойк|штраф|\bпен[иья]\b|гарант|претенз|действует\s+до|срок\w*\s+действия|не\s+изменя",
    re.IGNORECASE,
)
RELATIVE_TERM = re.compile(r"в\s+течение\s+\d+\s*(\([^)]*\)\s*)?(календарн\w*|рабоч\w*|банковск\w*)?\s*дн", re.IGNORECASE)


def _score(text: str, strong: re.Pattern, weak: re.Pattern, neg: re.Pattern) -> int:
    score = 0
    if strong.search(text):
        score += 3
    elif weak.search(text):
        score += 1
    if neg.search(text):
        score -= 2
    return score


def format_amount(value: float) -> str:
    whole = int(value)
    kop = round((value - whole) * 100)
    s = f"{whole:,}".replace(",", " ")
    if kop:
        s += f",{kop:02d}"
    return s + " ₽"


def format_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def extract_amount(paragraphs: list[Paragraph]) -> FieldResult:
    best: Optional[tuple[int, Paragraph, AmountMatch]] = None
    keyword_only: Optional[Paragraph] = None
    for p in paragraphs:
        sc = _score(p.text, AMOUNT_STRONG, AMOUNT_WEAK, AMOUNT_NEG)
        if sc <= 0:
            continue
        amounts = find_amounts(p.text)
        if not amounts:
            # заголовки разделов («2. ЦЕНА ДОГОВОРА …») не считаем основанием
            if keyword_only is None and AMOUNT_STRONG.search(p.text) and p.text.upper() != p.text:
                keyword_only = p
            continue
        # берём первую сумму в абзаце (обычно это и есть цена; аванс и т.п. отсечены скорингом)
        cand = (sc, p, amounts[0])
        if best is None or cand[0] > best[0]:
            best = cand
    if best:
        sc, p, a = best
        return FieldResult(
            status="ok", value=a.value, display=format_amount(a.value),
            evidence=Evidence(p.index, p.text, a.text),
        )
    if keyword_only:
        return FieldResult(
            status="no_data", evidence=Evidence(keyword_only.index, keyword_only.text, ""),
            note="В абзаце о цене договора нет числового значения (например, цена определяется спецификациями).",
        )
    return FieldResult(status="no_data", note="В документе не найден абзац о сумме/цене договора.")


def extract_delivery_date(paragraphs: list[Paragraph]) -> FieldResult:
    best: Optional[tuple[int, Paragraph, DateMatch]] = None
    relative: Optional[Paragraph] = None
    for p in paragraphs:
        sc = _score(p.text, DELIV_STRONG, DELIV_WEAK, DELIV_NEG)
        if sc <= 0:
            continue
        dates = [d for d in find_dates(p.text) if not d.after_ot]
        if not dates:
            if relative is None and DELIV_STRONG.search(p.text) and RELATIVE_TERM.search(p.text):
                relative = p
            continue
        cand = (sc, p, dates[0])
        if best is None or cand[0] > best[0]:
            best = cand
    if best:
        sc, p, d = best
        return FieldResult(
            status="ok", value=d.value, display=format_date(d.value),
            evidence=Evidence(p.index, p.text, d.text),
        )
    if relative:
        return FieldResult(
            status="check", display="требует проверки",
            evidence=Evidence(relative.index, relative.text, RELATIVE_TERM.search(relative.text).group(0)),
            note="Срок задан относительно события (в течение N дней), календарную дату определить автоматически нельзя.",
        )
    return FieldResult(status="no_data", note="В документе не найден абзац со сроком поставки и календарной датой.")


# Реестр полей: чтобы добавить новое поле (например, ставку неустойки),
# достаточно написать функцию extract_<field> и зарегистрировать её здесь.
FIELD_EXTRACTORS = {
    "amount": extract_amount,
    "delivery_date": extract_delivery_date,
}
FIELD_LABELS = {
    "amount": "Сумма договора",
    "delivery_date": "Срок поставки",
}


# ---------------------------------------------------------------------------
# Тип документа, статус, номера, поставщик
# ---------------------------------------------------------------------------

CONTRACT_REF_RE = re.compile(
    r"договор\w*(?:\s+поставки)?\s*№\s*(?P<num>[A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9\-/]*)",
    re.IGNORECASE,
)
AMENDMENT_TITLE_RE = re.compile(
    r"^\s*(дополнительное\s+соглашение|доп\.?\s*соглашение|соглашение\s+(об\s+изменении|о\s+внесении))",
    re.IGNORECASE,
)
AMENDMENT_NUMBER_RE = re.compile(r"соглашение\s*№\s*(?P<num>\S+)", re.IGNORECASE)
CONTRACT_TITLE_RE = re.compile(r"^\s*договор\b", re.IGNORECASE)
DRAFT_RE = re.compile(r"\bПРОЕКТ\b|проект\s+(дополнительного\s+)?соглашения\b|не\s+подписан|на\s+согласовании", re.IGNORECASE)
LETTER_RE = re.compile(r"\bисх\.|уважаем|письм|предлагаем|просим\s+рассмотреть", re.IGNORECASE)
SIGN_NAME_RE = re.compile(r"[/(]\s*[А-ЯЁ][а-яё\-]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.\s*[/)]")
SIGNED_MARK_RE = re.compile(r"подписано\s+сторонами|документ\s+подписан|подписан\s+обеими", re.IGNORECASE)
EFFECT_RE = re.compile(r"вступает\s+в\s+силу", re.IGNORECASE)
SIGNING_MOMENT_RE = re.compile(r"(момента|даты|дня)\s+(его\s+)?подписания", re.IGNORECASE)
ORG_RE = re.compile(r"(?:ООО|АО|ПАО|ЗАО|ОАО|ИП|НАО|ГУП|МУП)\s*[«\"][^»\"]+[»\"]")
SUPPLIER_ROLE_RE = re.compile(r"именуем\w*\s+(в\s+дальнейшем\s+)?[«\"]Поставщик[»\"]", re.IGNORECASE)
SUPPLIER_LINE_RE = re.compile(r"^Поставщик\s*:\s*(?P<org>(?:ООО|АО|ПАО|ЗАО|ОАО|ИП|НАО|ГУП|МУП)\s*[«\"][^»\"]+[»\"])", re.IGNORECASE)

TYPE_LABELS = {
    "contract": "Договор",
    "amendment": "Дополнительное соглашение",
    "letter": "Письмо",
    "unknown": "Не распознан",
}
STATUS_LABELS = {
    "signed": "подписан сторонами",
    "draft": "проект (не подписан)",
    "unsigned": "подписи не обнаружены",
    "n/a": "—",
}


def normalize_number(num: str) -> str:
    return num.strip().strip(",.;:»«\"'").upper()


@dataclass
class DocInfo:
    doc_id: str
    filename: str
    text: str
    paragraphs: list[Paragraph]
    doc_type: str                   # contract | amendment | letter | unknown
    is_draft: bool
    sign_status: str                # signed | draft | unsigned | n/a
    sign_evidence: Optional[Evidence]
    number: Optional[str]           # собственный номер документа
    contract_refs: list[str]        # номера договоров, на которые ссылается документ
    doc_date: Optional[date]
    doc_date_evidence: Optional[Evidence]
    effective_date: Optional[date]  # для допсоглашений
    effective_evidence: Optional[Evidence]
    effective_note: str
    supplier: FieldResult
    fields: dict[str, FieldResult]
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        def fr(f: FieldResult) -> dict:
            d = asdict(f)
            if isinstance(f.value, date):
                d["value"] = f.value.isoformat()
            return d
        return {
            "doc_id": self.doc_id,
            "filename": self.filename,
            "paragraphs": [p.text for p in self.paragraphs],
            "doc_type": self.doc_type,
            "doc_type_label": TYPE_LABELS[self.doc_type],
            "is_draft": self.is_draft,
            "sign_status": self.sign_status,
            "sign_status_label": STATUS_LABELS[self.sign_status],
            "sign_evidence": asdict(self.sign_evidence) if self.sign_evidence else None,
            "number": self.number,
            "contract_refs": self.contract_refs,
            "doc_date": self.doc_date.isoformat() if self.doc_date else None,
            "doc_date_display": format_date(self.doc_date) if self.doc_date else "нет данных",
            "doc_date_evidence": asdict(self.doc_date_evidence) if self.doc_date_evidence else None,
            "effective_date": self.effective_date.isoformat() if self.effective_date else None,
            "effective_date_display": format_date(self.effective_date) if self.effective_date else "не определена",
            "effective_evidence": asdict(self.effective_evidence) if self.effective_evidence else None,
            "effective_note": self.effective_note,
            "supplier": fr(self.supplier),
            "fields": {k: fr(v) for k, v in self.fields.items()},
            "notes": self.notes,
        }


def _head(paragraphs: list[Paragraph], n: int = 6) -> list[Paragraph]:
    return paragraphs[:n]


def detect_type(paragraphs: list[Paragraph], text: str) -> tuple[str, bool]:
    head = _head(paragraphs)
    head_text = "\n".join(p.text for p in head)
    is_draft = bool(DRAFT_RE.search(head_text)) or bool(re.search(r"не\s+подписан", text, re.IGNORECASE))
    for p in head[:4]:
        if AMENDMENT_TITLE_RE.search(p.text):
            return "amendment", is_draft
    for p in head[:3]:
        if CONTRACT_TITLE_RE.search(p.text):
            return "contract", is_draft
    if LETTER_RE.search(text):
        return "letter", is_draft
    return "unknown", is_draft


def detect_signatures(paragraphs: list[Paragraph], is_draft: bool) -> tuple[str, Optional[Evidence]]:
    """signed — найдены подписи с расшифровкой ФИО обеих сторон (или явная отметка);
    draft — пометка «проект»/«не подписан»; unsigned — подписей не найдено."""
    if is_draft:
        for p in paragraphs:
            m = DRAFT_RE.search(p.text)
            if m:
                return "draft", Evidence(p.index, p.text, m.group(0))
        return "draft", None
    names: list[tuple[Paragraph, str]] = []
    for p in paragraphs:
        for m in SIGN_NAME_RE.finditer(p.text):
            names.append((p, m.group(0)))
        m2 = SIGNED_MARK_RE.search(p.text)
        if m2:
            return "signed", Evidence(p.index, p.text, m2.group(0))
    if len(names) >= 2:
        p, frag = names[-1]
        return "signed", Evidence(p.index, p.text, frag)
    return "unsigned", None


def detect_numbers(paragraphs: list[Paragraph], doc_type: str) -> tuple[Optional[str], list[str]]:
    own: Optional[str] = None
    refs: list[str] = []
    head = _head(paragraphs)
    if doc_type == "contract":
        for p in head[:3]:
            m = CONTRACT_REF_RE.search(p.text)
            if m:
                own = normalize_number(m.group("num"))
                break
    elif doc_type == "amendment":
        for p in head[:4]:
            m = AMENDMENT_NUMBER_RE.search(p.text)
            if m:
                own = normalize_number(m.group("num"))
                break
    for p in paragraphs:
        for m in CONTRACT_REF_RE.finditer(p.text):
            num = normalize_number(m.group("num"))
            if num != own and num not in refs:
                refs.append(num)
    return own, refs


_OUTGOING_RE = re.compile(r"^(исх\.?|№)\s*", re.IGNORECASE)


def _preamble(paragraphs: list[Paragraph], limit: int = 8) -> list[Paragraph]:
    """Абзацы «шапки» — до первого нумерованного пункта."""
    out = []
    for p in paragraphs[:limit]:
        if out and _CLAUSE_START.match(p.text):
            break
        out.append(p)
    return out


def detect_doc_date(paragraphs: list[Paragraph]) -> tuple[Optional[date], Optional[Evidence]]:
    """Дата документа ищется только в «шапке» (до первого нумерованного пункта):
       1) строка исходящего номера «Исх. № … от <дата>»;
       2) короткая строка «г. Город <дата>» (дата в конце строки);
       3) иначе — первая дата шапки, не являющаяся реквизитом другого документа."""
    pre = _preamble(paragraphs)
    for p in pre:
        if _OUTGOING_RE.match(p.text):
            ds = [d for d in find_dates(p.text) if not d.in_contract_ref]
            if ds:
                return ds[0].value, Evidence(p.index, p.text, ds[0].text)
    for p in pre:
        if len(p.text) <= 80:
            ds = [d for d in find_dates(p.text) if not d.in_contract_ref]
            if ds and p.text.endswith(ds[-1].text):
                return ds[-1].value, Evidence(p.index, p.text, ds[-1].text)
    for p in pre:
        for d in find_dates(p.text):
            if d.in_contract_ref:
                continue
            return d.value, Evidence(p.index, p.text, d.text)
    return None, None


def detect_effective_date(paragraphs: list[Paragraph], doc_date: Optional[date]) -> tuple[Optional[date], Optional[Evidence], str]:
    for p in paragraphs:
        if not EFFECT_RE.search(p.text):
            continue
        dates = [d for d in find_dates(p.text) if not d.in_contract_ref]
        if dates:
            return dates[0].value, Evidence(p.index, p.text, dates[0].text), "дата указана в документе"
        if SIGNING_MOMENT_RE.search(p.text):
            if doc_date:
                return doc_date, Evidence(p.index, p.text, SIGNING_MOMENT_RE.search(p.text).group(0)), "с момента подписания — принята дата документа"
            return None, Evidence(p.index, p.text, SIGNING_MOMENT_RE.search(p.text).group(0)), "с момента подписания, но дата документа не найдена"
        return None, Evidence(p.index, p.text, ""), "условие о вступлении в силу не распознано"
    if doc_date:
        return doc_date, None, "условие о вступлении в силу отсутствует — принята дата документа"
    return None, None, "дата документа и условие о вступлении в силу не найдены"


def extract_supplier(paragraphs: list[Paragraph]) -> FieldResult:
    for p in paragraphs:
        m = SUPPLIER_ROLE_RE.search(p.text)
        if m:
            orgs = list(ORG_RE.finditer(p.text[: m.start()]))
            if orgs:
                org = orgs[-1].group(0)
                return FieldResult(status="ok", value=org, display=org, evidence=Evidence(p.index, p.text, org))
    for p in paragraphs:
        m = SUPPLIER_LINE_RE.search(p.text)
        if m:
            org = m.group("org")
            return FieldResult(status="ok", value=org, display=org, evidence=Evidence(p.index, p.text, org))
    return FieldResult(status="no_data", note="Не найдена сторона, именуемая «Поставщик».")


def analyze_document(doc_id: str, filename: str, text: str) -> DocInfo:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    paragraphs = split_paragraphs(text)
    doc_type, is_draft = detect_type(paragraphs, text)
    if doc_type == "letter":
        sign_status, sign_ev = "n/a", None
    else:
        sign_status, sign_ev = detect_signatures(paragraphs, is_draft)
    number, refs = detect_numbers(paragraphs, doc_type)
    doc_date, doc_date_ev = detect_doc_date(paragraphs)
    if doc_type == "amendment":
        eff, eff_ev, eff_note = detect_effective_date(paragraphs, doc_date)
    else:
        eff, eff_ev, eff_note = None, None, ""
    supplier = extract_supplier(paragraphs)
    fields = {name: fn(paragraphs) for name, fn in FIELD_EXTRACTORS.items()}

    notes: list[str] = []
    if doc_type == "unknown":
        notes.append("Тип документа не распознан: не найден заголовок «Договор», «Дополнительное соглашение» и признаки письма.")
    if doc_type == "contract" and not number:
        notes.append("Не найден номер договора в заголовке.")
    if doc_type in ("amendment", "letter") and not refs:
        notes.append("Документ не ссылается ни на один договор (№ не найден) — связать с договором нельзя.")
    if doc_type == "amendment" and sign_status == "unsigned":
        notes.append("Подписи с расшифровкой не обнаружены — статус подписания требует проверки.")

    for ev in (sign_ev, doc_date_ev, eff_ev, supplier.evidence, *(f.evidence for f in fields.values())):
        if ev:
            ev.doc_id, ev.filename = doc_id, filename

    return DocInfo(
        doc_id=doc_id, filename=filename, text=text, paragraphs=paragraphs,
        doc_type=doc_type, is_draft=is_draft, sign_status=sign_status, sign_evidence=sign_ev,
        number=number, contract_refs=refs, doc_date=doc_date, doc_date_evidence=doc_date_ev,
        effective_date=eff, effective_evidence=eff_ev, effective_note=eff_note,
        supplier=supplier, fields=fields, notes=notes,
    )


def read_text_file(path) -> str:
    """Чтение TXT с подбором кодировки (UTF-8 / UTF-8 BOM / Windows-1251)."""
    with open(path, "rb") as fh:
        return decode_bytes(fh.read())


def decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")
