//! Web search and page reading, for anything the model cannot know by itself (news, prices, weather,
//! exchange rates, product documentation): plain HTTPS requests, no browser.
//!
//! * search: DuckDuckGo's HTML endpoint, Bing as a fallback when it returns nothing;
//! * fetch: the page reduced to readable text (scripts, navigation, headers and footers dropped; the
//!   `<article>` or `<main>` element preferred when the page has one).

use std::time::Duration;

use scraper::{ElementRef, Html, Node, Selector};
use serde_json::{json, Value};

const USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(20);
/// Bodies larger than this are not read (a page, not a download).
const MAX_BODY_BYTES: usize = 8 * 1024 * 1024;
/// Elements whose text is never page content.
const SKIP: [&str; 14] = ["script", "style", "noscript", "nav", "footer", "header", "form", "svg", "aside", "iframe", "template", "button", "select", "canvas"];
/// Elements that start a new line of text.
const BLOCK: [&str; 24] = [
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6", "pre",
    "blockquote", "dd", "dt", "figcaption", "td", "th",
];

#[derive(Debug, Clone, PartialEq)]
pub struct SearchResult {
    pub title: String,
    pub url: String,
    pub snippet: String,
}

impl SearchResult {
    pub fn to_json(&self) -> Value {
        json!({"title": self.title, "url": self.url, "snippet": self.snippet})
    }
}

pub struct Page {
    pub url: String,
    pub title: String,
    pub text: String,
    pub truncated: bool,
}

pub struct Web {
    client: reqwest::Client,
}

impl Default for Web {
    fn default() -> Self {
        Self::new()
    }
}

impl Web {
    pub fn new() -> Self {
        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert(reqwest::header::ACCEPT_LANGUAGE, reqwest::header::HeaderValue::from_static("vi-VN,vi;q=0.9,en;q=0.8"));
        let client = reqwest::Client::builder()
            .user_agent(USER_AGENT)
            .default_headers(headers)
            .timeout(REQUEST_TIMEOUT)
            .build()
            .unwrap_or_default();
        Self { client }
    }

    pub async fn search(&self, query: &str, limit: usize) -> anyhow::Result<Vec<SearchResult>> {
        let mut results = self.duckduckgo(query).await.unwrap_or_default();
        if results.is_empty() {
            results = self.bing(query).await?;
        }
        results.retain(|r| r.url.starts_with("http"));
        results.truncate(limit);
        Ok(results)
    }

    async fn duckduckgo(&self, query: &str) -> anyhow::Result<Vec<SearchResult>> {
        let response = self.client.post("https://html.duckduckgo.com/html/").form(&[("q", query), ("kl", "vn-vi")]).send().await?;
        Ok(parse_duckduckgo(&response.error_for_status()?.text().await?))
    }

    async fn bing(&self, query: &str) -> anyhow::Result<Vec<SearchResult>> {
        let response = self.client.get("https://www.bing.com/search").query(&[("q", query), ("setlang", "vi")]).send().await?;
        Ok(parse_bing(&response.error_for_status()?.text().await?))
    }

    pub async fn fetch(&self, url: &str, max_chars: usize) -> anyhow::Result<Page> {
        let parsed = reqwest::Url::parse(url)?;
        anyhow::ensure!(matches!(parsed.scheme(), "http" | "https"), "only http and https addresses can be read");
        let response = self.client.get(parsed).send().await?;
        let status = response.status();
        anyhow::ensure!(status.is_success(), "the site answered HTTP {status}");
        let final_url = response.url().to_string();
        let content_type = response.headers().get(reqwest::header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).unwrap_or("").to_ascii_lowercase();
        let is_html = content_type.contains("html") || content_type.is_empty();
        if !is_html && !content_type.starts_with("text/") && !content_type.contains("json") && !content_type.contains("xml") {
            anyhow::bail!("not a text page ({content_type}); download it (shell.exec Invoke-WebRequest -OutFile) and read it with document.read");
        }
        if response.content_length().is_some_and(|n| n as usize > MAX_BODY_BYTES) {
            anyhow::bail!("the page is larger than {} MB", MAX_BODY_BYTES / 1024 / 1024);
        }
        let body = response.bytes().await?;
        let raw = String::from_utf8_lossy(&body[..body.len().min(MAX_BODY_BYTES)]).into_owned();
        let (title, text) = if is_html { page_text(&raw) } else { (String::new(), raw) };
        let truncated = text.chars().count() > max_chars;
        let text = if truncated { text.chars().take(max_chars).collect() } else { text };
        Ok(Page { url: final_url, title, text, truncated })
    }
}

