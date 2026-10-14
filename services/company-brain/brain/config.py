"""Cấu hình chung cho Company Brain MVP."""
from pathlib import Path

import os

ROOT = Path(__file__).resolve().parent.parent
# BRAIN_DATA_DIR: thư mục dữ liệu riêng (dùng cho test, không đụng dữ liệu thật)
DATA_DIR = Path(os.environ.get("BRAIN_DATA_DIR") or ROOT / "data")
CATALOG_DB = DATA_DIR / "catalog.db"      # Source of truth: SQLite (WAL) — 1 máy, 1 bên ghi chính; schema thuần SQL
QDRANT_DIR = DATA_DIR / "qdrant"          # chỉ dùng khi QDRANT_MODE = "local"
COLLECTION = "chunks"
# server: Qdrant server cục bộ (data/qdrant-server/qdrant.exe, tự khởi động) · local: nhúng trong Python (chậm khi
# > 20.000 điểm) · memory: trong RAM (test)
QDRANT_MODE = os.environ.get("BRAIN_QDRANT", "server")

# ---- Rust core (brain_core, PyO3) — bật từng phần qua biến môi trường; "0" = dùng bản Python cũ (rollback).
# Xem docs/MIGRATION_MAP.md. Bật mặc định chỉ sau khi scripts/parity_check.py ĐẠT trên dữ liệu thật.
USE_RUST_NORM = os.environ.get("USE_RUST_NORM", "1") == "1"
USE_RUST_SCANNER = os.environ.get("USE_RUST_SCANNER", "1") == "1"
USE_RUST_GOVERNOR = os.environ.get("USE_RUST_GOVERNOR", "1") == "1"  # đo GPU qua NVML thay vì chạy nvidia-smi mỗi giây
RUST_SCAN_THREADS = int(os.environ.get("RUST_SCAN_THREADS", "4"))  # duyệt/băm song song (I/O) — không chiếm hết CPU
QDRANT_URL = "http://127.0.0.1:6333"
QDRANT_EXE = DATA_DIR.parent / "data" / "qdrant-server" / "qdrant.exe" if not os.environ.get("BRAIN_DATA_DIR")     else ROOT / "data" / "qdrant-server" / "qdrant.exe"

OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "qwen3-embedding:0.6b"
CHAT_MODEL = "gemma4:12b"   # đa phương thức: chữ + ảnh + audio, có tool calling
MEDIA_MODEL = CHAT_MODEL     # model đọc ảnh/audio/video
# Bản gemma4:12b hiện tại kèm model "draft" làm Ollama 0.34 lỗi khi nạp => tắt draft.
LLM_OPTIONS = {"draft_num_predict": 0}

# ---- Bậc học (tier): mọi file đều được lập DANH MỤC (tên, nơi, ai tạo...), nhưng chỉ mở ra ĐỌC NỘI DUNG
# (tốn GPU) với bậc 1-2. Bậc 3 = thư viện/phần mềm cài sẵn: tìm được theo tên, không embed.
# So khớp trên đường dẫn viết thường, dấu "\\" đã đổi thành "/"
LIB_MARKERS = (
    "/site-packages/", "/dist-packages/", "/node_modules/", "/.venv/", "/venv/", "/python_embeded/",
    "/anaconda3/", "/.conda/", "/.bun/", "/.nuget/", "/.vscode/", "/.cache/", "/.cursor/", "/.codex/",
    "/.gemini/", "/.antigravity-ide/", "/.aionui-web/", "/.dotnet/", "/appdata/", "/program files/",
    "/program files (x86)/", "/programdata/", "/windows/", "/windows kits/", "/$recycle.bin/", "/vendor/",
    "/external/", "/third_party/", "/custom_nodes/", "/pkgs/", "/packages/", "/__pycache__/",
    "/xampp/php/", "/xampp/phpmyadmin/", "/xampp/perl/", "/xampp/tomcat/", "/xampp/mercurymail/",
    "/.git/", "/sketchup 20", "/autocad 20", "/comfyui/comfy/", "/comfyui/blueprints/", "/docs/_build/",
    "/dist/", "/build/", "/.next/", "/.gradle/", "/.m2/", "/python314/lib/", "/python314/doc/",
    "/python314/tcl/", "/python314/include/", "/python313/", "/python312/", "/python311/",
    # bộ công cụ Rust (~60.000 file tài liệu/mã nguồn của toolchain + crate tải về) và thư mục build
    "/.rustup/", "/.cargo/registry/", "/.cargo/git/", "/rust/target/", "/target/release/", "/target/debug/",
    # đầu ra do chính Trợ lý sinh ra (ảnh chụp, tác phẩm, log, bản tham chiếu): tìm được theo tên, không đọc/embed
    "/local-gemini-agent/artifacts/", "/local-gemini-agent/references/", "/local-gemini-agent/runtime/",
    "/local-gemini-agent/logs/", "/local-gemini-agent/memory/", "/local-gemini-agent/third_party/",
)
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".php", ".java", ".cs", ".cpp", ".c", ".h",
            ".go", ".rs", ".sql", ".sh", ".ps1", ".bat", ".yaml", ".yml", ".xml", ".ini", ".toml", ".ipynb"}
