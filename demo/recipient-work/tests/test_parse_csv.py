# Учебный демонстрационный файл для сценария передачи Staffetta (PILOT.md, §2).
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from parse_csv import parse_sales


def test_parse_sales():
    data = Path(__file__).resolve().parents[1] / "data" / "sales_local.csv"
    rows = parse_sales(data)
    assert rows[0]["amount"] == Decimal("1234.50")
    assert rows[1]["amount"] == Decimal("890.00")
    return rows


if __name__ == "__main__":
    rows = test_parse_sales()
    print(f"OK: разобрано строк: {len(rows)}; "
          f"первая: {rows[0]['date']} = {rows[0]['amount']}")

# локальная правка получателя (расхождение с версией отправителя)
