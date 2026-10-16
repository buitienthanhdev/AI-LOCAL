//! Opening the links and paths the chat shows (`POST /api/open`).
//!
//! The page never launches anything itself: it sends the target text it found in a message, and this module
//! decides what it is and whether it may be opened. The decision (`plan`) is pure and unit-tested; `execute`
//! only starts Explorer / the default browser / the editor with an argument vector (never a shell string).
//!
//! Rules: only http(s) URLs; every other scheme is refused. Relative paths resolve inside the workspace (or the
//! task's artifacts folder) only, `..` may not leave it. Programs and scripts are never started by a click: they
//! can only be revealed in Explorer. A path that does not exist is reported as such, never as opened.

use std::path::{Component, Path, PathBuf};
use std::process::Command;

use lga_core::config::Config;
use serde_json::{json, Value};

/// Extensions that run code when "opened". A click never starts them; Explorer can show where they are.
const EXECUTABLE_EXT: [&str; 26] = [
    "exe", "com", "bat", "cmd", "ps1", "psm1", "vbs", "vbe", "js", "jse", "wsf", "wsh", "msi", "msp", "scr", "pif", "lnk", "hta", "cpl",
    "reg", "jar", "appx", "msix", "gadget", "application", "url",
];
const CODE_EXT: [&str; 22] = [
    "rs", "py", "js", "ts", "tsx", "jsx", "json", "toml", "yaml", "yml", "md", "html", "css", "c", "h", "cpp", "cs", "go", "java", "sql", "sh", "txt",
];
const MAX_TARGET_CHARS: usize = 1500;

#[derive(Debug, PartialEq)]
pub enum Action {
    OpenUrl(String),
    OpenPath(PathBuf),
    Reveal(PathBuf),
    Ide(PathBuf, Option<u32>),
    Refuse { status: &'static str, message: String, extra: Value },
}

fn refuse(status: &'static str, message: impl Into<String>) -> Action {
    Action::Refuse { status, message: message.into(), extra: Value::Null }
}

fn has_scheme(text: &str) -> Option<&str> {
    let (scheme, rest) = text.split_once(':')?;
    let ok = scheme.len() > 1 && scheme.chars().next()?.is_ascii_alphabetic() && scheme.chars().all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c));
    // "C:\x" and "C:/x" are drive paths, not schemes (a one-letter scheme never exists); "https://" and "javascript:" are.
    (ok && !rest.is_empty()).then_some(scheme)
}

/// Strip a trailing `:line[:col]` and return (path text, line).
pub fn split_line(text: &str) -> (&str, Option<u32>) {
    let mut rest = text;
    let mut numbers: Vec<u32> = Vec::new();
    for _ in 0..2 {
        match rest.rsplit_once(':') {
            Some((head, tail)) if !tail.is_empty() && tail.len() <= 6 && tail.chars().all(|c| c.is_ascii_digit()) && head.len() > 1 => {
                numbers.push(tail.parse().unwrap_or(0));
                rest = head;
            }
            _ => break,
        }
    }
    // with "path:12:5" the line is the first number found from the left
    (rest, numbers.last().copied().filter(|n| *n > 0))
}

fn trim_punct(text: &str) -> &str {
    let mut t = text.trim();
    loop {
        let next = t
            .trim_matches(|c: char| matches!(c, '"' | '\'' | '`' | '“' | '”' | '‘' | '’'))
            .trim_end_matches(|c: char| ".,;:!?)]}…".contains(c))
            .trim();
        if next.len() == t.len() {
            return t;
        }
        t = next;
    }
}

fn lexically_inside(base: &Path, relative: &Path) -> Option<PathBuf> {
    let mut out = base.to_path_buf();
    for component in relative.components() {
        match component {
            Component::Normal(part) => out.push(part),
            Component::CurDir => {}
            Component::ParentDir => {
                if !out.pop() || !out.starts_with(base) {
                    return None;
                }
            }
            _ => return None, // a drive, a root or a prefix is not relative
        }
    }
    out.starts_with(base).then_some(out)
}