fn selector(css: &str) -> Selector {
    Selector::parse(css).expect("valid selector")
}

fn text_of(element: ElementRef<'_>) -> String {
    element.text().collect::<Vec<_>>().join(" ").split_whitespace().collect::<Vec<_>>().join(" ")
}

/// DuckDuckGo wraps result links as `//duckduckgo.com/l/?uddg=<real url>`.
fn unwrap_duckduckgo(href: &str) -> String {
    let absolute = if href.starts_with("//") { format!("https:{href}") } else { href.to_string() };
    reqwest::Url::parse(&absolute)
        .ok()
        .and_then(|u| u.query_pairs().find(|(k, _)| k == "uddg").map(|(_, v)| v.into_owned()))
        .unwrap_or(absolute)
}

pub fn parse_duckduckgo(html: &str) -> Vec<SearchResult> {
    let doc = Html::parse_document(html);
    let (result, link, snippet) = (selector(".result"), selector("a.result__a"), selector(".result__snippet"));
    doc.select(&result)
        .filter(|r| !r.value().classes().any(|c| c == "result--ad"))
        .filter_map(|r| {
            let a = r.select(&link).next()?;
            Some(SearchResult {
                title: text_of(a),
                url: unwrap_duckduckgo(a.value().attr("href")?),
                snippet: r.select(&snippet).next().map(text_of).unwrap_or_default(),
            })
        })
        .collect()
}

pub fn parse_bing(html: &str) -> Vec<SearchResult> {
    let doc = Html::parse_document(html);
    let (result, link, snippet) = (selector("li.b_algo"), selector("h2 a"), selector("p"));
    doc.select(&result)
        .filter_map(|r| {
            let a = r.select(&link).next()?;
            Some(SearchResult {
                title: text_of(a),
                url: a.value().attr("href")?.to_string(),
                snippet: r.select(&snippet).next().map(text_of).unwrap_or_default(),
            })
        })
        .collect()
}

fn collect_text(element: ElementRef<'_>, out: &mut String) {
    for child in element.children() {
        match child.value() {
            Node::Text(text) => out.push_str(text),
            Node::Element(e) => {
                let name = e.name();
                if SKIP.contains(&name) {
                    continue;
                }
                let block = BLOCK.contains(&name);
                if block {
                    out.push('\n');
                }
                if let Some(el) = ElementRef::wrap(child) {
                    collect_text(el, out);
                }
                if block {
                    out.push('\n');
                }
            }
            _ => {}
        }
    }
}

/// (title, readable text) of an HTML page.
pub fn page_text(html: &str) -> (String, String) {
    let doc = Html::parse_document(html);
    let title = doc.select(&selector("title")).next().map(text_of).unwrap_or_default();
    let root = ["article", "main", "body"].iter().find_map(|tag| doc.select(&selector(tag)).next()).unwrap_or_else(|| doc.root_element());
    let mut raw = String::new();
    collect_text(root, &mut raw);
    let lines: Vec<String> = raw.lines().map(|l| l.split_whitespace().collect::<Vec<_>>().join(" ")).filter(|l| !l.is_empty()).collect();
    (title, lines.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn duckduckgo_results_are_unwrapped_and_ads_skipped() {
        let html = r#"<div class="result result--ad"><a class="result__a" href="https://ads.example/x">Ad</a></div>
            <div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fvnexpress.net%2Fgia-vang&amp;rut=1">Giá <b>vàng</b> hôm nay</a>
            <a class="result__snippet">Giá vàng   SJC tăng</a></div>"#;
        let results = parse_duckduckgo(html);
        assert_eq!(results, vec![SearchResult { title: "Giá vàng hôm nay".into(), url: "https://vnexpress.net/gia-vang".into(), snippet: "Giá vàng SJC tăng".into() }]);
    }

    #[test]
    fn bing_results_are_parsed() {
        let html = r#"<ol><li class="b_algo"><h2><a href="https://example.com/a">Example A</a></h2><div><p>First snippet</p></div></li></ol>"#;
        assert_eq!(parse_bing(html)[0].url, "https://example.com/a");
    }

    #[test]
    fn page_text_keeps_content_and_drops_chrome() {
        let html = r#"<html><head><title>Tin tức</title><style>p{}</style></head><body><nav>Menu Trang chủ</nav>
            <article><h1>Tiêu đề</h1><p>Đoạn   một.</p><script>var x=1;</script><p>Đoạn hai.</p></article><footer>Bản quyền</footer></body></html>"#;
        let (title, text) = page_text(html);
        assert_eq!(title, "Tin tức");
        assert_eq!(text, "Tiêu đề\nĐoạn một.\nĐoạn hai.");
    }
}
