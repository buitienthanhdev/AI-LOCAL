//! The helpers around the planner's "main brain" (a small local model): deterministic regions that cover what it does
//! badly, so the whole is smarter than the model alone. Each region is small, testable and never calls a model.
//!
//! * `cerebellum`: motor correction. A tool call whose arguments only break the schema in trivial ways (an unknown
//!   key, a number written as text, a path under another name) is repaired and run instead of costing a planner turn.
//! * `cingulate`: error detection. A task is not allowed to finish while a file it wrote still holds a placeholder
//!   (`<result>`, `TODO`), an Excel error value, or the answer itself is a placeholder.
//! * `parietal` (numbers) lives in the worker as the `data.aggregate` tool.
//!
//! Every intervention is written to the audit log as a `cortex` event (region + note) and shown in the activity panel.

use serde_json::{json, Map, Value};

use crate::state::{StepRecord, StepStatus};

/// Names a model uses for the property the schema calls something else (tried only when the schema lacks the name).
const ALIASES: [(&str, &[&str]); 9] = [
    ("path", &["file", "filepath", "file_path", "filename", "file_name", "target_path", "directory", "dir", "folder", "location"]),
    ("content", &["text", "body", "contents", "data", "value"]),
    ("command", &["cmd", "script", "shell", "code_line"]),
    ("query", &["q", "search", "keyword", "keywords", "term", "text"]),
    ("url", &["link", "href", "address", "website"]),
    ("code", &["script", "python", "source", "program"]),
    ("target", &["app", "application", "program", "name", "exe"]),
    ("summary", &["answer", "result", "message", "final_answer"]),
    ("question", &["ask", "prompt", "message"]),
];

fn type_of(schema: &Value) -> Option<&str> {
    schema.get("type").and_then(Value::as_str)
}

fn coerce(value: &Value, schema: &Value) -> Option<Value> {
    match (type_of(schema)?, value) {
        ("integer", Value::String(s)) => s.trim().parse::<i64>().ok().map(|n| json!(n)),
        ("integer", Value::Number(n)) if n.as_f64().is_some_and(|f| f.fract() == 0.0 && n.as_i64().is_none()) => n.as_f64().map(|f| json!(f as i64)),
        ("number", Value::String(s)) => s.trim().parse::<f64>().ok().map(|n| json!(n)),
        ("string", Value::Number(n)) => Some(json!(n.to_string())),
        ("string", Value::Bool(b)) => Some(json!(b.to_string())),
        ("boolean", Value::String(s)) => match s.trim().to_lowercase().as_str() {
            "true" | "yes" | "có" | "co" | "1" | "on" => Some(json!(true)),
            "false" | "no" | "không" | "khong" | "0" | "off" => Some(json!(false)),
            _ => None,
        },
        ("array", v) if !v.is_array() && !v.is_null() && schema.get("items").is_some_and(|i| type_of(i).is_none_or(|t| t != "array")) => {
            let item_ok = schema.get("items").and_then(type_of).is_none_or(|t| match (t, v) {
                ("string", Value::String(_)) | ("object", Value::Object(_)) | ("number" | "integer", Value::Number(_)) => true,
                _ => false,
            });
            item_ok.then(|| json!([v.clone()]))
        }
        _ => None,
    }
}

/// Repair trivially wrong arguments against the tool's input schema. Returns the new arguments and what was changed,
/// or None when nothing could be fixed this way. The caller validates the result again before running anything.
pub fn refine_args(schema: &Value, args: &Value) -> Option<(Value, Vec<String>)> {
    let given = args.as_object()?;
    let props = schema.get("properties").and_then(Value::as_object)?;
    let closed = schema.get("additionalProperties") == Some(&Value::Bool(false));
    let mut out = Map::new();
    let mut notes: Vec<String> = Vec::new();
    for (key, value) in given {
        if props.contains_key(key) {
            out.insert(key.clone(), value.clone());
            continue;
        }
        // unknown key: an alias of a property the call does not have yet, otherwise dropped (when the schema is closed)
        let target = ALIASES.iter().find_map(|(real, names)| {
            (props.contains_key(*real) && names.contains(&key.as_str()) && !given.contains_key(*real) && !out.contains_key(*real)).then_some(*real)
        });
        if let Some(real) = target {
            out.insert(real.to_string(), value.clone());
            notes.push(format!("`{key}` -> `{real}`"));
        } else if closed {
            notes.push(format!("dropped unknown `{key}`"));
        } else {
            out.insert(key.clone(), value.clone());
        }
    }
    for (key, prop) in props {
        if let Some(value) = out.get(key) {
            if let Some(fixed) = coerce(value, prop) {
                if &fixed != value {
                    notes.push(format!("`{key}` converted to {}", type_of(prop).unwrap_or("the right type")));
                    out.insert(key.clone(), fixed);
                }
            }
        }
    }
    // a required key that is missing but has a documented default
    for key in schema.get("required").and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_str) {
        if !out.contains_key(key) {
            if let Some(default) = props.get(key).and_then(|p| p.get("default")) {
                out.insert(key.to_string(), default.clone());
                notes.push(format!("`{key}` set to its default"));
            }
        }
    }
    (!notes.is_empty()).then(|| (Value::Object(out), notes))
}

