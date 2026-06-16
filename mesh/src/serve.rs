//! Serve mode: bind an Iroh endpoint, accept inbound bi-directional streams,
//! and bridge each one to a local TCP service (e.g. llama.cpp rpc-server).
//!
//! Per accepted bi stream:
//!   1. Read DialerHello (CBOR frame).
//!   2. TCP-connect to `forward` upstream.
//!   3. Respond with ServerAck.
//!   4. Bidirectional copy until either side closes (half-close aware).
//!   5. Server-sign a ReceiptUnsigned and persist locally (next sprint).

use anyhow::{Context, Result};
use ed25519_dalek::{Signer, SigningKey};
use iroh::{endpoint::presets, Endpoint};
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

use crate::proto::{
    cbor_bytes, DialerHello, NodeAdvertisement, ReceiptSigned, ReceiptUnsigned, ServerAck, ALPN,
};

pub async fn run(
    id: SigningKey,
    forward: &str,
    capability: &str,
    advertisement: NodeAdvertisement,
    discovery: &str,
) -> Result<()> {
    let server_node_id = hex::encode(id.verifying_key().to_bytes());
    tracing::info!(
        node_id = %server_node_id,
        forward = %forward,
        capability = %capability,
        ?advertisement,
        "starting mesh serve loop"
    );

    let ep = Endpoint::builder(presets::N0)
        .secret_key(iroh::SecretKey::from(id.to_bytes()))
        .alpns(vec![ALPN.to_vec()])
        .bind()
        .await
        .map_err(|e| anyhow::anyhow!("iroh bind: {e}"))?;

    tracing::info!(
        node_id = %hex::encode(ep.id().as_bytes()),
        "iroh endpoint bound (id = our Ed25519 pubkey)"
    );

    if !discovery.trim().is_empty() {
        spawn_discovery_heartbeat(discovery.to_string(), advertisement.clone());
    }

    // Accept loop. Endpoint::accept() returns Future<Option<Incoming>>.
    loop {
        let Some(incoming) = ep.accept().await else {
            tracing::warn!("endpoint accept returned None; shutting down");
            break;
        };
        let id = id.clone();
        let forward = forward.to_string();
        let capability = capability.to_string();
        let server_node_id = server_node_id.clone();
        let discovery_owned = discovery.to_string();
        tokio::spawn(async move {
            if let Err(e) =
                handle_conn(id, incoming, &forward, &capability, &server_node_id, &discovery_owned).await
            {
                tracing::warn!(?e, "conn handler failed");
            }
        });
    }
    Ok(())
}

