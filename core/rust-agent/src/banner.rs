//! "The assistant is using this computer" banner: a small rounded always-on-top card at the top of the screen
//! while the agent operates applications. Its separate stop button is clickable; the rest of the card lets
//! clicks through and never takes focus. The first line is the title; the rest is the smaller hint.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{mpsc, Mutex, OnceLock};
use std::thread::JoinHandle;

use crate::cancel::CancelToken;
use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows_sys::Win32::Graphics::Gdi::InvalidateRect;
use windows_sys::Win32::Graphics::Gdi::{
    BeginPaint, CreateFontW, CreateRoundRectRgn, CreateSolidBrush, DeleteObject, DrawTextW,
    Ellipse, EndPaint, FillRect, GetStockObject, SelectObject, SetBkMode, SetTextColor,
    SetWindowRgn, DT_CENTER, DT_END_ELLIPSIS, DT_LEFT, DT_SINGLELINE, DT_VCENTER, NULL_PEN,
    PAINTSTRUCT, TRANSPARENT,
};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::Threading::GetCurrentThreadId;
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{ReleaseCapture, SetCapture};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, CreateWindowExW, DefWindowProcW, DestroyWindow, DispatchMessageW,
    GetClientRect, GetMessageW, GetSystemMetrics, GetWindowTextW, PostQuitMessage,
    PostThreadMessageW, RegisterClassW, SetLayeredWindowAttributes, SetWindowTextW,
    SetWindowsHookExW, TranslateMessage, UnhookWindowsHookEx, HC_ACTION, KBDLLHOOKSTRUCT,
    LLKHF_INJECTED, LLMHF_INJECTED, LWA_ALPHA, MSG, MSLLHOOKSTRUCT, SM_CXSCREEN, WH_KEYBOARD_LL,
    WH_MOUSE_LL, WM_DESTROY, WM_KEYDOWN, WM_KEYUP, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MBUTTONDOWN,
    WM_MBUTTONUP, WM_MOUSEHWHEEL, WM_MOUSEWHEEL, WM_PAINT, WM_QUIT, WM_RBUTTONDOWN, WM_RBUTTONUP,
    WM_SYSKEYDOWN, WM_SYSKEYUP, WM_XBUTTONDOWN, WM_XBUTTONUP, WNDCLASSW, WS_EX_LAYERED,
    WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_POPUP, WS_VISIBLE,
};

const HEIGHT: i32 = 56;
const WIDTH: i32 = 620;
const TOP_MARGIN: i32 = 10;
const RADIUS: i32 = 20;
const BACKGROUND: u32 = 0x00_37_29_1F; // BGR: slate #1F2937
const ACCENT: u32 = 0x00_99_D3_34; // BGR: green #34D399, the "working" dot
const TITLE: u32 = 0x00_FF_FF_FF;
const HINT: u32 = 0x00_E1_D5_CB; // BGR: #CBD5E1
const STOP: u32 = 0x00_33_33_DF; // BGR: red
const TEXT_LEFT: i32 = 46;
const STOP_CLASS: &str = "LgaAgentStop";
const STOP_WIDTH: i32 = 88;
const STOP_HEIGHT: i32 = 34;
const STOP_MARGIN: i32 = 10;

static USER_ACTIVITY_GENERATION: AtomicU64 = AtomicU64::new(0);
static ACTIVE_CANCEL: OnceLock<Mutex<Option<CancelToken>>> = OnceLock::new();

fn active_cancel() -> &'static Mutex<Option<CancelToken>> {
    ACTIVE_CANCEL.get_or_init(|| Mutex::new(None))
}

unsafe extern "system" fn keyboard_hook(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code == HC_ACTION as i32
        && matches!(
            wparam as u32,
            WM_KEYDOWN | WM_KEYUP | WM_SYSKEYDOWN | WM_SYSKEYUP
        )
    {
        let event = unsafe { &*(lparam as *const KBDLLHOOKSTRUCT) };
        if event.flags & LLKHF_INJECTED == 0 {
            USER_ACTIVITY_GENERATION.fetch_add(1, Ordering::Relaxed);
        }
    }
    unsafe { CallNextHookEx(std::ptr::null_mut(), code, wparam, lparam) }
}

