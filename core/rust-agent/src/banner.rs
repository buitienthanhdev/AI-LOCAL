//! "The assistant is using this computer" banner: a small rounded always-on-top card at the top of the screen
//! while the agent clicks and types in applications, so the person knows to keep their hands off for a moment
//! and how to stop it. The first line of the text is the title, the rest is the smaller hint. The card never
//! takes focus and lets every click through to the window below it.

use std::sync::mpsc;
use std::thread::JoinHandle;

use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows_sys::Win32::Graphics::Gdi::{
    BeginPaint, CreateFontW, CreateRoundRectRgn, CreateSolidBrush, DeleteObject, DrawTextW, Ellipse, EndPaint, FillRect, GetStockObject,
    SelectObject, SetBkMode, SetTextColor, SetWindowRgn, DT_END_ELLIPSIS, DT_LEFT, DT_SINGLELINE, DT_VCENTER, NULL_PEN, PAINTSTRUCT,
    TRANSPARENT,
};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::Threading::GetCurrentThreadId;
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DestroyWindow, DispatchMessageW, GetClientRect, GetMessageW, GetSystemMetrics, GetWindowTextW,
    PostQuitMessage, PostThreadMessageW, RegisterClassW, SetLayeredWindowAttributes, TranslateMessage, LWA_ALPHA, MSG, SM_CXSCREEN,
    WM_DESTROY, WM_PAINT, WM_QUIT, WNDCLASSW, WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_TRANSPARENT,
    WS_POPUP, WS_VISIBLE,
};

const HEIGHT: i32 = 56;
const WIDTH: i32 = 580;
const TOP_MARGIN: i32 = 10;
const RADIUS: i32 = 20;
const BACKGROUND: u32 = 0x00_37_29_1F; // BGR: slate #1F2937
const ACCENT: u32 = 0x00_99_D3_34; // BGR: green #34D399, the "working" dot
const TITLE: u32 = 0x00_FF_FF_FF;
const HINT: u32 = 0x00_E1_D5_CB; // BGR: #CBD5E1
const TEXT_LEFT: i32 = 46;

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

unsafe extern "system" fn window_proc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match msg {
        WM_PAINT => {
            // SAFETY: standard WM_PAINT handling on this window's own thread.
            unsafe {
                let mut ps: PAINTSTRUCT = std::mem::zeroed();
                let dc = BeginPaint(hwnd, &mut ps);
                let mut rect: RECT = std::mem::zeroed();
                GetClientRect(hwnd, &mut rect);
                let brush = CreateSolidBrush(BACKGROUND);
                FillRect(dc, &rect, brush);
                DeleteObject(brush);
                let mut buf = [0u16; 512];
                let len = GetWindowTextW(hwnd, buf.as_mut_ptr(), buf.len() as i32).max(0) as usize;
                let text = String::from_utf16_lossy(&buf[..len]);
                let (title, hint) = text.split_once('\n').unwrap_or((text.as_str(), ""));
                SetBkMode(dc, TRANSPARENT as i32);
                // the "working" dot
                let dot = CreateSolidBrush(ACCENT);
                let old_brush = SelectObject(dc, dot);
                let old_pen = SelectObject(dc, GetStockObject(NULL_PEN));
                Ellipse(dc, 18, HEIGHT / 2 - 7, 32, HEIGHT / 2 + 7);
                SelectObject(dc, old_pen);
                SelectObject(dc, old_brush);
                DeleteObject(dot);
                let face = wide("Segoe UI");
                let font = |px: i32, weight: i32| CreateFontW(-px, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, face.as_ptr());
                let draw = |text: &str, font, color: u32, top: i32, bottom: i32| {
                    if text.is_empty() {
                        return;
                    }
                    let old = SelectObject(dc, font);
                    SetTextColor(dc, color);
                    let w = wide(text);
                    let mut r = RECT { left: TEXT_LEFT, top, right: rect.right - 16, bottom };
                    DrawTextW(dc, w.as_ptr(), (w.len() - 1) as i32, &mut r, DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS);
                    SelectObject(dc, old);
                };
                let title_font = font(17, 600);
                let hint_font = font(13, 400);
                if hint.is_empty() {
                    draw(title.trim(), title_font, TITLE, 0, HEIGHT);
                } else {
                    draw(title.trim(), title_font, TITLE, 7, 30);
                    draw(hint.trim(), hint_font, HINT, 30, 50);
                }
                DeleteObject(title_font);
                DeleteObject(hint_font);
                EndPaint(hwnd, &ps);
            }
            0
        }
        WM_DESTROY => {
            // SAFETY: ends this thread's message loop.
            unsafe { PostQuitMessage(0) };
            0
        }
        // SAFETY: default handling for everything else.
        _ => unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) },
    }
}

/// The banner; it disappears when dropped.
pub struct Banner {
    thread_id: u32,
    thread: Option<JoinHandle<()>>,
}

impl Banner {
    pub fn show(text: &str) -> Option<Self> {
        let text = wide(text);
        let (tx, rx) = mpsc::channel();
        let thread = std::thread::Builder::new()
            .name("agent-banner".into())
            .spawn(move || {
                // SAFETY: plain Win32 window creation and message loop, all on this thread.
                unsafe {
                    let class = wide("LgaAgentBanner");
                    let instance = GetModuleHandleW(std::ptr::null());
                    let wc = WNDCLASSW {
                        lpfnWndProc: Some(window_proc),
                        hInstance: instance,
                        lpszClassName: class.as_ptr(),
                        ..std::mem::zeroed()
                    };
                    RegisterClassW(&wc); // fails harmlessly when already registered
                    let screen = GetSystemMetrics(SM_CXSCREEN);
                    let width = WIDTH.min(screen - 40);
                    let hwnd = CreateWindowExW(
                        WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                        class.as_ptr(),
                        text.as_ptr(),
                        WS_POPUP | WS_VISIBLE,
                        (screen - width) / 2,
                        TOP_MARGIN,
                        width,
                        HEIGHT,
                        std::ptr::null_mut(),
                        std::ptr::null_mut(),
                        instance,
                        std::ptr::null(),
                    );
                    if hwnd.is_null() {
                        let _ = tx.send(None);
                        return;
                    }
                    SetWindowRgn(hwnd, CreateRoundRectRgn(0, 0, width + 1, HEIGHT + 1, RADIUS, RADIUS), 1);
                    SetLayeredWindowAttributes(hwnd, 0, 240, LWA_ALPHA);
                    let _ = tx.send(Some(GetCurrentThreadId()));
                    let mut msg: MSG = std::mem::zeroed();
                    while GetMessageW(&mut msg, std::ptr::null_mut(), 0, 0) > 0 {
                        if msg.message == WM_QUIT {
                            break;
                        }
                        TranslateMessage(&msg);
                        DispatchMessageW(&msg);
                    }
                    DestroyWindow(hwnd);
                }
            })
            .ok()?;
        let thread_id = rx.recv().ok().flatten()?;
        Some(Self { thread_id, thread: Some(thread) })
    }
}

impl Drop for Banner {
    fn drop(&mut self) {
        // SAFETY: asks the banner thread to leave its message loop; it destroys its own window.
        unsafe { PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0) };
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn banner_shows_and_goes_away() {
        let banner = super::Banner::show("Trợ lý đang điều khiển máy tính\nTạm thời đừng dùng chuột, bàn phím").expect("banner window");
        std::thread::sleep(std::time::Duration::from_millis(300));
        drop(banner);
    }
}
