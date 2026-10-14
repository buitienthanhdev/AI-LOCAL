//! Company knowledge search and reading, done directly on Company Brain's index (no Python at query time).
//!
//! Hybrid search, same algorithm as Company Brain's `index.search`:
//! * vector branch: the query is embedded with the model the index was built with (Ollama
//!   `qwen3-embedding`, with its retrieval instruction) and searched in Qdrant's `chunks` collection;
//! * full-text branch: SQLite FTS5 (`chunks_fts`, BM25) in `catalog.db`, active tier 1–2 files only;
//! * both ranked lists are merged with reciprocal rank fusion, lightly re-ranked (keywords in the file name or
//!   location, agreement of both branches) and limited to a few passages per file.
//!
//! The agent runs on this machine, i.e. as Company Brain's local admin, so no ACL filter applies (the same
//! rule Company Brain's server uses for loopback callers). Either branch may fail (Qdrant or Ollama down);
//! the other still answers.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use regex::Regex;
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde_json::{json, Value};

use crate::config::{Config, KnowledgeCfg};

const RRF_K: f64 = 60.0;
const BRANCH_LIMIT: usize = 30;
const PER_FILE: usize = 2;
const QUERY_INSTRUCTION: &str = "Instruct: Given a question, retrieve company documents that answer it\nQuery: ";
const QDRANT_STARTUP: Duration = Duration::from_secs(20);
const TEXT_EXTENSIONS: [&str; 8] = [".txt", ".md", ".csv", ".json", ".log", ".xml", ".html", ".ini"];
/// Common Vietnamese function words that only add noise to a full-text OR query.
const FTS_STOP: [&str; 29] = [
    "la", "va", "cua", "co", "khong", "nao", "gi", "the", "nay", "do", "trong", "voi", "cho", "một", "những", "các", "và",
    "của", "là", "có", "không", "như", "thế", "nào", "bao", "nhiêu", "ai", "đã", "được",
];

#[derive(Debug, Clone)]
pub struct Hit {
    pub score: f64,
    pub file_id: i64,
    pub name: String,
    pub path: String,
    pub loc: String,
    pub text: String,
    pub why: String,
    pub author: String,
    pub modified_at: String,
}

impl Hit {
    pub fn citation(&self) -> String {
        if self.loc.is_empty() { self.name.clone() } else { format!("{} ({})", self.name, self.loc) }
    }

    pub fn to_json(&self) -> Value {
        json!({"score": self.score, "file_id": self.file_id.to_string(), "name": self.name, "path": self.path,
               "loc": self.loc, "text": self.text, "why": self.why, "author": self.author, "modified_at": self.modified_at})
    }
}

#[derive(Clone)]
pub struct Knowledge {
    cfg: KnowledgeCfg,
    db: PathBuf,
    qdrant_exe: PathBuf,
    root: PathBuf,
    client: reqwest::Client,
}

fn words() -> &'static Regex {
    static WORDS: OnceLock<Regex> = OnceLock::new();
    WORDS.get_or_init(|| Regex::new(r"\w+").expect("valid regex"))
}

/// FTS5 OR-query of the meaningful words of `text` (at most 12, quoted), or None when nothing is left.
pub fn fts_query(text: &str) -> Option<String> {
    let mut seen = HashSet::new();
    let tokens: Vec<String> = words()
        .find_iter(text)
        .map(|m| m.as_str())
        .filter(|t| t.chars().count() >= 2 && !FTS_STOP.contains(&t.to_lowercase().as_str()))
        .filter(|t| seen.insert(t.to_string()))
        .take(12)
        .map(|t| format!("\"{}\"", t.replace('"', "")))
        .collect();
    (!tokens.is_empty()).then(|| tokens.join(" OR "))
}

fn open_db(path: &Path) -> rusqlite::Result<Connection> {
    let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
    conn.busy_timeout(Duration::from_secs(5))?;
    Ok(conn)
}

fn fts_ranks(conn: &Connection, query: &str) -> rusqlite::Result<Vec<i64>> {
    let Some(q) = fts_query(query) else { return Ok(Vec::new()) };
    let mut stmt = conn.prepare(
        "SELECT c.chunk_id FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.rowid JOIN files f ON f.file_id = c.file_id \
         WHERE chunks_fts MATCH ?1 AND f.status = 'active' AND f.tier IN (1, 2) ORDER BY bm25(chunks_fts) LIMIT ?2",
    )?;
    let ids = stmt.query_map(rusqlite::params![q, BRANCH_LIMIT as i64], |r| r.get::<_, i64>(0))?.collect();
    ids
}

struct Row {
    file_id: i64,
    name: String,
    path: String,
    loc: String,
    text: String,
    author: String,
    modified_at: String,
}

fn chunk_rows(conn: &Connection, ids: &[i64]) -> rusqlite::Result<HashMap<i64, Row>> {
    if ids.is_empty() {
        return Ok(HashMap::new());
    }
    let marks = vec!["?"; ids.len()].join(",");
    let sql = format!(
        "SELECT c.chunk_id, f.file_id, f.name, f.path, COALESCE(c.loc, ''), c.text, COALESCE(f.doc_author, f.fs_owner, ''), \
         COALESCE(f.fs_modified_at, '') FROM chunks c JOIN files f ON f.file_id = c.file_id \
         WHERE c.chunk_id IN ({marks}) AND f.status = 'active'"
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(ids.iter()), |r| {
        Ok((
            r.get::<_, i64>(0)?,
            Row {
                file_id: r.get(1)?,
                name: r.get(2)?,
                path: r.get(3)?,
                loc: r.get(4)?,
                text: r.get(5)?,
                author: r.get(6)?,
                modified_at: r.get(7)?,
            },
        ))
    })?;
    rows.collect()
}

