//! The unified tool contract (tools/*.json) as seen by the core: loading, argument validation and the
//! catalog text shown to the planner. Core pseudo-tools (`task.*`) are defined here.

use std::collections::BTreeMap;
use std::path::Path;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RiskLevel {
    Low,
    Medium,
    High,
    Critical,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum VerificationMode {
    #[serde(rename = "self")]
    SelfCheck,
    Observe,
    None,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolSpec {
    pub name: String,
    pub description: String,
    #[serde(default)]
    pub category: String,
    pub input_schema: Value,
    pub output_schema: Value,
    pub timeout_ms: u64,
    pub risk_level: RiskLevel,
    pub requires_confirmation: bool,
    pub idempotent: bool,
    pub supports_rollback: bool,
    pub verification: VerificationMode,
    pub policy_category: String,
    /// "none" | "possible" | "yes". Defaults from `verification`: read-only tools have none.
    #[serde(default)]
    pub side_effects: Option<String>,
}

impl ToolSpec {
    /// True when a successful call definitely changed state (files, apps, documents, pages).
    pub fn changes_state(&self) -> bool {
        match self.side_effects.as_deref() {
            Some("none") | Some("possible") => false,
            Some(_) => true,
            None => self.verification != VerificationMode::None,
        }
    }

    /// Executed by the core itself rather than the worker.
    pub fn is_core_tool(&self) -> bool {
        self.name.starts_with("task.")
    }
}

#[derive(Debug, Deserialize)]
struct ContractFile {
    category: String,
    tools: Vec<ToolSpec>,
    #[serde(default, rename = "$defs")]
    defs: Map<String, Value>,
}

pub struct ToolCatalog {
    specs: BTreeMap<String, ToolSpec>,
    validators: BTreeMap<String, jsonschema::Validator>,
}

fn with_defs(schema: &Value, defs: &Map<String, Value>) -> Value {
    if defs.is_empty() {
        return schema.clone();
    }
    let mut merged = schema.clone();
    if let Value::Object(map) = &mut merged {
        let mut all = defs.clone();
        if let Some(Value::Object(own)) = map.get("$defs") {
            all.extend(own.clone());
        }
        map.insert("$defs".into(), Value::Object(all));
    }
    merged
}

fn core_tools() -> Vec<ToolSpec> {
    let base = |name: &str, description: &str, input: Value| ToolSpec {
        name: name.into(),
        description: description.into(),
        category: "task".into(),
        input_schema: input,
        output_schema: json!({"type": "object"}),
        timeout_ms: 300_000,
        risk_level: RiskLevel::Low,
        requires_confirmation: false,
        idempotent: true,
        supports_rollback: false,
        verification: VerificationMode::SelfCheck,
        policy_category: "task".into(),
        side_effects: Some("none".into()),
    };
    vec![
        base(
            "task.complete",
            "Declare the task finished. If any step changed state (files, apps, drawings, web forms), give              `checks`: read-only tool calls (filesystem.stat, desktop.find_element, browser.get_state,              autocad.verify_geometry, ...) with the values they must return; the core re-runs them and              accepts completion only if every check passes. For pure information requests you may instead              cite `evidence_steps`: ids of earlier successful read-only steps whose results contain the answer.",
            json!({
                "type": "object", "required": ["summary"], "additionalProperties": false,
                "anyOf": [{"required": ["checks"]}, {"required": ["evidence_steps"]}],
                "properties": {
                    "summary": {"type": "string", "minLength": 1},
                    "checks": {"type": "array", "minItems": 1, "items": {
                        "type": "object", "required": ["tool", "args", "expect"], "additionalProperties": false,
                        "properties": {
                            "tool": {"type": "string"},
                            "args": {"type": "object"},
                            "expect": {"type": "object", "minProperties": 1, "description": "Fields of the check's result data that must equal these values, e.g. {\"exists\": true}; dotted keys reach nested fields."}
                        }
                    }},
                    "evidence_steps": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^s[0-9]{3,}$"}},
                    "artifacts": {"type": "array", "items": {"type": "string"}}
                }
            }),
        ),
        base(
            "task.ask_user",
            "Ask the user a question when the task is ambiguous or needs a decision only they can make.",
            json!({"type": "object", "required": ["question"], "additionalProperties": false,
                   "properties": {"question": {"type": "string", "minLength": 1}}}),
        ),
        base(
            "task.fail",
            "Give up with an explanation when the task is impossible or blocked (e.g. needs Administrator rights).",
            json!({"type": "object", "required": ["reason"], "additionalProperties": false,
                   "properties": {"reason": {"type": "string", "minLength": 1}}}),
        ),
    ]
}

impl ToolCatalog {
    pub fn load(tools_dir: &Path) -> Result<Self> {
        let mut specs = BTreeMap::new();
        let mut files: Vec<_> = std::fs::read_dir(tools_dir)
            .with_context(|| format!("reading {}", tools_dir.display()))?
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| p.extension().is_some_and(|ext| ext == "json"))
            .collect();
        files.sort();
        if files.is_empty() {
            bail!("no tool contract files in {}", tools_dir.display());
        }
        for path in files {
            let text = std::fs::read_to_string(&path).with_context(|| format!("reading {}", path.display()))?;
            let file: ContractFile =
                serde_json::from_str(&text).with_context(|| format!("parsing {}", path.display()))?;
            for mut spec in file.tools {
                spec.category = file.category.clone();
                spec.input_schema = with_defs(&spec.input_schema, &file.defs);
                spec.output_schema = with_defs(&spec.output_schema, &file.defs);
                if specs.contains_key(&spec.name) {
                    bail!("tool {} defined twice", spec.name);
                }
                specs.insert(spec.name.clone(), spec);
            }
        }
        for spec in core_tools() {
            specs.insert(spec.name.clone(), spec);
        }
        let mut validators = BTreeMap::new();
        for (name, spec) in &specs {
            let validator = jsonschema::validator_for(&spec.input_schema)
                .map_err(|e| anyhow::anyhow!("invalid input_schema for {name}: {e}"))?;
            validators.insert(name.clone(), validator);
        }
        Ok(Self { specs, validators })
    }

    pub fn get(&self, name: &str) -> Option<&ToolSpec> {
        self.specs.get(name)
    }

    pub fn all(&self) -> impl Iterator<Item = &ToolSpec> {
        self.specs.values()
    }

    /// Validate planner-supplied arguments. Returns the list of violations (empty = valid).
    pub fn validate_args(&self, name: &str, args: &Value) -> Vec<String> {
        match self.validators.get(name) {
            None => vec![format!("unknown tool {name}")],
            Some(validator) => validator
                .iter_errors(args)
                .map(|e| {
                    let path = e.instance_path.to_string();
                    format!("{}: {e}", if path.is_empty() { "(root)" } else { &path })
                })
                .collect(),
        }
    }

    /// Compact catalog for the planner prompt, restricted to tools the worker actually serves.
    pub fn render_for_prompt(&self, available: &[String]) -> String {
        let mut out = String::new();
        let mut current = String::new();
        for spec in self.specs.values() {
            if !spec.is_core_tool() && !available.iter().any(|a| a == &spec.name) {
                continue;
            }
            if spec.category != current {
                current = spec.category.clone();
                out.push_str(&format!("\n## {current}\n"));
            }
            let args = compact_schema(&spec.input_schema);
            out.push_str(&format!(
                "- {} {args}\n  {} [risk={:?}, verification={:?}]\n",
                spec.name, spec.description, spec.risk_level, spec.verification
            ));
        }
        out
    }
}

/// Render an input schema as a short signature: {name: type, ?optional: type=default}.
fn compact_schema(schema: &Value) -> String {
    let props = schema.get("properties").and_then(Value::as_object);
    let required: Vec<&str> = schema
        .get("required")
        .and_then(Value::as_array)
        .map(|r| r.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    let Some(props) = props else { return "{}".into() };
    let mut parts = Vec::new();
    for (name, prop) in props {
        let ty = if let Some(values) = prop.get("enum").and_then(Value::as_array) {
            values.iter().map(|v| v.to_string()).collect::<Vec<_>>().join("|")
        } else if let Some(t) = prop.get("type") {
            match t {
                Value::String(s) if s == "array" => {
                    let item = prop.get("items").and_then(|i| i.get("type")).and_then(Value::as_str).unwrap_or("any");
                    format!("{item}[]")
                }
                Value::String(s) => s.clone(),
                Value::Array(a) => a.iter().filter_map(Value::as_str).collect::<Vec<_>>().join("|"),
                _ => "any".into(),
            }
        } else {
            "object".into()
        };
        let default = prop.get("default").map(|d| format!("={d}")).unwrap_or_default();
        let optional = if required.contains(&name.as_str()) { "" } else { "?" };
        parts.push(format!("{optional}{name}: {ty}{default}"));
    }
    if let Some(one_of) = schema.get("oneOf").and_then(Value::as_array) {
        let alternatives: Vec<String> = one_of
            .iter()
            .filter_map(|alt| alt.get("required").and_then(Value::as_array))
            .map(|req| req.iter().filter_map(Value::as_str).collect::<Vec<_>>().join("+"))
            .collect();
        parts.push(format!("(one of: {})", alternatives.join(" | ")));
    }
    format!("{{{}}}", parts.join(", "))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn catalog() -> ToolCatalog {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tools");
        ToolCatalog::load(&root).expect("tool contract loads")
    }

    #[test]
    fn contract_loads_with_core_tools() {
        let c = catalog();
        assert!(c.get("desktop.click").is_some());
        assert!(c.get("task.complete").is_some());
        assert_eq!(c.get("filesystem.write").unwrap().verification, VerificationMode::SelfCheck);
    }

    #[test]
    fn validates_arguments() {
        let c = catalog();
        assert!(c.validate_args("desktop.click", &json!({"x": 1, "y": 2})).is_empty());
        assert!(!c.validate_args("desktop.click", &json!({"x": 1})).is_empty());
        assert!(!c.validate_args("filesystem.write", &json!({"path": "C:\\a.txt"})).is_empty());
        assert!(!c.validate_args("desktop.click", &json!({"x": 1, "y": 2, "bogus": true})).is_empty());
        assert!(!c.validate_args("nope", &json!({})).is_empty());
    }

    #[test]
    fn prompt_catalog_lists_only_available_tools() {
        let c = catalog();
        let text = c.render_for_prompt(&["desktop.click".to_string()]);
        assert!(text.contains("desktop.click"));
        assert!(!text.contains("desktop.drag"));
        assert!(text.contains("task.complete"));
    }
}
