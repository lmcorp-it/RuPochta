// RuПочта desktop shell.
//
// The web application is served by the user's own deployment, so the client is
// a native window around it rather than a second implementation of the mail UI.
// It opens a local page that asks for the server address once, then navigates
// the same window there.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("failed to start the RuPochta desktop client");
}