unsafe extern "system" fn mouse_hook(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code == HC_ACTION as i32
        && matches!(
            wparam as u32,
            WM_LBUTTONDOWN
                | WM_LBUTTONUP
                | WM_RBUTTONDOWN
                | WM_RBUTTONUP
                | WM_MBUTTONDOWN
                | WM_MBUTTONUP
                | WM_MOUSEWHEEL
                | WM_MOUSEHWHEEL
                | WM_XBUTTONDOWN
                | WM_XBUTTONUP
        )
    {
        let event = unsafe { &*(lparam as *const MSLLHOOKSTRUCT) };
        if event.flags & LLMHF_INJECTED == 0 {
            USER_ACTIVITY_GENERATION.fetch_add(1, Ordering::Relaxed);
        }
    }
    unsafe { CallNextHookEx(std::ptr::null_mut(), code, wparam, lparam) }
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

unsafe extern "system" fn window_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    match msg {
        WM_PAINT => {
            // SAFETY: standard WM_PAINT handling on this window's own thread.
            unsafe {
                let mut ps: PAINTSTRUCT = std::mem::zeroed();
                let dc = BeginPaint(hwnd, &mut ps);
                let mut rect: RECT = std::mem::zeroed();
                GetClientRect(hwnd, &mut rect);
                let mut buf = [0u16; 512];
                let len = GetWindowTextW(hwnd, buf.as_mut_ptr(), buf.len() as i32).max(0) as usize;
                let text = String::from_utf16_lossy(&buf[..len]);
                if text == STOP_CLASS {
                    let brush = CreateSolidBrush(STOP);
                    FillRect(dc, &rect, brush);
                    DeleteObject(brush);
                    SetBkMode(dc, TRANSPARENT as i32);
                    SetTextColor(dc, TITLE);
                    let face = wide("Segoe UI");
                    let font =
                        CreateFontW(-15, 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 5, 0, face.as_ptr());
                    let old = SelectObject(dc, font);
                    let label = wide("■  DỪNG");
                    DrawTextW(
                        dc,
                        label.as_ptr(),
                        (label.len() - 1) as i32,
                        &mut rect,
                        DT_CENTER | DT_VCENTER | DT_SINGLELINE,
                    );
                    SelectObject(dc, old);
                    DeleteObject(font);
                    EndPaint(hwnd, &ps);
                    return 0;
                }
                let brush = CreateSolidBrush(BACKGROUND);
                FillRect(dc, &rect, brush);
                DeleteObject(brush);
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
                let font = |px: i32, weight: i32| {
                    CreateFontW(-px, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, face.as_ptr())
                };
                let draw = |text: &str, font, color: u32, top: i32, bottom: i32| {
                    if text.is_empty() {
                        return;
                    }
                    let old = SelectObject(dc, font);
                    SetTextColor(dc, color);
                    let w = wide(text);
                    let mut r = RECT {
                        left: TEXT_LEFT,
                        top,
                        right: rect.right - STOP_WIDTH - 2 * STOP_MARGIN,
                        bottom,
                    };
                    DrawTextW(
                        dc,
                        w.as_ptr(),
                        (w.len() - 1) as i32,
                        &mut r,
                        DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS,
                    );
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
        WM_LBUTTONDOWN => {
            // SAFETY: reads this window's own title to distinguish the stop button.
            let is_stop_button = unsafe {
                let mut buf = [0u16; 32];
                let len = GetWindowTextW(hwnd, buf.as_mut_ptr(), buf.len() as i32).max(0) as usize;
                String::from_utf16_lossy(&buf[..len]) == STOP_CLASS
            };
            if is_stop_button {
                unsafe { SetCapture(hwnd) };
                0
            } else {
                unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) }
            }
        }
        WM_LBUTTONUP => {
            // SAFETY: reads this window's own title to distinguish the stop button.
            let is_stop_button = unsafe {
                let mut buf = [0u16; 32];
                let len = GetWindowTextW(hwnd, buf.as_mut_ptr(), buf.len() as i32).max(0) as usize;
                String::from_utf16_lossy(&buf[..len]) == STOP_CLASS
            };
            if is_stop_button {
                unsafe { ReleaseCapture() };
                if let Ok(active) = active_cancel().lock() {
                    if let Some(cancel) = active.as_ref() {
                        cancel.cancel();
                    }
                }
                0
            } else {
                unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) }
            }
        }
        // SAFETY: default handling for everything else.
        _ => unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) },
    }
}

