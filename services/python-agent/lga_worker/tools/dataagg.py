"""data.aggregate (contract: tools/documents.json): exact sums, averages, counts and extremes over a table, computed by
code instead of by the model. The "parietal" helper of the assistant: small models add numbers wrongly or give up and
ask; this reads the real file(s) and returns the number with the rows it used.

Reads .csv (utf-8 / utf-8-sig / cp1258 / cp1252), .xlsx/.xlsm (cached values, first or named sheet) and .json (a list of
objects). A folder is read file by file (same extension set) and the rows are combined. Numbers may carry currency signs,
spaces, a percent sign, "1.950.000" (Vietnamese thousands dots) or "1,950,000.50".
"""

from __future__ import annotations

import csv
import io
import json
import re
import statistics
import unicodedata
from pathlib import Path
from typing import Any

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paths import absolute_path
from lga_worker.registry import ToolContext, ToolOutcome, ToolRegistry

BACKEND = "dataagg"
MAX_ROWS = 2_000_000
EXTENSIONS = (".csv", ".xlsx", ".xlsm", ".json")


def fold(text: str) -> str:
    """Case- and accent-insensitive key for matching column names."""
    text = unicodedata.normalize("NFD", str(text).strip().lower().replace("đ", "d"))
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in text if unicodedata.category(c) != "Mn")).strip()


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if not text:
        return None
    text = re.sub(r"(vnđ|vnd|đ|usd|\$|€|%|\s)", "", text.replace(" ", ""))
    negative = text.startswith("(") and text.endswith(")") or text.startswith("-")
    text = text.strip("()-+")
    if not re.fullmatch(r"[\d.,]+", text) or not re.search(r"\d", text):
        return None
    if "." in text and "," in text:
        decimal = "." if text.rfind(".") > text.rfind(",") else ","
        text = text.replace("," if decimal == "." else ".", "").replace(decimal, ".")
    elif text.count(".") > 1 or text.count(",") > 1:
        text = text.replace(".", "").replace(",", "")
    elif "." in text and re.fullmatch(r"\d{1,3}\.\d{3}", text):
        text = text.replace(".", "")           # 1.950 -> 1950 (Vietnamese thousands)
    elif "," in text:
        head, _, tail = text.partition(",")
        text = head + tail if re.fullmatch(r"\d{3}", tail) and len(head) <= 3 else head + "." + tail
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def _read_csv(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1258", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [dict(r) for r in csv.DictReader(io.StringIO(text), dialect=dialect)]


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _read_xlsx(path: Path, sheet: str | None) -> list[dict[str, Any]]:
    """Cached cell values of one sheet, read straight from the package (no Excel, no openpyxl needed)."""
    import zipfile
    from xml.etree import ElementTree as ET

    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
        book = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        sheets = [(s_.get("name"), rels.get(s_.get("{%s}id" % ns["r"]))) for s_ in book.find("m:sheets", ns)]
        chosen = next((t for n, t in sheets if sheet and n == sheet), sheets[0][1])
        target = chosen.lstrip("/") if chosen.startswith("/") else "xl/" + chosen
        table: list[list[Any]] = []
        for row in ET.fromstring(z.read(target)).iter("{%s}row" % ns["m"]):
            cells: dict[int, Any] = {}
            for c in row.findall("m:c", ns):
                v = c.find("m:v", ns)
                kind = c.get("t")
                if kind == "inlineStr":
                    value: Any = "".join(t.text or "" for t in c.iter("{%s}t" % ns["m"]))
                elif v is None or v.text is None:
                    value = None
                elif kind == "s":
                    value = shared[int(v.text)]
                elif kind in ("str", "e", "b"):
                    value = v.text
                else:
                    value = float(v.text)
                cells[_col_index(c.get("r"))] = value
            if cells:
                width = max(cells) + 1
                table.append([cells.get(i) for i in range(width)])
    if not table:
        return []
    header = [str(c).strip() if c not in (None, "") else f"col{i + 1}" for i, c in enumerate(table[0])]
    out = []
    for row in table[1:]:
        if all(c in (None, "") for c in row):
            continue
        out.append({h: row[i] if i < len(row) else None for i, h in enumerate(header)})
    return out


def _read_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [data])
    return [r for r in data if isinstance(r, dict)]


def load_rows(path: Path, sheet: str | None = None, recursive: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    files = [path] if path.is_file() else sorted(p for p in (path.rglob("*") if recursive else path.glob("*")) if p.suffix.lower() in EXTENSIONS and p.is_file())
    if not files:
        raise ToolError(ErrorCode.NOT_FOUND, f"không có tệp bảng (.csv/.xlsx/.json) tại {path}")
    rows: list[dict[str, Any]] = []
    used: list[str] = []
    for f in files:
        ext = f.suffix.lower()
        if ext == ".csv":
            part = _read_csv(f)
        elif ext in (".xlsx", ".xlsm"):
            part = _read_xlsx(f, sheet)
        elif ext == ".json":
            part = _read_json(f)
        else:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"định dạng {ext} chưa hỗ trợ")
        rows.extend(part)
        used.append(f.name)
    # files of one folder may spell a header differently ("Doanh thu" / "doanh_thu"): one canonical spelling per column
    canon: dict[str, str] = {}
    for r in rows:
        for k in r:
            canon.setdefault(fold(k), k)
    rows = [{canon[fold(k)]: v for k, v in r.items()} for r in rows]
    return rows, used


