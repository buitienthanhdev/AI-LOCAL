"""Nhịp học thích ứng: máy trống nhiều thì học nhiều hơn xíu, bận thì nhỏ giọt, 'Học ngay' thì tối đa."""
import threading
import time

import pytest

from conftest import scan_shared, write_docx


@pytest.fixture
def gov(monkeypatch):
    from brain import governor
    monkeypatch.setattr(governor, "_force", False)
    monkeypatch.setattr(governor, "ram_load", lambda: 10)
    return governor


def _set(gov, monkeypatch, *, idle: bool, gpu: float, cpu: float = 0.0):
    monkeypatch.setattr(gov, "idle_seconds", lambda: 9999.0 if idle else 0.0)
    gov._ext.update(gpu=gpu, cpu=cpu)


@pytest.mark.parametrize("idle,gpu,cpu,expected", [
    (False, 10, 10, 0.35),    # đang dùng máy nhưng trống nhiều => học vừa phải (trần 35%)
    (False, 50, 20, 0.25),    # trống một nửa => ít hơn
    (False, 70, 0, 0.05),     # khá bận => rất ít
    (False, 90, 90, 0.03),    # rất bận => nhỏ giọt (sàn 3%)
    (False, 100, 100, 0.03),
    (True, 0, 0, 0.60),       # máy rảnh, không ai tranh => tới 60%
    (True, 80, 0, 0.15),      # máy rảnh nhưng app khác đang chiếm GPU => nhường
    (True, 100, 0, 0.03),
])
def test_target_duty_follows_free_resources(gov, monkeypatch, idle, gpu, cpu, expected):
    _set(gov, monkeypatch, idle=idle, gpu=gpu, cpu=cpu)
    assert gov.target_duty() == pytest.approx(expected, abs=0.011)
    assert gov.state["mode"] == ("idle" if idle else "active")


def test_cpu_load_counts_less_than_gpu(gov, monkeypatch):
    """Việc của bot chủ yếu là GPU: CPU bận 100% (hệ số 0,8) vẫn nhường ít hơn GPU bận 100%."""
    _set(gov, monkeypatch, idle=False, gpu=0, cpu=100)
    assert gov.target_duty() == pytest.approx(0.0 + 0.03 if (100 - 80 - 25) < 0 else 0.0, abs=0.011)
    _set(gov, monkeypatch, idle=False, gpu=0, cpu=50)
    assert gov.target_duty() == pytest.approx(0.35, abs=0.011), "CPU 50% => headroom 60 => (60-25)=35%"


def test_force_is_maximum_performance(gov, monkeypatch):
    _set(gov, monkeypatch, idle=False, gpu=100, cpu=100)   # máy đang bận hết cỡ vẫn học tối đa khi bị cưỡng chế
    caps = []
    monkeypatch.setattr(gov, "_cpu_rate", lambda frac: caps.append(frac) or True)
    gov.set_force(True)
    assert gov.target_duty() == 1.0 and gov.state["mode"] == "max" and caps[-1] == 1.0, "gỡ giới hạn CPU"
    slept = []
    monkeypatch.setattr(gov.time, "sleep", lambda s: slept.append(s))
    gov._last = time.monotonic() - 5
    gov.rest()
    assert sum(slept) == 0, "tối đa: không nghỉ giữa các file"
    gov.set_force(False)
    assert caps[-1] == gov.RESOURCE_LIMIT, "tắt cưỡng chế => trả giới hạn CPU 60%"


def test_rest_length_matches_duty_and_wakes_early(gov, monkeypatch):
    _set(gov, monkeypatch, idle=False, gpu=90, cpu=0)          # duty 3%
    slept = []
    monkeypatch.setattr(gov.time, "sleep", lambda s: slept.append(s))
    gov._last = time.monotonic() - 3.0                          # vừa làm 3 giây
    gov.rest()
    assert sum(slept) == pytest.approx(3 * (1 / 0.03 - 1), rel=0.06), "nhỏ giọt: làm 3s nghỉ ~97s"
    assert gov.state["rest_s"] > 90

    # đang nghỉ dài mà người dùng bấm 'Học ngay' => dậy ngay, không chờ hết nhịp cũ
    slept.clear()
    state = {"t": 0.0}

    def fake_sleep(s):
        slept.append(s)
        state["t"] += s
        if state["t"] >= 5:
            gov._force = True
    monkeypatch.setattr(gov.time, "sleep", fake_sleep)
    gov._force = False
    gov._last = time.monotonic() - 3.0
    gov.rest()
    assert sum(slept) < 8, "dậy sớm khi chuyển sang chế độ tối đa"

    # đang nghỉ mà người dùng rời máy (rảnh) => nhịp tăng lên, dậy sớm hơn
    slept.clear()
    gov._force = False
    gov._ext.update(gpu=0, cpu=0)
    flag = {"t": 0.0}

    def fake_sleep2(s):
        slept.append(s)
        flag["t"] += s
    gov._ext.update(gpu=90, cpu=0)
    gov.idle_seconds = lambda: 9999.0 if flag["t"] > 6 else 0.0
    monkeypatch.setattr(gov.time, "sleep", fake_sleep2)
    gov._last = time.monotonic() - 3.0
    gov.rest()
    assert sum(slept) < 90, "máy chuyển sang rảnh giữa chừng thì nhịp nghỉ rút ngắn"


def test_ram_pause_is_stricter_normally_than_in_force(gov, monkeypatch):
    seq = {"n": 0}

    def ram():
        seq["n"] += 1
        return 93 if seq["n"] < 3 else 20
    monkeypatch.setattr(gov, "ram_load", ram)
    monkeypatch.setattr(gov.time, "sleep", lambda s: None)
    _set(gov, monkeypatch, idle=True, gpu=0)
    gov._last = time.monotonic()
    gov.rest()
    assert seq["n"] >= 3, "thường: RAM ≥ 90% thì tạm dừng tới khi giảm"
    seq["n"] = 0
    gov._force = True
    gov._last = time.monotonic()
    gov.rest()
    assert seq["n"] == 1, "tối đa: 93% RAM chưa phải lý do dừng (ngưỡng 97%)"


def test_real_load_probes_return_sane_values():
    from brain import governor
    governor.cpu_load()
    time.sleep(0.3)
    c = governor.cpu_load()
    assert c is None or 0 <= c <= 100
    g = governor.gpu_load()
    assert g is None or 0 <= g <= 100
    assert 0 <= governor.ram_load() <= 100 and governor.idle_seconds() >= 0


def test_force_indexes_files_in_parallel_without_duplicates(env, gov, monkeypatch):
    from brain import index
    from brain.catalog import connect
    for i in range(12):
        write_docx(env / "RND" / f"d{i}.docx", "A", [f"tài liệu số {i} " * 40])
    scan_shared(env)
    names = set()
    real = index.embed

    def spy(texts, query=False):
        names.add(threading.current_thread().name)
        time.sleep(0.05)
        return real(texts, query)
    monkeypatch.setattr(index, "embed", spy)
    gov._force = True
    st = index.index_all(throttle=lambda: None)
    assert st["indexed"] == 12 and st["errors"] == 0
    c = connect()
    assert c.execute("SELECT COUNT(*) FROM files WHERE indexed_sha256=sha256").fetchone()[0] == 12
    assert c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == index.client().count("chunks").count
    assert any(n.startswith("index") for n in names), f"chế độ tối đa phải chạy song song: {names}"
    assert c.execute("SELECT COUNT(*) FROM (SELECT file_id, idx FROM chunks GROUP BY file_id, idx HAVING COUNT(*)>1)"
                     ).fetchone()[0] == 0