async fn handle_conn(
    id: SigningKey,
    incoming: iroh::endpoint::Incoming,
    forward: &str,
    capability: &str,
    server_node_id: &str,
    discovery: &str,
) -> Result<()> {
    let connecting = incoming
        .accept()
        .map_err(|e| anyhow::anyhow!("incoming.accept: {e}"))?;
    let conn = connecting
        .await
        .map_err(|e| anyhow::anyhow!("connecting.await: {e}"))?;
    let remote_id = hex::encode(conn.remote_id().as_bytes());
    tracing::debug!(remote = %remote_id, "iroh connection accepted");

    loop {
        let (mut send, mut recv) = match conn.accept_bi().await {
            Ok(s) => s,
            Err(e) => {
                tracing::debug!(?e, "accept_bi closed");
                return Ok(());
            }
        };

        let hello_bytes = match read_framed(&mut recv).await {
            Ok(b) => b,
            Err(e) => {
                tracing::warn!(?e, "reading DialerHello");
                continue;
            }
        };
        let hello: DialerHello = match ciborium::from_reader(&hello_bytes[..]) {
            Ok(h) => h,
            Err(e) => {
                tracing::warn!(?e, "decoding DialerHello CBOR");
                continue;
            }
        };
        tracing::debug!(?hello, "got DialerHello");

        let opened_at = now();
        let upstream_id = format!("up-{}", &server_node_id[..8]);

        if hello.capability != capability {
            let ack = ServerAck {
                capability: capability.to_string(),
                server_node_id: server_node_id.to_string(),
                upstream_id,
                accepted_at: opened_at,
                reject_reason: format!(
                    "capability mismatch: want {capability}, got {}",
                    hello.capability
                ),
            };
            let _ = write_framed(&mut send, &cbor_bytes(&ack)).await;
            let _ = send.finish();
            continue;
        }

        let upstream = match TcpStream::connect(forward).await {
            Ok(s) => s,
            Err(e) => {
                tracing::warn!(?e, %forward, "upstream connect failed");
                let ack = ServerAck {
                    capability: capability.to_string(),
                    server_node_id: server_node_id.to_string(),
                    upstream_id,
                    accepted_at: opened_at,
                    reject_reason: format!("upstream connect failed: {e}"),
                };
                let _ = write_framed(&mut send, &cbor_bytes(&ack)).await;
                let _ = send.finish();
                continue;
            }
        };

        let ack = ServerAck {
            capability: capability.to_string(),
            server_node_id: server_node_id.to_string(),
            upstream_id,
            accepted_at: opened_at,
            reject_reason: String::new(),
        };
        write_framed(&mut send, &cbor_bytes(&ack)).await?;

        let session_id = hello.session_id.clone();
        let dialer_node_id = hello.dialer_node_id.clone();
        let capability_owned = capability.to_string();
        let id_for_task = id.clone();
        let server_node_id_owned = server_node_id.to_string();
        let discovery_owned = discovery.to_string();

        tokio::spawn(async move {
            let (mut upstream_r, mut upstream_w) = upstream.into_split();

            // direction A: dialer -> upstream
            let dir_a = async {
                let n = tokio::io::copy(&mut recv, &mut upstream_w).await;
                let _ = upstream_w.shutdown().await;
                n
            };
            // direction B: upstream -> dialer
            let dir_b = async {
                let n = tokio::io::copy(&mut upstream_r, &mut send).await;
                let _ = send.finish();
                n
            };

            // Run both; finish when both ends close OR either errors.
            let (bytes_in, bytes_out) = match tokio::join!(dir_a, dir_b) {
                (Ok(a), Ok(b)) => (a, b),
                (Ok(a), Err(_)) => (a, 0),
                (Err(_), Ok(b)) => (0, b),
                (Err(_), Err(_)) => (0, 0),
            };
            let closed_at = now();

            let unsigned = ReceiptUnsigned {
                schema: "prometeu/receipt/1".to_string(),
                session_id: session_id.clone(),
                server_node_id: server_node_id_owned,
                dialer_node_id,
                capability: capability_owned,
                model: String::new(),
                bytes_in,
                bytes_out,
                tokens_served: 0,
                opened_at,
                closed_at,
            };
            let canon = cbor_bytes(&unsigned);
            let sig = id_for_task.sign(&canon);
            let signed = ReceiptSigned {
                receipt: unsigned,
                server_sig: hex::encode(sig.to_bytes()),
                dialer_sig: String::new(),
            };
            tracing::info!(
                session = %session_id,
                bytes_in,
                bytes_out,
                "session closed; server-signed receipt issued"
            );
            if let Ok(j) = serde_json::to_string(&signed) {
                tracing::info!(target: "prometeu_mesh::receipt", "{}", j);
            }
            crate::receipts::spawn_submit(discovery_owned, signed);
        });
    }
}

async fn read_framed(recv: &mut iroh::endpoint::RecvStream) -> Result<Vec<u8>> {
    let mut len_buf = [0u8; 4];
    recv.read_exact(&mut len_buf)
        .await
        .context("read frame len")?;
    let len = u32::from_be_bytes(len_buf) as usize;
    if len > 1_048_576 {
        anyhow::bail!("frame too large: {len}");
    }
    let mut buf = vec![0u8; len];
    recv.read_exact(&mut buf)
        .await
        .context("read frame body")?;
    Ok(buf)
}

async fn write_framed(send: &mut iroh::endpoint::SendStream, body: &[u8]) -> Result<()> {
    let len = (body.len() as u32).to_be_bytes();
    send.write_all(&len)
        .await
        .map_err(|e| anyhow::anyhow!("write framed len: {e}"))?;
    send.write_all(body)
        .await
        .map_err(|e| anyhow::anyhow!("write framed body: {e}"))?;
    Ok(())
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or_default()
}

fn spawn_discovery_heartbeat(discovery: String, ad: NodeAdvertisement) {
    let base = discovery.trim_end_matches('/').to_string();
    tokio::spawn(async move {
        let client = reqwest::Client::new();
        let announce_url = format!("{base}/api/mesh/announce");
        let leave_url = format!("{base}/api/mesh/leave");
        let mut first = true;
        loop {
            let payload = serde_json::json!({
                "node_id": ad.node_id,
                "capability": ad.capability,
                "model": ad.model,
                "layers": ad.layers,
                "region_hint": ad.region_hint,
            });
            match client.post(&announce_url).json(&payload).send().await {
                Ok(r) if r.status().is_success() => {
                    if first {
                        tracing::info!(discovery = %base, node_id = %ad.node_id, "mesh discovery announce ok");
                        first = false;
                    }
                }
                Ok(r) => tracing::warn!(status = %r.status(), discovery = %base, "mesh discovery announce failed"),
                Err(e) => tracing::warn!(?e, discovery = %base, "mesh discovery announce error"),
            }
            tokio::time::sleep(std::time::Duration::from_secs(45)).await;
            // Best-effort leave only reachable by external SIGTERM handler later; URL kept here for parity.
            let _ = &leave_url;
        }
    });
}
