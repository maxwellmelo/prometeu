//! Wire protocol for the Prometeu mesh.
//!
//! All messages are CBOR-encoded structs. Canonical CBOR is required for any
//! payload that ends up signed (Ed25519 over the CBOR bytes), so downstream
//! verification (off-chain or on-chain) does not depend on serializer quirks.

use serde::{Deserialize, Serialize};

/// ALPN advertised by `serve` peers. Prometeu mesh v1.
pub const ALPN: &[u8] = b"prometeu/mesh/1";

/// First message a dialer sends to a server right after opening a bi stream.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DialerHello {
    /// Capability requested (e.g. "rpc-worker").
    pub capability: String,
    /// Dialer NodeId (hex, redundant with QUIC peer identity but useful for logging).
    pub dialer_node_id: String,
    /// Session id assigned by dialer (so receipts can be correlated).
    pub session_id: String,
    /// Wall-clock unix seconds when the dialer opened the stream.
    pub opened_at: u64,
}

/// Server's response to DialerHello before relaying TCP bytes.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServerAck {
    /// Capability the server is providing on this stream.
    pub capability: String,
    /// Server NodeId (hex).
    pub server_node_id: String,
    /// Server-assigned upstream id for this session.
    pub upstream_id: String,
    /// Server wall-clock at accept.
    pub accepted_at: u64,
    /// If non-empty, request was rejected and stream will close.
    pub reject_reason: String,
}

/// Structured advertisement a serving node attaches on first connect. This is
/// what discovery layers (and humans) will inspect to decide whether to dial.
/// Kept small and stable so it can be relayed over registry/dashboard later.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NodeAdvertisement {
    pub schema: String,         // "prometeu/advertisement/1"
    pub node_id: String,        // hex
    pub capability: String,     // "rpc-worker", "llama-master", ...
    pub model: String,          // free-form, e.g. "Qwen2.5-1.5B-Q4_K_M"
    pub layers: String,         // e.g. "9-16" — best-effort label
    pub region_hint: String,    // ISO country or empty
    pub advertised_at: u64,     // unix seconds
}

/// A signed receipt acknowledging that `server_node_id` served `tokens_served`
/// tokens of inference to `dialer_node_id` during a session. Designed to be
/// trivially verifiable off-chain today and on-chain tomorrow:
///   message_bytes = canonical_cbor(receipt_unsigned)
///   ed25519_verify(server_pub, message_bytes, server_sig)
///   ed25519_verify(dialer_pub, message_bytes ++ server_sig, dialer_sig)
///
/// Both sides sign so neither can fabricate receipts unilaterally.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReceiptUnsigned {
    pub schema: String,        // "prometeu/receipt/1"
    pub session_id: String,
    pub server_node_id: String,
    pub dialer_node_id: String,
    pub capability: String,
    pub model: String,
    pub bytes_in: u64,
    pub bytes_out: u64,
    pub tokens_served: u64,    // 0 if not measured yet
    pub opened_at: u64,
    pub closed_at: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReceiptSigned {
    pub receipt: ReceiptUnsigned,
    /// hex Ed25519 signature by server over canonical CBOR of `receipt`.
    pub server_sig: String,
    /// hex Ed25519 signature by dialer over canonical CBOR of `receipt` ++ `server_sig` raw bytes.
    /// Empty until the dialer co-signs.
    pub dialer_sig: String,
}

/// Helper: encode any serde-serializable value as canonical CBOR bytes.
pub fn cbor_bytes<T: Serialize>(value: &T) -> Vec<u8> {
    let mut buf = Vec::new();
    // ciborium emits deterministic CBOR with sorted maps when given structs in
    // declared field order. That's stable enough for v1; we'll harden with a
    // dedicated canonicalizer when on-chain verification lands.
    ciborium::into_writer(value, &mut buf).expect("CBOR encode never fails on owned struct");
    buf
}