MAX_CODE_KB = 200          # file code lớn hơn thì chỉ lập danh mục
MAX_CHUNKS_PER_FILE = 60
VERIFY_ANSWERS = True      # tự kiểm chứng câu trả lời với bằng chứng trước khi đưa ra
VERIFY_MAX_EVIDENCE_CHARS = 7000   # một file không được chiếm quá nhiều điểm trong kho vector

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200
MAX_FILE_MB = 50
MAX_TEXT_CHARS = 300_000   # cắt text quá dài (vd. file log/json khổng lồ) trước khi embed

# Chỉ loại tài liệu người viết. .json/.log phần lớn là file máy sinh ra => bỏ để đỡ tốn tài nguyên.
SUPPORTED_EXT = {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".csv",
                 # ảnh / audio / video: đọc bằng Gemma 4 (tốn GPU => chỉ xử lý khi máy rảnh)
                 ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff",
                 ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".wma", ".opus",
                 ".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".m4v",
                 # mã nguồn / cấu hình (thư viện như node_modules, site-packages đã bị loại ở EXCLUDE_DIRS)
                 ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".php", ".java", ".cs", ".cpp", ".c", ".h",
                 ".go", ".rs", ".sql", ".sh", ".ps1", ".bat", ".yaml", ".yml", ".xml", ".ini", ".toml", ".ipynb"}
MAX_MEDIA_MB = 500         # giới hạn riêng cho audio/video
MIN_IMAGE_KB = 30          # bỏ icon/ảnh giao diện nhỏ
VIDEO_FRAMES = 6           # số khung hình lấy từ mỗi video
AUDIO_CHUNK_SEC = 30       # cắt audio thành đoạn 30 giây để chép lời
AUDIO_MAX_SEC = 20 * 60    # chỉ chép tối đa 20 phút đầu mỗi file
MEDIA_KEEP_ALIVE = "2m"
MAX_ATTACH_MB = 15         # file đính kèm khi hỏi (ảnh/audio) trên web/nút nổi

# FULL QUYỀN ĐỌC: quét mọi thư mục (kể cả AppData, Program Files, thư mục ẩn, thư viện code).
# Chỉ bỏ: Thùng rác, vùng hệ thống không đọc được, và thư mục dữ liệu của chính Brain (xem scanner).
EXCLUDE_DIRS = {"$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws", "$sysreset"}
EXCLUDE_PATHS: set[str] = set()
# Theo dõi REALTIME bỏ qua các nơi file tạm/cache ghi liên tục (vẫn được quét định kỳ 6 giờ/lần),
# để GPU không phải đọc lại hàng nghìn file cache trình duyệt mỗi phút.
# (so khớp trên đường dẫn viết thường, dấu "\" đã đổi thành "/")
WATCH_IGNORE = ["/appdata/local/temp/", "/cache", "/code cache/", "/gpucache/", "/logs/",
                "/windows/temp/", "/windows/logs/", "/prefetch/", "/softwaredistribution/",
                "/winsxs/", "/.git/", "/node_modules/", "/__pycache__/"]

# ---- Web nội bộ ----
WEB_HOST = "0.0.0.0"       # nghe trên mọi card mạng => máy khác trong LAN truy cập được
WEB_PORT = 8765
USERS_FILE = DATA_DIR / "users.json"
ADMIN_GROUP = "admin"      # thấy mọi thứ, kể cả file riêng tư của máy này; truy cập từ chính máy = admin
SESSION_HOURS = 12
SCAN_EVERY_HOURS = 6       # server tự quét + index lại định kỳ
SCAN_ALL_DRIVES = True     # True: quét mọi ổ cứng (nặng, nhiều rác phần mềm); False: chỉ SCAN_ROOTS + SHARED_ROOTS

