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
    /// How many of its triggers must occur in the request (auto-written playbooks need 2: a lone common word is not enough).
    pub min_hits: usize,
}

/// Parse one playbook file; None when the front matter is missing.
pub fn parse(text: &str) -> Option<Playbook> {
    let text = text.trim_start_matches('\u{feff}');
    let rest = text.strip_prefix("---")?;
    let end = rest.find("\n---")?;
    let (front, body) = (&rest[..end], rest[end + 4..].trim());
    let mut name = String::new();
    let mut triggers = Vec::new();
    let mut min_hits = 1usize;
    for line in front.lines() {
        if let Some(v) = line.trim().strip_prefix("name:") {
            name = v.trim().to_string();
        } else if let Some(v) = line.trim().strip_prefix("min_hits:") {
            min_hits = v.trim().parse().unwrap_or(1).clamp(1, 6);
        } else if let Some(v) = line.trim().strip_prefix("triggers:") {
            triggers = v.split(',').map(|t| t.trim().to_lowercase()).filter(|t| !t.is_empty()).collect();
        }
    }
    (!name.is_empty() && !triggers.is_empty() && !body.is_empty()).then(|| Playbook { name, triggers, body: body.to_string(), min_hits })
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

/// Write (or refresh) `skills/<id>.md` for a playbook generated from repeated verified successes. At most `MAX_AUTO`
/// auto playbooks exist. Returns true when the file changed.
pub fn write_auto_skill(skills_dir: &Path, skill: &crate::memory::GeneratedSkill) -> bool {
    const MAX_AUTO: usize = 40;
    let path = skills_dir.join(format!("{}.md", skill.id));
    let existing = std::fs::read_dir(skills_dir).map(|d| d.flatten().filter(|e| e.file_name().to_string_lossy().starts_with("auto-")).count()).unwrap_or(0);
    if !path.exists() && existing >= MAX_AUTO {
        return false;
    }
    let content = format!("---
name: {}
kind: auto
min_hits: 2
triggers: {}
---
{}", skill.name, skill.triggers.join(", "), skill.body);
    if std::fs::read_to_string(&path).ok().as_deref() == Some(content.as_str()) {
        return false;
    }
    let tmp = path.with_extension("md.tmp");
    std::fs::write(&tmp, content).is_ok() && std::fs::rename(&tmp, &path).is_ok()
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
        .filter(|(n, p)| *n >= p.min_hits.max(1))
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
    fn an_auto_playbook_needs_two_trigger_words() {
        let p = parse("---
name: auto
kind: auto
min_hits: 2
triggers: doanh, csv, tong
---
body
").unwrap();
        let books = vec![p];
        assert!(matching(&books, "gui csv cho toi").is_empty(), "one common word is not enough");
        assert_eq!(matching(&books, "tong doanh thu trong csv").len(), 1);
    }

    #[test]
    fn the_drawing_spec_playbooks_apply_to_their_requests() {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../skills");
        let books = load(&dir);
        let names = |goal: &str| matching(&books, goal).iter().map(|p| p.name.clone()).collect::<Vec<_>>();
        let dora = names("ve doraemon toan than trong paint");
        assert!(dora.contains(&"ve-doraemon-v2".to_string()) && dora.contains(&"ve-doraemon-paint".to_string()), "{dora:?}");
        let cad = names("ve mat bang nha 2 tang bang autocad");
        assert!(cad.contains(&"autocad-ban-ve-nha".to_string()) && cad.contains(&"autocad-ban-ve-kiem-tra".to_string()), "{cad:?}");
        assert!(!names("tong doanh thu trong file csv").iter().any(|n| n.starts_with("ve-doraemon") || n.starts_with("autocad")));
        for need in ["FINAL_STATUS", "DRAFT", "UNKNOWN", "BLOCKER"] {
            assert!(books.iter().any(|b| b.name.starts_with("autocad") && b.body.contains(need)), "autocad playbooks lost {need}");
        }
        for need in ["DRAW_EXECUTION_PASS", "VISUAL_QUALITY_PASS", "6 whiskers"] {
            assert!(books.iter().any(|b| b.name.starts_with("ve-doraemon") && b.body.contains(need)), "doraemon playbooks lost {need}");
        }
    }

    #[test]
    fn the_office_and_paint_spec_playbooks_apply() {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../skills");
        let books = load(&dir);
        let names = |goal: &str| matching(&books, goal).iter().map(|p| p.name.clone()).collect::<Vec<_>>();
        let has = |v: &Vec<String>, n: &str| v.iter().any(|x| x == n);
        let outlook = names("soan email nhap trong outlook va luu vao sharepoint");
        assert!(has(&outlook, "office-microsoft-365"), "{outlook:?}");
        let flow = names("lam bao cao tuan roi tao email nhap kem file");
        assert!(has(&flow, "office-quy-trinh-lien-ung-dung"), "{flow:?}");
        let paint = names("ve ngoi nha bang paint roi to mau");
        assert!(has(&paint, "paint-ve-tu-dong") && has(&paint, "paint-to-mau-kiem-tra"), "{paint:?}");
        for need in ["UNKNOWN", "PARTIAL", "WAITING_FOR_USER"] {
            assert!(books.iter().any(|b| b.name.starts_with("office-") && b.body.contains(need)), "office playbooks lost {need}");
        }
        assert!(books.iter().any(|b| b.name == "paint-to-mau-kiem-tra" && b.body.contains("FillLeak")));
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
