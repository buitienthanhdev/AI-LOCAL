//! Python binding: `import brain_core`. Chỉ truyền kiểu native (str, list[str]) — không JSON.
use pyo3::exceptions::PyOSError;
use pyo3::prelude::*;
use std::path::Path;

#[pyfunction]
fn norm(s: &str) -> String {
    ::brain_core::text::norm(s)
}

#[pyfunction]
fn plain(s: &str) -> String {
    ::brain_core::text::plain(s)
}

#[pyfunction]
#[pyo3(signature = (text, limit=64))]
fn telex_variants(text: &str, limit: usize) -> std::collections::BTreeSet<String> {
    ::brain_core::text::telex_variants(text, limit)
}

#[pyfunction]
fn syllable_variants(word: &str) -> std::collections::BTreeSet<String> {
    ::brain_core::text::syllable_variants(word)
}

/// Chuẩn hoá cả danh sách trong một lần gọi (đỡ chi phí qua lại Python↔Rust khi xử lý hàng trăm nghìn tên).
#[pyfunction]
fn norm_many(items: Vec<String>) -> Vec<String> {
    items.iter().map(|s| ::brain_core::text::norm(s)).collect()
}

#[pyfunction]
fn hash_file(py: Python<'_>, path: String) -> PyResult<String> {
    py.detach(|| ::brain_core::hash::hash_file(Path::new(&path))).map_err(|e| PyOSError::new_err(e.to_string()))
}

#[pyfunction]
#[pyo3(signature = (paths, threads=4))]
fn hash_files(py: Python<'_>, paths: Vec<String>, threads: usize) -> Vec<Option<String>> {
    py.detach(|| ::brain_core::hash::hash_files(&paths, threads))
}

/// Duyệt thư mục như scanner.scan (xem brain_core::scan). Trả về (entries, repos);
/// entries = list[(path: str, size: int (-1 = stat lỗi), mtime: float)].
#[pyfunction]
#[pyo3(signature = (root, exts, excluded_names, excluded_paths, threads=4))]
fn walk(
    py: Python<'_>,
    root: std::ffi::OsString,
    exts: Vec<String>,
    excluded_names: Vec<String>,
    excluded_paths: Vec<String>,
    threads: usize,
) -> (Vec<(std::ffi::OsString, i64, f64)>, Vec<std::ffi::OsString>) {
    let pol = ::brain_core::scan::Policy {
        exts: exts.into_iter().collect(),
        excluded_names: excluded_names.into_iter().collect(),
        excluded_paths: excluded_paths.into_iter().collect(),
    };
    let r = py.detach(|| ::brain_core::scan::walk(std::path::Path::new(&root), &pol, threads));
    (r.entries.into_iter().map(|e| (e.path, e.size, e.mtime)).collect(), r.repos)
}

/// (util %, VRAM dùng MB, VRAM tổng MB) của GPU NVIDIA qua NVML; None nếu không có.
#[pyfunction]
fn gpu_stat(py: Python<'_>) -> Option<(u32, u64, u64)> {
    py.detach(|| ::brain_core::sysmon::gpu().map(|g| (g.util_percent, g.mem_used_mb, g.mem_total_mb)))
}

#[pymodule]
fn brain_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(norm, m)?)?;
    m.add_function(wrap_pyfunction!(plain, m)?)?;
    m.add_function(wrap_pyfunction!(telex_variants, m)?)?;
    m.add_function(wrap_pyfunction!(syllable_variants, m)?)?;
    m.add_function(wrap_pyfunction!(norm_many, m)?)?;
    m.add_function(wrap_pyfunction!(hash_file, m)?)?;
    m.add_function(wrap_pyfunction!(hash_files, m)?)?;
    m.add_function(wrap_pyfunction!(walk, m)?)?;
    m.add_function(wrap_pyfunction!(gpu_stat, m)?)?;
    Ok(())
}

