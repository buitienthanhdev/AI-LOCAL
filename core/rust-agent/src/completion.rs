//! Completion verification, separated from the execution loop (gap G7).
//!
//! `agent.rs` still owns the side effects (status transitions, audit, the LLM second opinion, finishing the task).
//! Everything that *decides* lives here and is testable without a worker, a policy file or a model:
//!
//! * [`assess`] re-runs the planner's read-only completion checks through a [`CheckExecutor`] and collects
//!   `failures` / `evidence`;
//! * [`needs_second_opinion`] says whether the independent verifier is asked;
//! * [`decide`] turns the failures into Accept (and *how*: verified / unverified / direct answer) or Reject.
//!
//! The rules are the ones the loop always had; this module only makes them explicit.

use async_trait::async_trait;
use serde_json::{json, Value};

use crate::state::{StepRecord, StepStatus};
use crate::tools::{ToolCatalog, ToolSpec, VerificationMode};

/// Characters of each check's result data kept for the verifier and the audit trail.
pub const CHECK_DATA_CHARS: usize = 1500;

/// What running one completion check produced.
#[derive(Debug, Clone, PartialEq)]
pub enum CheckOutcome {
    /// The tool ran and returned this data.
    Data(Value),
    /// The policy refused the call.
    Denied,
    /// The tool ran and reported an error.
    ToolError(String),
    /// The call could not be made at all (worker gone, timeout in the transport).
    NotRun(String),
}

/// Runs one read-only check call (policy first, then the worker). Implemented by the loop; faked in tests.
#[async_trait]
pub trait CheckExecutor: Send {
    /// The tool catalog checks are validated against.
    fn catalog(&self) -> &ToolCatalog;
    /// `Err` is a hard failure of the run itself (the worker could not be started), not of a check.
    async fn run(&mut self, spec: &ToolSpec, args: Value) -> anyhow::Result<CheckOutcome>;
}

#[derive(Debug, Default, Clone, PartialEq)]
pub struct Assessment {
    /// Reasons the completion is not proven. Empty = every check and every cited step held.
    pub failures: Vec<String>,
    pub evidence: Vec<String>,
    /// Check, expected and ACTUAL data, for the verifier and the audit trail.
    pub check_results: Vec<Value>,
}

/// How an accepted completion was established. Machine-readable (see `TaskRecord::completion_basis`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Basis {
    Verified,
    /// Accepted after repeated rejections although some check could not be confirmed.
    Unverified,
    /// No tool was used and no check was asked for: nothing to verify.
    DirectAnswer,
}

