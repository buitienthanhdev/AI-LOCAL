"""Huấn luyện model (LoRA/QLoRA) — CHỈ chạy khi đủ điều kiện, và KHÔNG tự đưa vào production.

preflight() kiểm tra từng điều kiện và nêu rõ cái gì đang thiếu. Máy này hiện chưa có PyTorch/PEFT, và model
chạy qua Ollama (GGUF) không thể huấn luyện trực tiếp: cần bản trọng số gốc (Hugging Face) của model nền.
Vì vậy train() trả về 'blocked' kèm lý do thật, thay vì giả vờ train. Khi điều kiện đủ, train() chạy train_worker (peft+trl),
sau đó model mới phải được đăng ký như 'candidate' và đi qua benchmark -> canary -> production (registry.py)."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import learning
from .config import DATA_DIR

BASE_MODEL_PATH = os.environ.get("BRAIN_TRAIN_BASE")   # thư mục HF của model nền (vd. Qwen3-4B-Instruct)
MIN_FREE_VRAM_GB = 10


def _free_vram_gb() -> float | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=memory.free", "--format=csv,noheader,nounits"], capture_output=True,
                             text=True, timeout=10).stdout.strip().splitlines()[0]
        return round(float(out) / 1024, 1)
    except (subprocess.SubprocessError, ValueError, IndexError):
        return None


def preflight() -> dict:
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    for mod in ("torch", "transformers", "peft", "trl", "datasets", "bitsandbytes"):
        add(f"thư viện {mod}", importlib.util.find_spec(mod) is not None,
            "đã cài" if importlib.util.find_spec(mod) else f"chưa cài (pip install {mod})")
    add("model nền (trọng số HF)", bool(BASE_MODEL_PATH and Path(BASE_MODEL_PATH).exists()),
        BASE_MODEL_PATH or "chưa đặt BRAIN_TRAIN_BASE (model Ollama dạng GGUF không huấn luyện được trực tiếp)")
    vram = _free_vram_gb()
    add(f"VRAM trống ≥ {MIN_FREE_VRAM_GB} GB", vram is not None and vram >= MIN_FREE_VRAM_GB,
        f"{vram} GB trống" if vram is not None else "không đọc được GPU")
    ds = learning.list_datasets()
    last = ds[-1] if ds else None
    add("dataset đạt cổng chất lượng", bool(last and last["gate"]["pass"]),
        (last["gate"]["reason"] or f"{last['version']}: {last['gate']['train_count']} mẫu") if last else "chưa có dataset")
    return {"ready": all(c["ok"] for c in checks), "checks": checks, "dataset": last["version"] if last else None}


def train(dataset_version: str | None = None) -> dict:
    pf = preflight()
    if not pf["ready"]:
        return {"started": False, "status": "blocked", "blockers": [c for c in pf["checks"] if not c["ok"]]}
    ds = dataset_version or pf["dataset"]
    out = DATA_DIR / "models" / f"lora-{ds}"
    out.mkdir(parents=True, exist_ok=True)
    cfg = {"base": BASE_MODEL_PATH, "dataset_dir": str(DATA_DIR / "datasets" / ds), "output_dir": str(out),
           "lora_r": 16, "lora_alpha": 32, "lr": 1e-4, "epochs": 2, "max_len": 1024, "batch": 1, "grad_accum": 16}
    (out / "config.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    p = subprocess.Popen([sys.executable, "-m", "brain.train_worker", str(out / "config.json")],
                         stdout=open(out / "train.log", "w"), stderr=subprocess.STDOUT)
    return {"started": True, "status": "training", "pid": p.pid, "output_dir": str(out),
            "next": "Khi xong: chuyển adapter sang GGUF, nạp vào Ollama, rồi chạy DEPLOY_MODEL (benchmark -> canary)."}
