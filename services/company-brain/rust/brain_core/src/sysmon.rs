//! Đo tài nguyên native cho governor: GPU qua NVML (nvml.dll của driver NVIDIA) — thay vì tạo tiến trình `nvidia-smi`
//! mỗi giây. NVML khởi tạo MỘT lần; không có GPU NVIDIA/driver => None (Python lùi về nvidia-smi).
use nvml_wrapper::Nvml;
use std::sync::OnceLock;

static NVML: OnceLock<Option<Nvml>> = OnceLock::new();

fn nvml() -> Option<&'static Nvml> {
    NVML.get_or_init(|| Nvml::init().ok()).as_ref()
}

pub struct GpuStat {
    pub util_percent: u32,
    pub mem_used_mb: u64,
    pub mem_total_mb: u64,
}

/// % sử dụng GPU 0 và VRAM. None nếu không đọc được.
pub fn gpu() -> Option<GpuStat> {
    let dev = nvml()?.device_by_index(0).ok()?;
    let util = dev.utilization_rates().ok()?;
    let mem = dev.memory_info().ok()?;
    Some(GpuStat { util_percent: util.gpu, mem_used_mb: mem.used / 1_048_576, mem_total_mb: mem.total / 1_048_576 })
}

#[cfg(test)]
mod tests {
    #[test]
    fn gpu_reads_or_returns_none() {
        if let Some(g) = super::gpu() {
            assert!(g.util_percent <= 100 && g.mem_total_mb > 0 && g.mem_used_mb <= g.mem_total_mb);
        }
    }
}
