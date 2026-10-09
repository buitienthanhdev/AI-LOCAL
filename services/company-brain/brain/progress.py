"""Tiến độ học: bao nhiêu % file CẦN HỌC đã được mở ra đọc nội dung, theo từng loại tệp.
Mẫu số chỉ gồm bậc 1-2 (tài liệu/media/mã nguồn của người dùng). Bậc 3 (thư viện, phần mềm cài sẵn) chỉ lập danh mục
để tìm theo tên — báo riêng, không làm phần trăm tụt oan."""
from .catalog import connect

# (nhãn, màu, các đuôi) — thứ tự = thứ tự hiển thị trên thanh
TYPE_GROUPS = [
    ("Word", "#2b6cb0", {".docx"}),
    ("Excel / CSV", "#2f855a", {".xlsx", ".csv"}),
    ("PDF", "#c53030", {".pdf"}),
    ("PowerPoint", "#dd6b20", {".pptx"}),
    ("Markdown", "#4a5568", {".md"}),
    ("Text", "#718096", {".txt"}),
    ("Python", "#d69e2e", {".py", ".ipynb"}),
    ("JavaScript / TS", "#ecc94b", {".js", ".ts", ".tsx", ".jsx"}),
    ("Web (HTML/CSS/PHP)", "#e53e9e", {".html", ".css", ".php"}),
    ("Code khác", "#805ad5", {".java", ".cs", ".cpp", ".c", ".h", ".go", ".rs", ".sql", ".sh", ".ps1", ".bat"}),
    ("Cấu hình", "#a0aec0", {".yaml", ".yml", ".xml", ".ini", ".toml"}),
    ("Ảnh", "#38b2ac", {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}),
    ("Audio", "#9f7aea", {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".wma", ".opus"}),
    ("Video", "#f56565", {".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".m4v"}),
]
_EXT2GROUP = {e: g[0] for g in TYPE_GROUPS for e in g[2]}


def compute(detail: bool = True) -> dict:
    conn = connect(readonly=True)
    rows = conn.execute("""SELECT ext, COUNT(*) n, SUM(indexed_sha256 = sha256) learned,
                                  SUM(extract_error IS NOT NULL) failed, SUM(size) bytes
                           FROM files WHERE status='active' AND tier IN (1,2) GROUP BY ext""").fetchall()
    cat_only = conn.execute("SELECT COUNT(*) FROM files WHERE status='active' AND tier=3").fetchone()[0]
    conn.close()
    total = sum(r["n"] for r in rows)
    learned = sum(r["learned"] or 0 for r in rows)
    failed = sum(r["failed"] or 0 for r in rows)
    out = {"total": total, "learned": learned, "failed": failed, "catalog_only": cat_only,
           # file lỗi (hỏng/mã hóa) không thể học => không tính vào mẫu số
           "percent": round(100 * learned / max(1, total - failed), 1)}
    if not detail:
        return out
    groups = {g[0]: {"label": g[0], "color": g[1], "count": 0, "learned": 0, "failed": 0, "bytes": 0, "exts": {}}
              for g in TYPE_GROUPS}
    for r in rows:
        g = groups.get(_EXT2GROUP.get(r["ext"], ""), None)
        if g is None:
            continue
        g["count"] += r["n"]
        g["learned"] += r["learned"] or 0
        g["failed"] += r["failed"] or 0
        g["bytes"] += r["bytes"] or 0
        g["exts"][r["ext"]] = r["n"]
    for g in groups.values():
        g["share"] = round(100 * g["count"] / max(1, total), 1)
        g["percent"] = round(100 * g["learned"] / max(1, g["count"] - g["failed"]), 1)
    out["types"] = [g for g in groups.values() if g["count"]]
    return out
