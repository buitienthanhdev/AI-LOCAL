//! Secret redaction for everything the core writes to logs, task state and memory.
//! Mirrors `lga_common/redact.py`.

use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value;

pub const REDACTED: &str = "[REDACTED]";
const MIN_LITERAL_LENGTH: usize = 6;

struct Rule {
    pattern: Regex,
    keep_prefix: bool,
}

fn rules() -> &'static [Rule] {
    static RULES: OnceLock<Vec<Rule>> = OnceLock::new();
    RULES.get_or_init(|| {
        let keep = |p: &str| Rule { pattern: Regex::new(p).expect("valid redaction regex"), keep_prefix: true };
        let whole = |p: &str| Rule { pattern: Regex::new(p).expect("valid redaction regex"), keep_prefix: false };
        vec![
            keep(r#"(\b(?:__Secure-[0-9A-Za-z]+PSID[A-Z]*|SAPISID|APISID|HSID|SSID|SID|NID)=)[^;\s"']+"#),
            keep(r"(?i)(\b(?:set-)?cookie\s*[:=]\s*)[^\r\n]+"),
            keep(r#"(?i)(\bauthorization\s*[:=]\s*)[^\r\n"',]+"#),
            keep(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
            keep(
                r#"(?i)(["']?\b(?:api[ _-]?key|x-api-key|access[_-]?token|refresh[_-]?token|admin[_-]?token|client[_-]?secret|secret|password|passwd)["']?\s*[:=]\s*["']?)[^"'\s,}]+"#,
            ),
            whole(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}|ghp_[A-Za-z0-9]{30,})\b"),
            // AWS access key id, JWT, Slack token, PEM private key block.
            whole(r"\bAKIA[0-9A-Z]{16}\b"),
            whole(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
            whole(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
            whole(r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
        ]
    })
}

#[derive(Debug, Clone, Default)]
pub struct Redactor {
    literals: Vec<String>,
}

impl Redactor {
    pub fn new() -> Self {
        Self::default()
    }

    /// Always redact this exact value (a resolved secret).
    pub fn add_literal(&mut self, value: Option<&str>) {
        if let Some(value) = value {
            if value.len() >= MIN_LITERAL_LENGTH && !self.literals.iter().any(|l| l == value) {
                self.literals.push(value.to_string());
            }
        }
    }

    pub fn redact(&self, text: &str) -> String {
        let mut out = text.to_string();
        for literal in &self.literals {
            if out.contains(literal.as_str()) {
                out = out.replace(literal.as_str(), REDACTED);
            }
        }
        for rule in rules() {
            out = if rule.keep_prefix {
                rule.pattern.replace_all(&out, format!("${{1}}{REDACTED}").as_str()).into_owned()
            } else {
                rule.pattern.replace_all(&out, REDACTED).into_owned()
            };
        }
        out
    }

    /// Redact every string inside a JSON value.
    pub fn redact_value(&self, value: &Value) -> Value {
        match value {
            Value::String(s) => Value::String(self.redact(s)),
            Value::Array(items) => Value::Array(items.iter().map(|v| self.redact_value(v)).collect()),
            Value::Object(map) => {
                Value::Object(map.iter().map(|(k, v)| (k.clone(), self.redact_value(v))).collect())
            }
            other => other.clone(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redacts_known_secret_shapes() {
        let r = Redactor::new();
        for (input, secret) in [
            ("Cookie: __Secure-1PSID=abcdef123456; NID=zzz", "abcdef123456"),
            ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
            ("{\"api_key\": \"k-1234567890\"}", "k-1234567890"),
            ("  API key:     p5w8IV8s8rWF...VhPE", "p5w8IV8s8rWF"),
            ("x sk-abcdefghijklmnopqrstuv y", "sk-abcdefghijklmnopqrstuv"),
        ] {
            let out = r.redact(input);
            assert!(!out.contains(secret), "{input} -> {out}");
            assert!(out.contains(REDACTED));
        }
    }

    #[test]
    fn redacts_literals_inside_json() {
        let mut r = Redactor::new();
        r.add_literal(Some("very-secret-token"));
        let v = serde_json::json!({"a": ["x very-secret-token y"]});
        assert!(!r.redact_value(&v).to_string().contains("very-secret-token"));
    }
}
