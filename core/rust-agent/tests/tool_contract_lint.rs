//! Contract lint for tools/*.json (gap G6): a tool cannot be added or edited without the metadata the core's
//! decisions depend on. Runs on the real files, offline.

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use serde_json::Value;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

struct Tool {
    file: String,
    name: String,
    raw: Value,
    /// The contract file's `$defs` (schemas may `$ref` into them).
    defs: Value,
}

fn tools() -> Vec<Tool> {
    let mut out = Vec::new();
    for entry in std::fs::read_dir(root().join("tools")).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().and_then(|e| e.to_str()) != Some("json") {
            continue;
        }
        let file = path.file_name().unwrap().to_string_lossy().into_owned();
        let value: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        for raw in value["tools"].as_array().unwrap() {
            out.push(Tool {
                file: file.clone(),
                name: raw["name"].as_str().unwrap().to_string(),
                raw: raw.clone(),
                defs: value.get("$defs").cloned().unwrap_or(Value::Null),
            });
        }
    }
    out
}

fn str_of<'a>(t: &'a Tool, key: &str) -> &'a str {
    t.raw.get(key).and_then(Value::as_str).unwrap_or("")
}

const SIDE_EFFECTS: [&str; 4] = ["none", "possible", "yes", "process"];

fn report(problems: Vec<String>, what: &str) {
    assert!(problems.is_empty(), "{what}:\n  {}", problems.join("\n  "));
}

#[test]
fn every_tool_declares_side_effects_explicitly() {
    let problems: Vec<String> = tools()
        .iter()
        .filter(|t| !SIDE_EFFECTS.contains(&str_of(t, "side_effects")))
        .map(|t| format!("{} ({}): side_effects is {:?}; allowed: {SIDE_EFFECTS:?}", t.name, t.file, t.raw.get("side_effects")))
        .collect();
    report(problems, "tools without an explicit, valid side_effects");
}

#[test]
fn side_effects_agree_with_verification_and_risk() {
    let mut problems = Vec::new();
    for t in tools() {
        let effects = str_of(&t, "side_effects");
        let verification = str_of(&t, "verification");
        let risk = str_of(&t, "risk_level");
        // Completion checks must be read-only: a tool with verification "none" may not claim to change state.
        if verification == "none" && matches!(effects, "yes" | "process") {
            problems.push(format!("{}: verification none but side_effects {effects}", t.name));
        }
        // A tool that changes state must say how its result is confirmed.
        if matches!(effects, "yes" | "process") && verification == "none" {
            problems.push(format!("{}: changes state without any verification mode", t.name));
        }
        // "high" / "critical" risk and "reads only" do not go together.
        if matches!(risk, "high" | "critical") && effects == "none" {
            problems.push(format!("{}: risk {risk} but side_effects none", t.name));
        }
    }
    report(problems, "inconsistent tool metadata");
}

#[test]
fn every_policy_category_has_a_verdict_in_every_mode() {
    // A category missing from a mode table silently becomes "confirm" (policy.rs unwrap_or(Confirm)).
    let text = std::fs::read_to_string(root().join("core/rust-agent/policy/default.toml")).unwrap();
    let policy: toml::Value = toml::from_str(&text).unwrap();
    let modes = policy["modes"].as_table().unwrap();
    let mut problems = Vec::new();
    for t in tools() {
        let category = str_of(&t, "policy_category");
        for (mode, table) in modes {
            if table.get(category).is_none() {
                problems.push(format!("{}: policy_category {category:?} is not in modes.{mode}", t.name));
            }
        }
    }
    report(problems, "policy categories without an explicit verdict");
}

#[test]
fn schemas_timeouts_names_and_descriptions_are_sane() {
    let mut problems = Vec::new();
    let mut seen = BTreeSet::new();
    for t in tools() {
        if !seen.insert(t.name.clone()) {
            problems.push(format!("{}: defined twice", t.name));
        }
        if !t.name.contains('.') || t.name.contains(' ') {
            problems.push(format!("{}: name must be <group>.<action>", t.name));
        }
        if str_of(&t, "description").trim().len() < 20 {
            problems.push(format!("{}: description is too short to guide the planner", t.name));
        }
        let timeout = t.raw["timeout_ms"].as_u64().unwrap_or(0);
        if timeout == 0 || timeout > 3_600_000 {
            problems.push(format!("{}: timeout_ms {timeout} out of range", t.name));
        }
        for key in ["input_schema", "output_schema"] {
            let schema = &t.raw[key];
            // An object schema, directly or through a $ref into the file's $defs.
            let resolved = match schema["$ref"].as_str().and_then(|r| r.strip_prefix("#/$defs/")) {
                Some(name) => &t.defs[name],
                None => schema,
            };
            if resolved["type"] != "object" {
                problems.push(format!("{}: {key} must be an object schema (directly or via $ref)", t.name));
            }
        }
        for key in ["requires_confirmation", "idempotent", "supports_rollback"] {
            if !t.raw[key].is_boolean() {
                problems.push(format!("{}: {key} must be a boolean", t.name));
            }
        }
        if !matches!(str_of(&t, "verification"), "self" | "observe" | "none") {
            problems.push(format!("{}: verification must be self | observe | none", t.name));
        }
        if !matches!(str_of(&t, "risk_level"), "low" | "medium" | "high" | "critical") {
            problems.push(format!("{}: risk_level invalid", t.name));
        }
    }
    report(problems, "contract hygiene");
}

#[test]
fn input_schemas_compile() {
    let mut problems = Vec::new();
    for t in tools() {
        let schema = t.raw["input_schema"].clone();
        // $defs may live at the contract file level; a standalone compile only has to be syntactically valid.
        if let Err(e) = jsonschema::validator_for(&schema) {
            if !e.to_string().contains("$defs") && !e.to_string().contains("Unresolvable") {
                problems.push(format!("{}: input_schema does not compile: {e}", t.name));
            }
        }
    }
    report(problems, "schema compile");
}

#[test]
fn contract_summary_is_stable() {
    // Not a gate on counts: prints the table a reviewer needs when a tool is added.
    let mut by_effects: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for t in tools() {
        by_effects.entry(str_of(&t, "side_effects").to_string()).or_default().push(t.name.clone());
    }
    let total: usize = by_effects.values().map(Vec::len).sum();
    assert!(total >= 100, "the contract shrank unexpectedly: {total} tools");
    for (effects, names) in &by_effects {
        println!("{effects}: {}", names.len());
    }
}
