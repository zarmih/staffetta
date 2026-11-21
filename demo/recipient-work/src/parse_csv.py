# Учебный демонстрационный файл для сценария передачи Staffetta (PILOT.md, §2).
"""Разбор CSV импорта продаж: разделитель ';', десятичная запятая, BOM."""
import csv
from decimal import Decimal


def parse_sales(path):
    """Возвращает [{'date': str, 'amount': Decimal}, ...]."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            amount = row["amount"].replace(" ", "").replace(",", ".")
            rows.append({"date": row["date"], "amount": Decimal(amount)})
    return rows
