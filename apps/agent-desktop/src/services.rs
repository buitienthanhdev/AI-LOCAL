//! Health of the subsystems the agent orchestrates, and starting them when the window opens
//! (what `scripts/start.ps1` used to do): the Gemini gateway and the Company Brain knowledge backend.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::os::windows::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Duration;

use lga_core::config::Config;

use crate::api::{CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW};

const PROBE_TIMEOUT: Duration = Duration::from_millis(1500);

pub struct Health {
    pub gateway: bool,
    pub knowledge: bool,
    pub vision: bool,
}

/// `GET <url>` over plain HTTP on loopback; true when the server answers 2xx.
pub fn http_ok(url: &str) -> bool {
    let Some(rest) = url.strip_prefix("http://") else { return false };
    let (authority, path) = rest.split_once('/').map_or((rest, "/".to_string()), |(a, p)| (a, format!("/{p}")));
    let Some(addr): Option<SocketAddr> = authority.to_socket_addrs().ok().and_then(|mut a| a.next()) else { return false };
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, PROBE_TIMEOUT) else { return false };
    let _ = stream.set_read_timeout(Some(PROBE_TIMEOUT));
    let _ = stream.set_write_timeout(Some(PROBE_TIMEOUT));
    let request = format!("GET {path} HTTP/1.0\r\nHost: {authority}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut head = [0u8; 16];
    let Ok(n) = stream.read(&mut head) else { return false };
    let line = String::from_utf8_lossy(&head[..n]);
    line.starts_with("HTTP/1.") && line.as_bytes().get(9) == Some(&b'2')
}

/// `GET <url>` on loopback with a bearer token; the JSON body when the server answers 2xx.
fn http_get_json(url: &str, bearer: &str, timeout: Duration) -> Option<serde_json::Value> {
    let rest = url.strip_prefix("http://")?;
    let (authority, path) = rest.split_once('/').map_or((rest, "/".to_string()), |(a, p)| (a, format!("/{p}")));
    let addr: SocketAddr = authority.to_socket_addrs().ok()?.next()?;
    let mut stream = TcpStream::connect_timeout(&addr, PROBE_TIMEOUT).ok()?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    let request = format!("GET {path} HTTP/1.0\r\nHost: {authority}\r\nAuthorization: Bearer {bearer}\r\nConnection: close\r\n\r\n");
    stream.write_all(request.as_bytes()).ok()?;
    let mut response = Vec::new();
    stream.read_to_end(&mut response).ok()?;
    let text = String::from_utf8_lossy(&response);
    let (head, body) = text.split_once("\r\n\r\n")?;
    (head.starts_with("HTTP/1.") && head.as_bytes().get(9) == Some(&b'2')).then(|| serde_json::from_str(body).ok())?
}

/// State of each brain behind the gateway: [{"name", "state", "detail"}] (empty when the gateway is down).
pub fn brains(config: &Config) -> serde_json::Value {
    let Some(key) = lga_core::secrets::resolve_secret(&config.root, &config.gateway.api_key_secret) else {
        return serde_json::json!([]);
    };
    let health = http_get_json(&join(&config.llm.gateway_url, "/v1/health"), &key, Duration::from_secs(5));
    let backends = health.as_ref().and_then(|h| h.get("backends")).and_then(|b| b.as_array()).cloned().unwrap_or_default();
    serde_json::Value::Array(
        backends
            .iter()
            .map(|b| serde_json::json!({"name": b["name"], "state": b["state"], "breaker": b["breaker"], "detail": b["detail"]}))
            .collect(),
    )
}

fn join(base: &str, path: &str) -> String {
    format!("{}{}", base.trim_end_matches('/'), path)
}

pub fn gateway_url(config: &Config) -> String {
    join(&config.llm.gateway_url, "/health")
}

pub fn knowledge_url(config: &Config) -> String {
    join(&config.knowledge.base_url, "/api/health")
}

pub fn probe_all(config: &Config) -> Health {
    let (gateway, knowledge, vision) = std::thread::scope(|s| {
        let g = s.spawn(|| http_ok(&gateway_url(config)));
        let k = s.spawn(|| http_ok(&knowledge_url(config)));
        let v = s.spawn(|| http_ok(&join(&config.vision.ui_tars_base_url, "/models")));
        (g.join().unwrap_or(false), k.join().unwrap_or(false), v.join().unwrap_or(false))
    });
    Health { gateway, knowledge, vision }
}

/// Start what is down. Runs on a background thread; failures only show up as red status dots.
pub fn warm_up(config: &Config, agent_exe: &Path) {
    if !http_ok(&gateway_url(config)) {
        // `agent gateway start` launches the gateway detached and waits until it is healthy.
        let _ = Command::new(agent_exe)
            .args(["gateway", "start"])
            .current_dir(&config.root)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW)
            .status();
    }
    if config.knowledge.autostart && !http_ok(&knowledge_url(config)) {
        let source = config.path(&config.knowledge.source_dir);
        if source.join("brain").join("__main__.py").is_file() {
            // Headless serve only (no window); prefer pythonw for the configured "python".
            let python = match config.knowledge.python.as_str() {
                "python" => "pythonw".to_string(),
                other => other.to_string(),
            };
            let _ = Command::new(python)
                .args(["-m", "brain", "serve"])
                .current_dir(source)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
                .spawn();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn probe_reads_status_line() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            for code in ["200 OK", "503 Unavailable"] {
                let (mut s, _) = listener.accept().unwrap();
                let mut buf = [0u8; 256];
                let _ = s.read(&mut buf);
                let _ = s.write_all(format!("HTTP/1.1 {code}\r\n\r\n").as_bytes());
            }
        });
        assert!(http_ok(&format!("http://127.0.0.1:{port}/health")));
        assert!(!http_ok(&format!("http://127.0.0.1:{port}/health")));
        server.join().unwrap();
        assert!(!http_ok("https://example.com/"));
    }
}
