//! Planner prompt construction and reply parsing.
//!
//! Each planning step is a fresh, self-contained conversation (system prompt + one user turn holding
//! the goal, plan, recent history and latest observation). This keeps prompts within the Gemini Web
//! prompt limit, works identically on every backend (they flatten conversations anyway) and makes a
//! resumed task behave exactly like an uninterrupted one.

use serde_json::Value;

use crate::config::ExecutionMode;
use crate::state::{StepRecord, StepStatus};

/// Characters of each older step's observation kept in the history section.
const HISTORY_OBSERVATION_CHARS: usize = 400;
/// Characters of the most recent observation shown in full.
const LATEST_OBSERVATION_CHARS: usize = 7000;
const ARGS_PREVIEW_CHARS: usize = 300;

#[derive(Debug, Clone, PartialEq)]
pub struct PlannerAction {
    pub thought: String,
    pub plan: Option<Vec<String>>,
    pub tool: String,
    pub args: Value,
}

fn clip(text: &str, max: usize) -> String {
    if text.chars().count() <= max {
        return text.to_string();
    }
    let kept: String = text.chars().take(max).collect();
    format!("{kept}... [truncated]")
}

pub fn system_prompt(catalog: &str, mode: ExecutionMode, language: &str) -> String {
    format!(
        r#"You are the planner of a local agent that operates a Windows 11 PC for the user. You never act directly: each reply chooses ONE tool call, a local tool router validates it against policy, executes it and reports the result. You then choose the next call, until the user's goal is achieved and proven.

# Reply format
Reply with exactly one JSON object and nothing else (no prose, no markdown fences):
{{"thought": "<what the current state is and why this action is next>", "plan": ["<remaining high-level steps>"], "action": {{"tool": "<tool name>", "args": {{ ... }}}}}}
Include "plan" in your first reply and whenever the plan changes; omit it otherwise.

# Rules
1. Use only the tools listed below, with arguments that match their schemas exactly. Paths must be absolute Windows paths.
2. Perception order: UI Automation first (desktop.inspect_ui, desktop.find_element, desktop.read_element). Use desktop.screenshot + vision.locate only when UI Automation cannot see the target. Never invent coordinates or element ids; take them from observations.
3. Do the task the way the user asked. If the user names an application or a UI action ("open Notepad and type...", "in Chrome search..."), carry it out in that application; filesystem/shell tools may prepare or verify, but must not replace the requested application work. Within an application prefer its structured tools: browser.* for web pages, autocad.* for AutoCAD drawings (never draw in AutoCAD by clicking or typing commands), desktop.set_value before keystrokes for text fields.
4. Read every result. "verification: passed" means the effect is confirmed. "deferred" means it is not yet confirmed: look at the latest observation before relying on it. An error or "verification: failed" means the step failed: follow its recovery hint and change your approach; never repeat an identical failing action.
5. Exit code 0 or "no error" is not proof of success. Finish only with task.complete. If anything was changed (a file, an app, a drawing, a web page), its "checks" must be read-only tool calls with expected values that prove the goal, e.g. {{"tool": "filesystem.stat", "args": {{"path": "C:\\temp\\hello.txt"}}, "expect": {{"exists": true}}}}. For a pure question/answer task, cite the ids of the steps that produced the answer in "evidence_steps" instead. Never create files, windows or other side effects the user did not ask for, not even to have something to check.
6. If the task needs Administrator rights, credentials, payment, or a decision only the user can make, use task.ask_user or task.fail. Never try to bypass UAC, read browser cookies/passwords, or disable security software.
7. Dialogs: if a dialog appears (Save As, confirmation), handle it explicitly; find its controls with desktop.find_element on its hwnd.
8. Write task.complete "summary", task.ask_user "question" and task.fail "reason" in this language: {language}.

Execution mode: {mode}. Coordinates are physical screen pixels.

# Tools
{catalog}"#,
        mode = mode.as_str(),
    )
}

pub struct TurnContext<'a> {
    pub goal: &'a str,
    pub plan: &'a [String],
    pub steps: &'a [StepRecord],
    pub history_steps: usize,
    pub max_steps: u32,
    pub consecutive_failures: u32,
    pub recall: &'a str,
    pub latest_observation: Option<&'a str>,
    pub notices: &'a [String],
}

fn step_line(step: &StepRecord, full: bool) -> String {
    let args = clip(&step.args.to_string(), ARGS_PREVIEW_CHARS);
    let outcome = match (&step.status, &step.error) {
        (_, Some(error)) => format!(
            "ERROR {}: {}{}",
            error.code.as_str(),
            clip(&error.message, HISTORY_OBSERVATION_CHARS),
            if full { format!("\n    Recovery hint: {}", error.code.recovery_hint()) } else { String::new() }
        ),
        (StepStatus::Succeeded, None) => {
            format!("ok, verification: {}", step.verification.as_deref().unwrap_or("not_applicable"))
        }
        (status, None) => format!("{status:?}"),
    };
    let mut line = format!("{} {} {} -> {}", step.step_id, step.tool, args, outcome);
    if !step.observation.is_empty() && step.error.is_none() {
        line.push_str(&format!("\n    {}", clip(&step.observation, HISTORY_OBSERVATION_CHARS).replace('\n', "\n    ")));
    }
    line
}

