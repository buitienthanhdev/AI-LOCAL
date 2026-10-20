//! Execution policy (rules in core/rust-agent/policy/*.toml).
//!
//! Every tool call passes through `Policy::evaluate` before it is routed. The evaluation:
//!   1. normalises path arguments (env vars, ~, `..`) and rewrites them in the call;
//!   2. refuses credential stores and secrets (`filesystem.deny_always`) in every mode;
//!   3. screens shell commands (`shell.deny` refused everywhere, `shell.confirm` needs consent);
//!   4. screens browser URLs by scheme/domain;
//!   5. applies the per-mode decision for the tool's `policy_category`, the tool's own
//!      `requires_confirmation`, the risk threshold and the filesystem write roots.
//!
//! The strictest outcome wins: Deny > Confirm > Allow. The agent never elevates; UAC is never bypassed.

use std::collections::HashMap;
use std::path::{Component, Path, PathBuf};

use anyhow::{anyhow, Context, Result};
use regex::{Regex, RegexBuilder};
use serde::Deserialize;
use serde_json::Value;

use crate::config::{Config, ExecutionMode};
use crate::guard::Guard;
use crate::tools::{RiskLevel, ToolSpec};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Verdict {
    Allow,
    Confirm,
    Deny,
}

#[derive(Debug, Clone)]
pub struct Decision {
    pub verdict: Verdict,
    pub reasons: Vec<String>,
    /// Arguments after path normalisation; this is what gets executed.
    pub args: Value,
    /// Classes of the "always confirm" guard that fired (empty = none). When non-empty the verdict is at least
    /// Confirm in EVERY mode, and the confirmation must come from a person (see guard.rs).
    pub always_confirm: Vec<String>,
    /// Fingerprint of the exact call (tool + normalised arguments) a confirmation is bound to.
    pub fingerprint: String,
}

#[derive(Debug, Deserialize)]
struct RiskRules {
    confirm_at_or_above_normal: RiskLevel,
    confirm_at_or_above_trusted: RiskLevel,
}