def find_column(rows: list[dict[str, Any]], wanted: str) -> str:
    columns: list[str] = []
    for r in rows[:200]:
        for k in r:
            if k not in columns:
                columns.append(k)
    key = fold(wanted)
    exact = [c for c in columns if fold(c) == key]
    if exact:
        return exact[0]
    partial = [c for c in columns if key and (key in fold(c) or fold(c) in key)]
    if len(partial) == 1:
        return partial[0]
    raise ToolError(ErrorCode.INVALID_ARGUMENT, f"không có cột '{wanted}'. Các cột có: {', '.join(columns)}")


def _matches(row: dict[str, Any], column: str, cond: dict[str, Any]) -> bool:
    value = row.get(column)
    text = "" if value is None else str(value).strip()
    if "equals" in cond:
        return fold(text) == fold(str(cond["equals"])) or (parse_number(text) is not None and parse_number(text) == parse_number(cond["equals"]))
    if "contains" in cond:
        return fold(str(cond["contains"])) in fold(text)
    number = parse_number(value)
    if "gt" in cond:
        return number is not None and number > float(cond["gt"])
    if "lt" in cond:
        return number is not None and number < float(cond["lt"])
    return True


def aggregate(rows: list[dict[str, Any]], column: str, op: str, where: dict[str, Any] | None = None, group_by: str | None = None) -> dict[str, Any]:
    col = find_column(rows, column)
    if where:
        wcol = find_column(rows, where["column"])
        rows = [r for r in rows if _matches(r, wcol, where)]
    gcol = find_column(rows, group_by) if group_by and rows else None

    def compute(part: list[dict[str, Any]]) -> dict[str, Any]:
        values = [parse_number(r.get(col)) for r in part]
        numbers = [v for v in values if v is not None]
        nonempty = [r.get(col) for r in part if r.get(col) not in (None, "")]
        skipped = len(nonempty) - len(numbers) if op in ("sum", "mean", "min", "max", "median") else 0
        if op == "count":
            value: Any = len(part)
        elif op == "count_nonempty":
            value = len(nonempty)
        elif op == "distinct":
            value = len({fold(str(v)) for v in nonempty})
        elif not numbers:
            value = None
        elif op == "sum":
            value = sum(numbers)
        elif op == "mean":
            value = sum(numbers) / len(numbers)
        elif op == "min":
            value = min(numbers)
        elif op == "max":
            value = max(numbers)
        elif op == "median":
            value = statistics.median(numbers)
        else:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "op phải là sum, mean, min, max, median, count, count_nonempty hoặc distinct")
        if isinstance(value, float) and value == int(value) and abs(value) < 1e15:
            value = int(value)
        return {"value": value, "rows": len(part), "numeric_values": len(numbers), "skipped_non_numeric": skipped}

    out: dict[str, Any] = {"column": col, "op": op, "rows_used": len(rows)}
    if gcol:
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault(str(r.get(gcol) or "").strip() or "(trống)", []).append(r)
        out["group_by"] = gcol
        out["groups"] = {k: compute(v) for k, v in groups.items()}
    else:
        out.update(compute(rows))
    return out


def register(registry: ToolRegistry) -> None:
    @registry.handler("data.aggregate")
    async def data_aggregate(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        path = absolute_path(args["path"])
        if not path.exists():
            raise ToolError(ErrorCode.NOT_FOUND, f"không có {path}")
        rows, files = load_rows(path, args.get("sheet"), bool(args.get("recursive")))
        if not rows:
            raise ToolError(ErrorCode.NOT_FOUND, f"không có dòng dữ liệu nào trong {', '.join(files)}")
        result = aggregate(rows, args["column"], args.get("op", "sum"), args.get("where"), args.get("group_by"))
        result["files"] = files
        if "groups" in result:
            body = "; ".join(f"{k}: {v['value']}" for k, v in list(result["groups"].items())[:40])
            text = f"{result['op']} của cột '{result['column']}' theo '{result['group_by']}' ({len(files)} tệp, {result['rows_used']} dòng): {body}"
        else:
            text = (f"{result['op']} của cột '{result['column']}' = {result['value']} "
                    f"({result['rows_used']} dòng từ {len(files)} tệp: {', '.join(files[:8])}; {result['numeric_values']} giá trị số"
                    + (f", bỏ qua {result['skipped_non_numeric']} ô không phải số" if result.get("skipped_non_numeric") else "") + ")")
        return ToolOutcome(data=result, backend=BACKEND, observation=text)
