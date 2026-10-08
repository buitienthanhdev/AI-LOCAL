//! Run the gateway: `lga-gateway` (normally started by the agent core or scripts/start-gateway.ps1).
//! Stops on Ctrl+C or `POST /v1/admin/shutdown`; managed backends are stopped with it.

use std::process::ExitCode;
use std::sync::Arc;

use lga_core::config::Config;
use lga_gateway::server::{router, AppState};
use lga_gateway::{app, log};
use tokio::sync::Notify;

#[tokio::main]
async fn main() -> ExitCode {
    let config = match Config::load() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("gateway configuration error: {e:#}");
            return ExitCode::from(2);
        }
    };
    let redactor = app::redactor(&config);
    log::init(&config.logs_dir(), &config.observability.log_level, redactor.clone());
    let assembly = match app::assemble(&config, Arc::new(redactor)) {
        Ok(a) => a,
        Err(e) => {
            log::error(&format!("gateway configuration error: {e:#}"));
            return ExitCode::from(2);
        }
    };
    let address = format!("{}:{}", assembly.host, assembly.port);
    let listener = match tokio::net::TcpListener::bind(&address).await {
        Ok(l) => l,
        Err(e) => {
            log::error(&format!("cannot listen on {address}: {e}"));
            return ExitCode::from(1);
        }
    };
    assembly.gateway.start().await;
    let stop = Arc::new(Notify::new());
    let state = AppState { gateway: Arc::clone(&assembly.gateway), api_key: Arc::new(assembly.api_key.clone()), stop: Arc::clone(&stop) };
    log::info(&format!("gateway listening on http://{address}"));
    let shutdown = async move {
        tokio::select! {
            _ = stop.notified() => log::info("shutdown requested"),
            _ = tokio::signal::ctrl_c() => log::info("interrupted"),
        }
    };
    let served = axum::serve(listener, router(state, assembly.body_limit)).with_graceful_shutdown(shutdown).await;
    assembly.gateway.close();
    drop(assembly.job);
    match served {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            log::error(&format!("server error: {e}"));
            ExitCode::from(1)
        }
    }
}