fn is_unc(text: &str) -> bool {
    text.starts_with("\\\\") && !text.starts_with("\\\\?\\") && !text.starts_with("\\\\.\\")
}

fn is_absolute(text: &str) -> bool {
    let b = text.as_bytes();
    (b.len() >= 3 && b[0].is_ascii_alphabetic() && b[1] == b':' && (b[2] == b'\\' || b[2] == b'/')) || is_unc(text)
}

/// The existing path that best matches `candidate`: first try to extend it with the words that follow it in the
/// message (a folder name with spaces), then shorten it at spaces (words that are not part of the path).
fn best_existing(candidate: &str, tail: &str, exists: &dyn Fn(&Path) -> bool) -> Option<PathBuf> {
    let candidate = candidate.trim();
    // longest extension of the candidate by the words after it that names an existing path
    let mut grown = candidate.to_string();
    let mut best: Option<PathBuf> = None;
    for word in tail.split_whitespace().take(5) {
        if word.chars().any(|c| "<>\"|?*".contains(c)) {
            break;
        }
        grown.push(' ');
        grown.push_str(word);
        let attempt = trim_punct(&grown).to_string();
        if exists(Path::new(&attempt)) {
            best = Some(PathBuf::from(attempt));
        }
    }
    if best.is_some() {
        return best;
    }
    if exists(Path::new(candidate)) {
        return Some(PathBuf::from(candidate));
    }
    let mut shrinking = candidate.to_string();
    while let Some((head, _)) = shrinking.rsplit_once(' ') {
        shrinking = head.trim_end().to_string();
        let attempt = trim_punct(&shrinking);
        if attempt.len() >= 3 && exists(Path::new(attempt)) {
            return Some(PathBuf::from(attempt));
        }
    }
    None
}

/// Decide what a click on `target` should do. `want` is "open" (default), "reveal" or "ide".
pub fn plan(config: &Config, target: &str, tail: &str, want: &str, task_id: &str, exists: &dyn Fn(&Path) -> bool) -> Action {
    let raw = target.trim();
    if raw.is_empty() || raw.chars().count() > MAX_TARGET_CHARS || raw.chars().any(|c| c.is_control()) {
        return refuse("invalid", "Đường dẫn trống, quá dài hoặc chứa ký tự điều khiển.");
    }
    if let Some(scheme) = has_scheme(raw) {
        let lower = scheme.to_ascii_lowercase();
        if (lower == "http" || lower == "https") && raw[scheme.len()..].starts_with("://") {
            let after = &raw[scheme.len() + 3..];
            let host_ok = after.split(['/', '?', '#']).next().is_some_and(|h| !h.is_empty() && !h.contains(char::is_whitespace));
            return if host_ok && !raw.contains(char::is_whitespace) {
                Action::OpenUrl(raw.to_string())
            } else {
                refuse("invalid", "Địa chỉ web không hợp lệ.")
            };
        }
        return refuse("blocked_scheme", format!("Không mở liên kết kiểu \"{lower}:\" vì không an toàn. Chỉ mở http và https."));
    }
    let (text, line) = split_line(trim_punct(raw));
    let text = text.trim();
    if text.starts_with("\\\\?\\") || text.starts_with("\\\\.\\") {
        return refuse("blocked_path", "Không mở đường dẫn thiết bị (\\\\?\\ hoặc \\\\.\\).");
    }
    let resolved = if is_absolute(text) {
        best_existing(text, tail, exists)
    } else {
        // Relative: only inside the workspace or this task's artifacts folder. ".." may not leave it.
        let relative = Path::new(text);
        let mut bases = vec![config.root.clone()];
        if !task_id.is_empty() && task_id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') {
            bases.insert(0, config.root.join("artifacts").join(task_id));
        }
        let mut inside_any = false;
        let mut found = None;
        for base in &bases {
            if let Some(path) = lexically_inside(base, relative) {
                inside_any = true;
                if exists(&path) {
                    found = Some(path);
                    break;
                }
            }
        }
        if found.is_none() && !inside_any {
            return refuse("outside_workspace", "Đường dẫn tương đối này đi ra ngoài thư mục làm việc. Hãy dùng đường dẫn đầy đủ.");
        }
        found
    };
    let Some(path) = resolved else {
        let shown = text.to_string();
        let parent = Path::new(text).parent().filter(|p| is_absolute(&p.to_string_lossy()) && exists(p)).map(|p| p.display().to_string());
        return Action::Refuse {
            status: "not_found",
            message: format!("Không tìm thấy: {shown}"),
            extra: json!({"parent": parent, "name": Path::new(text).file_name().map(|n| n.to_string_lossy().to_string())}),
        };
    };
    let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase();
    match want {
        "reveal" => Action::Reveal(path),
        "ide" => {
            if path.is_dir() || !CODE_EXT.contains(&ext.as_str()) {
                refuse("invalid", "Chỉ mở được tệp mã nguồn hoặc văn bản trong trình soạn thảo.")
            } else {
                Action::Ide(path, line)
            }
        }
        _ => {
            if !path.is_dir() && EXECUTABLE_EXT.contains(&ext.as_str()) {
                Action::Refuse {
                    status: "blocked_executable",
                    message: format!("Tệp .{ext} là chương trình hoặc script, nên bấm vào sẽ không chạy nó. Bạn có thể mở vị trí của nó."),
                    extra: json!({"resolved": path.display().to_string()}),
                }
            } else {
                Action::OpenPath(path)
            }
        }
    }
}

