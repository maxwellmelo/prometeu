//! Dial mode: connect to a remote peer by NodeId, expose its stream as a
//! local TCP listener. For each local TCP accept, open a fresh bi stream on
//! the persistent Iroh connection and pipe bytes both ways.

use anyhow::{Context, Result};
use ed25519_dalek::{Signer, SigningKey};
use iroh::{endpoint::presets, Endpoint, EndpointAddr, EndpointId};
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

use crate::proto::{
    cbor_bytes, DialerHello, ReceiptSigned, ReceiptUnsigned, ServerAck, ALPN,
};

pub async fn run(
    id: SigningKey,
    peer_hex: &str,
    listen: &str,
    capability: &str,
    _db_path: &str,
    discovery: &str,
) -> Result<()> {
    let dialer_node_id = hex::encode(id.verifying_key().to_bytes());
    let peer_bytes = hex::decode(peer_hex).context("peer NodeId is not valid hex")?;
    if peer_bytes.len() != 32 {
        anyhow::bail!("peer NodeId must decode to 32 bytes");
    }
    let mut arr = [0u8; 32];
    arr.copy_from_slice(&peer_bytes);
    let peer_id = EndpointId::from_bytes(&arr).map_err(|e| anyhow::anyhow!("{e:?}"))?;
    let peer_addr = EndpointAddr::new(peer_id);

    tracing::info!(
        dialer_node_id = %dialer_node_id,
        peer = %peer_hex,
        listen = %listen,
        capability = %capability,
        "starting mesh dial loop"
    );

    let ep = Endpoint::builder(presets::N0)
        .secret_key(iroh::SecretKey::from(id.to_bytes()))
        .bind()
        .await
        .map_err(|e| anyhow::anyhow!("iroh bind: {e}"))?;

    let listener = TcpListener::bind(listen)
        .await
        .with_context(|| format!("binding local listener {listen}"))?;
    tracing::info!(listen = %listen, "local TCP listener ready");

    loop {
        let (tcp, peer_local) = match listener.accept().await {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!(?e, "local accept failed");
                continue;
            }
        };
        let ep = ep.clone();
        let peer_addr = peer_addr.clone();
        let id = id.clone();
        let dialer_node_id = dialer_node_id.clone();
        let capability = capability.to_string();
        let discovery = discovery.to_string();
        tokio::spawn(async move {
            tracing::debug!(?peer_local, "local TCP accept");
            if let Err(e) = bridge_one(
                &ep,
                &peer_addr,
                &id,
                &dialer_node_id,
                &capability,
                &discovery,
                tcp,
            )
            .await
            {
                tracing::warn!(?e, "bridge failed");
            }
        });
    }
}

async fn bridge_one(
    ep: &Endpoint,
    peer_addr: &EndpointAddr,
    id: &SigningKey,
    dialer_node_id: &str,
    capability: &str,
    discovery: &str,
    tcp: tokio::net::TcpStream,
) -> Result<()> {
    let session_id = format!("s-{}-{}", &dialer_node_id[..8], rand::random::<u32>());
    let opened_at = now();

    let conn = ep
        .connect(peer_addr.clone(), ALPN)
        .await
        .map_err(|e| anyhow::anyhow!("connect: {e}"))?;
    let (mut send, mut recv) = conn
        .open_bi()
        .await
        .map_err(|e| anyhow::anyhow!("open_bi: {e}"))?;

    let hello = DialerHello {
        capability: capability.to_string(),
        dialer_node_id: dialer_node_id.to_string(),
        session_id: session_id.clone(),
        opened_at,
    };
    write_framed(&mut send, &cbor_bytes(&hello)).await?;

    let ack_bytes = read_framed(&mut recv).await?;
    let ack: ServerAck =
        ciborium::from_reader(&ack_bytes[..]).context("decoding ServerAck CBOR")?;
    if !ack.reject_reason.is_empty() {
        anyhow::bail!("server rejected: {}", ack.reject_reason);
    }
    tracing::debug!(?ack, "got ServerAck");

    let (mut tcp_r, mut tcp_w) = tcp.into_split();

    let to_peer = async {
        let n = tokio::io::copy(&mut tcp_r, &mut send).await;
        let _ = send.finish();
        n
    };
    let from_peer = async {
        let n = tokio::io::copy(&mut recv, &mut tcp_w).await;
        let _ = tcp_w.shutdown().await;
        n
    };
    let (bytes_out, bytes_in) = match tokio::join!(to_peer, from_peer) {
        (Ok(a), Ok(b)) => (a, b),
        (Ok(a), Err(_)) => (a, 0),
        (Err(_), Ok(b)) => (0, b),
        (Err(_), Err(_)) => (0, 0),
    };
    let closed_at = now();

    let unsigned = ReceiptUnsigned {
        schema: "prometeu/receipt/1".to_string(),
        session_id: session_id.clone(),
        server_node_id: ack.server_node_id,
        dialer_node_id: dialer_node_id.to_string(),
        capability: capability.to_string(),
        model: String::new(),
        bytes_in,
        bytes_out,
        tokens_served: 0,
        opened_at,
        closed_at,
    };
    let canon = cbor_bytes(&unsigned);
    let sig = id.sign(&canon);
    let signed = ReceiptSigned {
        receipt: unsigned,
        server_sig: String::new(),
        dialer_sig: hex::encode(sig.to_bytes()),
    };
    tracing::info!(
        session = %session_id,
        bytes_in,
        bytes_out,
        "session closed; dialer-signed receipt issued"
    );
    if let Ok(j) = serde_json::to_string(&signed) {
        tracing::info!(target: "prometeu_mesh::receipt", "{}", j);
    }
    crate::receipts::spawn_submit(discovery.to_string(), signed);

    Ok(())
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
