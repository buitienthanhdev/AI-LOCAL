//! Secret resolution: process environment, then the repo `.env`, then Windows Credential Manager
//! (generic credential "local-gemini-agent/<name>"). Values are never logged.

use std::path::Path;

pub const CREDENTIAL_TARGET_PREFIX: &str = "local-gemini-agent/";

fn read_dotenv(root: &Path, name: &str) -> Option<String> {
    let text = std::fs::read_to_string(root.join(".env")).ok()?;
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else { continue };
        if key.trim() != name {
            continue;
        }
        let value = value.trim();
        let unquoted = if value.len() >= 2
            && ((value.starts_with('"') && value.ends_with('"')) || (value.starts_with('\'') && value.ends_with('\'')))
        {
            &value[1..value.len() - 1]
        } else {
            value
        };
        if !unquoted.is_empty() {
            return Some(unquoted.to_string());
        }
    }
    None
}

#[cfg(windows)]
fn read_credential_manager(name: &str) -> Option<String> {
    use windows_sys::Win32::Security::Credentials::{CredFree, CredReadW, CREDENTIALW, CRED_TYPE_GENERIC};

    let target: Vec<u16> = format!("{CREDENTIAL_TARGET_PREFIX}{name}").encode_utf16().chain(std::iter::once(0)).collect();
    let mut credential: *mut CREDENTIALW = std::ptr::null_mut();
    // SAFETY: target is NUL-terminated UTF-16; on success `credential` points to a block owned by the
    // credential manager that is released with CredFree before returning.
    unsafe {
        if CredReadW(target.as_ptr(), CRED_TYPE_GENERIC, 0, &mut credential) == 0 || credential.is_null() {
            return None;
        }
        let blob_size = (*credential).CredentialBlobSize as usize;
        let blob = (*credential).CredentialBlob;
        let value = if blob.is_null() || blob_size < 2 {
            None
        } else {
            let units = std::slice::from_raw_parts(blob as *const u16, blob_size / 2);
            String::from_utf16(units).ok()
        };
        CredFree(credential as *const _);
        value.filter(|v| !v.is_empty())
    }
}

#[cfg(not(windows))]
fn read_credential_manager(_name: &str) -> Option<String> {
    None
}

pub fn resolve_secret(root: &Path, name: &str) -> Option<String> {
    if name.is_empty() {
        return None;
    }
    if let Ok(value) = std::env::var(name) {
        if !value.is_empty() {
            return Some(value);
        }
    }
    read_dotenv(root, name).or_else(|| read_credential_manager(name))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dotenv_parsing_handles_quotes_and_comments() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join(".env"), "# comment\nA=1\nB=\"two\"\nC='three'\n").unwrap();
        assert_eq!(read_dotenv(dir.path(), "A").as_deref(), Some("1"));
        assert_eq!(read_dotenv(dir.path(), "B").as_deref(), Some("two"));
        assert_eq!(read_dotenv(dir.path(), "C").as_deref(), Some("three"));
        assert_eq!(read_dotenv(dir.path(), "D"), None);
    }
}
