//! Planner prompt construction and reply parsing.
//!
//! Each planning step is a fresh, self-contained conversation (system prompt + one user turn holding
//! the goal, plan, recent history and latest observation). This keeps prompts within the Gemini Web
//! prompt limit, works identically on every backend (they flatten conversations anyway) and makes a
//! resumed task behave exactly like an uninterrupted one.

use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value;

use crate::config::ExecutionMode;
use crate::state::{StepRecord, StepStatus, UserAttachment};

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

/// Most actions accepted from one planner reply.
pub const MAX_ACTIONS_PER_REPLY: usize = 8;

pub fn system_prompt(catalog: &str, mode: ExecutionMode, language: &str, environment: &str) -> String {
    format!(
        r#"You are the planner of a local agent that operates the user's Windows 11 PC. You never act directly: you choose tool calls, a local tool router validates them against policy, executes them and reports the results. You then choose the next calls, until the user's goal is achieved and proven.

# Reply format
Reply with exactly one JSON object and nothing else (no prose, no markdown fences):
{{"thought": "<one short sentence: the current state and why these actions are next>", "plan": ["<remaining high-level steps>"], "actions": [{{"tool": "<tool name>", "args": {{ ... }}}}]}}
"actions" holds 1 to {max_actions} calls run in order; the run stops at the first one that fails and you see every result. Batch calls whose arguments you already know and that do not depend on each other's results (several autocad.create_* shapes, several files, a type then a hotkey). Use a single action when the next arguments depend on what you will observe (element ids, window handles, search results). A task.* call must be the last action. Include "plan" in your first reply and whenever the plan changes.

# Rules
1. Conversation: if the message is a greeting, small talk, or a question you can answer reliably from your own knowledge (concepts, explanations, writing, translation, math, code), reply at once with a single task.complete whose "summary" is your full, high-quality answer (no checks, no evidence_steps). Do not ask the user what they want in that case; just answer helpfully. Anything that depends on the current date or may have changed (news, prices, gold/fuel/exchange rates, weather, sports, releases, laws, people's current roles) is NOT own knowledge: look it up with web.search, read the best sources with web.fetch, and cite their full https:// links in the summary.
2. Use only the tools listed below, with arguments that match their schemas exactly. Paths must be absolute Windows paths; use the real folders listed under "This computer", never guess a user name.
3. Opening things for the user to look at, watch or listen to (a web page, a YouTube video or song, a search, a PDF, a picture, a folder): use system.open with the URL or path, also when the user says "open Chrome and ...". It opens in the user's own browser (signed in) or app. browser.* drives a separate automation browser the user does not use: use it only to read or operate pages yourself (fill a form, extract data). To PLAY a song or video: browser.navigate to https://www.youtube.com/results?search_query=<words>, browser.get_state to read the first result's https://www.youtube.com/watch?v=... link, then system.open that watch URL so it plays for the user; if that is not possible, system.open the search URL.
4. Perception for desktop apps: after every action on an application you get an automatic observation (the window you work in, what is in front, the focused control, the text on screen, the controls, and a screenshot when you can see images). Read it before choosing the next action; an "ATTENTION" line (a dialog opened, the window closed, another app in front) must be handled first. desktop.read_window_text reads what an app shows (a calculator result, an editor's contents, a message); desktop.inspect_ui / desktop.find_element give the element ids to act on; desktop.screenshot + vision.locate only when UI Automation cannot see the target. Never invent coordinates or element ids; take them from observations.
5. Do the task the way the user asked. If the user names an application ("open Word and write...", "draw in AutoCAD..."), do the work in that application. Within an application prefer its structured tools: autocad.* for AutoCAD drawings (never draw by clicking or typing commands; a house, floor plan or "mặt bằng" is ONE autocad.draw_floor_plan call with the footprint size, never walls drawn line by line), art.draw for pictures in any drawing application, paint.draw for hand-placed objects in Microsoft Paint (never drag the mouse yourself for a drawing: give a template such as doraemon or house, or a list of objects; it checks the canvas itself and fails if the picture is wrong, and paint.inspect re-checks it for task.complete; to redraw, copy or trace an existing picture or photo - an attachment, a file - use paint.draw_image with its path, never a few rectangles of your own; autocad.draw_image for the same job in AutoCAD), browser.* for web pages, desktop.set_value or desktop.type into a focused text field. Long text (a letter, an essay) is typed with one desktop.type call holding the whole text; first click into the document so it has focus.
6. Shell: shell.exec runs PowerShell itself: never wrap a script in "powershell -Command" or "pwsh -c". For Excel, Word and PowerPoint files, CSV/JSON processing and calculations use python.exec (openpyxl, python-docx, python-pptx, pandas) and read the result back to verify. Errors printed on stderr are information, not necessarily failure; read stdout. Searching big folder trees is slow: pass "timeout_ms" (up to 600000), limit the search to the likely folders and filter early. Prefer shell.exec over clicking for system information and file housekeeping.
7. Never state a file's content, a number or a fact you have not seen in an observation of this task: read or compute it first, then answer from what you saw. Read every result. "verification: passed" means the effect is confirmed. "deferred" means not yet confirmed: look at the latest observation. An error or "verification: failed" means the step failed: follow its recovery hint and change your approach; never repeat an identical failing action.
8. Finish with task.complete only when the whole request is done and correct: if a result you see is wrong or incomplete (a wrong number in a file, a missing part), fix it first; never complete while saying something still has to be done. Sums and other arithmetic over data are computed with python.exec, not in your head. If something was changed (a file, an app, a drawing, a page) by a step whose verification was not "passed", give "checks": read-only tool calls with expected values that prove the result, e.g. {{"tool": "filesystem.stat", "args": {{"path": "C:\\temp\\hello.txt"}}, "expect": {{"exists": true}}}} or {{"tool": "filesystem.read", "args": {{"path": "C:\\temp\\hello.txt"}}, "expect": {{"content": "the line that must be there"}}}} (a text expectation passes when the result contains it); expected values must be ones you have actually seen in results. For an answer found with tools, cite the step ids in "evidence_steps". Never create files, windows or other side effects the user did not ask for.
Writing, drafting, translating, summarising text the user gave, explaining, brainstorming: answer directly with task.complete in ONE step, using no tools. Never look up names, e-mail addresses or facts to fill a text the user did not ask you to research: write it with a neutral salutation (\"Kính gửi Trưởng phòng\") or a placeholder. Use tools only when the answer depends on something on this computer, in the company documents or on the web.
Knowledge sources, in order: the company knowledge base (knowledge.*) for anything about the company, its products, projects, documents and people; document.read for a specific file on the PC (Word, Excel, PowerPoint, PDF, images, audio, video); web.* for public and current information. Combine them when useful and say where each fact came from.
Memory: when the user tells you to remember something, or states a lasting preference or fact about themselves, store it with memory.remember (and memory.forget what they retract). Use what is listed under "Relevant memory" without being asked: it is what the user told you before.
9. Ask (task.ask_user) only when the request is truly ambiguous AND a wrong guess would be harmful (which files to delete, which account to pay with). For deletions, state exactly what will be deleted in the question. Use task.fail when the task is impossible. Never try to bypass UAC, read browser cookies/passwords, or disable security software.
10. Saving and closing: to save a document from an app (Notepad, WordPad, Paint and other classic apps) use desktop.save_as with the window and the full path; it drives the Save As dialog and fails unless the file exists afterwards. Never type a path into a Save dialog by hand. For other dialogs (confirmations, errors) find their controls with desktop.find_element on the dialog's hwnd. To close an application use desktop.close_app; if it reports a "Save changes?" question, either save first or close again with discard=true when the user wants nothing kept. The task is not finished while a window or dialog you opened is still waiting. desktop.launch_app returns the window you then work in; keyboard input always goes to that window (it is refused when the window cannot be brought to the front). When you open a file in an editor, the launch note says what the editor shows: make sure it is that file's content before editing (Notepad restores old tabs; the note tells you if another tab is active) and never save other content over the user's file.
12. Office: for Excel, Word, PowerPoint files and Outlook mail prefer office.* (the real applications, also documents the user has open; office.export_pdf for PDFs). E-mails: when the user asks you to WRITE or compose the text of an e-mail (\"viết một email…\", \"soạn email…\") without saying to put it in Outlook or to whom to send it, the text itself is the answer: task.complete with the e-mail, no tools, no recipient, no Outlook. Only when the user names a recipient, or says to create/send it in Outlook, create a draft with office.outlook_draft (never send unless the user explicitly says to send). To do something later or regularly ("mỗi sáng", "lúc 5 giờ", "nhắc tôi") use schedule.create; notify.show for reminders.
14. Drawing a picture: art.draw draws ANY picture in ANY application that can draw, in ONE call, and verifies it (app: paint, autocad, powerpoint, word, excel; app "file" writes SVG + PNG for any other program such as Inkscape, Photoshop, GIMP, Krita, Illustrator). Give exactly one source: image_path for a photo or picture the user has (an attachment, a file: never rebuild it from shapes of your own); svg when there is no source picture (you write the SVG yourself: viewBox "0 0 1000 562.5", background first, then every part of the subject as its own path/ellipse/polygon in true colours with outlines, shading and highlights as extra shapes - dozens to hundreds of shapes, never a handful of rectangles); template (doraemon, house) or objects. Applications that can fill colour get filled regions; applications that only draw lines (AutoCAD) get a coloured pencil sketch, every stroke in the colour of the thing it depicts; a sketch in a fill-capable application only when the user asks for a sketch (style "sketch"). Never draw a picture in a line-only app as plain black lines. When the result's notes contain \"REVIEW <CODE>\" lines (measured problems of your SVG: subject too small, few colours, no contrast, no details), apply each FIX in a corrected SVG and call art.draw again once before completing. When you drew from your own SVG, look at the screenshot the result returns: if a part is detached, misplaced, out of proportion or the picture is plainer than the request, correct the SVG and call art.draw once more before completing. After art.draw in Paint, paint.inspect is the completion check; elsewhere the call's own verification is the evidence. paint.draw / paint.draw_image / autocad.draw_image remain for compatibility; prefer art.draw.
Files: "tác giả" / "Authors" = the file's Authors property (file.info authors); "ngày tạo" = the day the file FIRST appeared (file.info first_seen), not the last-saved or modified time. To answer where / who / when about a file: find its path (knowledge.query on visible_files, or filesystem.list), then file.info on that path. Never guess an author from the owner or from the folder.
16. What the user is doing on this PC, now or earlier (\"tôi đang làm gì\", \"hôm nay tôi dùng Excel bao lâu\", \"tóm tắt ngày làm việc\"): activity.now (live: program and window in front; add desktop.observe or vision.ocr to read what is on the screen), activity.summary / activity.timeline (over a period, from the tracker). If the tracker has no data, say so and offer activity.control start; never turn tracking on unless the user asked for it. Report only what the tools returned.
15. Artistic drawing in Paint (a named painting, a style, a medium, "bút chì", "màu nước", "sơn dầu", "sơn mài", "trắng đen"): first art.brief with the user's words (it identifies a real work, decides reproduction / interpretation, medium and rendering mode, honours "không tô màu", and downloads a reference picture into data.image_path), then art.paint with its mode and that image_path (or an SVG you wrote when there is no reference). If art.brief returns questions, ask the user first. Never claim a work was verified beyond the verification state the tool reports, never call a Paint simulation real oil, watercolour or lacquer, and say plainly when no reference picture could be fetched. Pencil and ink drawings contain no colour unless asked.
13. Detail and thoroughness: always do the work at the highest level of detail and completeness the tools allow, from the first attempt. Cover every part of the request (every item, every file, every page, every visible part of a picture), check the result against what was asked, and never deliver a quick, summary-level or "basic" version first and wait for the user to ask for more. Use each tool's most detailed setting (for example paint.draw_image at its default maximum detail, drawings with every visible part, documents with every requested section). A lower level of detail is chosen only when the user explicitly asks for a quick, rough or short result.
11. Write task.complete "summary", task.ask_user "question" and task.fail "reason" in this language: {language}. The summary is what the user reads: say plainly what was done or answer the question. The chat shows Markdown and LaTeX: use **bold**, *italic*, lists, tables, `code`, fenced code blocks, and $x^2$ / $$ ... $$ for formulas when they make the answer clearer, never as decoration.

Execution mode: {mode}. Coordinates are physical screen pixels.

# This computer
{environment}

# Tools
{catalog}"#,
        mode = mode.as_str(),
        max_actions = MAX_ACTIONS_PER_REPLY,
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
    /// Local date and time, e.g. "Thursday 2026-10-08 15:04 (UTC+07:00)".
    pub now: &'a str,
    /// Earlier exchanges with the user (follow-up context, questions and answers).
    pub conversation: &'a [String],
    /// Files the user attached to the request.
    pub attachments: &'a [UserAttachment],
    /// Playbooks (skills/*.md) that match the request, rendered; "" when none.
    pub playbooks: &'a str,
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
    let mut out = format!("# Task\n{}\n\nNow: {}\n", ctx.goal, ctx.now);
    if !ctx.attachments.is_empty() {
        out.push_str("\n# Files the user attached to this request\n");
        for a in ctx.attachments {
            out.push_str(&format!("- {}\n", a.describe()));
        }
        let images = ctx.attachments.iter().filter(|a| a.is_image()).count();
        out.push_str("These are the files the request is about (\"file này\", \"ảnh này\", \"tài liệu đính kèm\"). ");
        if images > 0 {
            out.push_str("The attached images are shown to you with this message; describe or use what you see in them directly. ");
        }
        out.push_str("Read documents with document.read (or office.* for Office files); work on these copies or on outputs you create, never ask where the file is.\n");
    }
    if !ctx.conversation.is_empty() {
        out.push_str("\n# Conversation with the user so far\n");
        for line in ctx.conversation {
            out.push_str(&format!("- {}\n", clip(line, 1500)));
        }
    }
    if !ctx.playbooks.trim().is_empty() {
        out.push_str(&format!("\n# Playbooks for this kind of job (tested procedures: follow them)\n{}", ctx.playbooks));
    }
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
    out.push_str("\nChoose the next action(s). Reply with one JSON object only.");
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

fn markdown_link() -> &'static Regex {
    static LINK: OnceLock<Regex> = OnceLock::new();
    LINK.get_or_init(|| Regex::new(r"\[([^\[\]\s]+)\]\(([a-zA-Z][a-zA-Z0-9+.-]*:[^()\s]+)\)").expect("valid regex"))
}

/// Gemini Web turns text it recognises as a link into markdown (`[text](target)`, where target is the
/// text with a scheme added). That is undone by restoring exactly what the model wrote (`text`) whenever
/// the target is just the text plus scheme / trailing slash; real markdown links are left untouched.
fn is_autolink(label: &str, target: &str) -> bool {
    let bare = |s: &str| -> String {
        let s = s.split_once("://").map_or(s, |(_, rest)| rest);
        let s = s.strip_prefix("mailto:").unwrap_or(s);
        s.trim_end_matches('/').to_lowercase()
    };
    label == target || bare(label) == bare(target)
}

fn strip_autolinks(text: &str) -> String {
    markdown_link()
        .replace_all(text, |caps: &regex::Captures<'_>| {
            if is_autolink(&caps[1], &caps[2]) { caps[1].to_string() } else { caps[0].to_string() }
        })
        .into_owned()
}

fn strip_autolinks_in(value: &mut Value) {
    match value {
        Value::String(s) => {
            if s.contains("](") {
                *s = strip_autolinks(s);
            }
        }
        Value::Array(items) => items.iter_mut().for_each(strip_autolinks_in),
        Value::Object(map) => map.values_mut().for_each(strip_autolinks_in),
        _ => {}
    }
}

/// Repair the usual damage in a model's JSON: a backslash that is not a valid escape (Windows paths), raw line
/// breaks inside strings, trailing commas, and a reply that was cut off (unclosed string / brackets). Starts at the
/// first `{`; returns None when there is none.
pub fn repair_json(text: &str) -> Option<String> {
    let start = text.find('{')?;
    let chars: Vec<char> = text[start..].chars().collect();
    let mut out = String::with_capacity(chars.len() + 16);
    let mut stack: Vec<char> = Vec::new();
    let mut in_string = false;
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if in_string {
            match c {
                '\\' => {
                    let next = chars.get(i + 1).copied();
                    let valid = match next {
                        Some('"' | '\\' | '/' | 'b' | 'f' | 'n' | 'r' | 't') => true,
                        Some('u') => chars.get(i + 2..i + 6).is_some_and(|h| h.len() == 4 && h.iter().all(|x| x.is_ascii_hexdigit())),
                        _ => false,
                    };
                    if valid {
                        out.push('\\');
                        out.push(next.unwrap());
                        i += 2;
                        continue;
                    }
                    out.push_str("\\\\"); // a lone backslash: keep it as a literal backslash
                }
                '"' => {
                    // a quote that ends the string only if what follows can follow a string in JSON
                    let rest = chars[i + 1..].iter().find(|x| !x.is_whitespace());
                    if matches!(rest, None | Some(',' | '}' | ']' | ':')) {
                        in_string = false;
                        out.push('"');
                    } else {
                        out.push_str("\\\"");
                    }
                }
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                c if (c as u32) < 0x20 => {}
                c => out.push(c),
            }
        } else {
            match c {
                '"' => {
                    in_string = true;
                    out.push('"');
                }
                '{' => {
                    stack.push('}');
                    out.push('{');
                }
                '[' => {
                    stack.push(']');
                    out.push('[');
                }
                '}' | ']' => {
                    // a trailing comma before the closer
                    let trimmed = out.trim_end().len();
                    if out[..trimmed].ends_with(',') {
                        out.truncate(trimmed - 1);
                    }
                    if stack.last() == Some(&c) {
                        stack.pop();
                    }
                    out.push(c);
                    if stack.is_empty() {
                        return Some(out);
                    }
                }
                c => out.push(c),
            }
        }
        i += 1;
    }
    // cut off: close the open string and the open brackets
    if in_string {
        out.push('"');
    }
    let trimmed = out.trim_end().len();
    if out[..trimmed].ends_with(',') {
        out.truncate(trimmed - 1);
    }
    let trimmed = out.trim_end().len();
    if out[..trimmed].ends_with(':') {
        out.push_str("null");
    }
    while let Some(closer) = stack.pop() {
        out.push(closer);
    }
    Some(out)
}

/// The first JSON object in a model reply, if any (repaired when it is not valid as written).
pub fn extract_json_value(text: &str) -> Option<Value> {
    if let Some(v) = extract_json_object(text).and_then(|raw| serde_json::from_str(raw).ok()) {
        return Some(v);
    }
    repair_json(text).and_then(|raw| serde_json::from_str(&raw).ok())
}

fn parse_action(action: &Value) -> Result<(String, Value), String> {
    // Small models sometimes copy the catalog line ("browser.get_state {?max_elements: integer}"): the
    // tool name is the first word.
    let tool = action
        .get("tool")
        .and_then(Value::as_str)
        .and_then(|t| t.split(|c: char| c.is_whitespace() || c == '(' || c == '{').find(|w| !w.is_empty()))
        .ok_or_else(|| "missing action.tool".to_string())?
        .to_string();
    let mut args = match action.get("args") {
        None | Some(Value::Null) => Value::Object(Default::default()),
        Some(v @ Value::Object(_)) => v.clone(),
        Some(_) => return Err("action.args must be a JSON object".into()),
    };
    strip_autolinks_in(&mut args);
    Ok((tool, args))
}

/// Parse a planner reply into the actions to run in order (at most `MAX_ACTIONS_PER_REPLY`, ending at the
/// first `task.*` call). The first action carries the thought and the plan.
pub fn parse_reply(text: &str) -> Result<Vec<PlannerAction>, String> {
    let raw = extract_json_object(text);
    let value: Value = match raw.map(serde_json::from_str::<Value>) {
        Some(Ok(v)) => v,
        _ => {
            // not valid as written (a Windows path with single backslashes, a cut-off reply): repair, then parse
            let repaired = repair_json(text).ok_or_else(|| "the reply contains no JSON object".to_string())?;
            serde_json::from_str(&repaired).map_err(|e| format!("the reply is not valid JSON: {e}"))?
        }
    };
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
    let raw_actions: Vec<&Value> = match value.get("actions") {
        Some(Value::Array(items)) if !items.is_empty() => items.iter().take(MAX_ACTIONS_PER_REPLY).collect(),
        Some(Value::Array(_)) => return Err("\"actions\" is empty".into()),
        Some(_) => return Err("\"actions\" must be an array".into()),
        None => vec![value.get("action").unwrap_or(&value)],
    };
    let mut actions = Vec::with_capacity(raw_actions.len());
    for (i, raw) in raw_actions.into_iter().enumerate() {
        let (tool, args) = parse_action(raw)?;
        let terminal = tool.starts_with("task.");
        actions.push(PlannerAction {
            thought: if i == 0 { thought.clone() } else { format!("(continuing the batch) {}", clip(&thought, 120)) },
            plan: if i == 0 { plan.clone() } else { None },
            tool,
            args,
        });
        if terminal {
            break;
        }
    }
    Ok(actions)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn repairs_windows_paths_trailing_commas_and_cut_off_replies() {
        // single backslashes (invalid escapes) in a path
        let reply = r#"{"thought": "t", "actions": [{"tool": "filesystem.read", "args": {"path": "C:\Users\An\a.txt"}}]}"#;
        let actions = parse_reply(reply).unwrap();
        assert_eq!(actions[0].args["path"], "C:\\Users\\An\\a.txt".replace("\\\\", "\\"));
        // a trailing comma and a raw line break inside a string
        let reply = "{\"thought\": \"a\nb\", \"actions\": [{\"tool\": \"task.complete\", \"args\": {\"summary\": \"xong\",}},]}";
        assert_eq!(parse_reply(reply).unwrap()[0].tool, "task.complete");
        // cut off in the middle of the summary: the part that arrived is kept
        let cut = r#"{"thought": "x", "actions": [{"tool": "task.complete", "args": {"summary": "Thư mục C:\Users\An co 3 tep"#;
        let actions = parse_reply(cut).unwrap();
        assert!(actions[0].args["summary"].as_str().unwrap().starts_with("Thư mục C:"));
        assert!(parse_reply("no json here").is_err());
    }

    #[test]
    fn parses_fenced_reply_with_braces_in_strings() {
        let reply = "Sure:\n```json\n{\"thought\": \"type {x}\", \"plan\": [\"a\"], \"action\": {\"tool\": \"desktop.type\", \"args\": {\"text\": \"}{\"}}}\n```";
        let action = parse_reply(reply).unwrap().remove(0);
        assert_eq!(action.tool, "desktop.type");
        assert_eq!(action.args, json!({"text": "}{"}));
        assert_eq!(action.plan, Some(vec!["a".to_string()]));
    }

    #[test]
    fn undoes_gemini_web_autolinks_but_keeps_real_markdown() {
        let reply = r#"{"action": {"tool": "browser.navigate", "args": {"url": "[https://www.google.com](https://www.google.com)",
            "nested": ["see [www.x.org](http://www.x.org)", "[github.com/a/b](https://github.com/a/b)"],
            "text": "read [the docs](https://docs.rs) now"}}}"#;
        let action = parse_reply(reply).unwrap().remove(0);
        assert_eq!(action.args["url"], "https://www.google.com");
        assert_eq!(action.args["nested"][0], "see www.x.org");
        assert_eq!(action.args["nested"][1], "github.com/a/b");
        assert_eq!(action.args["text"], "read [the docs](https://docs.rs) now");
    }

    #[test]
    fn accepts_flat_action_and_rejects_garbage() {
        let action = parse_reply(r#"{"tool": "desktop.list_windows"}"#).unwrap().remove(0);
        assert_eq!(action.tool, "desktop.list_windows");
        assert!(parse_reply("I will click the button").is_err());
        assert!(parse_reply(r#"{"thought": "x"}"#).is_err());
        assert!(parse_reply(r#"{"action": {"tool": "x", "args": [1]}}"#).is_err());
        assert!(parse_reply(r#"{"actions": []}"#).is_err());
        let copied = parse_reply(r#"{"action": {"tool": "browser.get_state {?max_elements: integer}", "args": {}}}"#).unwrap();
        assert_eq!(copied[0].tool, "browser.get_state");
    }

    #[test]
    fn batches_stop_at_the_first_task_call_and_are_capped() {
        let reply = r#"{"thought": "draw", "plan": ["p"], "actions": [
            {"tool": "autocad.create_circle", "args": {"radius": 1}},
            {"tool": "autocad.create_line", "args": {}},
            {"tool": "task.complete", "args": {"summary": "done"}},
            {"tool": "autocad.erase", "args": {}}]}"#;
        let actions = parse_reply(reply).unwrap();
        let tools: Vec<&str> = actions.iter().map(|a| a.tool.as_str()).collect();
        assert_eq!(tools, ["autocad.create_circle", "autocad.create_line", "task.complete"]);
        assert_eq!(actions[0].plan, Some(vec!["p".to_string()]));
        assert_eq!(actions[1].plan, None);
        let many = format!(r#"{{"actions": [{}]}}"#, vec![r#"{"tool": "x.y"}"#; 20].join(","));
        assert_eq!(parse_reply(&many).unwrap().len(), MAX_ACTIONS_PER_REPLY);
    }
}