/// The banner; it disappears when dropped.
pub struct Banner {
    thread_id: u32,
    window_hwnd: isize,
    #[cfg(test)]
    stop_hwnd: isize,
    thread: Option<JoinHandle<()>>,
}

impl Banner {
    pub fn show(text: &str, cancel: CancelToken, monitor_activity: bool) -> Result<Self, String> {
        let text = wide(text);
        USER_ACTIVITY_GENERATION.store(0, Ordering::Relaxed);
        *active_cancel()
            .lock()
            .map_err(|_| "banner stop control is unavailable".to_string())? = Some(cancel);
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
                    let width = WIDTH.min(screen - 40).max(STOP_WIDTH + STOP_MARGIN * 2);
                    let x = (screen - width) / 2;
                    let hwnd = CreateWindowExW(
                        WS_EX_TOPMOST
                            | WS_EX_LAYERED
                            | WS_EX_TRANSPARENT
                            | WS_EX_TOOLWINDOW
                            | WS_EX_NOACTIVATE,
                        class.as_ptr(),
                        text.as_ptr(),
                        WS_POPUP | WS_VISIBLE,
                        x,
                        TOP_MARGIN,
                        width,
                        HEIGHT,
                        std::ptr::null_mut(),
                        std::ptr::null_mut(),
                        instance,
                        std::ptr::null(),
                    );
                    if hwnd.is_null() {
                        let _ =
                            tx.send(Err("could not create the screen-control ribbon".to_string()));
                        return;
                    }
                    SetWindowRgn(
                        hwnd,
                        CreateRoundRectRgn(0, 0, width + 1, HEIGHT + 1, RADIUS, RADIUS),
                        1,
                    );
                    SetLayeredWindowAttributes(hwnd, 0, 240, LWA_ALPHA);
                    let button_class = wide(STOP_CLASS);
                    let button = CreateWindowExW(
                        WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                        class.as_ptr(),
                        button_class.as_ptr(),
                        WS_POPUP | WS_VISIBLE,
                        x + width - STOP_WIDTH - STOP_MARGIN,
                        TOP_MARGIN + (HEIGHT - STOP_HEIGHT) / 2,
                        STOP_WIDTH,
                        STOP_HEIGHT,
                        std::ptr::null_mut(),
                        std::ptr::null_mut(),
                        instance,
                        std::ptr::null(),
                    );
                    if button.is_null() {
                        DestroyWindow(hwnd);
                        let _ = tx.send(Err(
                            "could not create the screen-control stop button".to_string()
                        ));
                        return;
                    }
                    SetWindowRgn(
                        button,
                        CreateRoundRectRgn(0, 0, STOP_WIDTH + 1, STOP_HEIGHT + 1, 12, 12),
                        1,
                    );
                    SetLayeredWindowAttributes(button, 0, 250, LWA_ALPHA);

                    let keyboard = if monitor_activity {
                        SetWindowsHookExW(WH_KEYBOARD_LL, Some(keyboard_hook), instance, 0)
                    } else {
                        std::ptr::null_mut()
                    };
                    let mouse = if monitor_activity {
                        SetWindowsHookExW(WH_MOUSE_LL, Some(mouse_hook), instance, 0)
                    } else {
                        std::ptr::null_mut()
                    };
                    if monitor_activity && (keyboard.is_null() || mouse.is_null()) {
                        if !keyboard.is_null() {
                            UnhookWindowsHookEx(keyboard);
                        }
                        if !mouse.is_null() {
                            UnhookWindowsHookEx(mouse);
                        }
                        DestroyWindow(button);
                        DestroyWindow(hwnd);
                        let _ = tx.send(Err(
                            "could not monitor physical mouse and keyboard activity".to_string(),
                        ));
                        return;
                    }
                    let _ = tx.send(Ok((GetCurrentThreadId(), hwnd as isize, button as isize)));
                    let mut msg: MSG = std::mem::zeroed();
                    while GetMessageW(&mut msg, std::ptr::null_mut(), 0, 0) > 0 {
                        if msg.message == WM_QUIT {
                            break;
                        }
                        TranslateMessage(&msg);
                        DispatchMessageW(&msg);
                    }
                    if !keyboard.is_null() {
                        UnhookWindowsHookEx(keyboard);
                    }
                    if !mouse.is_null() {
                        UnhookWindowsHookEx(mouse);
                    }
                    DestroyWindow(button);
                    DestroyWindow(hwnd);
                }
            })
            .map_err(|e| {
                if let Ok(mut active) = active_cancel().lock() {
                    *active = None;
                }
                format!("could not start the screen-control ribbon: {e}")
            })?;
        let (thread_id, window_hwnd, _stop_hwnd) = match rx.recv() {
            Ok(Ok(info)) => info,
            Ok(Err(error)) => {
                let _ = thread.join();
                if let Ok(mut active) = active_cancel().lock() {
                    *active = None;
                }
                return Err(error);
            }
            Err(error) => {
                let _ = thread.join();
                if let Ok(mut active) = active_cancel().lock() {
                    *active = None;
                }
                return Err(format!(
                    "screen-control ribbon stopped before becoming ready: {error}"
                ));
            }
        };
        Ok(Self {
            thread_id,
            window_hwnd,
            #[cfg(test)]
            stop_hwnd: _stop_hwnd,
            thread: Some(thread),
        })
    }

    pub fn activity_generation(&self) -> u64 {
        USER_ACTIVITY_GENERATION.load(Ordering::Relaxed)
    }

    pub fn set_text(&self, text: &str) -> Result<(), String> {
        let text = wide(text);
        // SAFETY: the HWND belongs to the live banner thread and the UTF-16 text is NUL-terminated.
        let ok = unsafe {
            let hwnd = self.window_hwnd as HWND;
            let changed = SetWindowTextW(hwnd, text.as_ptr());
            if changed != 0 {
                InvalidateRect(hwnd, std::ptr::null(), 0)
            } else {
                0
            }
        };
        if ok == 0 {
            return Err("could not update the screen-control ribbon".to_string());
        }
        Ok(())
    }
}

impl Drop for Banner {
    fn drop(&mut self) {
        // SAFETY: asks the banner thread to leave its message loop; it destroys its own window.
        unsafe { PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0) };
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
        if let Ok(mut active) = active_cancel().lock() {
            *active = None;
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn banner_shows_and_goes_away() {
        let cancel = crate::cancel::CancelToken::new(std::path::Path::new("."), "banner-test");
        let banner = super::Banner::show(
            "Trợ lý đang điều khiển máy tính\nThao tác của bạn sẽ tạm dừng AI · Dừng bất cứ lúc nào",
            cancel.clone(),
            false,
        ).expect("banner window");
        banner
            .set_text("Tạm dừng vì bạn đang thao tác\nAI tự tiếp tục sau 3 giây yên lặng")
            .expect("update banner text");
        unsafe {
            super::window_proc(banner.stop_hwnd as super::HWND, super::WM_LBUTTONDOWN, 0, 0);
            super::window_proc(banner.stop_hwnd as super::HWND, super::WM_LBUTTONUP, 0, 0);
        }
        assert!(
            cancel.is_cancelled(),
            "the ribbon's stop button cancels the active task"
        );
        std::thread::sleep(std::time::Duration::from_millis(300));
        drop(banner);
    }
}