fn explorer(arg: &std::ffi::OsStr) -> std::io::Result<()> {
    Command::new("explorer.exe").arg(arg).spawn().map(|_| ())
}

/// The reply of `POST /api/open`.
pub fn handle(config: &Config, body: &Value) -> Value {
    let target = body["target"].as_str().unwrap_or("");
    let tail = body["tail"].as_str().unwrap_or("");
    let want = body["action"].as_str().unwrap_or("open");
    let task_id = body["task_id"].as_str().unwrap_or("");
    let action = plan(config, target, tail, want, task_id, &|p| p.exists());
    let done = |status: &str, kind: &str, resolved: &str, message: String| json!({"ok": true, "status": status, "kind": kind, "resolved": resolved, "message": message});
    match action {
        Action::OpenUrl(url) => match explorer(std::ffi::OsStr::new(&url)) {
            Ok(()) => done("opened", "url", &url, "Đã mở trong trình duyệt mặc định.".into()),
            Err(e) => json!({"ok": false, "status": "error", "message": format!("Không mở được trình duyệt: {e}")}),
        },
        Action::OpenPath(path) => {
            let kind = if path.is_dir() { "dir" } else { "file" };
            match explorer(path.as_os_str()) {
                Ok(()) => done("opened", kind, &path.display().to_string(), if kind == "dir" { "Đã mở thư mục.".into() } else { "Đã mở bằng ứng dụng mặc định.".into() }),
                Err(e) => json!({"ok": false, "status": "error", "resolved": path.display().to_string(), "message": format!("Không mở được: {e}")}),
            }
        }
        Action::Reveal(path) => {
            let result = if path.is_dir() {
                explorer(path.as_os_str())
            } else {
                use std::os::windows::process::CommandExt;
                Command::new("explorer.exe").raw_arg(format!("/select,\"{}\"", path.display())).spawn().map(|_| ())
            };
            match result {
                Ok(()) => done("revealed", if path.is_dir() { "dir" } else { "file" }, &path.display().to_string(), "Đã mở vị trí trong Explorer.".into()),
                Err(e) => json!({"ok": false, "status": "error", "message": format!("Không mở được Explorer: {e}")}),
            }
        }
        Action::Ide(path, line) => match lga_core::native::shell::which("code") {
            Some(code) => {
                let line = line.or(body["line"].as_u64().map(|n| n as u32));
                let spec = match line {
                    Some(n) => format!("{}:{n}", path.display()),
                    None => path.display().to_string(),
                };
                match Command::new(code).arg("-g").arg(spec).spawn() {
                    Ok(_) => done("opened", "file", &path.display().to_string(), "Đã mở trong VS Code.".into()),
                    Err(e) => json!({"ok": false, "status": "error", "message": format!("Không chạy được VS Code: {e}")}),
                }
            }
            None => json!({"ok": false, "status": "ide_unavailable", "message": "Không thấy lệnh `code` (VS Code) trên máy. Dùng \"Mở\" hoặc \"Sao chép đường dẫn\"."}),
        },
        Action::Refuse { status, message, extra } => {
            let mut v = json!({"ok": false, "status": status, "message": message});
            if let Value::Object(map) = extra {
                for (k, val) in map {
                    v[k] = val;
                }
            }
            v
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    fn config() -> Config {
        let mut c = Config::load().expect("config");
        c.root = PathBuf::from("C:\\work\\repo");
        c
    }

    fn fs(paths: &[&str]) -> impl Fn(&Path) -> bool {
        let set: HashSet<String> = paths.iter().map(|p| p.to_string()).collect();
        move |p: &Path| set.contains(&p.display().to_string())
    }

    #[test]
    fn only_http_and_https_urls_open() {
        let c = config();
        let none = fs(&[]);
        assert_eq!(plan(&c, "https://example.com/a?b=1", "", "open", "", &none), Action::OpenUrl("https://example.com/a?b=1".into()));
        for bad in ["javascript:alert(1)", "file:///C:/Windows/System32/cmd.exe", "ms-msdt:/id", "vbscript:x", "data:text/html,<b>"] {
            assert!(matches!(plan(&c, bad, "", "open", "", &none), Action::Refuse { status: "blocked_scheme", .. }), "{bad}");
        }
        assert!(matches!(plan(&c, "https://", "", "open", "", &none), Action::Refuse { status: "invalid", .. }));
        assert!(matches!(plan(&c, "https://a b.com", "", "open", "", &none), Action::Refuse { status: "invalid", .. }));
    }

    #[test]
    fn windows_paths_with_spaces_unicode_and_line_numbers() {
        let c = config();
        let exists = fs(&["C:\\Users\\TIEN THANH\\Desktop\\Báo cáo", "C:\\Users\\TIEN THANH\\Desktop\\Báo cáo\\kết quả.xlsx", "C:\\proj\\src\\agent.rs"]);
        // the message continues after the path: shorten to what exists
        assert_eq!(
            plan(&c, "C:\\Users\\TIEN THANH\\Desktop\\Báo cáo\\kết quả.xlsx rồi gửi", "", "open", "", &exists),
            Action::OpenPath(PathBuf::from("C:\\Users\\TIEN THANH\\Desktop\\Báo cáo\\kết quả.xlsx"))
        );
        // the link text stopped early at a space: extend with the following words
        assert_eq!(
            plan(&c, "C:\\Users\\TIEN THANH\\Desktop\\Báo", "cáo và", "open", "", &exists),
            Action::OpenPath(PathBuf::from("C:\\Users\\TIEN THANH\\Desktop\\Báo cáo"))
        );
        assert_eq!(plan(&c, "C:\\proj\\src\\agent.rs:120:7", "", "ide", "", &exists), Action::Ide(PathBuf::from("C:\\proj\\src\\agent.rs"), Some(120)));
        assert_eq!(plan(&c, "\"C:\\proj\\src\\agent.rs\".", "", "open", "", &exists), Action::OpenPath(PathBuf::from("C:\\proj\\src\\agent.rs")));
    }

    #[test]
    fn missing_paths_are_reported_not_opened() {
        let c = config();
        let exists = fs(&["C:\\data"]);
        match plan(&c, "C:\\data\\gone.txt", "", "open", "", &exists) {
            Action::Refuse { status: "not_found", extra, .. } => assert_eq!(extra["parent"], "C:\\data"),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn relative_paths_stay_in_the_workspace() {
        let c = config();
        let exists = fs(&["C:\\work\\repo\\src\\agent.rs", "C:\\work\\repo\\artifacts\\t-1\\out.docx"]);
        assert_eq!(plan(&c, ".\\src\\agent.rs", "", "open", "", &exists), Action::OpenPath(PathBuf::from("C:\\work\\repo\\src\\agent.rs")));
        assert_eq!(plan(&c, "src/agent.rs", "", "open", "", &exists), Action::OpenPath(PathBuf::from("C:\\work\\repo\\src\\agent.rs")));
        assert_eq!(plan(&c, "out.docx", "", "open", "t-1", &exists), Action::OpenPath(PathBuf::from("C:\\work\\repo\\artifacts\\t-1\\out.docx")));
        for bad in ["..\\..\\Windows\\win.ini", "src\\..\\..\\secret.txt", "..\\shared\\module.py"] {
            assert!(matches!(plan(&c, bad, "", "open", "", &exists), Action::Refuse { status: "outside_workspace", .. }), "{bad}");
        }
    }

    #[test]
    fn programs_and_scripts_are_never_started_by_a_click() {
        let c = config();
        let exists = fs(&["C:\\tools\\setup.exe", "C:\\tools\\run.ps1", "C:\\tools\\a.BAT"]);
        for p in ["C:\\tools\\setup.exe", "C:\\tools\\run.ps1", "C:\\tools\\a.BAT"] {
            assert!(matches!(plan(&c, p, "", "open", "", &exists), Action::Refuse { status: "blocked_executable", .. }), "{p}");
            assert!(matches!(plan(&c, p, "", "reveal", "", &exists), Action::Reveal(_)), "{p} can be revealed");
        }
    }

    #[test]
    fn device_paths_and_junk_are_refused() {
        let c = config();
        let none = fs(&[]);
        assert!(matches!(plan(&c, "\\\\?\\C:\\x", "", "open", "", &none), Action::Refuse { status: "blocked_path", .. }));
        assert!(matches!(plan(&c, "C:\\x\u{0}y", "", "open", "", &none), Action::Refuse { status: "invalid", .. }));
        assert!(matches!(plan(&c, "", "", "open", "", &none), Action::Refuse { status: "invalid", .. }));
    }

    /// Real Windows run: starts Explorer / the default browser. `cargo test -p agent-desktop -- --ignored live_open`
    #[test]
    #[ignore]
    fn live_open_through_the_real_handler() {
        let c = Config::load().expect("config");
        let docs = c.root.join("docs").display().to_string();
        let r = handle(&c, &json!({"target": docs}));
        assert_eq!(r["ok"], true, "{r}");
        assert_eq!(r["kind"], "dir");
        let r = handle(&c, &json!({"target": "README.md", "action": "reveal"}));
        assert_eq!(r["ok"], true, "relative path inside the workspace: {r}");
        let r = handle(&c, &json!({"target": "https://example.com/"}));
        assert_eq!(r["ok"], true, "{r}");
        let r = handle(&c, &json!({"target": r"C:\Windows\System32\notepad.exe"}));
        assert_eq!(r["status"], "blocked_executable", "{r}");
        let r = handle(&c, &json!({"target": r"C:\khong\ton\tai\x.txt"}));
        assert_eq!(r["status"], "not_found", "{r}");
        let r = handle(&c, &json!({"target": r"..\..\..\Windows\win.ini"}));
        assert_eq!(r["status"], "outside_workspace", "{r}");
        // VS Code at a line (the person has `code` on PATH): started detached, reported as opened
        let r = handle(&c, &json!({"target": "README.md", "action": "ide", "line": 3}));
        assert!(r["ok"] == true || r["status"] == "ide_unavailable", "{r}");
        println!("ide result: {r}");
        let r = handle(&c, &json!({"target": "javascript:alert(1)"}));
        assert_eq!(r["status"], "blocked_scheme", "{r}");
    }

    #[test]
    fn unc_paths_are_resolved() {
        let c = config();
        let exists = fs(&["\\\\server\\share\\folder\\file.pdf"]);
        assert_eq!(plan(&c, "\\\\server\\share\\folder\\file.pdf", "", "open", "", &exists),
                   Action::OpenPath(PathBuf::from("\\\\server\\share\\folder\\file.pdf")));
    }
}
