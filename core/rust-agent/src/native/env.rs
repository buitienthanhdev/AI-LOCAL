//! Read-only detection of installed software (registry and file metadata; nothing is launched).

use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use windows_sys::Win32::Foundation::{ERROR_SUCCESS, MAX_PATH};
use windows_sys::Win32::Storage::FileSystem::{GetFileVersionInfoSizeW, GetFileVersionInfoW, VerQueryValueW, VS_FIXEDFILEINFO};
use windows_sys::Win32::System::Registry::{
    RegCloseKey, RegEnumKeyExW, RegGetValueW, RegOpenKeyExW, HKEY, HKEY_CLASSES_ROOT, HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE,
    KEY_READ, RRF_RT_REG_EXPAND_SZ, RRF_RT_REG_SZ, RRF_NOEXPAND,
};

const APP_PATHS: &str = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths";
const AUTOCAD_ROOT: &str = r"SOFTWARE\Autodesk\AutoCAD";

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// A string value (`name` None = the key's default value), unexpanded.
fn registry_string(hive: HKEY, key: &str, name: Option<&str>) -> Option<String> {
    let key_w = wide(key);
    let name_w = name.map(wide);
    let name_ptr = name_w.as_ref().map_or(std::ptr::null(), |n| n.as_ptr());
    let flags = RRF_RT_REG_SZ | RRF_RT_REG_EXPAND_SZ | RRF_NOEXPAND;
    let mut size: u32 = 0;
    // SAFETY: first call queries the size; the second fills a buffer of exactly that many bytes.
    unsafe {
        if RegGetValueW(hive, key_w.as_ptr(), name_ptr, flags, std::ptr::null_mut(), std::ptr::null_mut(), &mut size) != ERROR_SUCCESS {
            return None;
        }
        let mut buffer = vec![0u16; (size as usize).div_ceil(2) + 1];
        let mut bytes = (buffer.len() * 2) as u32;
        if RegGetValueW(hive, key_w.as_ptr(), name_ptr, flags, std::ptr::null_mut(), buffer.as_mut_ptr().cast(), &mut bytes) != ERROR_SUCCESS {
            return None;
        }
        let len = buffer.iter().position(|&c| c == 0).unwrap_or(buffer.len());
        Some(String::from_utf16_lossy(&buffer[..len]))
    }
}

/// Names of the direct sub-keys of `hive\key`.
fn subkeys(hive: HKEY, key: &str) -> Vec<String> {
    let key_w = wide(key);
    let mut handle: HKEY = std::ptr::null_mut();
    let mut names = Vec::new();
    // SAFETY: the opened key is closed before returning; each enumeration writes into a MAX_PATH buffer.
    unsafe {
        if RegOpenKeyExW(hive, key_w.as_ptr(), 0, KEY_READ, &mut handle) != ERROR_SUCCESS {
            return names;
        }
        for index in 0.. {
            let mut buffer = [0u16; MAX_PATH as usize];
            let mut len = buffer.len() as u32;
            let status = RegEnumKeyExW(handle, index, buffer.as_mut_ptr(), &mut len, std::ptr::null(), std::ptr::null_mut(), std::ptr::null_mut(), std::ptr::null_mut());
            if status != ERROR_SUCCESS {
                break;
            }
            names.push(String::from_utf16_lossy(&buffer[..len as usize]));
        }
        RegCloseKey(handle);
    }
    names
}

/// Product version of an executable ("152.0.7977.84").
pub fn file_version(path: &Path) -> Option<String> {
    let path_w = wide(&path.to_string_lossy());
    // SAFETY: the version block is sized by GetFileVersionInfoSizeW; VerQueryValueW returns a pointer into it.
    unsafe {
        let size = GetFileVersionInfoSizeW(path_w.as_ptr(), std::ptr::null_mut());
        if size == 0 {
            return None;
        }
        let mut block = vec![0u8; size as usize];
        if GetFileVersionInfoW(path_w.as_ptr(), 0, size, block.as_mut_ptr().cast()) == 0 {
            return None;
        }
        let mut info: *mut VS_FIXEDFILEINFO = std::ptr::null_mut();
        let mut len: u32 = 0;
        let root = wide("\\");
        if VerQueryValueW(block.as_ptr().cast(), root.as_ptr(), (&mut info as *mut *mut VS_FIXEDFILEINFO).cast(), &mut len) == 0 || info.is_null() {
            return None;
        }
        let (ms, ls) = ((*info).dwProductVersionMS, (*info).dwProductVersionLS);
        Some(format!("{}.{}.{}.{}", ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF))
    }
}

pub fn find_chrome(configured: &str) -> Option<PathBuf> {
    let mut candidates: Vec<String> = Vec::new();
    if !configured.is_empty() {
        candidates.push(configured.to_string());
    }
    for hive in [HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE] {
        if let Some(registered) = registry_string(hive, &format!(r"{APP_PATHS}\chrome.exe"), None) {
            candidates.push(registered.trim_matches('"').to_string());
        }
    }
    for var in ["ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"] {
        if let Ok(base) = std::env::var(var) {
            candidates.push(format!(r"{base}\Google\Chrome\Application\chrome.exe"));
        }
    }
    candidates
        .into_iter()
        .map(|c| super::fs::absolute_path(&c).unwrap_or_else(|_| PathBuf::from(c)))
        .find(|p| p.is_file())
}