pub fn user_turn(ctx: &TurnContext<'_>) -> String {
    let mut out = format!("# Task\n{}\n", ctx.goal);
    if !ctx.recall.trim().is_empty() {
        out.push_str(&format!("\n# Relevant memory from earlier tasks\n{}", ctx.recall));
    }
    out.push_str("\n# Plan\n");
    if ctx.plan.is_empty() {
        out.push_str("(none yet: include \"plan\" in your reply)\n");
    } else {
        for (i, item) in ctx.plan.iter().enumerate() {
            out.push_str(&format!("{}. {item}\n", i + 1));
        }
    }
    out.push_str(&format!(
        "\n# Progress (step {} of at most {}; consecutive failures: {})\n",
        ctx.steps.len() + 1,
        ctx.max_steps,
        ctx.consecutive_failures
    ));
    if ctx.steps.is_empty() {
        out.push_str("(no steps yet)\n");
    } else {
        let skip = ctx.steps.len().saturating_sub(ctx.history_steps);
        if skip > 0 {
            let earlier: Vec<String> = ctx.steps[..skip].iter().map(|s| format!("{} {:?}", s.tool, s.status)).collect();
            out.push_str(&format!("({skip} earlier steps: {})\n", earlier.join(", ")));
        }
        let last = ctx.steps.len() - 1;
        for (i, step) in ctx.steps.iter().enumerate().skip(skip) {
            out.push_str(&step_line(step, i == last));
            out.push('\n');
        }
    }
    if let Some(observation) = ctx.latest_observation {
        out.push_str(&format!("\n# Latest observation\n{}\n", clip(observation, LATEST_OBSERVATION_CHARS)));
    }
    if !ctx.notices.is_empty() {
        out.push_str("\n# Notices\n");
        for notice in ctx.notices {
            out.push_str(&format!("- {notice}\n"));
        }
    }
    out.push_str("\nChoose the next action. Reply with one JSON object only.");
    out
}

/// Extract the first balanced JSON object from a reply (tolerates markdown fences and stray prose).
fn extract_json_object(text: &str) -> Option<&str> {
    let bytes = text.as_bytes();
    let start = text.find('{')?;
    let mut depth = 0usize;
    let mut in_string = false;
    let mut escaped = false;
    for (offset, &b) in bytes[start..].iter().enumerate() {
        if in_string {
            match b {
                _ if escaped => escaped = false,
                b'\\' => escaped = true,
                b'"' => in_string = false,
                _ => {}
            }
            continue;
        }
        match b {
            b'"' => in_string = true,
            b'{' => depth += 1,
            b'}' => {
                depth -= 1;
                if depth == 0 {
                    return Some(&text[start..start + offset + 1]);
                }
            }
            _ => {}
        }
    }
    None
}

pub fn parse_reply(text: &str) -> Result<PlannerAction, String> {
    let raw = extract_json_object(text).ok_or_else(|| "the reply contains no JSON object".to_string())?;
    let value: Value = serde_json::from_str(raw).map_err(|e| format!("the reply is not valid JSON: {e}"))?;
    let thought = value.get("thought").and_then(Value::as_str).unwrap_or_default().to_string();
    let plan = match value.get("plan") {
        None | Some(Value::Null) => None,
        Some(Value::Array(items)) => Some(
            items
                .iter()
                .map(|i| i.as_str().map(String::from).ok_or("plan items must be strings"))
                .collect::<Result<Vec<_>, _>>()?,
        ),
        Some(_) => return Err("\"plan\" must be an array of strings".into()),
    };
    let action = value.get("action").unwrap_or(&value);
    let tool = action
        .get("tool")
        .and_then(Value::as_str)
        .filter(|t| !t.is_empty())
        .ok_or_else(|| "missing action.tool".to_string())?
        .to_string();
    let args = match action.get("args") {
        None | Some(Value::Null) => Value::Object(Default::default()),
        Some(v @ Value::Object(_)) => v.clone(),
        Some(_) => return Err("action.args must be a JSON object".into()),
    };
    Ok(PlannerAction { thought, plan, tool, args })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_fenced_reply_with_braces_in_strings() {
        let reply = "Sure:\n```json\n{\"thought\": \"type {x}\", \"plan\": [\"a\"], \"action\": {\"tool\": \"desktop.type\", \"args\": {\"text\": \"}{\"}}}\n```";
        let action = parse_reply(reply).unwrap();
        assert_eq!(action.tool, "desktop.type");
        assert_eq!(action.args, json!({"text": "}{"}));
        assert_eq!(action.plan, Some(vec!["a".to_string()]));
    }

    #[test]
    fn accepts_flat_action_and_rejects_garbage() {
        let action = parse_reply(r#"{"tool": "desktop.list_windows"}"#).unwrap();
        assert_eq!(action.tool, "desktop.list_windows");
        assert!(parse_reply("I will click the button").is_err());
        assert!(parse_reply(r#"{"thought": "x"}"#).is_err());
        assert!(parse_reply(r#"{"action": {"tool": "x", "args": [1]}}"#).is_err());
    }
}
