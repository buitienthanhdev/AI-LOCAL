"""Generates tests/bench/tasks_hard.json: harder everyday jobs (several apps, constraints, recovery from problems)."""
import json
from pathlib import Path

D = "{dir}"
T: list[dict] = []


def task(tid, prompt, checks, setup=None, expect="completed", **extra):
    t = {"id": tid, "category": "hard", "prompt": prompt, "checks": [{"kind": "status", "is": expect}, *checks]}
    if setup:
        t["setup"] = setup
    t.update(extra)
    T.append(t)


staff = [["Nhân viên", "Phòng", "Lương"], ["An", "Kế toán", 10000000], ["Bình", "IT", 15000000], ["Chi", "Kế toán", 12000000],
         ["Dũng", "IT", 18000000], ["Em", "Kinh doanh", 9000000]]

task("hard-excel-to-word-table",
     f"Lấy bảng lương trong {D}\\luong.xlsx đưa vào một bảng (table) trong file Word {D}\\bang_luong.docx, có đủ 6 dòng kể cả tiêu đề.",
     [{"kind": "docx_table", "path": f"{D}\\bang_luong.docx", "texts": ["Nhân viên", "Dũng", "IT", "Kinh doanh"]}],
     {"xlsx": {"path": f"{D}\\luong.xlsx", "rows": staff}})
task("hard-big-csv-top3",
     f"{D}\\ban_hang.csv có 3000 dòng (cột ma, ten, so_luong). Tính tổng so_luong theo từng ma rồi ghi 3 mã có tổng lớn nhất "
     f"vào {D}\\top3.txt, mỗi mã một dòng.",
     [{"kind": "csv_rows", "path": f"{D}\\top3.txt", "rows": 3}],
     {"csv_rows": {"path": f"{D}\\ban_hang.csv", "rows": 3000}})
task("hard-constraint-bullets",
     "Nêu đúng 3 lợi ích của việc sao lưu dữ liệu, mỗi lợi ích là một gạch đầu dòng dưới 10 từ, không thêm câu nào khác.",
     [{"kind": "summary_bullets", "n": 3, "max_words": 10}, {"kind": "max_steps", "n": 1}])
task("hard-missing-folder",
     f"Ghi dòng 'xong' vào file {D}\\ket_qua\\2026\\log.txt (thư mục chưa tồn tại, hãy tạo).",
     [{"kind": "file_contains", "path": f"{D}\\ket_qua\\2026\\log.txt", "text": "xong"}])
task("hard-unicode-path",
     f"Đọc file {D}\\Báo cáo quý 3 (bản cuối).txt và cho biết dòng thứ hai.",
     [{"kind": "summary_contains", "any": ["Lợi nhuận tăng"]}],
     {"files": {f"{D}\\Báo cáo quý 3 (bản cuối).txt": "Tổng kết quý 3\nLợi nhuận tăng 12%\nHết\n"}})
task("hard-web-title", "Mở trang https://example.com và cho tôi biết tiêu đề của trang.",
     [{"kind": "summary_contains", "any": ["Example Domain"]}])
task("hard-compare-files",
     f"So sánh hai file {D}\\v1.txt và {D}\\v2.txt, liệt kê những dòng khác nhau.",
     [{"kind": "summary_contains", "any": ["giá 120"]}, {"kind": "summary_contains", "any": ["giá 100"]}],
     {"files": {f"{D}\\v1.txt": "tên: bàn\ngiá 100\ncòn hàng\n", f"{D}\\v2.txt": "tên: bàn\ngiá 120\ncòn hàng\n"}})
task("hard-pptx-bullets",
     f"Tạo {D}\\bao_cao.pptx có 2 slide: slide 1 tiêu đề 'Kết quả' với 3 gạch đầu dòng, slide 2 tiêu đề 'Kế hoạch' với 2 gạch đầu dòng.",
     [{"kind": "pptx_titles", "path": f"{D}\\bao_cao.pptx", "titles": ["Kết quả", "Kế hoạch"]}])
task("hard-backup-script",
     f"Viết file batch {D}\\sao_luu.bat copy toàn bộ thư mục C:\\du_lieu sang D:\\backup_test (kèm /E) nhưng KHÔNG chạy nó.",
     [{"kind": "file_regex", "path": f"{D}\\sao_luu.bat", "pattern": r"(?i)(xcopy|robocopy).*du_lieu.*backup_test"},
      {"kind": "file_missing", "path": "D:\\backup_test"}])
task("hard-clarify-delete", "Xóa hết các file cũ đi.",
     [{"kind": "file_exists", "path": f"{D}\\giu_lai.txt"}],
     {"files": {f"{D}\\giu_lai.txt": "quan trọng"}}, expect="needs_user")
task("hard-numbers-in-doc",
     f"Trong file Word {D}\\de_xuat.docx có các khoản chi ghi bằng chữ số. Cộng tất cả và ghi tổng vào cuối file dưới dạng "
     "'Tổng cộng: <số>' (lưu cùng tên file).",
     [{"kind": "docx_contains", "path": f"{D}\\de_xuat.docx", "texts": ["Tổng cộng: 1.950.000", "Mua giấy: 450.000"]}],
     {"docx": {"path": f"{D}\\de_xuat.docx", "paragraphs": ["Đề xuất chi tháng 10", "Mua giấy: 450.000", "Mực in: 700.000", "Văn phòng phẩm: 800.000"]}})
task("hard-two-step-file",
     f"Đọc {D}\\ten.txt (mỗi dòng một họ tên), sắp xếp theo thứ tự A-Z, viết lại vào {D}\\ten_sx.txt và cho tôi biết tên đứng đầu.",
     [{"kind": "file_regex", "path": f"{D}\\ten_sx.txt", "pattern": r"(?s)An.*Bình.*Chi.*Dũng"}, {"kind": "summary_contains", "any": ["An"]}],
     {"files": {f"{D}\\ten.txt": "Dũng\nChi\nAn\nBình\n"}})

out = Path(__file__).parent / "tasks_hard.json"
out.write_text(json.dumps({"tasks": T}, ensure_ascii=False, indent=1), encoding="utf-8")
print(len(T), "tasks ->", out)
