//! Playbooks: short, tested procedures for the kinds of jobs people give an assistant, shown to the planner when the
//! request matches. They carry what is learned from running real tasks (which tool is right, the usual trap, how to
//! check the result), so a smaller model plans like an experienced one.
//!
//! Files: `skills/*.md` in the repository. Format:
//!
//! ```text
//! ---
//! name: sua-chu-trong-word
//! triggers: word, docx, thay chu, doi chu
//! ---
//! body shown to the planner (Markdown, a few lines)
//! ```
//!
//! `triggers` are accent-free, lower-case words or phrases; a playbook applies when at least one occurs in the
//! request (the accent-free form of the goal). The best matches (most triggers hit) are shown, at most `LIMIT`.

use std::path::Path;

const LIMIT: usize = 3;
const MAX_BODY_CHARS: usize = 2000;

#[derive(Debug, Clone)]
pub struct Playbook {
    pub name: String,
    pub triggers: Vec<String>,
    pub body: String,
}

/// Parse one playbook file; None when the front matter is missing.
pub fn parse(text: &str) -> Option<Playbook> {
    let text = text.trim_start_matches('\u{feff}');
    let rest = text.strip_prefix("---")?;
    let end = rest.find("\n---")?;
    let (front, body) = (&rest[..end], rest[end + 4..].trim());
    let mut name = String::new();
    let mut triggers = Vec::new();
    for line in front.lines() {
        if let Some(v) = line.trim().strip_prefix("name:") {
            name = v.trim().to_string();
        } else if let Some(v) = line.trim().strip_prefix("triggers:") {
            triggers = v.split(',').map(|t| t.trim().to_lowercase()).filter(|t| !t.is_empty()).collect();
        }
    }
    (!name.is_empty() && !triggers.is_empty() && !body.is_empty()).then(|| Playbook { name, triggers, body: body.to_string() })
}

pub fn load(dir: &Path) -> Vec<Playbook> {
    let Ok(entries) = std::fs::read_dir(dir) else { return Vec::new() };
    let mut out: Vec<Playbook> = entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|e| e == "md"))
        .filter_map(|p| std::fs::read_to_string(p).ok())
        .filter_map(|t| parse(&t))
        .collect();
    out.sort_by(|a, b| a.name.cmp(&b.name));
    out
}

/// The playbooks that apply to `goal` (already accent-free and lower case), best first.
pub fn matching<'a>(playbooks: &'a [Playbook], goal_normalized: &str) -> Vec<&'a Playbook> {
    let padded = format!(" {goal_normalized} ");
    // A playbook whose trigger is "*" always applies (the person's standing style/working instructions) and does not
    // take a place among the matched ones.
    let always = playbooks.iter().filter(|p| p.triggers.iter().any(|t| t == "*"));
    let mut scored: Vec<(usize, &Playbook)> = playbooks
        .iter()
        .filter(|p| !p.triggers.iter().any(|t| t == "*"))
        .map(|p| (p.triggers.iter().filter(|t| padded.contains(&format!(" {t} ")) || (t.contains(' ') && padded.contains(t.as_str()))).count(), p))
        .filter(|(n, _)| *n > 0)
        .collect();
    scored.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.name.cmp(&b.1.name)));
    always.chain(scored.into_iter().take(LIMIT).map(|(_, p)| p)).collect()
}

/// Text for the planner prompt ("" when nothing applies).
pub fn render(selected: &[&Playbook]) -> String {
    let mut out = String::new();
    for p in selected {
        let body: String = p.body.chars().take(MAX_BODY_CHARS).collect();
        out.push_str(&format!("## {}\n{}\n\n", p.name, body.trim()));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = "---\nname: word\ntriggers: word, docx, doi chu\n---\nDung python-docx.\n";

    #[test]
    fn parses_and_matches_whole_words() {
        let p = parse(SAMPLE).unwrap();
        assert_eq!(p.triggers, vec!["word", "docx", "doi chu"]);
        let books = vec![p];
        assert_eq!(matching(&books, "mo file word va doi chu abc").len(), 1);
        assert!(matching(&books, "keyword search").is_empty(), "'word' inside 'keyword' is not a match");
        assert!(parse("no front matter").is_none());
    }

    #[test]
    fn a_star_trigger_always_applies_without_using_a_slot() {
        let always = parse("---
name: style
triggers: *
---
Answer briefly.
").unwrap();
        let word = parse(SAMPLE).unwrap();
        let books = vec![always, word];
        assert_eq!(matching(&books, "gui email").len(), 1);
        assert_eq!(matching(&books, "mo file word").len(), 2);
    }

    #[test]
    fn the_shipped_playbooks_load() {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../skills");
        let books = load(&dir);
        assert!(books.len() >= 10, "only {} playbooks found in {}", books.len(), dir.display());
        for b in &books {
            assert!(b.body.chars().count() <= MAX_BODY_CHARS + 200, "{} is too long", b.name);
        }
    }
}
