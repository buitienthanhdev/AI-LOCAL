//! Local Gemini Agent desktop app: the one chat for the whole agent, as a native window (WebView2).
//!
//! Double-click `agent-desktop.exe`: it starts the Gemini gateway if it is down (company knowledge needs no
//! process: the core searches its index directly) and opens the chat. The page talks to `api.rs` over the private
//! `lga://` scheme; tasks run in the real `agent` CLI next to this executable, so they keep running if the
//! window is closed.

#![windows_subsystem = "windows"]

mod activity;
mod api;
mod extras;
mod openlink;
mod services;
mod vendor;

use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{anyhow, Context, Result};
use lga_core::config::Config;
use tao::dpi::LogicalSize;
use tao::event::{Event, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoop};
use tao::window::{Icon, WindowBuilder};
use windows_sys::Win32::Foundation::{GetLastError, ERROR_ALREADY_EXISTS};
use windows_sys::Win32::System::Threading::CreateMutexW;
use windows_sys::Win32::UI::WindowsAndMessaging::{
    FindWindowW, MessageBoxW, SetForegroundWindow, ShowWindow, MB_ICONERROR, MB_OK, SW_RESTORE,
};
use wry::{NewWindowResponse, WebContext, WebViewBuilder};

const WINDOW_TITLE: &str = "Illuminati";
const APP_ORIGIN: &str = "http://lga.localhost";

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// One window only: a second launch brings the existing window to the front (two WebView2 instances on
/// the same data directory would also lock each other out).
fn already_running() -> bool {
    // SAFETY: valid NUL-terminated UTF-16 strings; the mutex handle is intentionally kept for the
    // process lifetime.
    unsafe {
        CreateMutexW(std::ptr::null(), 0, wide("Local\\LocalGeminiAgentDesktop").as_ptr());
        if GetLastError() != ERROR_ALREADY_EXISTS {
            return false;
        }
        let hwnd = FindWindowW(std::ptr::null(), wide(WINDOW_TITLE).as_ptr());
        if !hwnd.is_null() {
            ShowWindow(hwnd, SW_RESTORE);
            SetForegroundWindow(hwnd);
        }
        true
    }
}

fn fatal(message: &str) -> ! {
    // SAFETY: valid NUL-terminated UTF-16 strings, no owner window.
    unsafe { MessageBoxW(std::ptr::null_mut(), wide(message).as_ptr(), wide(WINDOW_TITLE).as_ptr(), MB_OK | MB_ICONERROR) };
    std::process::exit(1);
}

fn find_agent_exe(config: &Config) -> Result<PathBuf> {
    let beside = std::env::current_exe()?.with_file_name("agent.exe");
    let candidates = [beside, config.root.join("target/release/agent.exe"), config.root.join("target/debug/agent.exe")];
    candidates
        .into_iter()
        .find(|p| p.is_file())
        .ok_or_else(|| anyhow!("Không tìm thấy agent.exe. Hãy chạy `cargo build --release` trong thư mục dự án."))
}

/// The app logo: a 256 x 256 RGBA picture (assets/icon-256.rgba, made from the logo by logs/dev/make_logo.py).
fn app_icon() -> Option<Icon> {
    const N: u32 = 256;
    let rgba = include_bytes!("../assets/icon-256.rgba");
    debug_assert_eq!(rgba.len(), (N * N * 4) as usize);
    Icon::from_rgba(rgba.to_vec(), N, N).ok()
}

fn open_in_browser(url: &str) {
    if url.starts_with("https://") || url.starts_with("http://") {
        let _ = std::process::Command::new("explorer.exe").arg(url).spawn();
    }
}

/// `agent-desktop.exe --task <id>` opens that task directly.
fn start_url() -> String {
    let args: Vec<String> = std::env::args().collect();
    match args.iter().position(|a| a == "--task").and_then(|i| args.get(i + 1)) {
        Some(id) if id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') => format!("lga://localhost/#task={id}"),
        _ => "lga://localhost/".to_string(),
    }
}

fn run() -> Result<()> {
    let config = Config::load().context("Không đọc được cấu hình (configs/config.toml)")?;
    let agent_exe = find_agent_exe(&config)?;
    let api = Arc::new(api::Api { config: config.clone(), agent_exe: agent_exe.clone() });

    // Start the gateway if it is down, without delaying the window.
    std::thread::spawn(move || services::warm_up(&config, &agent_exe));

    let event_loop = EventLoop::new();
    let window = WindowBuilder::new()
        .with_title(WINDOW_TITLE)
        .with_inner_size(LogicalSize::new(1080.0, 760.0))
        .with_min_inner_size(LogicalSize::new(420.0, 520.0))
        .with_window_icon(app_icon())
        .build(&event_loop)?;

    let data_dir = api.config.state_dir().join("webview2");
    let mut context = WebContext::new(Some(data_dir));
    let handler_api = Arc::clone(&api);
    let _webview = WebViewBuilder::new_with_web_context(&mut context)
        .with_asynchronous_custom_protocol("lga".into(), move |_id, request, responder| {
            let api = Arc::clone(&handler_api);
            // File reads and CLI calls must not block the UI thread.
            std::thread::spawn(move || responder.respond(api.handle(request)));
        })
        .with_navigation_handler(|url| {
            if url.starts_with(APP_ORIGIN) || url.starts_with("lga://") {
                return true;
            }
            open_in_browser(&url);
            false
        })
        .with_new_window_req_handler(|url, _features| {
            open_in_browser(&url);
            NewWindowResponse::Deny
        })
        .with_devtools(cfg!(debug_assertions))
        .with_url(start_url())
        .build(&window)?;
    // A launcher may pass SW_MINIMIZE/SW_HIDE in STARTUPINFO; the chat should always open in front.
    window.set_minimized(false);
    window.set_visible(true);
    window.set_focus();

    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        if let Event::WindowEvent { event: WindowEvent::CloseRequested, .. } = event {
            *control_flow = ControlFlow::Exit;
        }
    });
}

fn main() {
    if already_running() {
        return;
    }
    if let Err(e) = run() {
        fatal(&format!("{e:#}"));
    }
}
