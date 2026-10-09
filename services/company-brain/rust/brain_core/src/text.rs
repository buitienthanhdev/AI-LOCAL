//! Chuẩn hoá tiếng Việt — bản Rust của `brain/facts.py::norm`, `brain/vn.py::plain`, `brain/vn.py::telex_variants`.
use std::collections::BTreeSet;
use unicode_normalization::char::canonical_combining_class;
use unicode_normalization::UnicodeNormalization;

/// `vn.plain`: đ->d, NFKD, bỏ ký tự combining (ccc != 0), rồi chữ thường (Python `str.lower`).
pub fn plain(s: &str) -> String {
    let replaced = s.replace('đ', "d").replace('Đ', "D");
    let stripped: String = replaced.nfkd().filter(|c| canonical_combining_class(*c) == 0).collect();
    stripped.to_lowercase()
}

/// `facts.norm`: plain() rồi thay mọi chuỗi ký tự ngoài [a-z0-9.%/] bằng MỘT dấu cách, bỏ cách hai đầu.
pub fn norm(s: &str) -> String {
    let p = plain(s);
    let mut out = String::with_capacity(p.len());
    let mut gap = false;
    for c in p.chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() || c == '.' || c == '%' || c == '/' {
            if gap && !out.is_empty() {
                out.push(' ');
            }
            gap = false;
            out.push(c);
        } else {
            gap = true;
        }
    }
    out
}

fn tone_key(m: char) -> Option<&'static str> {
    match m {
        '\u{301}' => Some("s"),
        '\u{300}' => Some("f"),
        '\u{309}' => Some("r"),
        '\u{303}' => Some("x"),
        '\u{323}' => Some("j"),
        _ => None,
    }
}

fn base_keys(hat: &str) -> Option<&'static [&'static str]> {
    Some(match hat {
        "â" => &["aa"],
        "ă" => &["aw"],
        "ê" => &["ee"],
        "ô" => &["oo"],
        "ơ" => &["ow", "w"],
        "ư" => &["w", "uw"],
        "đ" => &["dd"],
        _ => return None,
    })
}

/// `vn._syllable_variants` (giữ đúng thứ tự sinh và giới hạn 32 tổ hợp).
pub fn syllable_variants(word: &str) -> BTreeSet<String> {
    let mut tones: Vec<&str> = Vec::new();
    let mut chars: Vec<Vec<String>> = Vec::new();
    let lowered: String = word.to_lowercase().nfc().collect();
    for ch in lowered.chars() {
        let d: Vec<char> = ch.to_string().nfd().collect();
        let base = d[0];
        let marks = &d[1..];
        if let Some(t) = marks.iter().find_map(|m| tone_key(*m)) {
            tones.push(t);
        }
        let mut hat = String::new();
        hat.push(base);
        hat.extend(marks.iter().filter(|m| tone_key(**m).is_none()));
        let hat: String = hat.nfc().collect();
        chars.push(match base_keys(&hat) {
            Some(opts) => opts.iter().map(|s| s.to_string()).collect(),
            None => vec![plain(&hat)],
        });
    }
    let mut seqs = vec![String::new()];
    for opts in &chars {
        let mut next = Vec::new();
        'outer: for s in &seqs {
            for o in opts {
                next.push(format!("{s}{o}"));
                if next.len() >= 32 {
                    break 'outer;
                }
            }
        }
        seqs = next;
    }
    let mut out: BTreeSet<String> = seqs.iter().cloned().collect();
    if let Some(t) = tones.first() {
        for s in &seqs {
            let cs: Vec<char> = s.chars().collect();
            for i in 0..=cs.len() {
                let mut v: String = cs[..i].iter().collect();
                v.push_str(t);
                v.extend(&cs[i..]);
                out.insert(v);
            }
        }
    }
    out
}

/// `vn.telex_variants`. Khác biệt CÓ CHỦ ĐÍCH: Python sắp `sorted(set, key=len)` nên khi cắt 12 biến thể các chuỗi cùng độ
/// dài được chọn theo thứ tự băm ngẫu nhiên của set (thay đổi mỗi lần chạy). Rust sắp (độ dài, chuỗi) => xác định.
/// Khi không bị cắt, hai bản cho cùng một tập kết quả.
pub fn telex_variants(text: &str, limit: usize) -> BTreeSet<String> {
    let words: Vec<&str> = text.split_whitespace().collect();
    let mut result = BTreeSet::new();
    result.insert(plain(text));
    if words.is_empty() || words.len() > 4 {
        return result;
    }
    let mut combos = vec![String::new()];
    for w in words {
        let mut vs: Vec<String> = syllable_variants(w).into_iter().collect();
        vs.sort_by(|a, b| a.chars().count().cmp(&b.chars().count()).then_with(|| a.cmp(b)));
        vs.truncate(12);
        let mut next = Vec::new();
        'outer: for c in &combos {
            for v in &vs {
                next.push(format!("{c} {v}").trim().to_string());
                if next.len() >= limit {
                    break 'outer;
                }
            }
        }
        combos = next;
    }
    result.extend(combos);
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn norm_matches_python_examples() {
        assert_eq!(norm("Báo Cáo Tuần.docx"), "bao cao tuan.docx");
        assert_eq!(norm("  Đường  ống —  pH 8,5%  "), "duong ong ph 8 5%");
        assert_eq!(norm("Ａｂｃ１"), "abc1"); // NFKD full-width
        assert_eq!(norm("M500_Formula_v14.xlsx"), "m500 formula v14.xlsx");
        assert_eq!(norm("日本語"), "");
        assert_eq!(norm("C:/Users/x"), "c /users/x");
    }

    #[test]
    fn plain_keeps_punctuation() {
        assert_eq!(plain("Ửod — Đà Lạt!"), "uod — da lat!");
    }

    #[test]
    fn telex_recovers_english_words() {
        assert!(telex_variants("ửod", 64).contains("word"));
        assert!(telex_variants("ẽcel", 64).contains("excel"));
    }
}
