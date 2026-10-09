"""Đo scanner.scan thật (Rust rồi Python) trên catalog thật — giống một vòng quét định kỳ của bot.

    python scripts/bench_scan.py [root]"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from brain import config, scanner  # noqa: E402

root = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home())
for rust in (True, False):
    config.USE_RUST_SCANNER = rust
    t = time.perf_counter()
    stats = scanner.scan(root, progress=lambda *_: None)
    print(f"{'Rust  ' if rust else 'Python'}: {time.perf_counter() - t:6.1f}s  {stats}", flush=True)
