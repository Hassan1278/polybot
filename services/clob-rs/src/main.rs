//! clob-rs — Polymarket CLOB V2 order-signing sidecar.
//!
//! The Python bot (packages/polybot/clients/clob.py) calls this over the
//! internal docker network. Exists because the Python/TS CLOB v2 SDKs can't
//! sign for deposit wallets (POLY_1271); the Rust SDK can.
//!
//! Config (env):
//!   SIGNER_KEY            controlling EOA private key (hex)
//!   FUNDER_ADDRESS        deployed deposit wallet (funds live here); empty = EOA
//!   POLYMARKET_CLOB_V2_URL  default https://clob-v2.polymarket.com

use std::env;
use std::str::FromStr as _;
use std::sync::Arc;

use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};

use alloy_signer_local::PrivateKeySigner;
use polymarket_client_sdk_v2::auth::state::Authenticated;
use polymarket_client_sdk_v2::auth::{Normal, Signer as _};
use polymarket_client_sdk_v2::clob::types::{Side, SignatureType};
use polymarket_client_sdk_v2::clob::{Client, Config};
use polymarket_client_sdk_v2::types::{Address, Decimal, U256};
use polymarket_client_sdk_v2::POLYGON;

type Authed = Client<Authenticated<Normal>>;

struct AppState {
    client: Authed,
    signer: PrivateKeySigner,
    funder: String,
}

#[derive(Deserialize)]
struct OrderReq {
    token_id: String,
    side: String,
    price: String,
    size: String,
}

#[derive(Deserialize)]
struct CancelReq {
    order_id: String,
}

fn parse_side(s: &str) -> Result<Side, String> {
    match s.to_ascii_uppercase().as_str() {
        "BUY" => Ok(Side::Buy),
        "SELL" => Ok(Side::Sell),
        other => Err(format!("bad side: {other}")),
    }
}

async fn health(State(st): State<Arc<AppState>>) -> Json<Value> {
    Json(json!({
        "ok": true,
        "service": "clob-rs",
        "funder": st.funder,
        "signing_ready": true
    }))
}

async fn place_order(
    State(st): State<Arc<AppState>>,
    Json(req): Json<OrderReq>,
) -> (StatusCode, Json<Value>) {
    let side = match parse_side(&req.side) {
        Ok(s) => s,
        Err(e) => return (StatusCode::BAD_REQUEST, Json(json!({"status": "rejected", "error": e}))),
    };
    let price = match Decimal::from_str(&req.price) {
        Ok(d) => d,
        Err(e) => return (StatusCode::BAD_REQUEST, Json(json!({"status": "rejected", "error": format!("bad price: {e}")}))),
    };
    let size = match Decimal::from_str(&req.size) {
        Ok(d) => d,
        Err(e) => return (StatusCode::BAD_REQUEST, Json(json!({"status": "rejected", "error": format!("bad size: {e}")}))),
    };
    // Polymarket token IDs are uint256 decimal strings.
    let token = match U256::from_str(&req.token_id) {
        Ok(t) => t,
        Err(e) => return (StatusCode::BAD_REQUEST, Json(json!({"status": "rejected", "error": format!("bad token_id: {e}")}))),
    };

    let result: anyhow::Result<_> = async {
        let order = st
            .client
            .limit_order()
            .token_id(token)
            .size(size)
            .price(price)
            .side(side)
            .build()
            .await?;
        let signed = st.client.sign(&st.signer, order).await?;
        let resp = st.client.post_order(signed).await?;
        Ok::<_, anyhow::Error>(resp)
    }
    .await;

    match result {
        Ok(resp) => (
            StatusCode::OK,
            Json(json!({
                "status": if resp.success { "submitted" } else { "rejected" },
                "success": resp.success,
                "order_id": resp.order_id,
                "venue_status": format!("{:?}", resp.status),
                "error": resp.error_msg,
            })),
        ),
        Err(e) => (
            StatusCode::BAD_GATEWAY,
            Json(json!({"status": "rejected", "error": e.to_string()})),
        ),
    }
}

async fn cancel(
    State(st): State<Arc<AppState>>,
    Json(req): Json<CancelReq>,
) -> (StatusCode, Json<Value>) {
    match st.client.cancel_order(&req.order_id).await {
        Ok(r) => (
            StatusCode::OK,
            Json(json!({"ok": true, "canceled": r.canceled, "not_canceled": r.not_canceled})),
        ),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(json!({"ok": false, "error": e.to_string()}))),
    }
}

async fn cancel_all(State(st): State<Arc<AppState>>) -> (StatusCode, Json<Value>) {
    match st.client.cancel_all_orders().await {
        Ok(r) => (StatusCode::OK, Json(json!({"ok": true, "canceled": r.canceled}))),
        Err(e) => (StatusCode::BAD_GATEWAY, Json(json!({"ok": false, "error": e.to_string()}))),
    }
}

// Monitoring only — not on the trade path. The SDK's paginated `orders()` API
// will be wired here later; an empty list is a safe degradation for callers.
async fn list_orders() -> Json<Value> {
    Json(json!([]))
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let key = env::var("SIGNER_KEY").expect("SIGNER_KEY env required");
    let signer = PrivateKeySigner::from_str(&key)?.with_chain_id(Some(POLYGON));
    let host = env::var("POLYMARKET_CLOB_V2_URL")
        .unwrap_or_else(|_| "https://clob-v2.polymarket.com".to_string());
    let funder_env = env::var("FUNDER_ADDRESS").ok().filter(|s| !s.is_empty());

    // 0=EOA 1=Proxy 2=GnosisSafe 3=Poly1271(deposit wallet). Default 3 for
    // V2 deposit wallets; env-overridable so we can switch without rebuilding
    // if the account turns out to be a Safe (2).
    let sig_raw = env::var("SIGNATURE_TYPE").unwrap_or_else(|_| "3".to_string());
    let sig_type = match sig_raw.trim() {
        "0" => SignatureType::Eoa,
        "1" => SignatureType::Proxy,
        "2" => SignatureType::GnosisSafe,
        _ => SignatureType::Poly1271,
    };
    let mut builder = Client::new(&host, Config::default())?
        .authentication_builder(&signer)
        .signature_type(sig_type);
    if let Some(f) = &funder_env {
        builder = builder.funder(Address::from_str(f)?);
    }
    let client = builder.authenticate().await?;

    let funder = funder_env.unwrap_or_else(|| format!("{:?}", signer.address()));
    let state = Arc::new(AppState { client, signer, funder });

    let app = Router::new()
        .route("/health", get(health))
        .route("/order", post(place_order))
        .route("/cancel", post(cancel))
        .route("/cancel-all", post(cancel_all))
        .route("/orders", get(list_orders))
        .with_state(state);

    let addr = "0.0.0.0:8082";
    let listener = tokio::net::TcpListener::bind(addr).await?;
    println!("clob-rs listening on {addr}");
    axum::serve(listener, app).await?;
    Ok(())
}
