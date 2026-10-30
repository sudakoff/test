"""Консольный запуск: python -m app.cli documents --date 2026-03-10

Печатает таблицу по договорам, основания и ответы на четыре вопроса задания.
Использует тот же конвейер, что и веб-интерфейс.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .extractor import read_text_file
from .resolver import analyze_files, answer_field, answer_before


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Проверка условий договоров поставки (TXT).")
    ap.add_argument("path", help="папка с TXT-файлами или отдельные файлы", nargs="+")
    ap.add_argument("--date", default="2026-03-10", help="дата проверки, ГГГГ-ММ-ДД")
    ap.add_argument("--before", default="2026-04-01", help="порог для вопроса «поставка раньше …»")
    ap.add_argument("--json", action="store_true", help="вывести полный JSON результата")
    args = ap.parse_args(argv)

    files = []
    for p in args.path:
        path = Path(p)
        if path.is_dir():
            files += sorted(path.glob("*.txt"))
        elif path.exists():
            files.append(path)
        else:
            print(f"не найдено: {p}", file=sys.stderr)
    if not files:
        print("нет TXT-файлов", file=sys.stderr)
        return 1

    check_date = date.fromisoformat(args.date)
    result = analyze_files([(f.name, read_text_file(f)) for f in files], check_date)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print(f"Дата проверки: {result['check_date_display']}\n")
    print("Документы:")
    for d in result["documents"]:
        refs = ", ".join(d["contract_refs"]) or "—"
        print(f"  {d['filename']}: {d['doc_type_label']}; статус: {d['sign_status_label']}; "
              f"дата: {d['doc_date_display']}; № {d['number'] or '—'}; ссылки на договоры: {refs}")
        for n in d["notes"]:
            print(f"      ! {n}")
    print()

    print(f"{'Договор':<10} {'Поставщик':<32} {'Сумма':>16} {'Срок поставки':>18}  Примечания")
    for row in result["contracts"]:
        a, dd = row["fields"]["amount"], row["fields"]["delivery_date"]
        notes = []
        for f in (a, dd):
            if f["changed_by"]:
                notes.append(f"{f['label']} — изменено: {f['changed_by']['filename']}")
            if f["status"] == "check":
                notes.append(f"{f['label']}: ТРЕБУЕТ ПРОВЕРКИ")
        if row["pending_changes"]:
            notes.append(f"несогласованных изменений: {len(row['pending_changes'])}")
        notes += row["warnings"]
        print(f"{row['number']:<10} {row['supplier']['display'][:32]:<32} {a['display']:>16} {dd['display']:>18}  {'; '.join(notes)}")
    print()

    print("Основания:")
    for row in result["contracts"]:
        for f in row["fields"].values():
            ev = f["evidence"]
            src = f"{ev['filename']}, абзац {ev['paragraph_index'] + 1}: «{ev['quote']}»" if ev else "—"
            print(f"  {row['number']} / {f['label']} = {f['display']}\n      {src}")
        for p in row["pending_changes"]:
            print(f"  {row['number']} / несогласовано: {p['label']} → {p['display']} ({p['doc']['filename']}: {p['reason']})")
    print()

    print("Ответы на вопросы задания:")
    print("1) " + answer_field(result, "STN-101", "delivery_date"))
    print("2) " + answer_field(result, "STN-102", "amount"))
    print("3) " + answer_field(result, "STN-103", "amount"))
    print("4) " + answer_before(result, date.fromisoformat(args.before)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