pub fn autocad_installations() -> Vec<Value> {
    let mut found = Vec::new();
    for release in subkeys(HKEY_LOCAL_MACHINE, AUTOCAD_ROOT) {
        let release_key = format!(r"{AUTOCAD_ROOT}\{release}");
        for product in subkeys(HKEY_LOCAL_MACHINE, &release_key) {
            // Sub-keys such as InstalledProducts have no AcadLocation.
            let Some(location) = registry_string(HKEY_LOCAL_MACHINE, &format!(r"{release_key}\{product}"), Some("AcadLocation")) else {
                continue;
            };
            let exe = Path::new(&location).join("acad.exe");
            if exe.is_file() {
                found.push(json!({"release": release, "product": product, "location": location,
                                  "executable": exe.display().to_string(), "version": file_version(&exe)}));
            }
        }
    }
    found
}

pub fn com_registered(prog_id: &str) -> bool {
    registry_string(HKEY_CLASSES_ROOT, &format!(r"{prog_id}\CLSID"), None).is_some()
}

pub fn process_running(image_name: &str) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS,
    };
    let wanted = image_name.to_lowercase();
    // SAFETY: the snapshot handle is closed before returning; `entry` has its dwSize set.
    unsafe {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snapshot == INVALID_HANDLE_VALUE {
            return false;
        }
        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
        let mut running = false;
        if Process32FirstW(snapshot, &mut entry) != 0 {
            loop {
                let len = entry.szExeFile.iter().position(|&c| c == 0).unwrap_or(entry.szExeFile.len());
                if String::from_utf16_lossy(&entry.szExeFile[..len]).to_lowercase() == wanted {
                    running = true;
                    break;
                }
                if Process32NextW(snapshot, &mut entry) == 0 {
                    break;
                }
            }
        }
        CloseHandle(snapshot);
        running
    }
}

pub fn screen_layout() -> Value {
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetSystemMetrics, SM_CMONITORS, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN, SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN,
    };
    // SAFETY: GetSystemMetrics has no preconditions.
    unsafe {
        json!({"virtual_left": GetSystemMetrics(SM_XVIRTUALSCREEN), "virtual_top": GetSystemMetrics(SM_YVIRTUALSCREEN),
               "virtual_width": GetSystemMetrics(SM_CXVIRTUALSCREEN), "virtual_height": GetSystemMetrics(SM_CYVIRTUALSCREEN),
               "monitors": GetSystemMetrics(SM_CMONITORS)})
    }
}

pub fn is_admin() -> bool {
    // SAFETY: IsUserAnAdmin has no preconditions.
    unsafe { windows_sys::Win32::UI::Shell::IsUserAnAdmin() != 0 }
}

/// The `system.environment` result and its one-paragraph summary.
pub fn environment(chrome_path: &str, prog_id: &str, shell: Option<&Path>) -> (Value, String) {
    let chrome = find_chrome(chrome_path);
    let installs = autocad_installations();
    let data = json!({
        "chrome": {"found": chrome.is_some(), "path": chrome.as_ref().map(|p| p.display().to_string()),
                   "version": chrome.as_deref().and_then(file_version)},
        "autocad": {"installations": installs, "com_registered": com_registered(prog_id), "running": process_running("acad.exe")},
        "powershell": {"path": shell.map(|p| p.display().to_string()), "version": shell.and_then(file_version)},
        "screen": screen_layout(),
        "user": {"name": std::env::var("USERNAME").unwrap_or_default(), "is_admin": is_admin()},
    });
    let acad = data["autocad"]["installations"]
        .as_array()
        .map(|a| a.iter().map(|i| format!("{} at {}", i["release"].as_str().unwrap_or("?"), i["location"].as_str().unwrap_or("?"))).collect::<Vec<_>>().join(", "))
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "not installed".into());
    let summary = [
        format!("Chrome: {}", data["chrome"]["version"].as_str().unwrap_or("not found")),
        format!(
            "AutoCAD: {acad} (COM {}, {})",
            if data["autocad"]["com_registered"] == true { "registered" } else { "not registered" },
            if data["autocad"]["running"] == true { "running" } else { "not running" }
        ),
        format!("Screen: {}", data["screen"]),
        format!("User: {} (admin: {})", data["user"]["name"].as_str().unwrap_or(""), if data["user"]["is_admin"] == true { "True" } else { "False" }),
    ]
    .join("\n");
    (data, summary)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_this_machine() {
        let (data, summary) = environment("", "AutoCAD.Application", None);
        assert!(data["screen"]["virtual_width"].as_i64().unwrap() > 0);
        assert!(summary.contains("Chrome:"));
        let notepad = Path::new(r"C:\Windows\System32\notepad.exe");
        if notepad.is_file() {
            assert!(file_version(notepad).is_some());
        }
        assert!(process_running("System") || process_running("explorer.exe"));
    }
}
