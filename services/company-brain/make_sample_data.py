"""Sinh bộ dữ liệu mẫu (giả lập file server công ty sơn) để thử MVP."""
import shutil
from datetime import datetime
from pathlib import Path

import docx
import openpyxl
import pptx

ROOT = Path(__file__).parent / "sample_data"


def make_docx(path: Path, author: str, editor: str, title: str, paras: list[str]):
    d = docx.Document()
    d.add_heading(title, 1)
    for p in paras:
        d.add_paragraph(p)
    cp = d.core_properties
    cp.author, cp.last_modified_by, cp.title = author, editor, title
    cp.created = datetime(2026, 8, 14, 9, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(path)


def main():
    shutil.rmtree(ROOT, ignore_errors=True)

    make_docx(ROOT / "RND/M500/M500_Viscosity_Investigation.docx", "Nguyễn Văn A", "Trần Thị Ly",
              "Điều tra độ nhớt M500 bị tụt", [
                  "Hiện tượng: batch Long An 01 sau 7 ngày lưu kho độ nhớt giảm từ 105 KU xuống 88 KU.",
                  "Nguyên nhân chính: chất làm đặc HEC bị phân hủy do nhiễm vi sinh, pH giảm từ 8.5 xuống 7.6.",
                  "Nguyên nhân phụ: lượng nước rửa bồn dư khoảng 1.2%.",
                  "Khắc phục: bổ sung biocide 0.15%, kiểm soát pH 8.5 - 9.0, vệ sinh bồn trước khi pha.",
              ])
    make_docx(ROOT / "RND/M500/M500_Resin_Variation.docx", "Trần Thị Ly", "Trần Thị Ly",
              "Thử nghiệm thay đổi nhựa M500", [
                  "So sánh Acronal PS 673 (BASF) với nhựa thay thế giá rẻ hơn 12%.",
                  "Kết quả: Acronal PS 673 cho độ bám dính và chống thấm tốt hơn; giữ Acronal PS 673.",
              ])

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Formula v12"
    ws.append(["Nguyên liệu", "Nhà cung cấp", "% khối lượng"])
    for row in [("Nước", "-", 32.0), ("Acronal PS 673", "BASF", 28.5), ("TiO2", "Chemours", 18.0),
                ("CaCO3", "Local", 17.2), ("HEC", "Ashland", 0.45), ("Biocide", "Lonza", 0.15)]:
        ws.append(row)
    wb.properties.creator, wb.properties.lastModifiedBy = "Nguyễn Văn A", "Lê Văn C"
    wb.properties.title = "Công thức M500 v12"
    (ROOT / "RND/M500").mkdir(parents=True, exist_ok=True)
    wb.save(ROOT / "RND/M500/M500_Formula_v12.xlsx")

    lab = openpyxl.Workbook()
    ws = lab.active
    ws.title = "Lab history"
    ws.append(["Ngày", "Thí nghiệm", "Người thực hiện", "Độ nhớt (KU)", "pH", "Ghi chú"])
    for row in [("2026-08-14", "Long An Batch 01", "Thành", 105, 8.5, "Đạt"),
                ("2026-08-21", "Long An Batch 01 - sau 7 ngày", "Thành", 88, 7.6, "Tụt nhớt"),
                ("2026-08-28", "Resin variation", "Ly", 103, 8.6, "Đạt"),
                ("2026-09-04", "Biocide 0.15%", "Thành", 104, 8.7, "Ổn định sau 14 ngày")]:
        ws.append(row)
    lab.properties.creator = "Thành"
    lab.save(ROOT / "RND/M500/M500_Lab_History.xlsx")

    prs = pptx.Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Họp tối ưu công thức OneCoat"
    s.placeholders[1].text = ("Mục tiêu giảm giá thành 8%\nGiữ Acronal PS 673\n"
                              "Thêm biocide để chống tụt nhớt khi lưu kho")
    prs.core_properties.author = "Phạm Minh D"
    (ROOT / "RND/OneCoat").mkdir(parents=True, exist_ok=True)
    prs.save(ROOT / "RND/OneCoat/Formula_Optimization_Meeting.pptx")

    (ROOT / "Sales/LongAn").mkdir(parents=True, exist_ok=True)
    (ROOT / "Sales/LongAn/customer_feedback.md").write_text(
        "# Phản hồi khách hàng Long An\n\nĐại lý báo sơn M500 bị loãng sau khi mở thùng, "
        "khách phải khuấy lâu. Yêu cầu R&D kiểm tra lô tháng 8.\n", encoding="utf-8")

    make_docx(ROOT / "HR/Salary_2026.docx", "Phòng Nhân sự", "Phòng Nhân sự", "Bảng lương 2026 (MẬT)", [
        "Nguyễn Văn A: 45.000.000 VND", "Trần Thị Ly: 38.000.000 VND"])

    # Bản copy để kiểm tra phát hiện trùng lặp
    (ROOT / "Sales/LongAn/copy").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "RND/M500/M500_Formula_v12.xlsx", ROOT / "Sales/LongAn/copy/formula_final.xlsx")
    print(f"Đã tạo dữ liệu mẫu tại {ROOT}")


if __name__ == "__main__":
    main()
