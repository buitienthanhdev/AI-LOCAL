//! Gives agent-desktop.exe its icon (Explorer, taskbar, shortcuts): `assets/app.res` holds the RT_GROUP_ICON and
//! RT_ICON resources made by logs/dev/make_logo.py from the logo picture; the MSVC linker takes a .res file as input.

use std::path::PathBuf;

fn main() {
    let res: PathBuf = [env!("CARGO_MANIFEST_DIR"), "assets", "app.res"].iter().collect();
    println!("cargo:rerun-if-changed={}", res.display());
    let windows_msvc = std::env::var("CARGO_CFG_TARGET_OS").is_ok_and(|v| v == "windows")
        && std::env::var("CARGO_CFG_TARGET_ENV").is_ok_and(|v| v == "msvc");
    if windows_msvc && res.is_file() {
        println!("cargo:rustc-link-arg-bins={}", res.display());
    }
}
