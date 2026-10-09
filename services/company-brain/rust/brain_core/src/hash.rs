//! SHA-256 dạng hex, đọc streaming 1 MiB — y hệt `scanner._sha256` (hashlib). KHÔNG đổi thuật toán: sha256 đã lưu ở
//! files.sha256, indexed_sha256, facts_sha256, file_versions.sha256 và tên snapshot.
use rayon::prelude::*;
use sha2::{Digest, Sha256};
use std::fs::File;
use std::io::{self, Read};
use std::path::Path;

pub fn hash_file(path: &Path) -> io::Result<String> {
    let mut f = File::open(path)?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(hex::encode(h.finalize()))
}

/// Băm nhiều file song song với số luồng giới hạn (governor quyết định). Lỗi từng file => None, không dừng cả lô.
pub fn hash_files(paths: &[String], threads: usize) -> Vec<Option<String>> {
    let pool = rayon::ThreadPoolBuilder::new().num_threads(threads.max(1)).build().expect("thread pool");
    pool.install(|| paths.par_iter().map(|p| hash_file(Path::new(p)).ok()).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_digest() {
        let p = std::env::temp_dir().join("brain_core_hash_test.txt");
        std::fs::write(&p, b"abc").unwrap();
        assert_eq!(hash_file(&p).unwrap(), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
        let _ = std::fs::remove_file(p);
    }
}
