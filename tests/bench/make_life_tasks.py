"""Generates tests/bench/tasks_life.json: file memory (where / who / when), work tracking, organising, media.

File-memory tasks use a real file from the local Company Brain catalog (chosen when this script runs), so the
expected answers are facts of this machine, read from the catalog, not invented.
"""
import datetime as dt
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
D = "{dir}"
T: list[dict] = []


def task(tid, prompt, checks, setup=None, expect="completed", cleanup=None):
    t = {"id": tid, "category": "life", "prompt": prompt, "checks": [{"kind": "status", "is": expect}, *checks]}
    if setup:
        t["setup"] = setup
    if cleanup:
        t["cleanup"] = cleanup
    T.append(t)


# ---- file memory from the catalog ------------------------------------------------------------------------
db = ROOT / "services/company-brain/data/catalog.db"
if db.is_file():
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    row = conn.execute(
        """SELECT f.path, f.name, f.doc_author, f.fs_modified_at FROM files f
           WHERE f.status='active' AND f.tier IN (1,2) AND f.ext='.docx' AND f.doc_author IS NOT NULL AND length(f.doc_author) > 3
             AND f.name NOT LIKE '~%' AND (SELECT COUNT(*) FROM files g WHERE g.name = f.name AND g.status='active') = 1
             AND length(f.name) BETWEEN 12 AND 60 ORDER BY f.file_id DESC LIMIT 1""").fetchone()
    conn.close()
    if row:
        path, name, author, modified = row
        parent = Path(path).parent.name
        when = dt.datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else None
        task("life-file-where", f"File '{name}' đang nằm ở thư mục nào trên máy?",
             [{"kind": "summary_contains", "any": [parent]}])
        task("life-file-author", f"Ai là tác giả (người tạo) của file '{name}'?",
             [{"kind": "summary_contains", "any": [author, author.split()[-1]]}])
        if when:
            local = when.astimezone() if when.tzinfo else when
            task("life-file-when", f"File '{name}' được sửa lần cuối vào ngày nào?",
                 [{"kind": "summary_regex",
                   "pattern": rf"({local:%d/%m/%Y}|{local:%Y-%m-%d}|{local.day} tháng {local.month}(,| năm){' ' if False else ''}.*{local.year}|{local:%d-%m-%Y}|{local:%d/%m}\b)"}])


task("life-file-authors-property", f"Ai là tác giả (Authors) của file {D}\\x.docx và file được tạo vào ngày nào?",
     [{"kind": "summary_contains", "any": ["python-docx"]}, {"kind": "tool_used", "name": "file.info"}],
     {"docx": {"path": f"{D}\\x.docx", "paragraphs": ["nội dung"]}})

# ---- work tracking ---------------------------------------------------------------------------------------
task("life-activity-now", "Bây giờ tôi đang mở chương trình nào trên máy?",
     [{"kind": "tool_used", "name": "activity.now"}, {"kind": "max_steps", "n": 3}])
task("life-activity-summary", "Hôm nay tôi đã dùng những chương trình nào nhiều nhất?",
     [{"kind": "tool_used", "prefix": "activity."}])

# ---- organising and cleaning ---------------------------------------------------------------------------
task("life-dedupe-files", f"Trong thư mục {D}\\anh có nhiều file giống hệt nhau về nội dung. Giữ lại đúng một bản của mỗi nội dung, xóa các bản trùng.",
     [{"kind": "file_count", "path": f"{D}\\anh", "pattern": "*.jpg", "n": 2}],
     {"files": {f"{D}\\anh\\a1.jpg": "AAAA", f"{D}\\anh\\a2.jpg": "AAAA", f"{D}\\anh\\b1.jpg": "BBBB", f"{D}\\anh\\a3.jpg": "AAAA"}})
task("life-empty-folders", f"Xóa tất cả thư mục rỗng trong {D}\\du_an (giữ thư mục có file).",
     [{"kind": "dir_missing", "path": f"{D}\\du_an\\rong1"}, {"kind": "dir_missing", "path": f"{D}\\du_an\\a\\rong2"},
      {"kind": "file_exists", "path": f"{D}\\du_an\\co_file\\x.txt"}],
     {"dirs": [f"{D}\\du_an\\rong1", f"{D}\\du_an\\a\\rong2"], "files": {f"{D}\\du_an\\co_file\\x.txt": "x"}})
