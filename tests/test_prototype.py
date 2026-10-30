"""Проверки: корректность данных, обработка отсутствующих значений, учёт статуса документа.

Запуск:  python -m unittest discover -s tests -v
"""
import unittest
from datetime import date
from pathlib import Path

from app.extractor import analyze_document, find_amounts, find_dates, read_text_file, decode_bytes
from app.resolver import analyze_files, resolve, answer_before

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "documents"
CHECK = date(2026, 3, 10)


def load_samples():
    return [(p.name, read_text_file(p)) for p in sorted(DOCS.glob("*.txt"))]


def row(result, number):
    return next(r for r in result["contracts"] if r["number"] == number)


class PrimitiveExtractors(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(find_amounts("составляет 4 850 000 (четыре миллиона) рублей 00 копеек")[0].value, 4850000)
        self.assertEqual(find_amounts("1 250 000,50 руб.")[0].value, 1250000.5)
        self.assertEqual(find_amounts("980000 рублей 75 копеек")[0].value, 980000.75)
        self.assertEqual(find_amounts("в размере 0,1% от стоимости"), [])  # процент — не сумма

    def test_dates(self):
        ds = find_dates("в срок до 25 марта 2026 года и 01.04.2026")
        self.assertEqual([d.value for d in ds], [date(2026, 3, 25), date(2026, 4, 1)])
        ref = find_dates("Договор № STN-1 от 15 января 2026 г.")[0]
        self.assertTrue(ref.after_ot and ref.in_contract_ref)

    def test_cp1251(self):
        self.assertEqual(decode_bytes("Договор".encode("cp1251")), "Договор")


class SampleDocuments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = analyze_files(load_samples(), CHECK)

    def test_three_contracts(self):
        self.assertEqual([r["number"] for r in self.result["contracts"]], ["STN-101", "STN-102", "STN-103"])

    def test_q1_term_stn101_changed_by_signed_amendment(self):
        f = row(self.result, "STN-101")["fields"]["delivery_date"]
        self.assertEqual(f["value"], "2026-04-20")
        self.assertEqual(f["status"], "ok")
        self.assertEqual(f["changed_by"]["filename"], "dop_soglashenie_1.txt")
        self.assertEqual(f["original"]["display"], "25.03.2026")
        self.assertIn("20 апреля 2026", f["evidence"]["quote"])

    def test_q2_amount_stn102(self):
        f = row(self.result, "STN-102")["fields"]["amount"]
        self.assertEqual(f["value"], 2300000)
        self.assertEqual(f["evidence"]["filename"], "dogovor_STN-102.txt")
        self.assertIsNone(f["changed_by"])

    def test_q3_amount_stn103_no_data_and_draft_not_applied(self):
        r = row(self.result, "STN-103")
        self.assertEqual(r["fields"]["amount"]["status"], "no_data")
        self.assertEqual(r["fields"]["amount"]["display"], "нет данных")
        pend = [p for p in r["pending_changes"] if p["field"] == "amount"]
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["display"], "6 400 000 ₽")
        self.assertEqual(pend[0]["verdict"], "pending")

    def test_q4_before_april(self):
        text = answer_before(self.result, date(2026, 4, 1))
        self.assertIn("STN-102", text)
        self.assertIn("STN-103", text)
        self.assertNotIn("• STN-101", text)
        self.assertIn("pismo_postavshchika.txt", text)
        self.assertIn("proekt_izmeneniya.txt", text)

    def test_letter_is_not_applied(self):
        f = row(self.result, "STN-102")["fields"]["delivery_date"]
        self.assertEqual(f["value"], "2026-03-20")
        self.assertIsNone(f["changed_by"])

    def test_advance_payment_not_taken_as_amount(self):
        f = row(self.result, "STN-101")["fields"]["amount"]
        self.assertEqual(f["value"], 4850000)  # не 1 455 000 из пункта об авансе

    def test_document_statuses_and_dates(self):
        docs = {d["filename"]: d for d in self.result["documents"]}
        self.assertEqual(docs["dop_soglashenie_1.txt"]["sign_status"], "signed")
        self.assertEqual(docs["dop_soglashenie_1.txt"]["effective_date"], "2026-02-20")
        self.assertEqual(docs["proekt_izmeneniya.txt"]["sign_status"], "draft")
        self.assertIsNone(docs["proekt_izmeneniya.txt"]["doc_date"])  # «___» ______ 2026 г. — даты нет
        self.assertEqual(docs["pismo_postavshchika.txt"]["doc_type"], "letter")
        self.assertEqual(docs["pismo_postavshchika.txt"]["doc_date"], "2026-03-02")

    def test_check_date_before_amendment(self):
        """На 15.02.2026 допсоглашение ещё не подписано — действует исходный срок."""
        result = analyze_files(load_samples(), date(2026, 2, 15))
        f = row(result, "STN-101")["fields"]["delivery_date"]
        self.assertEqual(f["value"], "2026-03-25")
        self.assertIsNone(f["changed_by"])
        pend = [p for p in row(result, "STN-101")["pending_changes"] if p["field"] == "delivery_date"]
        self.assertEqual(pend[0]["display"], "20.04.2026")
        self.assertIn("после даты проверки", pend[0]["reason"])


