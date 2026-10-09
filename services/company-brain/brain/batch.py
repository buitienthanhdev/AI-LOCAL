"""Thao tác file HÀNG LOẠT theo điều kiện: "gom tất cả ảnh tải về tháng 9-10/2026 trong Downloads vào thư mục mới",
"chuyển các file .exe trong Downloads sang D:\\Setup", "sao chép mọi file pdf có chữ 'hop dong' vào ...".

Bot lọc file (thư mục nguồn, loại file, khoảng ngày tạo/sửa, mẫu tên) -> MỘT yêu cầu xác nhận kèm danh sách xem trước ->
người dùng bấm Thực hiện một lần -> chạy cả loạt. Mỗi lần chuyển được ghi sổ (data/batches) để HOÀN TÁC được."""
import fnmatch
import json
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import DATA_DIR
from .facts import norm

MAX_FILES = 10000
BATCH_DIR = DATA_DIR / "batches"

TOOLS = [
    {"type": "function", "function": {
        "name": "batch_files",
        "description": ("Di chuyển / sao chép NHIỀU file cùng lúc theo điều kiện (gom, dọn, sắp xếp thư mục): lọc theo thư mục "
                        "nguồn, loại file (anh, video, nhac, pdf, word, excel, powerpoint, text, code, nen hoặc đuôi .xyz), "
                        "khoảng ngày (date_from/date_to dạng YYYY-MM-DD; 'tải về' = ngày tạo file), mẫu tên. destination: tên "
                        "thư mục mới (tạo trong thư mục nguồn) hoặc đường dẫn đầy đủ. Tạo MỘT yêu cầu xác nhận kèm danh sách "
                        "xem trước; người dùng bấm Thực hiện một lần là xong cả loạt; hoàn tác được bằng undo_batch."),
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "description": "thư mục nguồn, vd. Downloads / Desktop / D:\\Anh (mặc định Downloads)"},
            "types": {"type": "array", "items": {"type": "string"}},
            "date_from": {"type": "string"}, "date_to": {"type": "string"},
            "date_field": {"type": "string", "enum": ["created", "modified"],
                           "description": "created = ngày file xuất hiện trên máy (tải về); modified = ngày sửa"},
            "name_pattern": {"type": "string", "description": "lọc theo tên, vd. *hop dong* (không dấu được)"},
            "recursive": {"type": "boolean", "description": "tìm cả trong thư mục con (mặc định không)"},
            "action": {"type": "string", "enum": ["move", "copy"]},
            "destination": {"type": "string"}},
            "required": ["destination"]}}},
    {"type": "function", "function": {
        "name": "undo_batch",
        "description": "HOÀN TÁC lần di chuyển hàng loạt gần nhất (hoặc batch_id): đưa các file về chỗ cũ. Cần bấm Thực hiện.",
        "parameters": {"type": "object", "properties": {"batch_id": {"type": "string"}}}}},
]
NAMES = ("batch_files", "undo_batch")

KNOWN = {"downloads": "Downloads", "tai ve": "Downloads", "download": "Downloads", "desktop": "Desktop",
         "man hinh": "Desktop", "documents": "Documents", "tai lieu": "Documents", "pictures": "Pictures",
         "hinh anh": "Pictures", "videos": "Videos", "music": "Music", "nhac": "Music"}


def _folder(s: str | None) -> Path:
    s = (s or "Downloads").strip().strip('"')
    p = Path(os.path.expandvars(os.path.expanduser(s)))
    if p.is_absolute():
        return p
    return Path.home() / KNOWN.get(norm(s).strip(), s)


