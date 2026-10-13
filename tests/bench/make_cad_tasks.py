"""Generates tests/bench/tasks_cad.json: 3D parts through cad.build (STEP exists, size and validity in the answer)."""
import json
from pathlib import Path

D = "{dir}"
T: list[dict] = []


def task(tid, prompt, checks):
    T.append({"id": tid, "category": "cad", "prompt": prompt,
              "checks": [{"kind": "status", "is": "completed"}, {"kind": "tool_used", "name": "cad.build"}, *checks]})


task("cad-bracket",
     "Thiết kế mô hình 3D giá đỡ chữ L: đế 60 x 30 mm dày 6 mm, tường đứng cao 30 mm dày 6 mm, đế có 2 lỗ tròn đường kính 8 mm. "
     "Xuất file STEP và cho biết thể tích.",
     [{"kind": "summary_regex", "pattern": r"(?i)(20[ .,]?9\d\d|21[ .,]?0\d\d|60\s*[x×]\s*30\s*[x×]\s*36)"},
      {"kind": "summary_regex", "pattern": r"(?i)\.step"}])
task("cad-tube", "Tạo mô hình 3D ống tròn đường kính ngoài 20 mm, đường kính trong 14 mm, dài 40 mm để in 3D, xuất STL và STEP, và cho biết thể tích.",
     [{"kind": "summary_regex", "pattern": r"(?i)(6[ .,]?4\d\d|20\s*[x×]\s*20\s*[x×]\s*40)"}])
task("cad-box-lid", "Thiết kế vỏ hộp 3D hình chữ nhật 80 x 50 x 30 mm, thành và đáy đều dày 2 mm, hở mặt trên. Cho biết thể tích vật liệu.",
     [{"kind": "summary_regex", "pattern": r"(?i)(22[ .,]?[01]\d\d)"}])

out = Path(__file__).parent / "tasks_cad.json"
out.write_text(json.dumps({"tasks": T}, ensure_ascii=False, indent=1), encoding="utf-8")
print(len(T), "tasks ->", out)
