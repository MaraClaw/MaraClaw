from __future__ import annotations

import importlib
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast


class _Worksheet(Protocol):
    def append(self, values: list[str]) -> object: ...


class _Workbook(Protocol):
    worksheets: Sequence[_Worksheet]

    def save(self, filename: str) -> object: ...


class _OpenpyxlModule(Protocol):
    Workbook: type[_Workbook]


def _convert_csv_to_xlsx(src_file: Path, tgt_file: Path, target_path: str) -> str:
    import csv

    openpyxl = cast(_OpenpyxlModule, importlib.import_module("openpyxl"))

    text = src_file.read_text(encoding="utf-8-sig")
    lines = [line.strip() for line in text.splitlines() if line.strip()][:10]
    candidates = [",", "\uff0c", ";", "\t", "|"]
    delimiter = ","
    if lines:
        scores = {candidate: sum(line.count(candidate) for line in lines) for candidate in candidates}
        if any(scores.values()):
            delimiter = max(scores, key=lambda candidate: scores[candidate])

    workbook = openpyxl.Workbook()
    worksheet = workbook.worksheets[0]
    with src_file.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, delimiter=delimiter)
        for row in reader:
            values = list(row)
            while values and not str(values[-1] or "").strip():
                _ = values.pop()
            if values:
                worksheet.append(values)

    tgt_file.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(tgt_file))
    return f"✅ Successfully converted CSV to Excel: {target_path}"