impl Basis {
    pub fn as_str(self) -> &'static str {
        match self {
            Basis::Verified => "verified",
            Basis::Unverified => "unverified",
            Basis::DirectAnswer => "direct_answer",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    Accept(Basis),
    Reject,
}

fn clip_text(text: &str, max: usize) -> String {
    if text.chars().count() <= max {
        text.to_string()
    } else {
        format!("{}...", text.chars().take(max).collect::<String>())
    }
}

fn clip_json(value: &Value, max: usize) -> Value {
    let text = value.to_string();
    if text.chars().count() <= max {
        value.clone()
    } else {
        Value::String(clip_text(&text, max))
    }
}

/// Whitespace-insensitive, case-insensitive form of a text for completion checks.
fn normalize_text(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ").to_lowercase()
}

/// Do the fields of `expect` hold in `actual`? Dotted keys reach nested fields; text expectations are substring
/// tests (a file's content or a window's text is checked for the part that matters).
pub fn json_matches(actual: &Value, expect: &Value) -> Result<(), String> {
    let Value::Object(expected) = expect else {
        return if actual == expect { Ok(()) } else { Err(format!("expected {expect}, got {actual}")) };
    };
    for (key, want) in expected {
        let got = key
            .split('.')
            .try_fold(actual, |node, part| node.get(part))
            .ok_or_else(|| format!("{key} missing in result"))?;
        let ok = match (got, want) {
            (Value::Number(a), Value::Number(b)) => match (a.as_f64(), b.as_f64()) {
                (Some(x), Some(y)) => (x - y).abs() <= 1e-6 * y.abs().max(1.0),
                _ => a == b,
            },
            (Value::String(a), Value::String(b)) => a == b || (!b.is_empty() && normalize_text(a).contains(&normalize_text(b))),
            (Value::Array(items), Value::String(b)) => items.iter().any(|i| i.as_str().is_some_and(|a| normalize_text(a).contains(&normalize_text(b)))),
            (a, b) => a == b,
        };
        if !ok {
            return Err(format!("{key} = {got}, expected {want}"));
        }
    }
    Ok(())
}

/// Succeeded steps that changed state and whose effect no tool confirmed itself: they need a completion check.
pub fn unconfirmed_state_changes(steps: &[StepRecord], catalog: &ToolCatalog) -> Vec<String> {
    steps
        .iter()
        .filter(|s| s.status == StepStatus::Succeeded)
        .filter(|s| catalog.get(&s.tool).is_some_and(|spec| spec.changes_state()))
        // A change the tool verified itself by reading it back (files, AutoCAD entities, whole drawings)
        // is already proven; only unconfirmed effects (clicks, typing, shell commands) need checks.
        .filter(|s| s.verification.as_deref() != Some("passed"))
        .map(|s| format!("{} {}", s.step_id, s.tool))
        .collect()
}

/// Run the checks and cited evidence of a (normalised, schema-valid) `task.complete` call.
pub async fn assess(args: &Value, steps: &[StepRecord], executor: &mut dyn CheckExecutor) -> anyhow::Result<Assessment> {
    let mut out = Assessment::default();
    let checks = args["checks"].as_array().cloned().unwrap_or_default();
    let state_changing = unconfirmed_state_changes(steps, executor.catalog());
    if checks.is_empty() && !state_changing.is_empty() {
        out.failures.push(format!(
            "this task changed state ({}); evidence_steps are not enough - give read-only checks that prove the result",
            state_changing.join(", ")
        ));
    }
    for step_id in args["evidence_steps"].as_array().cloned().unwrap_or_default() {
        let id = step_id.as_str().unwrap_or_default();
        match steps.iter().find(|s| s.step_id == id) {
            None => out.failures.push(format!("evidence step {id} does not exist")),
            Some(step) if step.status != StepStatus::Succeeded => {
                out.failures.push(format!("evidence step {id} did not succeed ({:?})", step.status))
            }
            Some(step) if !matches!(step.verification.as_deref(), Some("passed" | "not_applicable")) => out.failures.push(format!(
                "evidence step {id} is not verified (verification {})",
                step.verification.as_deref().unwrap_or("none")
            )),
            Some(step) => out
                .evidence
                .push(format!("{id} {} succeeded (verification {})", step.tool, step.verification.as_deref().unwrap_or("?"))),
        }
    }
    for (i, check) in checks.iter().enumerate() {
        let tool = check["tool"].as_str().unwrap_or_default();
        let Some(spec) = executor.catalog().get(tool).cloned() else {
            out.failures.push(format!("check {i}: unknown tool {tool}"));
            continue;
        };
        if spec.verification != VerificationMode::None || spec.is_core_tool() {
            out.failures.push(format!("check {i}: {tool} is not a read-only tool; checks must only observe"));
            continue;
        }
        let mut call_args = check["args"].clone();
        executor.catalog().coerce_args(tool, &mut call_args);
        let violations = executor.catalog().validate_args(tool, &call_args);
        if !violations.is_empty() {
            out.failures.push(format!("check {i}: invalid args for {tool}: {}", violations.join("; ")));
            continue;
        }
        match executor.run(&spec, call_args.clone()).await? {
            CheckOutcome::Denied => out.failures.push(format!("check {i}: policy denied {tool}")),
            CheckOutcome::Data(data) => {
                out.check_results.push(json!({"tool": tool, "args": call_args, "expect": check["expect"],
                                              "actual": clip_json(&data, CHECK_DATA_CHARS)}));
                match json_matches(&data, &check["expect"]) {
                    Ok(()) => out.evidence.push(format!("{tool} {} -> {}", call_args, check["expect"])),
                    Err(problem) => out.failures.push(format!("check {i} ({tool}): {problem}")),
                }
            }
            CheckOutcome::ToolError(error) => out.failures.push(format!("check {i} ({tool}) failed: {error}")),
            CheckOutcome::NotRun(why) => out.failures.push(format!("check {i} ({tool}) could not run: {why}")),
        }
    }
    Ok(out)
}

/// A direct answer (greeting, small talk, general knowledge) touched nothing and asked for no check.
pub fn is_direct_answer(steps: &[StepRecord], args: &Value) -> bool {
    steps.is_empty() && args["checks"].as_array().is_none_or(|c| c.is_empty())
}

/// The second opinion exists for UI work (desktop clicks, web pages), where a check can pass without proving the
/// goal. Shell, file, AutoCAD and knowledge results are exact data the core has just re-read itself.
pub fn has_ui_work(steps: &[StepRecord], catalog: &ToolCatalog) -> bool {
    steps.iter().any(|s| {
        s.status == StepStatus::Succeeded
            && catalog.get(&s.tool).is_some_and(|spec| matches!(spec.category.as_str(), "desktop" | "browser" | "vision"))
    })
}

pub fn needs_second_opinion(failures: &[String], verifier_enabled: bool, direct_answer: bool, ui_work: bool) -> bool {
    failures.is_empty() && verifier_enabled && !direct_answer && ui_work
}

/// Accept or reject. The only way to accept while something is unproven is repeated rejection of work that was
/// actually done (so a badly phrased check cannot throw finished work away); it is accepted as `Unverified`,
/// never as `Verified`.
pub fn decide(failures: &[String], direct_answer: bool, did_work: bool, rejections: u32, max_rejections: u32) -> Verdict {
    if failures.is_empty() {
        return Verdict::Accept(if direct_answer { Basis::DirectAnswer } else { Basis::Verified });
    }
    if did_work && rejections >= max_rejections {
        return Verdict::Accept(Basis::Unverified);
    }
    Verdict::Reject
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Utc;
    use std::collections::VecDeque;
    use std::path::PathBuf;

    fn catalog() -> ToolCatalog {
        ToolCatalog::load(&PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tools")).unwrap()
    }

    fn step(id: &str, tool: &str, status: StepStatus, verification: Option<&str>) -> StepRecord {
        StepRecord {
            step_id: id.into(),
            index: 1,
            started_at: Utc::now(),
            finished_at: None,
            thought: String::new(),
            tool: tool.into(),
            args: json!({}),
            input_hash: String::new(),
            status,
            verification: verification.map(String::from),
            observation: String::new(),
            error: None,
            retry_count: 0,
            duration_ms: 0,
            backend: String::new(),
            provider: String::new(),
            artifacts: vec![],
        }
    }

    /// Scripted executor: pops one outcome per call and records the calls.
    struct Fake {
        catalog: ToolCatalog,
        outcomes: VecDeque<CheckOutcome>,
        calls: Vec<(String, Value)>,
        hard_error: bool,
    }

    impl Fake {
        fn new(outcomes: Vec<CheckOutcome>) -> Self {
            Self { catalog: catalog(), outcomes: outcomes.into(), calls: vec![], hard_error: false }
        }
    }

    #[async_trait]
    impl CheckExecutor for Fake {
        fn catalog(&self) -> &ToolCatalog {
            &self.catalog
        }

        async fn run(&mut self, spec: &ToolSpec, args: Value) -> anyhow::Result<CheckOutcome> {
            self.calls.push((spec.name.clone(), args));
            if self.hard_error {
                anyhow::bail!("worker could not be started");
            }
            Ok(self.outcomes.pop_front().expect("unexpected extra check call"))
        }
    }

    fn run_assess(args: &Value, steps: &[StepRecord], fake: &mut Fake) -> Assessment {
        block_on(assess(args, steps, fake)).unwrap()
    }

    fn block_on<F: std::future::Future>(f: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap().block_on(f)
    }

    fn stat_check(path: &str, expect: Value) -> Value {
        json!({"tool": "filesystem.stat", "args": {"path": path}, "expect": expect})
    }

    #[test]
    fn all_checks_pass_is_verified() {
        let steps = vec![step("s001", "filesystem.write", StepStatus::Succeeded, Some("passed"))];
        let args = json!({"summary": "done", "checks": [stat_check("C:\\temp\\a.txt", json!({"exists": true}))]});
        let mut fake = Fake::new(vec![CheckOutcome::Data(json!({"exists": true, "size": 3}))]);
        let a = run_assess(&args, &steps, &mut fake);
        assert!(a.failures.is_empty(), "{:?}", a.failures);
        assert_eq!(a.evidence.len(), 1);
        assert_eq!(a.check_results[0]["actual"]["exists"], true);
        assert_eq!(decide(&a.failures, false, true, 0, 2), Verdict::Accept(Basis::Verified));
        assert_eq!(fake.calls.len(), 1);
    }

    #[test]
    fn a_failed_check_names_the_mismatch_and_rejects() {
        let steps = vec![step("s001", "filesystem.write", StepStatus::Succeeded, Some("passed"))];
        let args = json!({"summary": "done", "checks": [
            stat_check("C:\\temp\\a.txt", json!({"exists": true})),
            stat_check("C:\\temp\\b.txt", json!({"exists": true, "size": 10}))]});
        let mut fake = Fake::new(vec![
            CheckOutcome::Data(json!({"exists": true})),
            CheckOutcome::Data(json!({"exists": true, "size": 3})),
        ]);
        let a = run_assess(&args, &steps, &mut fake);
        assert_eq!(a.failures.len(), 1);
        assert!(a.failures[0].starts_with("check 1 (filesystem.stat): size = 3, expected 10"), "{}", a.failures[0]);
        assert_eq!(a.evidence.len(), 1, "the passing check is still evidence");
        assert_eq!(decide(&a.failures, false, true, 0, 2), Verdict::Reject);
    }

    #[test]
    fn missing_or_invalid_verification_data_is_a_failure_not_a_pass() {
        let steps = vec![step("s001", "filesystem.write", StepStatus::Succeeded, Some("passed"))];
        // result lacks the expected field
        let args = json!({"summary": "x", "checks": [stat_check("C:\\temp\\a.txt", json!({"exists": true}))]});
        let mut fake = Fake::new(vec![CheckOutcome::Data(json!({"size": 3}))]);
        let a = run_assess(&args, &steps, &mut fake);
        assert_eq!(a.failures, vec!["check 0 (filesystem.stat): exists missing in result".to_string()]);
        // the check's arguments violate the tool's schema: the tool is not even called
        let args = json!({"summary": "x", "checks": [{"tool": "filesystem.stat", "args": {"nope": 1}, "expect": {"exists": true}}]});
        let mut fake = Fake::new(vec![]);
        let a = run_assess(&args, &steps, &mut fake);
        assert_eq!(a.failures.len(), 1);
        assert!(a.failures[0].starts_with("check 0: invalid args for filesystem.stat"), "{}", a.failures[0]);
        assert!(fake.calls.is_empty());
        // an unknown tool, and a tool that is not read-only
        let args = json!({"summary": "x", "checks": [
            {"tool": "no.such_tool", "args": {}, "expect": {"a": 1}},
            {"tool": "filesystem.write", "args": {"path": "C:\\a", "content": "x"}, "expect": {"a": 1}},
            {"tool": "task.complete", "args": {"summary": "x"}, "expect": {"a": 1}}]});
        let mut fake = Fake::new(vec![]);
        let a = run_assess(&args, &steps, &mut fake);
        assert_eq!(a.failures[0], "check 0: unknown tool no.such_tool");
        assert!(a.failures[1].contains("is not a read-only tool"), "{}", a.failures[1]);
        assert!(a.failures[2].contains("is not a read-only tool"), "{}", a.failures[2]);
        assert!(fake.calls.is_empty());
    }

    #[test]
    fn tool_errors_timeouts_and_denials_are_failures() {
        let steps = vec![step("s001", "filesystem.write", StepStatus::Succeeded, Some("passed"))];
        let args = json!({"summary": "x", "checks": [
            stat_check("C:\\a", json!({"exists": true})),
            stat_check("C:\\b", json!({"exists": true})),
            stat_check("C:\\c", json!({"exists": true}))]});
        let mut fake = Fake::new(vec![
            CheckOutcome::ToolError("TIMEOUT: tool exceeded 5000 ms".into()),
            CheckOutcome::NotRun("worker is gone".into()),
            CheckOutcome::Denied,
        ]);
        let a = run_assess(&args, &steps, &mut fake);
        assert_eq!(a.failures, vec![
            "check 0 (filesystem.stat) failed: TIMEOUT: tool exceeded 5000 ms".to_string(),
            "check 1 (filesystem.stat) could not run: worker is gone".to_string(),
            "check 2: policy denied filesystem.stat".to_string(),
        ]);
        assert!(a.check_results.is_empty() && a.evidence.is_empty());
    }

    #[test]
    fn state_changes_need_checks_and_evidence_steps_must_be_verified() {
        // an unconfirmed click changed state: no checks => rejected with the step named
        let steps = vec![step("s001", "desktop.click", StepStatus::Succeeded, Some("deferred"))];
        let a = run_assess(&json!({"summary": "x"}), &steps, &mut Fake::new(vec![]));
        assert_eq!(a.failures.len(), 1);
        assert!(a.failures[0].contains("s001 desktop.click") && a.failures[0].contains("give read-only checks"), "{}", a.failures[0]);
        // a self-verified change needs none
        let steps = vec![step("s001", "filesystem.write", StepStatus::Succeeded, Some("passed"))];
        assert!(run_assess(&json!({"summary": "x"}), &steps, &mut Fake::new(vec![])).failures.is_empty());
        // evidence steps: missing, failed, unverified, good
        let steps = vec![
            step("s001", "web.fetch", StepStatus::Succeeded, Some("not_applicable")),
            step("s002", "web.fetch", StepStatus::Failed, None),
            step("s003", "web.fetch", StepStatus::Succeeded, Some("deferred")),
        ];
        let args = json!({"summary": "x", "evidence_steps": ["s001", "s002", "s003", "s009"]});
        let a = run_assess(&args, &steps, &mut Fake::new(vec![]));
        assert_eq!(a.evidence.len(), 1);
        assert_eq!(a.failures.len(), 3);
        assert!(a.failures.iter().any(|f| f.contains("s002 did not succeed")));
        assert!(a.failures.iter().any(|f| f.contains("s003 is not verified")));
        assert!(a.failures.iter().any(|f| f.contains("s009 does not exist")));
    }

    #[test]
    fn a_worker_that_cannot_start_is_a_hard_error_not_a_quiet_failure() {
        let steps = vec![step("s001", "filesystem.write", StepStatus::Succeeded, Some("passed"))];
        let args = json!({"summary": "x", "checks": [stat_check("C:\\a", json!({"exists": true}))]});
        let mut fake = Fake::new(vec![]);
        fake.hard_error = true;
        assert!(block_on(assess(&args, &steps, &mut fake)).is_err());
    }

    #[test]
    fn direct_answers_are_recognised_and_labelled() {
        assert!(is_direct_answer(&[], &json!({"summary": "hi"})));
        assert!(is_direct_answer(&[], &json!({"summary": "hi", "checks": []})));
        assert!(!is_direct_answer(&[], &json!({"summary": "hi", "checks": [{"tool": "x"}]})));
        assert!(!is_direct_answer(&[step("s001", "web.search", StepStatus::Succeeded, None)], &json!({"summary": "hi"})));
        assert_eq!(decide(&[], true, false, 0, 2), Verdict::Accept(Basis::DirectAnswer));
    }

    #[test]
    fn rejections_are_bounded_and_unverified_acceptance_is_never_called_verified() {
        let failing = vec!["check 0 (filesystem.stat): exists = false, expected true".to_string()];
        // first and second rejection: still rejected
        assert_eq!(decide(&failing, false, true, 0, 2), Verdict::Reject);
        assert_eq!(decide(&failing, false, true, 1, 2), Verdict::Reject);
        // after the bound, work that was really done is accepted, labelled unverified
        assert_eq!(decide(&failing, false, true, 2, 2), Verdict::Accept(Basis::Unverified));
        // but never when nothing succeeded: no work, no completion
        assert_eq!(decide(&failing, false, false, 5, 2), Verdict::Reject);
        assert_eq!(Basis::Unverified.as_str(), "unverified");
        assert_eq!(Basis::Verified.as_str(), "verified");
        assert_eq!(Basis::DirectAnswer.as_str(), "direct_answer");
    }

    #[test]
    fn the_second_opinion_is_only_for_proven_ui_work() {
        let cat = catalog();
        let ui = vec![step("s001", "desktop.click", StepStatus::Succeeded, Some("passed"))];
        let file = vec![step("s001", "filesystem.write", StepStatus::Succeeded, Some("passed"))];
        assert!(has_ui_work(&ui, &cat) && !has_ui_work(&file, &cat));
        assert!(needs_second_opinion(&[], true, false, true));
        assert!(!needs_second_opinion(&[], false, false, true), "disabled in config");
        assert!(!needs_second_opinion(&[], true, true, true), "direct answers have nothing to verify");
        assert!(!needs_second_opinion(&[], true, false, false), "file/shell results are exact data");
        assert!(!needs_second_opinion(&["x".into()], true, false, true), "already failing");
    }

    #[test]
    fn matching_rules_are_the_documented_ones() {
        assert!(json_matches(&json!({"content": "Hello\r\n World "}), &json!({"content": "hello world"})).is_ok());
        assert!(json_matches(&json!({"n": 1.0000001}), &json!({"n": 1})).is_ok());
        assert!(json_matches(&json!({"a": {"b": [ "x y", "z" ]}}), &json!({"a.b": "x y"})).is_ok());
        assert!(json_matches(&json!({"exists": false}), &json!({"exists": true})).is_err());
        assert_eq!(json_matches(&json!({}), &json!({"k": 1})).unwrap_err(), "k missing in result");
        assert!(json_matches(&json!(3), &json!(4)).is_err());
    }
}