/// Text that stands where a real value should be.
pub fn placeholder_in(text: &str) -> Option<String> {
    let lower = text.to_lowercase();
    for marker in ["<result>", "<kết quả>", "<ket qua>", "<số>", "<so>", "<value>", "<giá trị>", "<gia tri>", "<total>", "<tổng>", "<tong>", "<answer>", "[điền", "[dien", "lorem ipsum"] {
        if lower.contains(marker) {
            return Some(marker.to_string());
        }
    }
    for err in ["#div/0!", "#value!", "#n/a", "#ref!", "#name?", "#num!", "#null!"] {
        if lower.contains(err) {
            return Some(err.to_uppercase());
        }
    }
    None
}

/// Why a task must not finish yet, from what it has seen: its own answer is a placeholder, or the latest read of a
/// file shows a placeholder / error value and nothing was written after that read.
pub fn unfinished_work(steps: &[StepRecord], summary: &str) -> Option<String> {
    if let Some(p) = placeholder_in(summary) {
        return Some(format!("the answer itself contains the placeholder {p}"));
    }
    let writes = |t: &str| t.starts_with("filesystem.write") || t.starts_with("office.") && t.contains("write") || t == "python.exec" || t == "shell.exec" || t.starts_with("office.excel_set") || t.starts_with("desktop.");
    let reads = |t: &str| matches!(t, "filesystem.read" | "document.read" | "office.excel_read" | "office.word_read");
    let last_bad_read = steps.iter().rposition(|s| s.status == StepStatus::Succeeded && reads(&s.tool) && placeholder_in(&s.observation).is_some())?;
    let written_after = steps[last_bad_read + 1..].iter().any(|s| s.status == StepStatus::Succeeded && writes(&s.tool));
    if written_after {
        return None;
    }
    let step = &steps[last_bad_read];
    let found = placeholder_in(&step.observation)?;
    Some(format!("{} (step {}) still shows {found}: the file or cell is not finished", step.tool, step.step_id))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn schema() -> Value {
        json!({"type": "object", "required": ["path"], "additionalProperties": false, "properties": {
            "path": {"type": "string"}, "limit": {"type": "integer"}, "recursive": {"type": "boolean"}, "names": {"type": "array", "items": {"type": "string"}},
            "mode": {"type": "string", "default": "read"}}})
    }

    #[test]
    fn unknown_keys_are_renamed_or_dropped_and_types_are_fixed() {
        let (fixed, notes) = refine_args(&schema(), &json!({"file": "C:\\a.txt", "limit": "5", "recursive": "yes", "names": "x.txt", "save": true})).unwrap();
        assert_eq!(fixed, json!({"path": "C:\\a.txt", "limit": 5, "recursive": true, "names": ["x.txt"]}));
        assert!(notes.iter().any(|n| n.contains("`file` -> `path`")) && notes.iter().any(|n| n.contains("dropped unknown `save`")), "{notes:?}");
    }

    #[test]
    fn a_valid_call_is_left_alone_and_an_alias_never_overwrites_a_real_value() {
        assert!(refine_args(&schema(), &json!({"path": "C:\\a.txt", "limit": 3})).is_none());
        let (fixed, _) = refine_args(&schema(), &json!({"path": "C:\\real.txt", "file": "C:\\other.txt"})).unwrap();
        assert_eq!(fixed["path"], "C:\\real.txt");
        assert!(fixed.get("file").is_none());
    }

    #[test]
    fn open_schemas_keep_unknown_keys() {
        let open = json!({"type": "object", "properties": {"a": {"type": "string"}}});
        assert!(refine_args(&open, &json!({"a": "x", "b": 1})).is_none());
    }

    #[test]
    fn placeholders_and_error_values_are_found() {
        assert_eq!(placeholder_in("Tổng doanh thu: <result>").as_deref(), Some("<result>"));
        assert_eq!(placeholder_in("B5 = #DIV/0!").as_deref(), Some("#DIV/0!"));
        assert_eq!(placeholder_in("Tổng doanh thu: 1500"), None);
        assert_eq!(placeholder_in("<p>HTML</p>"), None);
    }

    fn step(id: &str, tool: &str, status: StepStatus, observation: &str) -> StepRecord {
        serde_json::from_value(json!({"step_id": id, "index": 1, "started_at": "2026-10-16T00:00:00Z", "finished_at": null, "thought": "", "tool": tool,
            "args": {}, "input_hash": id, "status": match status { StepStatus::Succeeded => "succeeded", _ => "failed" }, "verification": null,
            "observation": observation, "error": null, "retry_count": 0, "duration_ms": 1, "backend": "", "provider": "", "artifacts": []})).unwrap()
    }

    #[test]
    fn a_task_cannot_finish_over_a_placeholder_it_has_just_read() {
        let steps = vec![step("s001", "python.exec", StepStatus::Succeeded, "ok"), step("s002", "filesystem.read", StepStatus::Succeeded, "Tổng doanh thu: <result>")];
        assert!(unfinished_work(&steps, "Xong.").unwrap().contains("<result>"));
        assert!(unfinished_work(&steps[..1], "Tổng là <số>").unwrap().contains("answer itself"));
        let mut fixed = steps.clone();
        fixed.push(step("s003", "python.exec", StepStatus::Succeeded, "wrote 1500"));
        assert!(unfinished_work(&fixed, "Xong.").is_none(), "a write after the bad read clears it");
        assert!(unfinished_work(&steps[..1], "Xong.").is_none());
    }
}
