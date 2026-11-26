"""Generate a portable Excel calculator from a measured full request."""

import argparse
import json
from pathlib import Path
import xlsxwriter

root = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--benchmark", type=Path)
p.add_argument("--output", type=Path, default=root / "build/cost.xlsx")
args = p.parse_args()
run = (
    json.loads(args.benchmark.read_text(encoding="utf-8-sig"))
    if args.benchmark
    else dict(
        seconds=12,
        calls=1,
        usage=dict(prompt_tokens=800, completion_tokens=40, total_tokens=840),
        measured_utc="Синтетический пример",
    )
)
args.output.parent.mkdir(parents=True, exist_ok=True)
w = xlsxwriter.Workbook(args.output)
s = w.add_worksheet("Стоимость запроса")
s.set_column("A:A", 44)
s.set_column("B:B", 19)
s.set_column("C:C", 78)
s.set_row(0, 34)
head = w.add_format(
    {
        "bg_color": "#153E40",
        "font_color": "white",
        "bold": True,
        "font_size": 16,
        "valign": "vcenter",
    }
)
label = w.add_format({"text_wrap": True, "valign": "vcenter"})
num = w.add_format({"num_format": "0.000", "align": "right", "valign": "vcenter"})
editable = w.add_format(
    {
        "num_format": "0.000",
        "bg_color": "#DCF0E5",
        "align": "right",
        "valign": "vcenter",
    }
)
result = w.add_format(
    {
        "bold": True,
        "num_format": "0.000000",
        "bg_color": "#E7F2F4",
        "align": "right",
        "valign": "vcenter",
    }
)
s.merge_range("A1:C1", "ParadiigmaMGR   /   стоимость одного полного запроса", head)
rows = [
    ("Модель", run.get("model", "Qwen3-4B"), "Локальный сценарий расчёта."),
    (
        "Время HTTP-запроса, с",
        run["seconds"],
        "Время запроса после загрузки модели. Без файла замера используются условные значения.",
    ),
    ("Вызовы модели", run["calls"], "Количество вызовов на один разбор."),
    (
        "Входные токены",
        run["usage"]["prompt_tokens"],
        "Счётчик сервера или условное значение из примера.",
    ),
    (
        "Выходные токены",
        run["usage"]["completion_tokens"],
        "Счётчик сервера или условное значение из примера.",
    ),
    ("Всего токенов", None, "Сумма входных и выходных токенов."),
    (
        "Внешняя плата за API, ₽",
        0,
        "Предполагается запуск модели локально.",
    ),
    (
        "Сценарный тариф вычислений, ₽/час",
        15,
        "Редактируемый пример. Замените на фактический тариф машины. Время простоя сюда не входит.",
    ),
    ("Вычисления для запроса по тарифу, ₽", None, "Время HTTP-запроса × тариф / 3600."),
    (
        "Цена одного запроса по сценарию, ₽",
        None,
        "Сумма внешней платы и оценки вычислений.",
    ),
    ("Запросов в месяц, сценарий", 100, "Изменяемое допущение команды."),
    ("Месяц по сценарию, ₽", None, "Количество запросов × цена запроса."),
    (
        "Дата/время замера UTC",
        run["measured_utc"],
        "Ручная экономия времени, стоимость оборудования и круглосуточного хостинга отдельно не измерялись.",
    ),
]
for i, (a, b, c) in enumerate(rows, 1):
    s.set_row(i, 35 if i != 13 else 46)
    s.write(i, 0, a, label)
    s.write(i, 2, c, label)
    if b is not None:
        s.write(
            i,
            1,
            b,
            editable if i in (8, 11) else num if isinstance(b, float) else label,
        )
s.write_formula("B7", "=SUM(B5:B6)", result, run["usage"]["total_tokens"])
estimated = run["seconds"] * 15 / 3600
s.write_formula("B10", "=B3*B9/3600", result, estimated)
s.write_formula("B11", "=B8+B10", result, estimated)
s.write_formula("B13", "=B11*B12", result, estimated * 100)
s.data_validation("B9", {"validate": "decimal", "criteria": ">=", "value": 0})
s.data_validation("B12", {"validate": "integer", "criteria": ">=", "value": 0})
s.freeze_panes(1, 1)
s.autofilter("A1:C1")
s.set_landscape()
s.fit_to_pages(1, 1)
w.close()