/// Reciprocal rank fusion of the two branches, light re-ranking, at most `PER_FILE` passages per file.
/// First integer 0-10 in a model reply.
fn parse_rating(reply: &str) -> Option<u8> {
    let digits: String = reply.trim().chars().skip_while(|c| !c.is_ascii_digit()).take_while(char::is_ascii_digit).collect();
    digits.parse::<u8>().ok().filter(|n| *n <= 10)
}

/// Stable reorder of the first `ratings.len()` hits by rating (best first); unrated hits count as 0 and keep the
/// fusion order among equals. Hits beyond the rated window stay behind.
fn reorder_by_ratings(hits: &mut Vec<Hit>, ratings: &[Option<u8>]) {
    let n = ratings.len().min(hits.len());
    let mut head: Vec<(u8, Hit)> = hits.drain(..n).zip(ratings.iter()).map(|(h, r)| (r.unwrap_or(0), h)).collect();
    head.sort_by(|a, b| b.0.cmp(&a.0));
    for (rating, hit) in &mut head {
        hit.why = if hit.why.is_empty() { format!("rerank {rating}/10") } else { format!("{}; rerank {rating}/10", hit.why) };
    }
    let rest: Vec<Hit> = std::mem::take(hits);
    hits.extend(head.into_iter().map(|(_, h)| h));
    hits.extend(rest);
}

fn fuse(query: &str, vector: &[i64], fts: &[i64], rows: &HashMap<i64, Row>, limit: usize) -> Vec<Hit> {
    let mut ranks: HashMap<i64, (Option<usize>, Option<usize>)> = HashMap::new();
    for (i, id) in vector.iter().enumerate() {
        ranks.entry(*id).or_default().0 = Some(i + 1);
    }
    for (i, id) in fts.iter().enumerate() {
        ranks.entry(*id).or_default().1 = Some(i + 1);
    }
    let keywords: HashSet<String> =
        words().find_iter(query).map(|m| m.as_str().to_lowercase()).filter(|k| k.chars().count() >= 3).collect();
    let mut scored: Vec<(f64, i64, Option<usize>, Option<usize>)> = ranks
        .iter()
        .filter_map(|(id, (v, f))| {
            let row = rows.get(id)?;
            let mut s: f64 = [v, f].iter().filter_map(|r| r.map(|r| 1.0 / (RRF_K + r as f64))).sum();
            let hay = format!("{} {}", row.name, row.loc).to_lowercase();
            let in_name = keywords.iter().filter(|k| hay.contains(k.as_str())).count() as f64;
            s *= 1.0 + 0.15 * in_name + if v.is_some() && f.is_some() { 0.2 } else { 0.0 };
            Some((s, *id, *v, *f))
        })
        .collect();
    scored.sort_by(|a, b| b.0.total_cmp(&a.0).then(a.1.cmp(&b.1)));
    let mut per_file: HashMap<i64, usize> = HashMap::new();
    let mut out = Vec::new();
    for (s, id, v, f) in scored {
        let row = &rows[&id];
        let count = per_file.entry(row.file_id).or_default();
        if *count >= PER_FILE {
            continue;
        }
        *count += 1;
        let why = [v.map(|r| format!("vector#{r}")), f.map(|r| format!("từ khóa#{r}"))].into_iter().flatten().collect::<Vec<_>>().join(" + ");
        out.push(Hit {
            score: (s * 1000.0 * 10.0).round() / 10.0,
            file_id: row.file_id,
            name: row.name.clone(),
            path: row.path.clone(),
            loc: row.loc.clone(),
            text: row.text.clone(),
            why,
            author: row.author.clone(),
            modified_at: row.modified_at.clone(),
        });
        if out.len() >= limit {
            break;
        }
    }
    out
}

