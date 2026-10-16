"""data.aggregate: exact numbers from real files (the 'parietal' helper)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from lga_worker.errors import ToolError
from lga_worker.tools import dataagg
from lga_worker.tools.dataagg import aggregate, find_column, load_rows, parse_number


@pytest.mark.parametrize("raw,expected", [
    ("1.950.000", 1950000.0), ("1,950,000.50", 1950000.5), ("1.950", 1950.0), ("12,5", 12.5), ("1.234,56", 1234.56),
    ("450.000 đ", 450000.0), ("45%", 45.0), ("(1.200)", -1200.0), ("abc", None), ("", None), (None, None), (7, 7.0), (True, None),
])
def test_numbers_are_read_the_way_people_write_them(raw, expected):
    assert parse_number(raw) == expected


def _csv(path: Path, header, rows, delimiter=","):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=delimiter)
        w.writerow(header)
        w.writerows(rows)


def test_sum_over_a_folder_of_csv_files_combines_the_rows(tmp_path):
    _csv(tmp_path / "t1.csv", ["Ngày", "Doanh thu"], [["1/10", "500"], ["2/10", "250"]])
    _csv(tmp_path / "t2.csv", ["Ngày", "doanh_thu"], [["3/10", "1.000"]], delimiter=";")  # other delimiter, other spelling of the header
    rows, files = load_rows(tmp_path)
    assert files == ["t1.csv", "t2.csv"]
    r1 = aggregate(rows, "doanh thu", "sum")           # "Doanh thu" and "doanh_thu" are the same column
    assert r1["value"] == 1750 and r1["rows"] == 3


def test_column_names_match_without_accents_or_case(tmp_path):
    _csv(tmp_path / "a.csv", ["Tên", "Số lượng"], [["A", "3"], ["B", "4"], ["A", "5"]])
    rows, _ = load_rows(tmp_path / "a.csv")
    assert find_column(rows, "so luong") == "Số lượng"
    assert aggregate(rows, "SỐ LƯỢNG", "sum")["value"] == 12
    assert aggregate(rows, "so luong", "mean")["value"] == 4
    assert aggregate(rows, "so luong", "max")["value"] == 5
    assert aggregate(rows, "ten", "distinct")["value"] == 2
    grouped = aggregate(rows, "so luong", "sum", group_by="ten")
    assert grouped["groups"]["A"]["value"] == 8 and grouped["groups"]["B"]["value"] == 4


def test_filters_and_skipped_cells_are_reported(tmp_path):
    _csv(tmp_path / "a.csv", ["Loại", "Tiền"], [["x", "10"], ["y", "20"], ["x", "n/a"], ["x", "5"]])
    rows, _ = load_rows(tmp_path / "a.csv")
    r = aggregate(rows, "tien", "sum", where={"column": "loai", "equals": "x"})
    assert r["value"] == 15 and r["skipped_non_numeric"] == 1 and r["rows"] == 3
    assert aggregate(rows, "tien", "count", where={"column": "tien", "gt": 8})["value"] == 2


def test_unknown_column_lists_the_real_ones(tmp_path):
    _csv(tmp_path / "a.csv", ["A", "B"], [["1", "2"]])
    rows, _ = load_rows(tmp_path / "a.csv")
    with pytest.raises(ToolError) as e:
        aggregate(rows, "zzz", "sum")
    assert "A, B" in str(e.value)


def test_xlsx_and_json_are_read(tmp_path):
    openpyxl = pytest.importorskip("openpyxl", reason="only needed to build the fixture")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Điểm", "Tên"])
    ws.append([8, "An"])
    ws.append([6, "Bình"])
    wb.save(tmp_path / "d.xlsx")
    rows, _ = load_rows(tmp_path / "d.xlsx")
    assert aggregate(rows, "diem", "mean")["value"] == 7
    (tmp_path / "d.json").write_text(json.dumps({"data": [{"gia": "1.000"}, {"gia": "2.500"}]}), encoding="utf-8")
    rows, _ = load_rows(tmp_path / "d.json")
    assert aggregate(rows, "gia", "sum")["value"] == 3500


def test_a_folder_without_tables_is_an_error(tmp_path):
    (tmp_path / "x.txt").write_text("hi")
    with pytest.raises(ToolError):
        load_rows(tmp_path)


def test_the_module_is_registered_as_a_tool():
    assert hasattr(dataagg, "register")
