"""Сведение документов в таблицу по договорам на дату проверки.

Правило приоритета (из задания):
  * условия договора изменяет ТОЛЬКО допсоглашение, которое
      (а) подписано сторонами и (б) вступило в силу не позднее даты проверки;
  * письма поставщика и неподписанные проекты — не меняют условия,
    показываются отдельно как «несогласованные изменения / сведения для проверки»;
  * имя файла и порядок загрузки на приоритет не влияют — используются
    только даты из текста документов.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Optional

from .extractor import (
    DocInfo, FieldResult, Evidence, FIELD_EXTRACTORS, FIELD_LABELS,
    format_date, analyze_document, decode_bytes,
)


def _ev(ev: Optional[Evidence]) -> Optional[dict]:
    return asdict(ev) if ev else None


def _value_json(v):
    return v.isoformat() if isinstance(v, date) else v


def _doc_ref(doc: DocInfo) -> dict:
    return {
        "doc_id": doc.doc_id,
        "filename": doc.filename,
        "doc_type": doc.doc_type,
        "number": doc.number,
        "doc_date": doc.doc_date.isoformat() if doc.doc_date else None,
        "doc_date_display": format_date(doc.doc_date) if doc.doc_date else "нет данных",
        "sign_status": doc.sign_status,
    }


def classify_amendment(doc: DocInfo, check_date: date) -> tuple[str, str]:
    """Возвращает (verdict, reason):
       applicable — меняет условия;
       pending    — не меняет (проект / не вступило / письмо);
       check      — нельзя надёжно решить, нужна ручная проверка."""
    if doc.doc_type == "letter":
        return "pending", "Письмо поставщика — предложение, не согласовано сторонами"
    if doc.is_draft or doc.sign_status == "draft":
        return "pending", "Проект допсоглашения — не подписан сторонами"
    if doc.sign_status == "unsigned":
        return "check", "Допсоглашение без распознанных подписей — статус требует проверки"
    # signed
    if doc.effective_date is None:
        return "check", f"Подписано, но дата вступления в силу не определена ({doc.effective_note})"
    if doc.effective_date > check_date:
        return "pending", f"Подписано, но вступает в силу {format_date(doc.effective_date)} — после даты проверки"
    return "applicable", f"Подписано, вступило в силу {format_date(doc.effective_date)} ({doc.effective_note})"


def resolve(docs: list[DocInfo], check_date: date) -> dict:
    contracts: dict[str, DocInfo] = {}
    duplicates: dict[str, list[DocInfo]] = {}
    for d in docs:
        if d.doc_type == "contract" and d.number:
            if d.number in contracts:
                duplicates.setdefault(d.number, []).append(d)
            else:
                contracts[d.number] = d

    # связи: номер договора -> документы, ссылающиеся на него
    related: dict[str, list[DocInfo]] = {num: [] for num in contracts}
    unlinked: list[DocInfo] = []
    for d in docs:
        if d.doc_type == "contract":
            continue
        if not d.contract_refs:
            unlinked.append(d)
            continue
        for num in d.contract_refs:
            related.setdefault(num, []).append(d)

    rows = []
    for num in sorted(related.keys()):
        contract = contracts.get(num)
        rel = related[num]
        row: dict = {
            "number": num,
            "contract_doc": _doc_ref(contract) if contract else None,
            "supplier": None,
            "fields": {},
            "pending_changes": [],
            "related_docs": [_doc_ref(d) | {"verdict": None, "reason": ""} for d in rel],
            "warnings": [],
        }
        if contract is None:
            row["warnings"].append("Текст договора не загружен — есть только ссылающиеся на него документы.")
            row["supplier"] = {"status": "no_data", "display": "нет данных", "value": None, "evidence": None, "note": ""}
        else:
            s = contract.supplier
            row["supplier"] = {"status": s.status, "display": s.display, "value": s.value, "evidence": _ev(s.evidence), "note": s.note}
        if num in duplicates:
            row["warnings"].append(
                "Загружено несколько договоров с этим номером: " +
                ", ".join(d.filename for d in [contracts[num], *duplicates[num]]) + " — требует проверки."
            )

        # классификация связанных документов
        verdicts = []
        for d, ref in zip(rel, row["related_docs"]):
            verdict, reason = classify_amendment(d, check_date)
            ref["verdict"], ref["reason"] = verdict, reason
            verdicts.append((verdict, reason, d))

        applicable = sorted(
            [(d.effective_date, d.doc_date or d.effective_date, d, reason) for v, reason, d in verdicts if v == "applicable"],
            key=lambda t: (t[0], t[1]),
        )

        for fname in FIELD_EXTRACTORS:
            base: FieldResult = contract.fields[fname] if contract else FieldResult(status="no_data", note="Договор не загружен.")
            cur = {
                "field": fname,
                "label": FIELD_LABELS[fname],
                "status": base.status,
                "value": _value_json(base.value),
                "display": base.display,
                "evidence": _ev(base.evidence),
                "note": base.note,
                "changed_by": None,
                "original": None,
                "needs_check": [],
            }
            # применяем вступившие в силу допсоглашения по порядку
            last_eff = None
            for eff, _dd, d, reason in applicable:
                f = d.fields[fname]
                if f.status != "ok":
                    continue
                if cur["changed_by"] and last_eff == eff:
                    cur["needs_check"].append(
                        f"Два вступивших в силу допсоглашения с одной датой меняют поле: {cur['changed_by']['filename']} и {d.filename}."
                    )
                if cur["original"] is None:
                    cur["original"] = {"display": cur["display"], "status": cur["status"], "evidence": cur["evidence"]}
                cur.update(status="ok", value=_value_json(f.value), display=f.display, evidence=_ev(f.evidence), note="")
                cur["changed_by"] = _doc_ref(d) | {"reason": reason}
                last_eff = eff

            # документы, которые не меняют условия, но содержат значение этого поля
            for verdict, reason, d in verdicts:
                if verdict == "applicable":
                    continue
                f = d.fields[fname]
                if f.status == "no_data":
                    continue
                item = {
                    "field": fname, "label": FIELD_LABELS[fname],
                    "verdict": verdict, "reason": reason,
                    "doc": _doc_ref(d),
                    "display": f.display, "value": _value_json(f.value), "evidence": _ev(f.evidence),
                }
                row["pending_changes"].append(item)
                if verdict == "check":
                    cur["needs_check"].append(f"{d.filename}: {reason}; в документе указано «{f.display}».")
            if cur["needs_check"] and cur["status"] == "ok":
                cur["status"] = "check"
            row["fields"][fname] = cur
        rows.append(row)

    return {
        "check_date": check_date.isoformat(),
        "check_date_display": format_date(check_date),
        "contracts": rows,
        "documents": [d.to_json() for d in docs],
        "unlinked_documents": [_doc_ref(d) | {"notes": d.notes} for d in unlinked],
        "field_labels": FIELD_LABELS,
    }


def analyze_files(files: list[tuple[str, bytes | str]], check_date: date) -> dict:
    docs = []
    for i, (name, content) in enumerate(files):
        text = decode_bytes(content) if isinstance(content, bytes) else content
        docs.append(analyze_document(f"doc{i}", name, text))
    return resolve(docs, check_date)


# ---------------------------------------------------------------------------
# Ответы на типовые вопросы (используются CLI и тестами; фронтенд считает то же самое на клиенте)
# ---------------------------------------------------------------------------

def answer_field(result: dict, number: str, fname: str) -> str:
    row = next((r for r in result["contracts"] if r["number"] == number.upper()), None)
    if not row:
        return f"Договор {number}: нет данных (документ не загружен)."
    f = row["fields"][fname]
    parts = [f"{f['label']} по договору {number} на {result['check_date_display']}: {f['display']}."]
    if f["evidence"]:
        parts.append(f"Основание: {f['evidence']['filename']}, абзац {f['evidence']['paragraph_index'] + 1}: «{f['evidence']['quote']}»")
    if f["changed_by"]:
        parts.append(f"Изменено документом {f['changed_by']['filename']} ({f['changed_by']['reason']}); было: {f['original']['display']}.")
    if f["note"]:
        parts.append(f["note"])
    for w in f["needs_check"]:
        parts.append("Требует проверки: " + w)
    pend = [p for p in row["pending_changes"] if p["field"] == fname]
    for p in pend:
        parts.append(f"Несогласованное изменение: {p['doc']['filename']} — {p['display']} ({p['reason']}).")
    return "\n".join(parts)


def answer_before(result: dict, before: date) -> str:
    lines = [f"Договоры с плановой поставкой раньше {format_date(before)} (по состоянию на {result['check_date_display']}):"]
    found = False
    for row in result["contracts"]:
        f = row["fields"]["delivery_date"]
        if f["value"] and date.fromisoformat(f["value"]) < before:
            found = True
            line = f"  • {row['number']}: {f['display']}"
            if f["changed_by"]:
                line += f" (изменено: {f['changed_by']['filename']})"
            pend = [p for p in row["pending_changes"] if p["field"] == "delivery_date"]
            if pend:
                line += "; несогласованные изменения срока: " + "; ".join(
                    f"{p['doc']['filename']} → {p['display']} ({p['reason']})" for p in pend)
            else:
                line += "; несогласованных изменений срока нет"
            if f["needs_check"]:
                line += "; ТРЕБУЕТ ПРОВЕРКИ"
            lines.append(line)
        elif f["status"] != "ok":
            lines.append(f"  • {row['number']}: срок — {f['display']} (в выборку не включён, проверьте вручную)")
    if not found:
        lines.append("  (нет таких договоров)")
    return "\n".join(lines)
