"""Tiến trình huấn luyện LoRA (peft + trl). CHƯA được kiểm chứng trên máy này (thiếu PyTorch/PEFT) — chỉ được train() gọi
khi preflight đạt. Đọc config JSON, huấn luyện trên train.jsonl, đánh giá loss trên eval.jsonl, lưu adapter."""
import json
import sys
from pathlib import Path


def main(cfg_path: str):
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    import torch
    from datasets import load_dataset
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import SFTConfig, SFTTrainer

    tok = AutoTokenizer.from_pretrained(cfg["base"])
    model = AutoModelForCausalLM.from_pretrained(
        cfg["base"], quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16,
                                                            bnb_4bit_quant_type="nf4"), device_map="auto")
    ds = load_dataset("json", data_files={"train": f"{cfg['dataset_dir']}/train.jsonl",
                                          "eval": f"{cfg['dataset_dir']}/eval.jsonl"})
    trainer = SFTTrainer(
        model=model, processing_class=tok, train_dataset=ds["train"], eval_dataset=ds["eval"],
        peft_config=LoraConfig(r=cfg["lora_r"], lora_alpha=cfg["lora_alpha"], target_modules="all-linear",
                               task_type="CAUSAL_LM"),
        args=SFTConfig(output_dir=cfg["output_dir"], num_train_epochs=cfg["epochs"], learning_rate=cfg["lr"],
                       per_device_train_batch_size=cfg["batch"], gradient_accumulation_steps=cfg["grad_accum"],
                       max_length=cfg["max_len"], eval_strategy="epoch", save_strategy="epoch", logging_steps=10,
                       bf16=True, report_to=[]))
    trainer.train()
    trainer.save_model(cfg["output_dir"] + "/adapter")
    print("DONE")


if __name__ == "__main__":
    main(sys.argv[1])
