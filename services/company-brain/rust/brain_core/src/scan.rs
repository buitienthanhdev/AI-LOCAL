//! Duyệt thư mục song song — tương đương `os.walk(root)` + bộ lọc trong `scanner.scan` (Python 3.14):
//! - thư mục bị loại: tên (thường) ∈ excluded_names, hoặc đường dẫn đầy đủ (thường) ∈ excluded_paths;
//! - thư mục tên ".git": ghi nhận repo (thư mục cha) và KHÔNG đi vào;
//! - os.walk đi vào junction (mount point) nhưng KHÔNG đi vào symlink thư mục (os.path.islink) => giữ y như vậy;
//! - file: đuôi theo luật `pathlib.Path.suffix` (i = rfind('.'), 0 < i < len-1), so chữ thường với `exts`; bỏ tên "~$...";
//! - file stat lỗi (khóa/symlink hỏng) vẫn được trả về (size = -1) vì Python ghi nhận "seen" trước khi stat.
use rayon::prelude::*;
use std::collections::HashSet;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

pub struct Policy {
    pub exts: HashSet<String>,
    pub excluded_names: HashSet<String>,
    pub excluded_paths: HashSet<String>,
}

pub struct Entry {
    pub path: OsString,
    pub size: i64,  // -1: không stat được
    pub mtime: f64, // giây epoch (như os.stat().st_mtime); NaN nếu không stat được
}

#[derive(Default)]
pub struct WalkResult {
    pub entries: Vec<Entry>,
    pub repos: Vec<OsString>,
}

/// pathlib.PurePath.suffix
pub fn suffix_lower(name: &str) -> Option<String> {
    let i = name.rfind('.')?;
    let n = name.len();
    if i > 0 && i + 1 < n {
        Some(name[i..].to_lowercase())
    } else {
        None
    }
}

#[cfg(windows)]
fn is_true_symlink(p: &Path) -> bool {
    // os.path.islink: chỉ IO_REPARSE_TAG_SYMLINK; junction (MOUNT_POINT) không phải link => os.walk vẫn đi vào
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{FindClose, FindFirstFileW, WIN32_FIND_DATAW};
    const IO_REPARSE_TAG_SYMLINK: u32 = 0xA000_000C;
    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
    let wide: Vec<u16> = p.as_os_str().encode_wide().chain(std::iter::once(0)).collect();
    unsafe {
        let mut data: WIN32_FIND_DATAW = std::mem::zeroed();
        let h = FindFirstFileW(wide.as_ptr(), &mut data);
        if h == windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE {
            return false;
        }
        FindClose(h);
        data.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 && data.dwReserved0 == IO_REPARSE_TAG_SYMLINK
    }
}

#[cfg(not(windows))]
fn is_true_symlink(p: &Path) -> bool {
    fs::symlink_metadata(p).map(|m| m.file_type().is_symlink()).unwrap_or(false)
}

fn stat(p: &Path) -> (i64, f64) {
    match fs::metadata(p) {
        Ok(m) => {
            let mt = m
                .modified()
                .ok()
                .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_secs_f64())
                .unwrap_or(f64::NAN);
            (m.len() as i64, mt)
        }
        Err(_) => (-1, f64::NAN),
    }
}

fn walk_dir(dir: PathBuf, pol: &Policy) -> WalkResult {
    let mut out = WalkResult::default();
    let rd = match fs::read_dir(&dir) {
        Ok(r) => r,
        Err(_) => return out, // os.walk(onerror=None): bỏ qua im lặng
    };
    let mut subdirs: Vec<PathBuf> = Vec::new();
    let mut has_git = false;
    for e in rd.flatten() {
        let name_os = e.file_name();
        let name = name_os.to_string_lossy();
        let path = e.path();
        let ft = match e.file_type() {
            Ok(t) => t,
            Err(_) => continue,
        };
        // scandir.is_dir() theo link: symlink/junction tới thư mục vẫn là "dirnames"
        let is_dir = if ft.is_symlink() { fs::metadata(&path).map(|m| m.is_dir()).unwrap_or(false) } else { ft.is_dir() };
        if is_dir {
            if name == ".git" {
                has_git = true;
                continue;
            }
            let full_low = path.to_string_lossy().to_lowercase();
            if pol.excluded_names.contains(&name.to_lowercase()) || pol.excluded_paths.contains(&full_low) {
                continue;
            }
            if ft.is_symlink() && is_true_symlink(&path) {
                continue; // os.walk(followlinks=False) không đi vào symlink thư mục
            }
            subdirs.push(path);
        } else {
            if name.starts_with("~$") {
                continue;
            }
            match suffix_lower(&name) {
                Some(s) if pol.exts.contains(&s) => {}
                _ => continue,
            }
            let (size, mtime) = stat(&path);
            out.entries.push(Entry { path: path.into_os_string(), size, mtime });
        }
    }
    if has_git {
        out.repos.push(dir.into_os_string());
    }
    let children: Vec<WalkResult> = subdirs.into_par_iter().map(|d| walk_dir(d, pol)).collect();
    for c in children {
        out.entries.extend(c.entries);
        out.repos.extend(c.repos);
    }
    out
}

pub fn walk(root: &Path, pol: &Policy, threads: usize) -> WalkResult {
    let pool = rayon::ThreadPoolBuilder::new().num_threads(threads.max(1)).build().expect("thread pool");
    pool.install(|| walk_dir(root.to_path_buf(), pol))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn suffix_rules_match_pathlib() {
        assert_eq!(suffix_lower("a.DOCX").as_deref(), Some(".docx"));
        assert_eq!(suffix_lower(".bashrc"), None);
        assert_eq!(suffix_lower("a."), None);
        assert_eq!(suffix_lower("a.tar.gz").as_deref(), Some(".gz"));
        assert_eq!(suffix_lower("..py").as_deref(), Some(".py"));
        assert_eq!(suffix_lower("noext"), None);
    }

    #[test]
    fn walk_filters_like_scanner() {
        let root = std::env::temp_dir().join("brain_core_walk_test");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("keep/.git")).unwrap();
        fs::create_dir_all(root.join("$Recycle.Bin")).unwrap();
        fs::write(root.join("keep/a.docx"), b"x").unwrap();
        fs::write(root.join("keep/~$a.docx"), b"x").unwrap();
        fs::write(root.join("keep/b.exe"), b"x").unwrap();
        fs::write(root.join("keep/.git/c.docx"), b"x").unwrap();
        fs::write(root.join("$Recycle.Bin/d.docx"), b"x").unwrap();
        let pol = Policy {
            exts: [".docx".to_string()].into_iter().collect(),
            excluded_names: ["$recycle.bin".to_string()].into_iter().collect(),
            excluded_paths: HashSet::new(),
        };
        let r = walk(&root, &pol, 2);
        let names: Vec<String> = r.entries.iter().map(|e| e.path.to_string_lossy().into_owned()).collect();
        assert_eq!(names.len(), 1, "{names:?}");
        assert!(names[0].ends_with("a.docx"));
        assert_eq!(r.entries[0].size, 1);
        assert_eq!(r.repos.len(), 1);
        let _ = fs::remove_dir_all(&root);
    }
}
