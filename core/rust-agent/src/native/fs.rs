//! Filesystem primitives with verifiable results (hashes, sizes) and atomic writes.

use std::fs;
use std::io::{ErrorKind as IoKind, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::time::UNIX_EPOCH;

use base64::Engine;
use encoding_rs::Encoding;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::error::{ErrorCode, ToolErrorInfo};

const BINARY_SNIFF_BYTES: usize = 8192;

pub type FsResult<T> = Result<T, ToolErrorInfo>;

pub fn tool_error(code: ErrorCode, message: impl Into<String>) -> ToolErrorInfo {
    ToolErrorInfo::new(code, message, super::default_retryable(code))
}

pub fn sha256_bytes(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

pub fn sha256_file(path: &Path) -> std::io::Result<String> {
    let mut file = fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 1 << 20];
    loop {
        let n = file.read(&mut buffer)?;
        if n == 0 {
            return Ok(hex::encode(hasher.finalize()));
        }
        hasher.update(&buffer[..n]);
    }
}

fn os_error(error: &std::io::Error, path: &Path) -> ToolErrorInfo {
    let winerror = json!({"winerror": error.raw_os_error()});
    match error.kind() {
        IoKind::NotFound => tool_error(ErrorCode::NotFound, format!("not found: {}", path.display())),
        IoKind::PermissionDenied => {
            tool_error(ErrorCode::PolicyDenied, format!("the OS denied access to {}: {error}", path.display())).with_details(winerror)
        }
        IoKind::IsADirectory | IoKind::NotADirectory => tool_error(ErrorCode::InvalidArgument, format!("{}: {error}", path.display())),
        _ => tool_error(ErrorCode::ExecutionFailed, format!("{}: {error}", path.display())).with_details(winerror),
    }
}

/// Expand `~`, `%VAR%`, `$VAR` and `${VAR}`; require an absolute path; normalise `.` and `..`.
pub fn absolute_path(raw: &str) -> FsResult<PathBuf> {
    let mut text = raw.to_string();
    if text == "~" || text.starts_with("~/") || text.starts_with("~\\") {
        if let Ok(home) = std::env::var("USERPROFILE") {
            text = format!("{home}{}", &text[1..]);
        }
    }
    let expanded = expand_vars(&text);
    let path = Path::new(&expanded);
    if !path.is_absolute() {
        return Err(tool_error(ErrorCode::InvalidArgument, format!("path must be absolute: {raw:?}")));
    }
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                if out.parent().is_some() {
                    out.pop();
                }
            }
            other => out.push(other.as_os_str()),
        }
    }
    Ok(out)
}

fn expand_vars(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let chars: Vec<char> = text.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        let (name, consumed) = if c == '%' {
            match chars[i + 1..].iter().position(|&ch| ch == '%') {
                Some(end) if end > 0 => (chars[i + 1..i + 1 + end].iter().collect::<String>(), end + 2),
                _ => (String::new(), 0),
            }
        } else if c == '$' && chars.get(i + 1) == Some(&'{') {
            match chars[i + 2..].iter().position(|&ch| ch == '}') {
                Some(end) if end > 0 => (chars[i + 2..i + 2 + end].iter().collect::<String>(), end + 3),
                _ => (String::new(), 0),
            }
        } else if c == '$' {
            let len = chars[i + 1..].iter().take_while(|ch| ch.is_ascii_alphanumeric() || **ch == '_').count();
            (chars[i + 1..i + 1 + len].iter().collect::<String>(), if len > 0 { len + 1 } else { 0 })
        } else {
            (String::new(), 0)
        };
        if consumed > 0 {
            if let Ok(value) = std::env::var(&name) {
                out.push_str(&value);
                i += consumed;
                continue;
            }
        }
        out.push(c);
        i += 1;
    }
    out
}