NEW_CONTRACT = """ДОГОВОР № STN-104 поставки оборудования

г. Казань                                                       03.02.2026

ООО «Волга-Комплект», именуемое в дальнейшем «Поставщик», в лице директора Галиева Р.Р., и ООО «ТехноСнаб», именуемое в дальнейшем «Покупатель», заключили настоящий Договор:

1. Поставщик обязуется поставить насосное оборудование, а Покупатель — принять и оплатить его.
2. Стоимость Договора составляет 1 250 000,50 руб., включая НДС.
3. Покупатель оплачивает 50% в срок до 10.02.2026, остаток — в течение 5 дней после поставки.
4. Поставщик обязуется поставить оборудование не позднее 28.03.2026.
5. За просрочку поставки Поставщик уплачивает пени 0,3% в день от суммы 1 250 000,50 руб.

Поставщик: ________ /Галиев Р.Р./      Покупатель: ________ /Петрова А.С./
"""

RELATIVE_TERM_CONTRACT = """ДОГОВОР ПОСТАВКИ № STN-105

г. Тула                                                        5 февраля 2026 г.

АО «Тульский Привод», именуемое в дальнейшем «Поставщик», и ООО «ТехноСнаб», именуемое в дальнейшем «Покупатель».

2.1. Цена Договора согласовывается Сторонами в Спецификациях.
3.1. Срок поставки Товара — в течение 30 календарных дней с даты получения предоплаты.

Поставщик: ________ /Орлов О.О./   Покупатель: ________ /Петрова А.С./
"""

FUTURE_AMENDMENT = """ДОПОЛНИТЕЛЬНОЕ СОГЛАШЕНИЕ № 2
к Договору поставки № STN-104 от 03.02.2026

г. Казань                                                       05.03.2026

1. Изложить пункт 4 Договора в следующей редакции: «Поставщик обязуется поставить оборудование не позднее 30.06.2026».
2. Настоящее Соглашение вступает в силу с 1 апреля 2026 г.

Поставщик: ________ /Галиев Р.Р./      Покупатель: ________ /Петрова А.С./
"""

UNSIGNED_AMENDMENT = """ДОПОЛНИТЕЛЬНОЕ СОГЛАШЕНИЕ № 3
к Договору поставки № STN-104 от 03.02.2026

г. Казань                                                       06.03.2026

1. Стоимость Договора составляет 1 300 000 руб.
2. Настоящее Соглашение вступает в силу с момента подписания.

Поставщик: ________ /____________/      Покупатель: ________ /____________/
"""

ORPHAN_LETTER = """Исх. № 5 от 01.03.2026
Уважаемые коллеги! Предлагаем перенести срок поставки по Договору № STN-999 на 15.06.2026.
"""


