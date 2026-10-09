//! Company Brain system core. Mọi hàm ở đây phải cho kết quả Y HỆT bản Python tương ứng
//! (xem docs/MIGRATION_MAP.md) — dữ liệu đã lưu (name_key, subject_key, sha256…) phụ thuộc vào chúng.
pub mod hash;
pub mod scan;
pub mod sysmon;
pub mod text;