# ---- Tiết kiệm tài nguyên ----
# Thư mục cá nhân được quét (riêng tư, chỉ admin thấy). Thêm/bớt tùy ý.
SCAN_ROOTS = [Path.home() / "Desktop", Path.home() / "Documents", Path.home() / "Downloads"]
RESOURCE_LIMIT = 0.6       # học ngầm chỉ dùng tối đa 60% tài nguyên máy (CPU chặn cứng, GPU theo chu kỳ làm/nghỉ)
RAM_PAUSE_AT = 90          # RAM máy dùng >= 90% thì tạm dừng học
# Học THÍCH ỨNG theo tài nguyên đang trống (xem governor.py):
ACTIVE_MAX_DUTY = 0.35     # đang dùng máy mà còn trống nhiều: học tối đa ~35% thời gian
ACTIVE_RESERVE = 25        # đang dùng máy: luôn chừa ≥ 25% tài nguyên cho người dùng
IDLE_RESERVE = 5           # máy rảnh: chừa 5%
TRICKLE_DUTY = 0.03        # máy bận: học "nhỏ giọt" ~3% thời gian
MAX_REST_S = 120           # mỗi lần nghỉ tối đa 2 phút
FORCE_RAM_PAUSE_AT = 97    # chế độ tối đa: chỉ tạm dừng khi RAM gần cạn hẳn
FORCE_WORKERS = 3          # chế độ tối đa: số file xử lý song song
IDLE_MINUTES = 3           # "máy rảnh" = không chạm chuột/phím ≥ 3 phút => được học nhiều hơn (tới RESOURCE_LIMIT)           # chỉ index khi không chạm chuột/bàn phím N phút; đang dùng máy => tạm dừng
PAUSE_BETWEEN_FILES = 0.3  # giây nghỉ giữa các file để không chiếm GPU liên tục
EMBED_KEEP_ALIVE = "30s"   # Ollama nhả VRAM của model embedding ngay sau khi dùng
CHAT_KEEP_ALIVE = "5m"     # nhả model chat (~9 GB VRAM) sau 5 phút không ai hỏi

# ---- Học tức thời (watcher) ----
DEBOUNCE_SEC = 5           # file phải "yên" 5 giây mới xử lý (Save/copy bắn hàng chục event)
REALTIME_INDEX_MAX = 3     # <= 3 file đổi cùng lúc => index ngay; nhiều hơn => đợi máy rảnh

# ---- Fact Store ----
# FACT_ROOTS (cuối file): chỉ trích fact từ tài liệu công ty, không đụng file cá nhân của máy.
FACT_EXT = {".xlsx", ".docx", ".pdf", ".pptx", ".md", ".csv", ".txt"}
FACT_MAX_CHARS = 12_000    # đưa tối đa ngần này ký tự vào LLM mỗi file
# Độ tin cậy gốc theo loại nguồn (khớp từ khóa trong tên file, ưu tiên từ trên xuống)
SOURCE_WEIGHTS = [
    (("spec", "master", "tieu chuan", "tiêu chuẩn", "approved"), 0.95),
    (("formula", "công thức", "cong thuc"), 0.9),
    (("lab", "test", "report", "báo cáo", "investigation", "điều tra"), 0.85),
    (("meeting", "họp", "minutes", "note", "ghi chú"), 0.65),
    (("feedback", "phản hồi", "chat", "draft", "nháp"), 0.4),
]
DEFAULT_SOURCE_WEIGHT = 0.5
# Ngưỡng trạng thái
TRUSTED_AT = 0.8
USABLE_AT = 0.5

# ---- Phân quyền ----
# Thư mục CHIA SẺ cho mọi người trong LAN. File ở bất cứ đâu khác trên máy => "private" (chỉ admin).
SHARED_ROOTS = [ROOT / "sample_data"]

# Bên trong thư mục chia sẻ: thư mục cấp 1 -> nhóm được phép đọc. Không khớp => "all". Bản thật sẽ lấy từ ACL NTFS / nhóm AD.
ACL_RULES = {
    "HR": ["hr", "board"],
    "Finance": ["finance", "board"],
}

# Thư mục được trích fact (mặc định = thư mục chia sẻ)
FACT_ROOTS = list(SHARED_ROOTS)