class NewContractSameFormat(unittest.TestCase):
    """Сценарий встречи: загружается новый договор того же общего формата, но с другими значениями."""

    def test_new_contract(self):
        result = analyze_files([("new.txt", NEW_CONTRACT)], CHECK)
        r = row(result, "STN-104")
        self.assertEqual(r["supplier"]["display"], "ООО «Волга-Комплект»")
        self.assertEqual(r["fields"]["amount"]["value"], 1250000.5)
        self.assertEqual(r["fields"]["amount"]["display"], "1 250 000,50 ₽")
        self.assertEqual(r["fields"]["delivery_date"]["value"], "2026-03-28")  # не 10.02 из пункта об оплате
        self.assertIn("не позднее 28.03.2026", r["fields"]["delivery_date"]["evidence"]["quote"])

    def test_relative_term_and_missing_amount(self):
        result = analyze_files([("rel.txt", RELATIVE_TERM_CONTRACT)], CHECK)
        r = row(result, "STN-105")
        self.assertEqual(r["fields"]["amount"]["status"], "no_data")
        self.assertIn("Спецификациях", r["fields"]["amount"]["evidence"]["quote"])
        self.assertEqual(r["fields"]["delivery_date"]["status"], "check")
        self.assertEqual(r["fields"]["delivery_date"]["display"], "требует проверки")

    def test_signed_but_not_yet_effective_amendment(self):
        result = analyze_files([("new.txt", NEW_CONTRACT), ("ds2.txt", FUTURE_AMENDMENT)], CHECK)
        r = row(result, "STN-104")
        self.assertEqual(r["fields"]["delivery_date"]["value"], "2026-03-28")
        pend = r["pending_changes"]
        self.assertEqual(pend[0]["display"], "30.06.2026")
        self.assertIn("после даты проверки", pend[0]["reason"])
        # а на 10 апреля — уже действует
        result2 = analyze_files([("new.txt", NEW_CONTRACT), ("ds2.txt", FUTURE_AMENDMENT)], date(2026, 4, 10))
        self.assertEqual(row(result2, "STN-104")["fields"]["delivery_date"]["value"], "2026-06-30")

    def test_unsigned_amendment_requires_check(self):
        result = analyze_files([("new.txt", NEW_CONTRACT), ("ds3.txt", UNSIGNED_AMENDMENT)], CHECK)
        f = row(result, "STN-104")["fields"]["amount"]
        self.assertEqual(f["value"], 1250000.5)      # значение не изменено
        self.assertEqual(f["status"], "check")       # но помечено «требует проверки»
        self.assertTrue(any("1 300 000" in w for w in f["needs_check"]))

    def test_reference_to_missing_contract(self):
        result = analyze_files([("letter.txt", ORPHAN_LETTER)], CHECK)
        r = row(result, "STN-999")
        self.assertIsNone(r["contract_doc"])
        self.assertEqual(r["fields"]["amount"]["display"], "нет данных")
        self.assertTrue(any("не загружен" in w for w in r["warnings"]))
        self.assertEqual(r["pending_changes"][0]["display"], "15.06.2026")

    def test_hard_wrapped_lines(self):
        """Пункт, разбитый жёстким переносом на две строки, всё равно читается целиком."""
        wrapped = NEW_CONTRACT.replace(
            "4. Поставщик обязуется поставить оборудование не позднее 28.03.2026.",
            "4. Поставщик обязуется поставить оборудование\nне позднее 28.03.2026.")
        result = analyze_files([("w.txt", wrapped)], CHECK)
        self.assertEqual(row(result, "STN-104")["fields"]["delivery_date"]["value"], "2026-03-28")

    def test_filename_does_not_matter(self):
        """Имя файла с «поздней» датой не делает проект приоритетным."""
        samples = dict(load_samples())
        files = [("2026-12-31_latest.txt", samples["proekt_izmeneniya.txt"]), ("a.txt", samples["dogovor_STN-103.txt"])]
        result = analyze_files(files, CHECK)
        self.assertEqual(row(result, "STN-103")["fields"]["delivery_date"]["value"], "2026-03-30")


if __name__ == "__main__":
    unittest.main()
