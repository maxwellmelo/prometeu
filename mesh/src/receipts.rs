//! Local receipt store: an append-only ledger of signed work receipts.
//!
//! For Sprint 3A we just persist receipts locally so they survive restarts.
//! Future sprints will batch-submit them to the Prometeu token chain.

use anyhow::{Context, Result};
use rusqlite::{params, Connection};
use std::path::Path;

use crate::proto::ReceiptSigned;

pub struct Store(pub Connection);

pub fn open<P: AsRef<Path>>(path: P) -> Result<Store> {
    let conn = Connection::open(path.as_ref())
        .with_context(|| format!("opening mesh db {}", path.as_ref().display()))?;
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS receipts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      TEXT NOT NULL,
            server_node_id  TEXT NOT NULL,
            dialer_node_id  TEXT NOT NULL,
            capability      TEXT NOT NULL,
            bytes_in        INTEGER NOT NULL,
            bytes_out       INTEGER NOT NULL,
            tokens_served   INTEGER NOT NULL,
            opened_at       INTEGER NOT NULL,
            closed_at       INTEGER NOT NULL,
            server_sig      TEXT NOT NULL,
            dialer_sig      TEXT NOT NULL,
            cbor            BLOB NOT NULL,
            submitted       INTEGER NOT NULL DEFAULT 0,
            created_at      INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_receipts_session ON receipts(session_id);
        CREATE INDEX IF NOT EXISTS idx_receipts_submitted ON receipts(submitted);
        "#,
    )?;
    Ok(Store(conn))
}

impl Store {
    pub fn insert_receipt(&self, signed: &ReceiptSigned, cbor: &[u8]) -> Result<i64> {
        let r = &signed.receipt;
        self.0.execute(
            r#"INSERT INTO receipts
                (session_id, server_node_id, dialer_node_id, capability,
                 bytes_in, bytes_out, tokens_served, opened_at, closed_at,
                 server_sig, dialer_sig, cbor)
               VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)"#,
            params![
                r.session_id,
                r.server_node_id,
                r.dialer_node_id,
                r.capability,
                r.bytes_in as i64,
                r.bytes_out as i64,
                r.tokens_served as i64,
                r.opened_at as i64,
                r.closed_at as i64,
                signed.server_sig,
                signed.dialer_sig,
                cbor,
            ],
        )?;
        Ok(self.0.last_insert_rowid())
    }
}

/// Best-effort submission of a signed receipt to the gateway aggregator.
///
/// Fire-and-forget: errors are logged but never bubbled up — the local SQLite
/// store remains the canonical ledger, the gateway aggregate is a public view.
pub fn spawn_submit(discovery: String, signed: ReceiptSigned) {
    let base = discovery.trim_end_matches('/').to_string();
    if base.is_empty() {
        return;
    }
    tokio::spawn(async move {
        let url = format!("{base}/api/mesh/receipts");
        let client = match reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(5))
            .build()
        {
            Ok(c) => c,
            Err(e) => {
                tracing::warn!(?e, "receipt submit client build failed");
                return;
            }
        };
        match client.post(&url).json(&signed).send().await {
            Ok(r) if r.status().is_success() => {
                tracing::info!(url = %url, session = %signed.receipt.session_id, "receipt submitted to gateway");
            }
            Ok(r) => tracing::warn!(status = %r.status(), url = %url, "receipt submit non-2xx"),
            Err(e) => tracing::warn!(?e, url = %url, "receipt submit error"),
        }
    });
}
