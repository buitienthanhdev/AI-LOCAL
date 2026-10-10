//! "The assistant is using this computer" banner: a thin always-on-top strip at the top of the screen while
//! the agent clicks and types in applications, so the person knows to keep their hands off for a moment and
//! how to stop it. The strip never takes focus and lets every click through to the window below it.

use std::sync::mpsc;
use std::thread::JoinHandle;

use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows_sys::Win32::Graphics::Gdi::{
    BeginPaint, CreateSolidBrush, DeleteObject, DrawTextW, EndPaint, FillRect, SetBkMode, SetTextColor, DT_CENTER, DT_SINGLELINE,
    DT_VCENTER, PAINTSTRUCT, TRANSPARENT,
};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::Threading::GetCurrentThreadId;
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DestroyWindow, DispatchMessageW, GetClientRect, GetMessageW, GetSystemMetrics, GetWindowTextW,
    PostQuitMessage, PostThreadMessageW, RegisterClassW, SetLayeredWindowAttributes, TranslateMessage, LWA_ALPHA, MSG, SM_CXSCREEN,
    WM_DESTROY, WM_PAINT, WM_QUIT, WNDCLASSW, WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_TRANSPARENT,
    WS_POPUP, WS_VISIBLE,
};

const HEIGHT: i32 = 30;
const WIDTH_FRACTION: i32 = 2; // half the screen width, centred
const BACKGROUND: u32 = 0x00_7A_3D_00; // BGR: dark blue
const FOREGROUND: u32 = 0x00_FF_FF_FF;

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
                let mut text = [0u16; 512];
                let len = GetWindowTextW(hwnd, text.as_mut_ptr(), text.len() as i32);
                SetBkMode(dc, TRANSPARENT as i32);
                SetTextColor(dc, FOREGROUND);
                DrawTextW(dc, text.as_ptr(), len, &mut rect, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
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
                    let width = screen / WIDTH_FRACTION;
                    let hwnd = CreateWindowExW(
                        WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                        class.as_ptr(),
                        text.as_ptr(),
                        WS_POPUP | WS_VISIBLE,
                        (screen - width) / 2,
                        0,
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
                    SetLayeredWindowAttributes(hwnd, 0, 225, LWA_ALPHA);
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
        let banner = super::Banner::show("Trợ lý đang điều khiển máy tính (test)").expect("banner window");
        std::thread::sleep(std::time::Duration::from_millis(300));
        drop(banner);
    }
}