def _day(s: str | None, end: bool = False) -> datetime | None:
    """'2026-09-01' / '01/09/2026' / '2026-09' / '9/2026' -> mốc thời gian (end=True: cuối ngày/cuối tháng)."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            d = datetime.strptime(s, fmt)
            return d + timedelta(days=1) - timedelta(microseconds=1) if end else d
        except ValueError:
            pass
    for fmt in ("%Y-%m", "%m/%Y", "%m-%Y"):
        try:
            d = datetime.strptime(s, fmt)
            if end:
                nxt = d.replace(year=d.year + (d.month == 12), month=d.month % 12 + 1)
                return nxt - timedelta(microseconds=1)
            return d
        except ValueError:
            pass
    return None


def _when(st: os.stat_result, field: str) -> datetime:
    if field == "modified":
        return datetime.fromtimestamp(st.st_mtime)
    return datetime.fromtimestamp(getattr(st, "st_birthtime", st.st_ctime))  # Windows: ctime = ngày tạo


def select(args: dict) -> tuple[Path, Path, list[dict]] | dict:
    from .actions import _protected
    from .launcher import exts_from
    src = _folder(args.get("source"))
    if not src.is_dir():
        return {"error": f"Không có thư mục nguồn: {src}"}
    dest_raw = (args.get("destination") or "").strip().strip('"')
    if not dest_raw:
        return {"error": "Cần destination (tên thư mục mới hoặc đường dẫn đầy đủ)."}
    parts = Path(dest_raw).parts
    if Path(dest_raw).is_absolute():
        dst = Path(dest_raw)
    elif len(parts) > 1 and parts[0].lower() == src.name.lower():
        dst = src.joinpath(*parts[1:])  # "Downloads\\Anh 9-10" với nguồn là Downloads => Downloads\Anh 9-10 (không lồng 2 lần)
    elif len(parts) > 1 and norm(parts[0]).strip() in KNOWN:
        dst = _folder(parts[0]).joinpath(*parts[1:])  # "Desktop\\Anh" => C:\Users\...\Desktop\Anh
    else:
        dst = src / dest_raw
    if _protected(dst) or (args.get("action") or "move") == "move" and _protected(src):
        return {"error": "Không được ghi/chuyển trong thư mục hệ thống."}
    exts = set(exts_from(args.get("types"))) if args.get("types") else None
    if args.get("types") and not exts:
        return {"error": f"Không hiểu loại file: {args.get('types')}"}
    d0, d1 = _day(args.get("date_from")), _day(args.get("date_to"), end=True)
    if (args.get("date_from") and not d0) or (args.get("date_to") and not d1):
        return {"error": "Ngày phải dạng YYYY-MM-DD (hoặc YYYY-MM)."}
    pat = norm(args.get("name_pattern") or "").strip()
    if pat and not any(c in pat for c in "*?["):
        pat = f"*{pat}*"
    field = args.get("date_field") or "created"
    picked = []
    walker = os.walk(src) if args.get("recursive") else [(str(src), [], [e.name for e in os.scandir(src) if e.is_file()])]
    for dirpath, dirnames, filenames in walker:
        dirnames[:] = [d for d in dirnames if Path(dirpath, d) != dst]
        if Path(dirpath) == dst:
            continue
        for name in filenames:
            p = Path(dirpath, name)
            if exts is not None and p.suffix.lower() not in exts:
                continue
            if pat and not fnmatch.fnmatch(norm(name), pat):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            w = _when(st, field)
            if (d0 and w < d0) or (d1 and w > d1):
                continue
            picked.append({"path": str(p), "size": st.st_size, "when": w.strftime("%Y-%m-%d")})
            if len(picked) >= MAX_FILES:
                break
    return src, dst, picked


def propose(args: dict, user: str, is_admin: bool) -> dict:
    if not is_admin:
        return {"error": "Chỉ người dùng trên máy chủ được thao tác file hàng loạt."}
    r = select(args)
    if isinstance(r, dict):
        return r
    src, dst, files = r
    act = args.get("action") or "move"
    if not files:
        return {"found": 0, "source": str(src), "note": "Không có file nào khớp điều kiện — báo người dùng (nêu điều kiện đã lọc)."}
    total = sum(f["size"] for f in files) / 1048576
    verb = "Chuyển" if act == "move" else "Sao chép"
    summary = f"{verb} {len(files)} file ({total:.1f} MB) từ {src} vào {dst}" + ("" if dst.exists() else " (thư mục mới)")
    preview = "\n".join(f"{f['when']}  {Path(f['path']).name}" for f in files[:40]) + (
        f"\n… và {len(files) - 40} file nữa" if len(files) > 40 else "")
    from .actions import add_pending
    return add_pending("batch", summary, {"action": act, "dst": str(dst), "files": [f["path"] for f in files]}, user,
                       args.get("reason"), detail=preview) | {"found": len(files), "size_mb": round(total, 1)}


def _free(target: Path) -> Path:
    if not target.exists():
        return target
    for i in range(1, 1000):
        c = target.with_name(f"{target.stem} ({i}){target.suffix}")
        if not c.exists():
            return c
    raise OSError(f"Quá nhiều file trùng tên: {target.name}")


def run(plan: dict) -> str:
    dst = Path(plan["dst"])
    dst.mkdir(parents=True, exist_ok=True)
    done, skipped = [], 0
    for s in plan["files"]:
        sp = Path(s)
        if not sp.exists():
            skipped += 1
            continue
        target = _free(dst / sp.name)
        try:
            if plan["action"] == "copy":
                shutil.copy2(sp, target)
            else:
                shutil.move(str(sp), str(target))
            done.append([str(sp), str(target)])
        except OSError:
            skipped += 1
    bid = time.strftime("%Y%m%d-%H%M%S")
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    (BATCH_DIR / f"{bid}.json").write_text(json.dumps({"id": bid, "action": plan["action"], "dst": str(dst), "moves": done},
                                                      ensure_ascii=False), encoding="utf-8")
    verb = "Đã chuyển" if plan["action"] == "move" else "Đã sao chép"
    return (f"{verb} {len(done)} file vào {dst}" + (f", bỏ qua {skipped} file (không còn/đang mở)" if skipped else "")
            + (f". Hoàn tác được (mã {bid})." if plan["action"] == "move" and done else "."))


def propose_undo(args: dict, user: str, is_admin: bool) -> dict:
    if not is_admin:
        return {"error": "Chỉ người dùng trên máy chủ."}
    files = sorted(BATCH_DIR.glob("*.json")) if BATCH_DIR.exists() else []
    if args.get("batch_id"):
        files = [f for f in files if f.stem == args["batch_id"]]
    man = None
    for f in reversed(files):
        m = json.loads(f.read_text(encoding="utf-8"))
        if m["action"] == "move" and m["moves"] and not m.get("undone"):
            man = m
            break
    if not man:
        return {"error": "Không có lần di chuyển hàng loạt nào để hoàn tác."}
    from .actions import add_pending
    return add_pending("batch_undo", f"Hoàn tác: đưa {len(man['moves'])} file từ {man['dst']} về chỗ cũ",
                       {"id": man["id"]}, user, detail="\n".join(Path(a).name for a, _b in man["moves"][:40]))


def run_undo(plan: dict) -> str:
    f = BATCH_DIR / f"{plan['id']}.json"
    man = json.loads(f.read_text(encoding="utf-8"))
    back, skipped = 0, 0
    for orig, now in man["moves"]:
        o, n = Path(orig), Path(now)
        if n.exists() and not o.exists():
            o.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(n), str(o))
            back += 1
        else:
            skipped += 1
    man["undone"] = True
    f.write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    d = Path(man["dst"])
    if d.exists() and not any(d.iterdir()):
        d.rmdir()  # thư mục do lần gom tạo ra, giờ trống => dọn luôn
    return f"Đã đưa {back} file về chỗ cũ" + (f", bỏ qua {skipped} file (đã đổi chỗ/trùng tên)" if skipped else "") + "."