/// (group label, colour, extensions): the same grouping Company Brain's own progress report uses.
const TYPE_GROUPS: &[(&str, &str, &[&str])] = &[
    ("Word", "#2b6cb0", &[".docx", ".doc"]),
    ("Excel / CSV", "#2f855a", &[".xlsx", ".xls", ".csv"]),
    ("PDF", "#c53030", &[".pdf"]),
    ("PowerPoint", "#dd6b20", &[".pptx", ".ppt"]),
    ("Markdown", "#4a5568", &[".md"]),
    ("Text", "#718096", &[".txt"]),
    ("Python", "#d69e2e", &[".py", ".ipynb"]),
    ("JavaScript / TS", "#ecc94b", &[".js", ".ts", ".tsx", ".jsx"]),
    ("Web (HTML/CSS/PHP)", "#e53e9e", &[".html", ".htm", ".css", ".php"]),
    ("Code khác", "#805ad5", &[".java", ".cs", ".cpp", ".c", ".h", ".go", ".rs", ".sql", ".sh", ".ps1", ".bat"]),
    ("Cấu hình", "#a0aec0", &[".yaml", ".yml", ".xml", ".ini", ".toml", ".json"]),
    ("Ảnh", "#38b2ac", &[".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"]),
    ("Audio", "#9f7aea", &[".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".wma", ".opus"]),
    ("Video", "#f56565", &[".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".m4v"]),
];

fn group_of(ext: &str) -> usize {
    let e = ext.to_lowercase();
    TYPE_GROUPS.iter().position(|(_, _, exts)| exts.contains(&e.as_str())).unwrap_or(TYPE_GROUPS.len())
}

impl Knowledge {
    /// How much of the machine's data the assistant has learned, and what that data is made of, as ONE tree that
    /// adds up to 100 % of the active files in the catalog (by count and by bytes):
    ///
    /// machine data = learned (content read: by file type) + waiting to be learned + unreadable
    ///                + catalogued by name only (libraries and installed software, tier 3).
    pub fn learning_progress(&self) -> anyhow::Result<Value> {
        let conn = open_db(&self.db)?;
        // tier 1-2: documents, media, code of the person; tier 3: catalogued by name only
        let mut stmt = conn.prepare(
            "SELECT COALESCE(tier, 3), LOWER(COALESCE(ext, '')), COUNT(*), COALESCE(SUM(size), 0),                     COALESCE(SUM(CASE WHEN indexed_sha256 IS NOT NULL AND indexed_sha256 = sha256 THEN 1 ELSE 0 END), 0),                     COALESCE(SUM(CASE WHEN indexed_sha256 IS NOT NULL AND indexed_sha256 = sha256 THEN size ELSE 0 END), 0),                     COALESCE(SUM(CASE WHEN extract_error IS NOT NULL THEN 1 ELSE 0 END), 0),                     COALESCE(SUM(CASE WHEN extract_error IS NOT NULL THEN size ELSE 0 END), 0)              FROM files WHERE status = 'active' GROUP BY 1, 2",
        )?;
        struct Acc {
            files: i64,
            bytes: i64,
            learned: i64,
            learned_bytes: i64,
            failed: i64,
            failed_bytes: i64,
            exts: HashMap<String, (i64, i64)>, // ext -> (learned files, learned bytes)
        }
        let blank = || Acc { files: 0, bytes: 0, learned: 0, learned_bytes: 0, failed: 0, failed_bytes: 0, exts: HashMap::new() };
        let mut groups: Vec<Acc> = (0..=TYPE_GROUPS.len()).map(|_| blank()).collect();
        let (mut catalog_files, mut catalog_bytes) = (0_i64, 0_i64);
        let rows = stmt.query_map([], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, i64>(2)?, r.get::<_, i64>(3)?, r.get::<_, i64>(4)?,
                r.get::<_, i64>(5)?, r.get::<_, i64>(6)?, r.get::<_, i64>(7)?))
        })?;
        for row in rows {
            let (tier, ext, n, bytes, learned, learned_bytes, failed, failed_bytes) = row?;
            if tier >= 3 {
                catalog_files += n;
                catalog_bytes += bytes;
                continue;
            }
            let g = &mut groups[group_of(&ext)];
            g.files += n;
            g.bytes += bytes;
            g.learned += learned;
            g.learned_bytes += learned_bytes;
            g.failed += failed;
            g.failed_bytes += failed_bytes;
            let e = g.exts.entry(if ext.is_empty() { "(không đuôi)".into() } else { ext }).or_insert((0, 0));
            e.0 += learned;
            e.1 += learned_bytes;
        }
        let learnable_files: i64 = groups.iter().map(|g| g.files).sum();
        let learnable_bytes: i64 = groups.iter().map(|g| g.bytes).sum();
        let learned_files: i64 = groups.iter().map(|g| g.learned).sum();
        let learned_bytes: i64 = groups.iter().map(|g| g.learned_bytes).sum();
        let failed_files: i64 = groups.iter().map(|g| g.failed).sum();
        let failed_bytes: i64 = groups.iter().map(|g| g.failed_bytes).sum();
        let waiting_files = (learnable_files - learned_files - failed_files).max(0);
        let waiting_bytes = (learnable_bytes - learned_bytes - failed_bytes).max(0);
        let total_files = learnable_files + catalog_files;
        let total_bytes = learnable_bytes + catalog_bytes;
        let pct = |part: i64, whole: i64| if whole > 0 { (1000.0 * part as f64 / whole as f64).round() / 10.0 } else { 0.0 };

        let mut children: Vec<Value> = Vec::new();
        let mut type_nodes: Vec<Value> = Vec::new();
        for (i, g) in groups.iter().enumerate() {
            if g.learned == 0 && g.files == 0 {
                continue;
            }
            let (label, color) = TYPE_GROUPS.get(i).map(|t| (t.0, t.1)).unwrap_or(("Loại khác", "#94a3b8"));
            let mut exts: Vec<(&String, &(i64, i64))> = g.exts.iter().filter(|(_, v)| v.0 > 0).collect();
            exts.sort_by_key(|(_, v)| std::cmp::Reverse(v.0));
            let ext_nodes: Vec<Value> = exts.iter().take(8).map(|(e, v)| {
                json!({"label": e, "files": v.0, "bytes": v.1, "percent_files": pct(v.0, total_files),
                       "percent_bytes": pct(v.1, total_bytes), "color": color})
            }).collect();
            type_nodes.push(json!({
                "label": label, "color": color, "files": g.learned, "bytes": g.learned_bytes,
                "percent_files": pct(g.learned, total_files), "percent_bytes": pct(g.learned_bytes, total_bytes),
                "of_type_total": g.files, "type_learned_percent": pct(g.learned, (g.files - g.failed).max(1)),
                "children": ext_nodes,
            }));
        }
        type_nodes.sort_by_key(|n| std::cmp::Reverse(n["files"].as_i64().unwrap_or(0)));
        children.push(json!({"label": "Đã học (đã đọc nội dung)", "color": "#16a34a", "files": learned_files, "bytes": learned_bytes,
                             "percent_files": pct(learned_files, total_files), "percent_bytes": pct(learned_bytes, total_bytes),
                             "children": type_nodes}));
        children.push(json!({"label": "Đang chờ học", "color": "#f59e0b", "files": waiting_files, "bytes": waiting_bytes,
                             "percent_files": pct(waiting_files, total_files), "percent_bytes": pct(waiting_bytes, total_bytes), "children": []}));
        children.push(json!({"label": "Không đọc được (hỏng / mã hóa)", "color": "#dc2626", "files": failed_files, "bytes": failed_bytes,
                             "percent_files": pct(failed_files, total_files), "percent_bytes": pct(failed_bytes, total_bytes), "children": []}));
        children.push(json!({"label": "Chỉ lập danh mục theo tên (thư viện, phần mềm cài sẵn)", "color": "#94a3b8", "files": catalog_files,
                             "bytes": catalog_bytes, "percent_files": pct(catalog_files, total_files),
                             "percent_bytes": pct(catalog_bytes, total_bytes), "children": []}));
        let pending: i64 = conn.query_row("SELECT COUNT(*) FROM ingest_queue", [], |r| r.get(0)).unwrap_or(0);
        Ok(json!({
            "total_files": total_files, "total_bytes": total_bytes,
            "learnable_files": learnable_files, "learnable_bytes": learnable_bytes,
            "learned_files": learned_files, "learned_bytes": learned_bytes,
            "learned_percent_of_learnable": pct(learned_files, (learnable_files - failed_files).max(1)),
            "learned_percent_of_machine": pct(learned_files, total_files),
            "learned_percent_bytes_of_machine": pct(learned_bytes, total_bytes),
            "queue": pending, "update": self.update_state(),
            "tree": {"label": "Dữ liệu trên máy", "color": "#334155", "files": total_files, "bytes": total_bytes,
                     "percent_files": 100.0, "percent_bytes": 100.0, "children": children},
        }))
    }

    pub fn new(config: &Config) -> Self {
        let cfg = config.knowledge.clone();
        let data = config.path(&cfg.data_dir);
        let client = reqwest::Client::builder()
            .no_proxy()
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_millis(cfg.request_timeout_ms))
            .build()
            .expect("HTTP client with static settings");
        Self { db: data.join("catalog.db"), qdrant_exe: data.join("qdrant-server").join("qdrant.exe"), root: config.root.clone(), cfg, client }
    }

    /// The Python interpreter Company Brain runs with (also used by python.exec).
    pub fn python(&self) -> &str {
        &self.cfg.python
    }

    pub fn limit(&self) -> usize {
        self.cfg.search_limit
    }

    async fn qdrant_alive(&self) -> bool {
        matches!(self.client.get(format!("{}/healthz", self.cfg.qdrant_url)).timeout(Duration::from_secs(2)).send().await,
                 Ok(r) if r.status().is_success())
    }

    /// Start Company Brain's Qdrant server when it is not running (it keeps running for later searches).
    async fn ensure_qdrant(&self) -> bool {
        if self.qdrant_alive().await {
            return true;
        }
        let Some(dir) = self.qdrant_exe.parent().filter(|_| self.qdrant_exe.is_file()) else { return false };
        let storage = dir.join("storage").to_string_lossy().into_owned();
        let snapshots = dir.join("snapshots").to_string_lossy().into_owned();
        let env = [
            ("QDRANT__SERVICE__HOST", "127.0.0.1"),
            ("QDRANT__TELEMETRY_DISABLED", "true"),
            ("QDRANT__STORAGE__STORAGE_PATH", storage.as_str()),
            ("QDRANT__STORAGE__SNAPSHOTS_PATH", snapshots.as_str()),
        ];
        if crate::process::spawn_detached(&self.qdrant_exe, &[], dir, &env).is_err() {
            return false;
        }
        let deadline = Instant::now() + QDRANT_STARTUP;
        while Instant::now() < deadline {
            tokio::time::sleep(Duration::from_millis(500)).await;
            if self.qdrant_alive().await {
                return true;
            }
        }
        false
    }

    async fn embed_query(&self, query: &str) -> anyhow::Result<Vec<f64>> {
        // On the CPU (num_gpu 0, under a second for one query): the 2 GB embedding model on the GPU next to
        // the 13 GB planner model pushed the planner into system memory (6 tokens/s instead of 27).
        let body = json!({"model": self.cfg.embed_model, "input": [format!("{QUERY_INSTRUCTION}{query}")],
                          "keep_alive": "30s", "truncate": true, "options": {"num_ctx": 2048, "num_gpu": 0}});
        let reply: Value = self.client.post(format!("{}/api/embed", self.cfg.ollama_url)).json(&body).send().await?.error_for_status()?.json().await?;
        let vector: Vec<f64> = reply
            .pointer("/embeddings/0")
            .and_then(Value::as_array)
            .map(|v| v.iter().filter_map(Value::as_f64).collect())
            .unwrap_or_default();
        anyhow::ensure!(!vector.is_empty(), "the embedding model returned no vector");
        Ok(vector)
    }

    async fn vector_ranks(&self, query: &str) -> anyhow::Result<Vec<i64>> {
        anyhow::ensure!(self.ensure_qdrant().await, "Qdrant is not running and could not be started");
        let vector = self.embed_query(query).await?;
        let body = json!({"query": vector, "limit": BRANCH_LIMIT, "with_payload": false});
        let reply: Value = self
            .client
            .post(format!("{}/collections/{}/points/query", self.cfg.qdrant_url, self.cfg.collection))
            .json(&body)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        Ok(reply
            .pointer("/result/points")
            .and_then(Value::as_array)
            .map(|points| points.iter().filter_map(|p| p.get("id").and_then(Value::as_i64)).collect())
            .unwrap_or_default())
    }

    /// Hybrid search. Returns the hits and notes about a branch that could not run.
    pub async fn search(&self, query: &str, limit: usize) -> anyhow::Result<(Vec<Hit>, Vec<String>)> {
        anyhow::ensure!(self.db.is_file(), "company knowledge index not found at {}", self.db.display());
        let mut notes = Vec::new();
        let vector = match self.vector_ranks(query).await {
            Ok(ids) => ids,
            Err(e) => {
                notes.push(format!("vector search unavailable ({e}); full-text results only"));
                Vec::new()
            }
        };
        let (db, query_owned, vector_ids) = (self.db.clone(), query.to_string(), vector.clone());
        let (fts, rows) = tokio::task::spawn_blocking(move || -> rusqlite::Result<(Vec<i64>, HashMap<i64, Row>)> {
            let conn = open_db(&db)?;
            let fts = fts_ranks(&conn, &query_owned).unwrap_or_default();
            let ids: Vec<i64> = vector_ids.iter().chain(fts.iter()).copied().collect::<HashSet<_>>().into_iter().collect();
            Ok((fts, chunk_rows(&conn, &ids)?))
        })
        .await??;
        let mut hits = fuse(query, &vector, &fts, &rows, limit);
        if !self.cfg.rerank_model.trim().is_empty() && hits.len() > 1 {
            match self.rerank(query, &mut hits).await {
                Ok(()) => {}
                Err(e) => notes.push(format!("rerank unavailable ({e}); fusion order kept")),
            }
        }
        Ok((hits, notes))
    }

    /// Re-score the best hits with a small Ollama model (0-10 relevance) and reorder them; the fusion order breaks
    /// ties. Any failure leaves the order untouched.
    async fn rerank(&self, query: &str, hits: &mut Vec<Hit>) -> anyhow::Result<()> {
        let top = self.cfg.rerank_top.clamp(2, 20).min(hits.len());
        let url = format!("{}/api/chat", self.cfg.ollama_url);
        let calls = hits.iter().take(top).map(|hit| {
            let passage: String = hit.text.chars().take(700).collect();
            let body = json!({
                "model": self.cfg.rerank_model, "stream": false, "think": false, "keep_alive": "5m",
                "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 4},
                "messages": [{"role": "user", "content": format!(
                    "Query: {query}

Passage:
{passage}

How well does the passage answer the query? Reply with one integer from 0 (irrelevant) to 10 (answers it fully), nothing else.")}],
            });
            let client = self.client.clone();
            let url = url.clone();
            async move {
                let reply: Value = client.post(url).json(&body).send().await.ok()?.json().await.ok()?;
                parse_rating(reply.pointer("/message/content")?.as_str()?)
            }
        });
        let ratings = tokio::time::timeout(Duration::from_secs(25), futures::future::join_all(calls)).await?;
        if ratings.iter().all(Option::is_none) {
            anyhow::bail!("the rerank model gave no usable scores");
        }
        reorder_by_ratings(hits, &ratings);
        Ok(())
    }

    /// Text of ANY file on the PC (not only indexed ones), with Company Brain's own extractors run one-shot:
    /// Word, Excel, PowerPoint, PDF, Markdown/CSV/JSON/text, and images (described), audio and video
    /// (transcribed) through the local models. Returns the sections as (location, text).
    pub async fn extract_file(&self, path: &Path, timeout: Duration) -> anyhow::Result<Vec<(String, String)>> {
        const SCRIPT: &str = "import sys, json\nfrom pathlib import Path\ntry:\n    from brain.extract import extract_segments\n    segs, _ = extract_segments(Path(sys.argv[1]))\n    out = {'segments': [[loc, text] for loc, text in segs]}\nexcept Exception as e:\n    out = {'error': f'{type(e).__name__}: {e}'}\nsys.stdout.write('\\n' + json.dumps(out, ensure_ascii=False) + '\\n')\n";
        let source = self.root.join(&self.cfg.source_dir);
        anyhow::ensure!(source.join("brain").join("extract.py").is_file(), "Company Brain not found at {}", source.display());
        let mut command = tokio::process::Command::new(&self.cfg.python);
        command
            .args(["-c", SCRIPT])
            .arg(path.as_os_str())
            .current_dir(&source)
            .env("PYTHONIOENCODING", "utf-8")
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .kill_on_drop(true)
            .creation_flags(0x0800_0000); // CREATE_NO_WINDOW
        let output = tokio::time::timeout(timeout, command.output())
            .await
            .map_err(|_| anyhow::anyhow!("reading the file took longer than {} s", timeout.as_secs()))??;
        let stdout = String::from_utf8_lossy(&output.stdout);
        let line = stdout.lines().rev().find(|l| l.starts_with('{')).ok_or_else(|| {
            let stderr = String::from_utf8_lossy(&output.stderr);
            anyhow::anyhow!("the document reader failed: {}", stderr.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or("no output"))
        })?;
        let parsed: Value = serde_json::from_str(line)?;
        if let Some(error) = parsed["error"].as_str() {
            anyhow::bail!("{error}");
        }
        Ok(parsed["segments"]
            .as_array()
            .map(|segs| {
                segs.iter()
                    .filter_map(|s| Some((s[0].as_str()?.to_string(), s[1].as_str()?.to_string())))
                    .filter(|(_, text)| !text.trim().is_empty())
                    .collect()
            })
            .unwrap_or_default())
    }

    /// Text of a knowledge document: its indexed passages in order (works for Word, Excel, PDF, images...),
    /// or the file itself for plain-text formats that were not indexed. Accepts a numeric id or a file name.
    pub fn read_document(&self, id_or_name: &str, max_chars: usize) -> anyhow::Result<(i64, String, String, bool)> {
        let conn = open_db(&self.db)?;
        let key = id_or_name.trim();
        let file_id: Option<i64> = match key.parse::<i64>() {
            Ok(id) => Some(id),
            Err(_) => conn
                .query_row(
                    "SELECT file_id FROM files WHERE status = 'active' AND (lower(name) = lower(?1) OR lower(path) = lower(?1)) \
                     ORDER BY fs_modified_at DESC LIMIT 1",
                    [key],
                    |r| r.get(0),
                )
                .optional()?,
        };
        let Some(file_id) = file_id else { anyhow::bail!("no company document named {key:?}; use a file_id from knowledge.search") };
        let (name, path, ext): (String, String, String) = conn
            .query_row("SELECT name, path, COALESCE(ext, '') FROM files WHERE file_id = ?1 AND status = 'active'", [file_id], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?))
            })
            .optional()?
            .ok_or_else(|| anyhow::anyhow!("no company document with id {file_id}"))?;
        let mut stmt = conn.prepare("SELECT COALESCE(loc, ''), text FROM chunks WHERE file_id = ?1 ORDER BY idx")?;
        let passages: Vec<(String, String)> = stmt.query_map([file_id], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<rusqlite::Result<_>>()?;
        let mut text = String::new();
        if passages.is_empty() {
            let readable = TEXT_EXTENSIONS.contains(&ext.to_lowercase().as_str());
            anyhow::ensure!(readable, "{name} has not been indexed yet (its text is not in the knowledge base)");
            text = String::from_utf8_lossy(&std::fs::read(&path)?).into_owned();
        } else {
            let mut last_loc = String::new();
            for (loc, passage) in passages {
                if !loc.is_empty() && loc != last_loc {
                    text.push_str(&format!("\n[{loc}]\n"));
                    last_loc = loc;
                }
                text.push_str(&passage);
                text.push('\n');
            }
        }
        let truncated = text.chars().count() > max_chars;
        if truncated {
            text = text.chars().take(max_chars).collect();
        }
        Ok((file_id, name, text, truncated))
    }

    /// Whether the index is present, which search branches work right now, and the learning job's state.
    pub async fn status(&self) -> Value {
        let index = self.db.is_file();
        let vectors = self.qdrant_alive().await;
        json!({"ready": index, "base_url": self.db.display().to_string(), "index": index, "vectors": vectors,
               "source_dir": self.root.join(&self.cfg.source_dir).display().to_string(), "update": self.update_state()})
    }

    /// One read-only SELECT over permission-filtered views of the catalog (Company Brain's `sqlbox`): an
    /// authorizer allows only SELECT, reads through the views and the `norm()` function, so neither writes nor
    /// direct reads of the underlying tables are possible.
    pub fn query(&self, sql: &str, max_rows: usize) -> anyhow::Result<(Vec<Value>, bool)> {
        use rusqlite::functions::FunctionFlags;
        use rusqlite::hooks::{AuthAction, AuthContext, Authorization};
        use rusqlite::types::ValueRef;

        let conn = open_db(&self.db)?;
        conn.execute_batch(CATALOG_VIEWS)?;
        conn.create_scalar_function("norm", 1, FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC, |ctx| {
            Ok(ctx.get::<Option<String>>(0)?.map(|s| brain_core::text::norm(&s)))
        })?;
        conn.authorizer(Some(|ctx: AuthContext<'_>| match ctx.action {
            AuthAction::Select | AuthAction::Function { .. } => Authorization::Allow,
            // Base tables of the main database may only be read through one of the views.
            // (A read with no column name reveals no data: SQLite reports it for tables a flattened view
            // touches. Company Brain's Python sandbox denied it too, which broke every visible_facts query.)
            AuthAction::Read { column_name, .. }
                if ctx.database_name == Some("main") && ctx.accessor.is_none() && !column_name.is_empty() =>
            {
                Authorization::Deny
            }
            AuthAction::Read { .. } => Authorization::Allow,
            _ => Authorization::Deny,
        }));
        let mut stmt = conn.prepare(sql.trim().trim_end_matches(';')).map_err(|e| {
            anyhow::anyhow!("{e}. Only one SELECT over the visible_* views is allowed (see the tool description).")
        })?;
        let columns: Vec<String> = stmt.column_names().iter().map(|c| c.to_string()).collect();
        let mut rows = stmt.query([])?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            if out.len() == max_rows {
                return Ok((out, true));
            }
            let mut object = serde_json::Map::new();
            for (i, name) in columns.iter().enumerate() {
                let value = match row.get_ref(i)? {
                    ValueRef::Null => Value::Null,
                    ValueRef::Integer(n) => json!(n),
                    ValueRef::Real(f) => json!(f),
                    ValueRef::Text(t) => json!(String::from_utf8_lossy(t)),
                    ValueRef::Blob(b) => json!(format!("<{} bytes>", b.len())),
                };
                object.insert(name.clone(), value);
            }
            out.push(Value::Object(object));
        }
        Ok((out, false))
    }

    fn update_state_path(&self) -> PathBuf {
        self.root.join("logs").join("knowledge").join("update.json")
    }

    /// The learning job's last state: {"running", "started_at", "log", "paths"} (Null when it never ran).
    pub fn update_state(&self) -> Value {
        let Ok(text) = std::fs::read_to_string(self.update_state_path()) else { return Value::Null };
        let mut state: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
        let running = state["pid"].as_u64().is_some_and(|pid| process_alive(pid as u32));
        if let Some(object) = state.as_object_mut() {
            object.insert("running".into(), json!(running));
        }
        state
    }

    /// Hours since the last learning run started (None when it never ran).
    pub fn hours_since_update(&self) -> Option<f64> {
        let started = self.update_state()["started_at"].as_str().map(String::from)?;
        let at = chrono::DateTime::parse_from_rfc3339(&started).ok()?;
        Some((chrono::Utc::now() - at.with_timezone(&chrono::Utc)).num_minutes() as f64 / 60.0)
    }

    /// Learn new and changed documents: Company Brain's own scanner and indexer (`python -m brain scan` then
    /// `index`) as ONE detached background job that ends by itself. Nothing stays resident.
    /// `force`: the user asked for it now, so it does not wait for the machine to be idle (Company Brain's
    /// governor otherwise lets background learning yield to the user).
    pub fn start_update(&self, paths: &[String], force: bool) -> anyhow::Result<Value> {
        let state = self.update_state();
        if state["running"] == true {
            return Ok(json!({"started": false, "already_running": true, "log": state["log"]}));
        }
        let source = self.root.join(&self.cfg.source_dir);
        anyhow::ensure!(source.join("brain").join("__main__.py").is_file(), "Company Brain not found at {}", source.display());
        let shell = super::shell::resolve_powershell("pwsh").ok_or_else(|| anyhow::anyhow!("PowerShell not found"))?;
        let dir = self.root.join("logs").join("knowledge");
        std::fs::create_dir_all(&dir)?;
        let log = dir.join(format!("update-{}.log", chrono::Local::now().format("%Y%m%d-%H%M%S")));
        // PowerShell single-quoted literal; verbatim (\\?\) prefixes from canonicalized paths are dropped,
        // since PowerShell's file cmdlets do not accept them.
        let quote = |s: &str| format!("'{}'", s.trim_start_matches(r"\\?\").replace('\'', "''"));
        let scan_args = if paths.is_empty() { "--all".to_string() } else { paths.iter().map(|p| quote(p)).collect::<Vec<_>>().join(" ") };
        let python = quote(&self.cfg.python);
        let script = format!(
            "$ErrorActionPreference = 'Continue'; $env:PYTHONIOENCODING = 'utf-8'; [Console]::OutputEncoding = [Text.Encoding]::UTF8; Set-Location -LiteralPath {src}; \
             {force_env}(Get-Process -Id $PID).PriorityClass = 'BelowNormal'; \
             \"started $(Get-Date -Format o)\" | Out-File -Append -Encoding utf8 {log}; \
             & {python} -m brain scan {scan_args} *>&1 | Out-File -Append -Encoding utf8 {log}; \
             & {python} -m brain index *>&1 | Out-File -Append -Encoding utf8 {log}; \
             \"finished $(Get-Date -Format o)\" | Out-File -Append -Encoding utf8 {log}",
            src = quote(&source.to_string_lossy()),
            force_env = if force { "$env:BRAIN_FORCE = '1'; " } else { "" },
            log = quote(&log.to_string_lossy()),
        );
        let encoded = {
            use base64::Engine;
            let utf16: Vec<u8> = script.encode_utf16().flat_map(u16::to_le_bytes).collect();
            base64::engine::general_purpose::STANDARD.encode(utf16)
        };
        let pid = crate::process::spawn_hidden_console(&shell, &["-NoProfile", "-NonInteractive", "-EncodedCommand", &encoded], &source, &[])?;
        let state = json!({"pid": pid, "started_at": chrono::Utc::now().to_rfc3339(), "log": log.display().to_string(),
                           "paths": paths});
        std::fs::write(self.update_state_path(), serde_json::to_vec_pretty(&state)?)?;
        Ok(json!({"started": true, "already_running": false, "log": log.display().to_string()}))
    }
}

/// Permission-filtered views of the catalog for the local admin (Company Brain's `sqlbox._views`, admin case).
const CATALOG_VIEWS: &str = "
CREATE TEMP VIEW visible_files AS SELECT file_id, path, name, ext, size, sha256, fs_created_at, fs_modified_at, fs_owner,
  doc_author, doc_last_editor, doc_created_at, title, department, project, status, first_seen_at, last_seen_at, tier,
  version_no, name_key FROM main.files;
CREATE TEMP VIEW visible_events AS SELECT e.* FROM main.file_events e JOIN visible_files f ON f.file_id = e.file_id;
CREATE TEMP VIEW visible_versions AS SELECT v.file_id, f.name AS file_name, v.version_no, v.sha256, v.size, v.fs_modified_at,
  v.editor, v.observed_at, v.diff_added, v.diff_removed, v.diff_summary
  FROM main.file_versions v JOIN visible_files f ON f.file_id = v.file_id;
CREATE TEMP VIEW visible_contributions AS SELECT p.name AS person, c.kind, c.weight, c.file_id, f.name AS file_name, c.project,
  c.department, c.at, c.evidence FROM main.contributions c JOIN main.people p ON p.person_id = c.person_id
  LEFT JOIN visible_files f ON f.file_id = c.file_id;
CREATE TEMP VIEW visible_relations AS SELECT a.name AS from_name, a.type AS from_type, r.rel AS relation, b.name AS to_name,
  b.type AS to_type, r.source_file_id, f.name AS source_file, r.evidence, r.valid_from, r.valid_to, r.confidence
  FROM main.relations r JOIN main.entities a ON a.entity_id = r.src JOIN main.entities b ON b.entity_id = r.dst
  LEFT JOIN visible_files f ON f.file_id = r.source_file_id;
CREATE TEMP VIEW visible_facts AS SELECT x.fact_id, x.subject, x.predicate, x.value, x.unit, x.subject_key, x.predicate_key,
  x.evidence, x.source_file_id, f.name AS source_name, f.path AS source_path, f.file_id, f.name, f.path, x.author,
  x.valid_from, x.valid_to, x.confidence, x.status, x.note, x.reviewed_by
  FROM main.facts x JOIN visible_files f ON f.file_id = x.source_file_id WHERE x.status != 'rejected';
CREATE TEMP VIEW visible_gaps AS SELECT kind, subject, detail, evidence, status, created_at FROM main.knowledge_gaps;
";

fn process_alive(pid: u32) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
    use windows_sys::Win32::System::Threading::{GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION};
    // SAFETY: the handle is checked and closed; the exit code is written into a local.
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return false;
        }
        let mut code = 0u32;
        let alive = GetExitCodeProcess(handle, &mut code) != 0 && code == STILL_ACTIVE as u32;
        CloseHandle(handle);
        alive
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fts_query_drops_stop_words_and_quotes_tokens() {
        assert_eq!(fts_query("tỷ lệ rắn của sơn OneCoat Pro là bao nhiêu?").as_deref(), Some(r#""tỷ" OR "lệ" OR "rắn" OR "sơn" OR "OneCoat" OR "Pro""#));
        assert_eq!(fts_query("là của ?"), None);
    }

    #[test]
    fn rerank_ratings_parse_and_reorder_stably() {
        assert_eq!(parse_rating(" 8
"), Some(8));
        assert_eq!(parse_rating("Score: 10."), Some(10));
        assert_eq!(parse_rating("no idea"), None);
        assert_eq!(parse_rating("42"), None);
        let mk = |n: &str| Hit { score: 0.0, file_id: 0, name: n.into(), path: String::new(), loc: String::new(), text: String::new(), why: String::new(), author: String::new(), modified_at: String::new() };
        let mut hits = vec![mk("a"), mk("b"), mk("c"), mk("d")];
        reorder_by_ratings(&mut hits, &[Some(2), Some(9), None]);
        let order: Vec<&str> = hits.iter().map(|h| h.name.as_str()).collect();
        assert_eq!(order, ["b", "a", "c", "d"]);
        assert!(hits[0].why.contains("rerank 9/10"));
    }

    #[test]
    fn fusion_rewards_agreement_and_limits_passages_per_file() {
        let row = |file_id: i64, name: &str| Row {
            file_id,
            name: name.into(),
            path: format!("C:/d/{name}"),
            loc: String::new(),
            text: "t".into(),
            author: String::new(),
            modified_at: String::new(),
        };
        let rows: HashMap<i64, Row> = [(1, row(10, "a.docx")), (2, row(10, "a.docx")), (3, row(10, "a.docx")), (4, row(20, "OneCoat.pdf"))].into();
        let hits = fuse("OneCoat solids", &[1, 2, 3, 4], &[4], &rows, 10);
        assert_eq!(hits[0].file_id, 20, "both branches + name match ranks first");
        assert_eq!(hits[0].why, "vector#4 + từ khóa#1");
        assert_eq!(hits.iter().filter(|h| h.file_id == 10).count(), PER_FILE);
    }
}