task("life-archive-old", f"Chuyển các file không được sửa trong 30 ngày qua trong {D}\\hs vào thư mục {D}\\hs\\luu_tru, giữ nguyên file mới.",
     [{"kind": "file_exists", "path": f"{D}\\hs\\luu_tru\\cu.txt"}, {"kind": "file_exists", "path": f"{D}\\hs\\moi.txt"},
      {"kind": "file_missing", "path": f"{D}\\hs\\cu.txt"}],
     {"files": {f"{D}\\hs\\cu.txt": "cũ", f"{D}\\hs\\moi.txt": "mới"}, "mtimes": {f"{D}\\hs\\cu.txt": -90 * 86400}})
task("life-excel-clean", f"Trong {D}\\kh.xlsx hãy xóa các dòng trống và các dòng trùng nhau, giữ dòng tiêu đề, lưu lại cùng tên file.",
     [{"kind": "xlsx_rows", "path": f"{D}\\kh.xlsx", "rows": 4}, {"kind": "xlsx_has", "path": f"{D}\\kh.xlsx", "values": ["An", "Bình", "Chi"]}],
     {"xlsx": {"path": f"{D}\\kh.xlsx", "rows": [["Tên", "SĐT"], ["An", "090"], [None, None], ["Bình", "091"], ["An", "090"], ["Chi", "092"], [None, None]]}})
task("life-word-remove-section", f"Trong file Word {D}\\bc.docx hãy xóa đoạn 'Ghi chú nội bộ: không gửi ra ngoài' và giữ các đoạn còn lại.",
     [{"kind": "docx_absent", "path": f"{D}\\bc.docx", "texts": ["Ghi chú nội bộ"]}, {"kind": "docx_contains", "path": f"{D}\\bc.docx", "texts": ["Kết quả quý 3", "Kiến nghị"]}],
     {"docx": {"path": f"{D}\\bc.docx", "paragraphs": ["Kết quả quý 3", "Ghi chú nội bộ: không gửi ra ngoài", "Kiến nghị: tăng ngân sách"]}})
task("life-put-in-place", f"Các file trong {D}\\lung_tung gồm hóa đơn, hợp đồng và ảnh. Đưa mỗi loại vào một thư mục con nằm bên trong đó, tên 'hoa_don', 'hop_dong', 'anh' "
                          "(hóa đơn có chữ 'hoadon' trong tên, hợp đồng có 'hopdong', còn lại là ảnh).",
     [{"kind": "file_exists", "path": f"{D}\\lung_tung\\hoa_don\\hoadon_01.pdf"}, {"kind": "file_exists", "path": f"{D}\\lung_tung\\hop_dong\\hopdong_abc.docx"},
      {"kind": "file_exists", "path": f"{D}\\lung_tung\\anh\\chup_1.jpg"}],
     {"files": {f"{D}\\lung_tung\\hoadon_01.pdf": "p", f"{D}\\lung_tung\\hopdong_abc.docx": "d", f"{D}\\lung_tung\\chup_1.jpg": "j"}})

# ---- media ---------------------------------------------------------------------------------------------------
media = ROOT / "logs" / "dev" / "media"
if (media / "hoadon.png").is_file():
    task("life-image-ocr", f"Đọc ảnh {media}\\hoadon.png và cho biết tổng tiền.", [{"kind": "summary_contains", "any": ["4.250.000", "4,250,000", "4250000"]}])
if (media / "noi.wav").is_file():
    task("life-audio", f"Nghe file ghi âm {media}\\noi.wav và cho biết người nói hẹn họp vào ngày nào, mấy giờ.",
         [{"kind": "summary_regex", "pattern": r"(?i)(friday|thứ sáu).*(3|three|ba)|(3|three|ba).*(friday|thứ sáu)"}])
if (media / "clip.mp4").is_file():
    task("life-video", f"Xem video {media}\\clip.mp4 và mô tả ngắn gọn có gì trong video.",
         [{"kind": "summary_regex", "pattern": r"(?i)(xin chào|chữ|chấm|tròn|circle|text)"}])

out = Path(__file__).parent / "tasks_life.json"
out.write_text(json.dumps({"tasks": T}, ensure_ascii=False, indent=1), encoding="utf-8")
print(len(T), "tasks ->", out)