/// A named text encoding. `encoding_rs` covers the WHATWG set; UTF-16 and BOM-carrying UTF-8 are handled
/// here because WHATWG has no UTF-16 encoder.
enum Codec {
    Utf8 { bom: bool },
    Utf16 { little_endian: bool, bom: bool },
    Other(&'static Encoding),
}

fn codec(name: &str) -> FsResult<Codec> {
    let lowered = name.trim().to_ascii_lowercase().replace('_', "-");
    Ok(match lowered.as_str() {
        "utf-8" | "utf8" => Codec::Utf8 { bom: false },
        "utf-8-sig" | "utf8-sig" => Codec::Utf8 { bom: true },
        "utf-16" | "utf16" => Codec::Utf16 { little_endian: true, bom: true },
        "utf-16-le" | "utf-16le" => Codec::Utf16 { little_endian: true, bom: false },
        "utf-16-be" | "utf-16be" => Codec::Utf16 { little_endian: false, bom: false },
        other => {
            let label = other.strip_prefix("cp").filter(|n| n.chars().all(|c| c.is_ascii_digit())).map(|n| format!("windows-{n}"));
            let label = match other {
                "latin-1" | "latin1" | "iso8859-1" => "iso-8859-1".to_string(),
                _ => label.unwrap_or_else(|| other.to_string()),
            };
            Codec::Other(
                Encoding::for_label(label.as_bytes())
                    .ok_or_else(|| tool_error(ErrorCode::InvalidArgument, format!("unknown encoding {name:?}")))?,
            )
        }
    })
}

fn decode_with(data: &[u8], name: &str) -> FsResult<String> {
    let fail = || tool_error(ErrorCode::InvalidArgument, format!("cannot decode as {name}"));
    match codec(name)? {
        Codec::Utf8 { bom } => {
            let body = if bom { data.strip_prefix(b"\xef\xbb\xbf").unwrap_or(data) } else { data };
            String::from_utf8(body.to_vec()).map_err(|_| fail())
        }
        Codec::Utf16 { little_endian, bom } => {
            let (body, le) = match (bom, data) {
                (true, [0xff, 0xfe, rest @ ..]) => (rest, true),
                (true, [0xfe, 0xff, rest @ ..]) => (rest, false),
                _ => (data, little_endian),
            };
            if body.len() % 2 != 0 {
                return Err(fail());
            }
            let units: Vec<u16> =
                body.chunks_exact(2).map(|c| if le { u16::from_le_bytes([c[0], c[1]]) } else { u16::from_be_bytes([c[0], c[1]]) }).collect();
            String::from_utf16(&units).map_err(|_| fail())
        }
        Codec::Other(encoding) => encoding.decode_without_bom_handling_and_without_replacement(data).map(|c| c.into_owned()).ok_or_else(fail),
    }
}

fn encode_with(text: &str, name: &str) -> FsResult<Vec<u8>> {
    match codec(name)? {
        Codec::Utf8 { bom } => Ok([if bom { &b"\xef\xbb\xbf"[..] } else { &[] }, text.as_bytes()].concat()),
        Codec::Utf16 { little_endian, bom } => {
            let mut out = Vec::with_capacity(text.len() * 2 + 2);
            if bom {
                out.extend_from_slice(if little_endian { &[0xff, 0xfe] } else { &[0xfe, 0xff] });
            }
            for unit in text.encode_utf16() {
                out.extend_from_slice(&if little_endian { unit.to_le_bytes() } else { unit.to_be_bytes() });
            }
            Ok(out)
        }
        Codec::Other(encoding) => {
            let (bytes, _, unmappable) = encoding.encode(text);
            if unmappable || encoding.output_encoding() != encoding {
                return Err(tool_error(ErrorCode::InvalidArgument, format!("cannot encode content as {name}")));
            }
            Ok(bytes.into_owned())
        }
    }
}

/// Decode file bytes: an explicit encoding, else BOM, else binary (base64), else UTF-8, else cp1252.
fn decode(data: &[u8], encoding: Option<&str>) -> FsResult<(String, String)> {
    if let Some(name) = encoding {
        return Ok((decode_with(data, name)?, name.to_string()));
    }
    if let Some(rest) = data.strip_prefix(b"\xef\xbb\xbf") {
        return Ok((String::from_utf8_lossy(rest).into_owned(), "utf-8-sig".into()));
    }
    if data.starts_with(&[0xff, 0xfe]) || data.starts_with(&[0xfe, 0xff]) {
        return Ok((decode_with(data, "utf-16").unwrap_or_else(|_| String::from_utf8_lossy(data).into_owned()), "utf-16".into()));
    }
    if data[..data.len().min(BINARY_SNIFF_BYTES)].contains(&0) {
        return Ok((base64::engine::general_purpose::STANDARD.encode(data), "base64".into()));
    }
    match std::str::from_utf8(data) {
        Ok(text) => Ok((text.to_string(), "utf-8".into())),
        // Legacy ANSI text (e.g. files written by old Windows tools); the encoding name tells the caller.
        Err(_) => Ok((encoding_rs::WINDOWS_1252.decode_without_bom_handling(data).0.into_owned(), "cp1252".into())),
    }
}

pub fn read_file(path: &Path, max_bytes: usize, encoding: Option<&str>) -> FsResult<Value> {
    let meta = fs::metadata(path).map_err(|e| os_error(&e, path))?;
    if meta.is_dir() {
        return Err(tool_error(ErrorCode::InvalidArgument, format!("{} is a directory", path.display())));
    }
    let mut data = Vec::new();
    fs::File::open(path)
        .and_then(|f| f.take(max_bytes as u64 + 1).read_to_end(&mut data))
        .map_err(|e| os_error(&e, path))?;
    let truncated = data.len() > max_bytes;
    data.truncate(max_bytes);
    let (content, used) = decode(&data, encoding)?;
    let sha256 = if truncated { sha256_bytes(&data) } else { sha256_file(path).map_err(|e| os_error(&e, path))? };
    Ok(json!({"path": path.display().to_string(), "size": meta.len(), "encoding": used, "content": content,
              "truncated": truncated, "sha256": sha256}))
}

pub fn write_file(path: &Path, content: &str, encoding: &str, overwrite: bool, create_dirs: bool, max_bytes: usize) -> FsResult<Value> {
    let data = encode_with(content, encoding)?;
    if data.len() > max_bytes {
        return Err(tool_error(ErrorCode::InvalidArgument, format!("content is {} bytes; limit is {max_bytes}", data.len())));
    }
    let existed = path.exists();
    if existed && !overwrite {
        return Err(tool_error(ErrorCode::Conflict, format!("{} already exists and overwrite is false", path.display())));
    }
    if existed && path.is_dir() {
        return Err(tool_error(ErrorCode::InvalidArgument, format!("{} is a directory", path.display())));
    }
    let parent = path.parent().ok_or_else(|| tool_error(ErrorCode::InvalidArgument, format!("{} has no parent directory", path.display())))?;
    if !parent.exists() {
        if !create_dirs {
            return Err(tool_error(ErrorCode::NotFound, format!("parent directory {} does not exist", parent.display())));
        }
        fs::create_dir_all(parent).map_err(|e| os_error(&e, parent))?;
    }
    let tmp = parent.join(format!(".lga-{}.tmp", uuid::Uuid::new_v4().simple()));
    let written = (|| {
        let mut file = fs::File::create(&tmp)?;
        file.write_all(&data)?;
        file.sync_all()?;
        drop(file);
        fs::rename(&tmp, path)
    })();
    if let Err(e) = written {
        let _ = fs::remove_file(&tmp);
        return Err(os_error(&e, path));
    }
    let size = fs::metadata(path).map_err(|e| os_error(&e, path))?.len();
    let digest = sha256_file(path).map_err(|e| os_error(&e, path))?;
    if size as usize != data.len() || digest != sha256_bytes(&data) {
        return Err(tool_error(ErrorCode::VerificationFailed, format!("read-back of {} does not match what was written", path.display()))
            .with_details(json!({"expected_size": data.len(), "actual_size": size})));
    }
    Ok(json!({"path": path.display().to_string(), "size": size, "sha256": digest, "created": !existed}))
}

fn mtime(meta: &fs::Metadata) -> f64 {
    meta.modified().ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map_or(0.0, |d| d.as_secs_f64())
}

pub fn list_directory(path: &Path, max_entries: usize) -> FsResult<(Vec<Value>, bool)> {
    if !path.is_dir() {
        return Err(if path.exists() {
            tool_error(ErrorCode::InvalidArgument, format!("{} is not a directory", path.display()))
        } else {
            tool_error(ErrorCode::NotFound, format!("not found: {}", path.display()))
        });
    }
    let mut items: Vec<(bool, String, fs::Metadata)> = fs::read_dir(path)
        .map_err(|e| os_error(&e, path))?
        .flatten()
        // An entry that vanished or is inaccessible is skipped; listing the rest is still correct.
        .filter_map(|entry| {
            let meta = fs::symlink_metadata(entry.path()).ok()?;
            Some((meta.is_dir(), entry.file_name().to_string_lossy().into_owned(), meta))
        })
        .collect();
    items.sort_by(|a, b| (!a.0, a.1.to_lowercase()).cmp(&(!b.0, b.1.to_lowercase())));
    let truncated = items.len() > max_entries;
    let entries = items
        .into_iter()
        .take(max_entries)
        .map(|(is_dir, name, meta)| {
            json!({"name": name, "is_dir": is_dir, "size": if is_dir { Value::Null } else { json!(meta.len()) }, "modified_unix": mtime(&meta)})
        })
        .collect();
    Ok((entries, truncated))
}

pub fn stat(path: &Path, hash_max_bytes: u64) -> FsResult<Value> {
    let display = path.display().to_string();
    let Ok(meta) = fs::metadata(path) else {
        return Ok(json!({"path": display, "exists": false}));
    };
    let digest = if meta.is_file() && meta.len() <= hash_max_bytes { sha256_file(path).ok() } else { None };
    Ok(json!({"path": display, "exists": true, "is_dir": meta.is_dir(), "size": meta.len(), "modified_unix": mtime(&meta), "sha256": digest}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn write_read_round_trip_with_verification() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("sub").join("a.txt");
        let written = write_file(&target, "xin chào\nhello", "utf-8", false, true, 1024).unwrap();
        assert_eq!(written["created"], true);
        let read = read_file(&target, 1024, None).unwrap();
        assert_eq!(read["content"], "xin chào\nhello");
        assert_eq!(read["sha256"], written["sha256"]);
        assert_eq!(write_file(&target, "again", "utf-8", false, false, 1024).unwrap_err().code, ErrorCode::Conflict);
        assert_eq!(write_file(&target, "again", "utf-8", true, false, 1024).unwrap()["created"], false);
        assert_eq!(read_file(&dir.path().join("missing.txt"), 10, None).unwrap_err().code, ErrorCode::NotFound);
    }

    #[test]
    fn encodings_and_binary_detection() {
        let dir = tempfile::tempdir().unwrap();
        let blob = dir.path().join("b.bin");
        fs::write(&blob, b"\x00\x01\x02abc").unwrap();
        assert_eq!(read_file(&blob, 100, None).unwrap()["encoding"], "base64");
        let ansi = dir.path().join("ansi.txt");
        fs::write(&ansi, b"caf\xe9").unwrap();
        let read = read_file(&ansi, 100, None).unwrap();
        assert_eq!((read["encoding"].as_str(), read["content"].as_str()), (Some("cp1252"), Some("café")));
        let utf16 = dir.path().join("u16.txt");
        write_file(&utf16, "Việt", "utf-16", false, true, 100).unwrap();
        assert_eq!(read_file(&utf16, 100, None).unwrap()["content"], "Việt");
        write_file(&ansi, "Cà phê", "cp1258", true, true, 100).unwrap();
        assert_eq!(read_file(&ansi, 100, Some("cp1258")).unwrap()["content"], "Cà phê");
        assert_eq!(write_file(&ansi, "日本", "cp1252", true, true, 100).unwrap_err().code, ErrorCode::InvalidArgument);
    }

    #[test]
    fn listing_sorts_directories_first_and_truncates() {
        let dir = tempfile::tempdir().unwrap();
        for name in ["b.txt", "A.txt", "c.txt"] {
            fs::write(dir.path().join(name), "x").unwrap();
        }
        fs::create_dir(dir.path().join("zdir")).unwrap();
        let (entries, truncated) = list_directory(dir.path(), 3).unwrap();
        let names: Vec<&str> = entries.iter().map(|e| e["name"].as_str().unwrap()).collect();
        assert_eq!(names, vec!["zdir", "A.txt", "b.txt"]);
        assert!(truncated);
        assert_eq!(entries[0]["size"], Value::Null);
    }

    #[test]
    fn paths_are_expanded_and_must_be_absolute() {
        std::env::set_var("LGA_TEST_DIR", r"C:\data");
        assert_eq!(absolute_path(r"%LGA_TEST_DIR%\x\..\y.txt").unwrap(), PathBuf::from(r"C:\data\y.txt"));
        assert_eq!(absolute_path("$LGA_TEST_DIR/z").unwrap(), PathBuf::from(r"C:\data\z"));
        assert_eq!(absolute_path("relative.txt").unwrap_err().code, ErrorCode::InvalidArgument);
        assert_eq!(stat(Path::new(r"C:\definitely\missing\file"), 10).unwrap()["exists"], false);
    }
}
