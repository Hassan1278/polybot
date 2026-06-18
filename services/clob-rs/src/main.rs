//! clob-rs — Polymarket CLOB V2 order-signing sidecar.
//!
//! The Python bot (packages/polybot/clients/clob.py) calls this over the
//! internal docker network. This crate exists because the Python/TS CLOB v2
//! SDKs can't sign for deposit wallets (POLY_1271) — the Rust SDK can.
//!
//! Phase 1 (this file): HTTP surface + /health so we can verify the Rust
//! build pipeline in Docker. Order endpoints return 501 until Phase 2 wires
//! in rs-clob-client-v2.
//!
//! Config (env): SIGNER_KEY, FUNDER_ADDRESS (deposit wallet), SIGNATURE_TYPE,
//! POLYMARKET_CLOB_URL, POLYGON_CHAIN_ID.

use axum::{
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use serde_json::{json, Value};
use std::env;

async fn health() -> Json<Value> {
    let funder = env::var("FUNDER_ADDRESS").unwrap_or_default();
    Json(json!({
        "ok": true,
        "service": "clob-rs",
        "phase": 1,
        "funder": funder,
        "signing_ready": false
    }))
}

// Placeholder endpoints — wired to rs-clob-client-v2 in Phase 2.
async fn not_ready() -> (StatusCode, Json<Value>) {
    (
        StatusCode::NOT_IMPLEMENTED,
        Json(json!({"status": "rejected", "error": "clob-rs phase 1: order signing not wired yet"})),
    )
}

#[tokio::main]
async fn main() {
    let app = Router::new()
        .route("/health", get(health))
        .route("/order", post(not_ready))
        .route("/cancel", post(not_ready))
        .route("/cancel-all", post(not_ready))
        .route("/orders", get(not_ready));

    let addr = "0.0.0.0:8082";
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("bind 0.0.0.0:8082");
    println!("clob-rs listening on {addr}");
    axum::serve(listener, app).await.expect("serve");
}