#[derive(Debug, Deserialize)]
struct PathRules {
    path_args: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct FsRules {
    deny_always: Vec<String>,
    /// File NAMES (regex, case-insensitive) that are secrets wherever they are: private keys, key stores.
    #[serde(default)]
    deny_file_patterns: Vec<String>,
    write_roots_normal: Vec<String>,
    protected: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct ShellRules {
    deny: Vec<String>,
    confirm: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct BrowserRules {
    confirm_schemes: Vec<String>,
    deny_schemes: Vec<String>,
    deny_domains: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct AutocadRules {
    destructive_tools: Vec<String>,
}

/// Areas where deleting is the agent's own housekeeping (its temp and artifact folders): the "always confirm"
/// deletion classes do not fire for targets strictly inside them. Absent section = these defaults.
#[derive(Debug, Deserialize)]
struct AlwaysConfirmCfg {
    scratch_roots: Vec<String>,
}

impl Default for AlwaysConfirmCfg {
    fn default() -> Self {
        Self {
            scratch_roots: vec![
                "%TEMP%".into(),
                "%TMP%".into(),
                "C:\\temp".into(),
                "{artifacts}".into(),
            ],
        }
    }
}

#[derive(Debug, Deserialize)]
struct PolicyFile {
    modes: HashMap<String, HashMap<String, Verdict>>,
    #[serde(default)]
    always_confirm: AlwaysConfirmCfg,
    risk: RiskRules,
    paths: PathRules,
    filesystem: FsRules,
    shell: ShellRules,
    browser: BrowserRules,
    autocad: AutocadRules,
}

pub struct Policy {
    mode: ExecutionMode,
    modes: HashMap<String, HashMap<String, Verdict>>,
    risk: RiskRules,
    path_args: Vec<String>,
    deny_always: Vec<PathBuf>,
    deny_files: Vec<Regex>,
    write_roots: Vec<PathBuf>,
    protected: Vec<PathBuf>,
    shell_deny: Vec<Regex>,
    shell_confirm: Vec<Regex>,
    browser: BrowserRules,
    destructive_tools: Vec<String>,
    guard: Guard,
}

fn regexes(patterns: &[String]) -> Result<Vec<Regex>> {
    patterns
        .iter()
        .map(|p| {
            RegexBuilder::new(p)
                .case_insensitive(true)
                .build()
                .with_context(|| format!("bad pattern {p:?}"))
        })
        .collect()
}

/// Expand %VAR% (case-insensitive), a leading ~ and project placeholders.
pub fn expand(raw: &str, placeholders: &HashMap<&str, PathBuf>) -> String {
    let mut text = raw.trim().trim_matches('"').to_string();
    for (key, value) in placeholders {
        text = text.replace(&format!("{{{key}}}"), &value.to_string_lossy());
    }
    if let Some(rest) = text.strip_prefix('~') {
        if let Ok(home) = std::env::var("USERPROFILE") {
            text = format!("{home}{rest}");
        }
    }
    let env: HashMap<String, String> = std::env::vars()
        .map(|(k, v)| (k.to_uppercase(), v))
        .collect();
    let mut out = String::new();
    let mut rest = text.as_str();
    while let Some(start) = rest.find('%') {
        out.push_str(&rest[..start]);
        let after = &rest[start + 1..];
        match after.find('%') {
            Some(end) => {
                let name = &after[..end];
                match env.get(&name.to_uppercase()) {
                    Some(value) => out.push_str(value),
                    None => {
                        out.push('%');
                        out.push_str(name);
                        out.push('%');
                    }
                }
                rest = &after[end + 1..];
            }
            None => {
                out.push_str(&rest[start..]);
                rest = "";
            }
        }
    }
    out.push_str(rest);
    out
}

/// Lexical normalisation: resolves `.` and `..`, unifies separators. Does not touch the filesystem.
pub fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

fn is_within(path: &Path, root: &Path) -> bool {
    let p = path.to_string_lossy().to_lowercase().replace('/', "\\");
    let r = root.to_string_lossy().to_lowercase().replace('/', "\\");
    let r = r.trim_end_matches('\\');
    p == r || p.starts_with(&format!("{r}\\"))
}

fn risk_rank(level: RiskLevel) -> u8 {
    match level {
        RiskLevel::Low => 0,
        RiskLevel::Medium => 1,
        RiskLevel::High => 2,
        RiskLevel::Critical => 3,
    }
}

impl Policy {
    pub fn load(config: &Config, mode: ExecutionMode) -> Result<Self> {
        let dir = config.path(&config.paths.policy_dir);
        let text = std::fs::read_to_string(dir.join("default.toml"))
            .context("reading policy/default.toml")?;
        let mut value: toml::Value =
            toml::from_str(&text).context("parsing policy/default.toml")?;
        let local = dir.join("local.toml");
        if local.is_file() {
            let overlay: toml::Value = toml::from_str(&std::fs::read_to_string(&local)?)?;
            merge(&mut value, overlay);
        }
        let file: PolicyFile = value
            .try_into()
            .context("policy file has an unexpected shape")?;
        if !file.modes.contains_key(mode.as_str()) {
            return Err(anyhow!("policy has no rules for mode {}", mode.as_str()));
        }
        let placeholders = HashMap::from([
            ("repo", config.root.clone()),
            ("artifacts", config.artifacts_dir()),
            ("state", config.state_dir()),
            ("memory", config.path(&config.paths.memory_dir)),
            ("logs", config.logs_dir()),
        ]);
        let paths = |list: &[String]| -> Vec<PathBuf> {
            list.iter()
                .map(|p| expand(p, &placeholders))
                .filter(|p| !p.contains('%')) // unresolved variable: rule does not apply on this machine
                .map(|p| normalize(Path::new(&p)))
                .collect()
        };
        let protected = paths(&file.filesystem.protected);
        let guard = Guard::new(paths(&file.always_confirm.scratch_roots), protected.clone());
        Ok(Self {
            mode,
            guard,
            deny_always: paths(&file.filesystem.deny_always),
            deny_files: regexes(&file.filesystem.deny_file_patterns)?,
            write_roots: paths(&file.filesystem.write_roots_normal),
            protected,
            shell_deny: regexes(&file.shell.deny)?,
            shell_confirm: regexes(&file.shell.confirm)?,
            modes: file.modes,
            risk: file.risk,
            path_args: file.paths.path_args,
            browser: file.browser,
            destructive_tools: file.autocad.destructive_tools,
        })
    }

    pub fn mode(&self) -> ExecutionMode {
        self.mode
    }

    pub fn evaluate(&self, spec: &ToolSpec, args: &Value) -> Decision {
        let mut verdict = Verdict::Allow;
        let mut reasons = Vec::new();
        let raise =
            |v: Verdict, reason: String, verdict: &mut Verdict, reasons: &mut Vec<String>| {
                if v > Verdict::Allow {
                    reasons.push(reason);
                }
                if v > *verdict {
                    *verdict = v;
                }
            };

        let category_verdict = self
            .modes
            .get(self.mode.as_str())
            .and_then(|m| m.get(&spec.policy_category))
            .copied()
            .unwrap_or(Verdict::Confirm);
        raise(
            category_verdict,
            format!(
                "{} actions are '{category_verdict:?}' in {} mode",
                spec.policy_category,
                self.mode.as_str()
            ),
            &mut verdict,
            &mut reasons,
        );

        if spec.requires_confirmation {
            raise(
                Verdict::Confirm,
                format!("{} always requires confirmation", spec.name),
                &mut verdict,
                &mut reasons,
            );
        }
        let threshold = match self.mode {
            ExecutionMode::Normal => Some(self.risk.confirm_at_or_above_normal),
            ExecutionMode::Trusted => Some(self.risk.confirm_at_or_above_trusted),
            ExecutionMode::FullAccess => None,
        };
        if let Some(threshold) = threshold {
            if risk_rank(spec.risk_level) >= risk_rank(threshold) {
                raise(
                    Verdict::Confirm,
                    format!("{} is {:?} risk", spec.name, spec.risk_level),
                    &mut verdict,
                    &mut reasons,
                );
            }
        }
        if self.destructive_tools.iter().any(|t| t == &spec.name) {
            let v = self
                .modes
                .get(self.mode.as_str())
                .and_then(|m| m.get("autocad.destructive"))
                .copied();
            raise(
                v.unwrap_or(Verdict::Confirm),
                format!("{} destroys drawing content", spec.name),
                &mut verdict,
                &mut reasons,
            );
        }

        let mut normalized = args.clone();
        let writes = spec.policy_category.ends_with(".write") || spec.policy_category == "shell";
        if let Value::Object(map) = &mut normalized {
            for key in &self.path_args {
                let Some(Value::String(raw)) = map.get(key) else {
                    continue;
                };
                let expanded = expand(raw, &HashMap::new());
                let path = normalize(Path::new(&expanded));
                if !path.is_absolute() {
                    raise(
                        Verdict::Deny,
                        format!("{key} must be an absolute path, got {raw:?}"),
                        &mut verdict,
                        &mut reasons,
                    );
                    continue;
                }
                if let Some(root) = self.deny_always.iter().find(|r| is_within(&path, r)) {
                    raise(
                        Verdict::Deny,
                        format!(
                            "{} is inside a protected credential/secret location ({})",
                            path.display(),
                            root.display()
                        ),
                        &mut verdict,
                        &mut reasons,
                    );
                }
                if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                    if self.deny_files.iter().any(|re| re.is_match(name)) {
                        raise(
                            Verdict::Deny,
                            format!(
                                "{name} is a private key or key store: it is never read or written"
                            ),
                            &mut verdict,
                            &mut reasons,
                        );
                    }
                }
                if writes && self.mode != ExecutionMode::FullAccess {
                    if self.protected.iter().any(|r| is_within(&path, r)) {
                        raise(
                            Verdict::Confirm,
                            format!("{} is a protected system location", path.display()),
                            &mut verdict,
                            &mut reasons,
                        );
                    } else if self.mode == ExecutionMode::Normal
                        && spec.policy_category == "filesystem.write"
                        && !self.write_roots.iter().any(|r| is_within(&path, r))
                    {
                        raise(
                            Verdict::Confirm,
                            format!("{} is outside the normal-mode write roots", path.display()),
                            &mut verdict,
                            &mut reasons,
                        );
                    }
                }
                map.insert(
                    key.clone(),
                    Value::String(path.to_string_lossy().into_owned()),
                );
            }

            if spec.policy_category == "shell" {
                // shell.exec carries its text in `command`, python.exec in `code`. Both run with the user's rights, so
                // both are scanned: before this, python.exec bypassed every shell pattern (it is not a boundary against
                // obfuscation, only against the plain cases; mode and confirmation remain the real control).
                for key in ["command", "code"] {
                    let Some(Value::String(command)) = map.get(key) else {
                        continue;
                    };
                    for re in &self.shell_deny {
                        if re.is_match(command) {
                            raise(
                                Verdict::Deny,
                                format!("{key} matches a forbidden pattern ({})", re.as_str()),
                                &mut verdict,
                                &mut reasons,
                            );
                        }
                    }
                    if self.mode != ExecutionMode::FullAccess {
                        for re in &self.shell_confirm {
                            if re.is_match(command) {
                                raise(
                                    Verdict::Confirm,
                                    format!("{key} needs consent ({})", re.as_str()),
                                    &mut verdict,
                                    &mut reasons,
                                );
                            }
                        }
                    }
                }
            }

            if spec.policy_category.starts_with("browser") {
                if let Some(Value::String(url)) = map.get("url") {
                    let scheme = url.split(':').next().unwrap_or_default().to_lowercase();
                    if self.browser.deny_schemes.iter().any(|s| s == &scheme) {
                        raise(
                            Verdict::Deny,
                            format!("URL scheme {scheme}: is not allowed"),
                            &mut verdict,
                            &mut reasons,
                        );
                    } else if self.browser.confirm_schemes.iter().any(|s| s == &scheme)
                        && self.mode != ExecutionMode::FullAccess
                    {
                        raise(
                            Verdict::Confirm,
                            format!("URL scheme {scheme}: needs consent"),
                            &mut verdict,
                            &mut reasons,
                        );
                    }
                    let host = url
                        .split("://")
                        .nth(1)
                        .and_then(|r| r.split(['/', ':', '?']).next())
                        .unwrap_or_default()
                        .to_lowercase();
                    if self
                        .browser
                        .deny_domains
                        .iter()
                        .any(|d| host == *d || host.ends_with(&format!(".{d}")))
                    {
                        raise(
                            Verdict::Deny,
                            format!("domain {host} is blocked by policy"),
                            &mut verdict,
                            &mut reasons,
                        );
                    }
                }
            }
        }
        // "Always confirm": evaluated after everything else and in every mode, full_access included. It can only
        // raise a verdict (Allow -> Confirm); a Deny stays a Deny.
        let assessment = self.guard.assess(spec, &normalized);
        let always_confirm: Vec<String> =
            assessment.classes().iter().map(|c| c.to_string()).collect();
        for finding in &assessment.findings {
            raise(
                Verdict::Confirm,
                format!("always-confirm [{}]: {}", finding.class, finding.detail),
                &mut verdict,
                &mut reasons,
            );
        }
        let fingerprint = crate::guard::fingerprint(&spec.name, &normalized);
        Decision {
            verdict,
            reasons,
            args: normalized,
            always_confirm,
            fingerprint,
        }
    }
}

fn merge(base: &mut toml::Value, overlay: toml::Value) {
    match (base, overlay) {
        (toml::Value::Table(b), toml::Value::Table(o)) => {
            for (k, v) in o {
                match b.get_mut(&k) {
                    Some(existing) if existing.is_table() && v.is_table() => merge(existing, v),
                    _ => {
                        b.insert(k, v);
                    }
                }
            }
        }
        (b, o) => *b = o,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tools::ToolCatalog;
    use serde_json::json;

    fn setup(mode: ExecutionMode) -> (Policy, ToolCatalog) {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let config = Config::load_from(&root).unwrap();
        (
            Policy::load(&config, mode).unwrap(),
            ToolCatalog::load(&root.join("tools")).unwrap(),
        )
    }

    #[test]
    fn credential_stores_are_denied_in_full_access() {
        let (policy, tools) = setup(ExecutionMode::FullAccess);
        let local = std::env::var("LOCALAPPDATA").unwrap();
        let path = format!("{local}\\Google\\Chrome\\User Data\\Default\\Cookies");
        let d = policy.evaluate(
            tools.get("filesystem.read").unwrap(),
            &json!({"path": path}),
        );
        assert_eq!(d.verdict, Verdict::Deny, "{:?}", d.reasons);
    }

    #[test]
    fn private_keys_are_denied_wherever_they_are() {
        let (policy, tools) = setup(ExecutionMode::FullAccess);
        for path in [
            "C:\\Users\\x\\.ollama\\id_ed25519",
            "C:\\work\\deploy\\server.pem",
            "D:\\keys\\wallet.kdbx",
        ] {
            let d = policy.evaluate(
                tools.get("filesystem.read").unwrap(),
                &json!({"path": path}),
            );
            assert_eq!(d.verdict, Verdict::Deny, "{path}: {:?}", d.reasons);
        }
        let public = policy.evaluate(
            tools.get("filesystem.read").unwrap(),
            &json!({"path": "C:\\Users\\x\\.ollama\\id_ed25519.pub"}),
        );
        assert_ne!(public.verdict, Verdict::Deny, "{:?}", public.reasons);
        let hunt = policy.evaluate(
            tools.get("shell.exec").unwrap(),
            &json!({"command": "Get-ChildItem -Recurse -Include id_rsa,id_ed25519 C:\\Users"}),
        );
        assert_eq!(hunt.verdict, Verdict::Deny, "{:?}", hunt.reasons);
        let normal = policy.evaluate(
            tools.get("shell.exec").unwrap(),
            &json!({"command": "Get-ChildItem C:\\temp"}),
        );
        assert_ne!(normal.verdict, Verdict::Deny);
    }

    #[test]
    fn traversal_is_normalised_before_checks() {
        let (policy, tools) = setup(ExecutionMode::FullAccess);
        let raw = "%LOCALAPPDATA%\\Temp\\..\\Google\\Chrome\\User Data\\Local State";
        let d = policy.evaluate(tools.get("filesystem.read").unwrap(), &json!({"path": raw}));
        assert_eq!(d.verdict, Verdict::Deny);
    }

    #[test]
    fn elevation_is_refused_in_every_mode() {
        for mode in [
            ExecutionMode::Normal,
            ExecutionMode::Trusted,
            ExecutionMode::FullAccess,
        ] {
            let (policy, tools) = setup(mode);
            let d = policy.evaluate(
                tools.get("shell.exec").unwrap(),
                &json!({"command": "Start-Process pwsh -Verb RunAs"}),
            );
            assert_eq!(d.verdict, Verdict::Deny, "mode {mode:?}");
        }
    }

    #[test]
    fn shell_needs_confirmation_in_normal_but_not_trusted() {
        let (normal, tools) = setup(ExecutionMode::Normal);
        let cmd = json!({"command": "Get-ChildItem C:\\"});
        assert_eq!(
            normal
                .evaluate(tools.get("shell.exec").unwrap(), &cmd)
                .verdict,
            Verdict::Confirm
        );
        let (trusted, tools) = setup(ExecutionMode::Trusted);
        assert_eq!(
            trusted
                .evaluate(tools.get("shell.exec").unwrap(), &cmd)
                .verdict,
            Verdict::Allow
        );
        let risky = json!({"command": "Remove-Item C:\\temp\\x -Recurse"});
        assert_eq!(
            trusted
                .evaluate(tools.get("shell.exec").unwrap(), &risky)
                .verdict,
            Verdict::Confirm
        );
    }

    #[test]
    fn normal_mode_write_roots() {
        let (policy, tools) = setup(ExecutionMode::Normal);
        let inside = json!({"path": "C:\\temp\\hello.txt", "content": "x"});
        let d = policy.evaluate(tools.get("filesystem.write").unwrap(), &inside);
        assert_eq!(d.verdict, Verdict::Allow, "{:?}", d.reasons);
        let outside = json!({"path": "D:\\somewhere\\x.txt", "content": "x"});
        assert_eq!(
            policy
                .evaluate(tools.get("filesystem.write").unwrap(), &outside)
                .verdict,
            Verdict::Confirm
        );
        let relative = json!({"path": "x.txt", "content": "x"});
        assert_eq!(
            policy
                .evaluate(tools.get("filesystem.write").unwrap(), &relative)
                .verdict,
            Verdict::Deny
        );
    }

    #[test]
    fn env_vars_are_expanded_in_executed_args() {
        let (policy, tools) = setup(ExecutionMode::Normal);
        let d = policy.evaluate(
            tools.get("filesystem.stat").unwrap(),
            &json!({"path": "%TEMP%\\a.txt"}),
        );
        let path = d.args["path"].as_str().unwrap();
        assert!(!path.contains('%') && path.ends_with("a.txt"), "{path}");
    }
}
