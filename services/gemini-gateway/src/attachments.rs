//! In-memory attachment store backing `POST /v1/upload`.
//!
//! Attachments are referenced by ID in chat messages and encoded per backend when a request is sent.
//! They live in memory only, are bounded in count and total bytes, and expire after a TTL.

use std::collections::VecDeque;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use base64::Engine;

const ALLOWED_MIME_TYPES: [&str; 4] = ["image/png", "image/jpeg", "image/webp", "image/gif"];

#[derive(Debug, Clone)]
pub struct Attachment {
    pub attachment_id: String,
    pub mime_type: String,
    pub data: Arc<Vec<u8>>,
    pub filename: String,
    created_at: Instant,
}

impl Attachment {
    pub fn data_url(&self) -> String {
        format!("data:{};base64,{}", self.mime_type, base64::engine::general_purpose::STANDARD.encode(self.data.as_slice()))
    }
}

pub fn sniff_mime(data: &[u8], filename: &str) -> Result<&'static str, String> {
    if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        return Ok("image/png");
    }
    if data.starts_with(b"\xff\xd8\xff") {
        return Ok("image/jpeg");
    }
    if data.starts_with(b"GIF8") {
        return Ok("image/gif");
    }
    if data.len() >= 12 && &data[..4] == b"RIFF" && &data[8..12] == b"WEBP" {
        return Ok("image/webp");
    }
    let ext = Path::new(filename).extension().and_then(|e| e.to_str()).map(str::to_ascii_lowercase);
    match ext.as_deref() {
        Some("png") => Ok("image/png"),
        Some("jpg" | "jpeg") => Ok("image/jpeg"),
        Some("webp") => Ok("image/webp"),
        Some("gif") => Ok("image/gif"),
        _ => Err(format!("unsupported attachment type for {filename:?}")),
    }
}

pub struct AttachmentStore {
    max_each: usize,
    max_total: usize,
    ttl: Duration,
    items: Mutex<(VecDeque<Attachment>, usize)>,
}

impl AttachmentStore {
    pub fn new(max_each: usize, max_total: usize, ttl: Duration) -> Self {
        Self { max_each, max_total, ttl, items: Mutex::new((VecDeque::new(), 0)) }
    }

    fn expire(&self, items: &mut (VecDeque<Attachment>, usize)) {
        let now = Instant::now();
        let ttl = self.ttl;
        items.0.retain(|a| {
            let keep = now.duration_since(a.created_at) <= ttl;
            if !keep {
                items.1 -= a.data.len();
            }
            keep
        });
    }

    pub fn put(&self, data: Vec<u8>, filename: &str, mime_type: Option<&str>) -> Result<Attachment, String> {
        if data.is_empty() {
            return Err("attachment is empty".into());
        }
        if data.len() > self.max_each {
            return Err(format!("attachment is {} bytes; limit is {}", data.len(), self.max_each));
        }
        let mime = match mime_type {
            Some(m) => m.to_string(),
            None => sniff_mime(&data, filename)?.to_string(),
        };
        if !ALLOWED_MIME_TYPES.contains(&mime.as_str()) {
            return Err(format!("mime type {mime} is not allowed"));
        }
        let name = Path::new(filename).file_name().and_then(|n| n.to_str()).filter(|n| !n.is_empty()).unwrap_or("image");
        let attachment = Attachment {
            attachment_id: format!("att_{}", hex::encode(rand::random::<[u8; 8]>())),
            mime_type: mime,
            data: Arc::new(data),
            filename: name.to_string(),
            created_at: Instant::now(),
        };
        let mut items = self.items.lock().expect("attachment store lock");
        self.expire(&mut items);
        while !items.0.is_empty() && items.1 + attachment.data.len() > self.max_total {
            if let Some(evicted) = items.0.pop_front() {
                items.1 -= evicted.data.len();
            }
        }
        items.1 += attachment.data.len();
        items.0.push_back(attachment.clone());
        Ok(attachment)
    }

    pub fn put_file(&self, path: &Path) -> Result<Attachment, String> {
        let meta = std::fs::metadata(path).map_err(|_| format!("{} is not a file", path.display()))?;
        if !meta.is_file() {
            return Err(format!("{} is not a file", path.display()));
        }
        if meta.len() as usize > self.max_each {
            return Err(format!("{} is {} bytes; limit is {}", path.display(), meta.len(), self.max_each));
        }
        let data = std::fs::read(path).map_err(|e| format!("{}: {e}", path.display()))?;
        self.put(data, &path.to_string_lossy(), None)
    }

    pub fn get(&self, attachment_id: &str) -> Result<Attachment, String> {
        let mut items = self.items.lock().expect("attachment store lock");
        self.expire(&mut items);
        items
            .0
            .iter()
            .find(|a| a.attachment_id == attachment_id)
            .cloned()
            .ok_or_else(|| format!("unknown or expired attachment {attachment_id:?}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PNG: &[u8] = b"\x89PNG\r\n\x1a\n0000";

    #[test]
    fn stores_sniffs_and_evicts() {
        let store = AttachmentStore::new(100, 20, Duration::from_secs(60));
        let a = store.put(PNG.to_vec(), "x.bin", None).unwrap();
        assert_eq!(a.mime_type, "image/png");
        assert!(a.data_url().starts_with("data:image/png;base64,"));
        let b = store.put(PNG.to_vec(), "y.png", None).unwrap();
        assert!(store.get(&a.attachment_id).is_err(), "oldest evicted when over the total limit");
        assert!(store.get(&b.attachment_id).is_ok());
        assert!(store.put(b"not an image".to_vec(), "x.txt", None).is_err());
        assert!(store.put(vec![0; 101], "x.png", None).is_err());
    }
}
