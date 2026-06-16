//! Prometeu P2P mesh entrypoint.
//!
//! Modes:
//!   - `serve`: bind Iroh endpoint, accept inbound streams, forward each to a
//!     local TCP service (e.g. llama.cpp rpc-server on :50052).
//!   - `dial`: connect to a remote peer by NodeId, expose remote stream as a
//!     local TCP listener.
//!   - `id`: print this node's persistent Ed25519 identity (NodeId).
//!   - `peers`: list discovered peers from the local cache.

use anyhow::Result;
use clap::{Parser, Subcommand};

mod identity;
mod receipts;
mod serve;
mod dial;
mod proto;

const DEFAULT_DISCOVERY_URL: &str = "https://prometeu.mx3dev.com";

#[derive(Parser, Debug)]
#[command(
    name = "prometeu-mesh",
    version,
    about = "Prometeu P2P mesh — censorship-resistant overlay for distributed LLM inference"
)]
struct Cli {
    /// Path to the persistent identity file (Ed25519 secret key).
    #[arg(long, env = "PROMETEU_MESH_IDENTITY", default_value = "/var/lib/prometeu-mesh/identity.key")]
    identity: String,

    /// Path to local SQLite database (receipts, known peers).
    #[arg(long, env = "PROMETEU_MESH_DB", default_value = "/var/lib/prometeu-mesh/mesh.db")]
    db: String,

    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand, Debug)]
enum Cmd {
    /// Print this node's persistent identity (NodeId hex).
    Id,

    /// Bind an Iroh endpoint and forward inbound streams to a local TCP service.
    Serve {
        /// Local TCP service to forward inbound streams to (e.g. 127.0.0.1:50052).
        #[arg(long, env = "PROMETEU_MESH_FORWARD")]
        forward: String,

        /// Capability label advertised to peers (e.g. "rpc-worker", "llama-server").
        #[arg(long, env = "PROMETEU_MESH_CAPABILITY", default_value = "rpc-worker")]
        capability: String,

        /// JSON metadata advertised with the node (model, layers, region…).
        #[arg(long, env = "PROMETEU_MESH_META", default_value = "{}")]
        meta: String,

        /// Discovery gateway base URL. Empty disables announce/leave heartbeat.
        #[arg(long, env = "PROMETEU_MESH_DISCOVERY", default_value = DEFAULT_DISCOVERY_URL)]
        discovery: String,
    },

    /// List discovered mesh peers from gateway.
    Peers {
        /// Discovery gateway base URL.
        #[arg(long, env = "PROMETEU_MESH_DISCOVERY", default_value = DEFAULT_DISCOVERY_URL)]
        discovery: String,

        /// Optional capability filter.
        #[arg(long, default_value = "rpc-worker")]
        capability: String,
    },

    /// Connect to a remote peer by NodeId, expose its remote service as a local TCP listener.
    Dial {
        /// Remote peer NodeId (hex).
        #[arg(long)]
        peer: String,

        /// Local TCP address to listen on (will forward each accept into a stream to peer).
        #[arg(long, env = "PROMETEU_MESH_LISTEN")]
        listen: String,

        /// Capability label expected on the remote side.
        #[arg(long, default_value = "rpc-worker")]
        capability: String,

        /// Discovery gateway base URL for submitting signed receipts. Empty disables submit.
        #[arg(long, env = "PROMETEU_MESH_DISCOVERY", default_value = DEFAULT_DISCOVERY_URL)]
        discovery: String,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info,iroh=warn")),
        )
        .init();

    let cli = Cli::parse();

    // Ensure parent dirs exist for identity + db.
    if let Some(parent) = std::path::Path::new(&cli.identity).parent() {
        std::fs::create_dir_all(parent).ok();
    }
    if let Some(parent) = std::path::Path::new(&cli.db).parent() {
        std::fs::create_dir_all(parent).ok();
    }

    let id = identity::load_or_create(&cli.identity)?;
    let _db = receipts::open(&cli.db)?;

    match cli.cmd {
        Cmd::Id => {
            println!("{}", hex::encode(id.verifying_key().to_bytes()));
            Ok(())
        }
        Cmd::Serve { forward, capability, meta, discovery } => {
            let meta_json: serde_json::Value = serde_json::from_str(&meta)
                .unwrap_or_else(|_| {
                    let mut m = serde_json::Map::new();
                    for part in meta.trim_matches(|c| c == '{' || c == '}').split(',') {
                        if let Some((k, v)) = part.split_once(':') {
                            m.insert(k.trim().to_string(), serde_json::Value::String(v.trim().to_string()));
                        }
                    }
                    serde_json::Value::Object(m)
                });
            let ad = proto::NodeAdvertisement {
                schema: "prometeu/advertisement/1".to_string(),
                node_id: hex::encode(id.verifying_key().to_bytes()),
                capability: capability.clone(),
                model: meta_json.get("model").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                layers: meta_json.get("layers").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                region_hint: meta_json.get("region").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                advertised_at: std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0),
            };
            serve::run(id, &forward, &capability, ad, &discovery).await
        }
        Cmd::Dial { peer, listen, capability, discovery } => {
            dial::run(id, &peer, &listen, &capability, &cli.db, &discovery).await
        }
        Cmd::Peers { discovery, capability } => {
            let url = format!(
                "{}/api/mesh/peers?capability={}",
                discovery.trim_end_matches('/'),
                urlencoding::encode(&capability)
            );
            let v: serde_json::Value = reqwest::Client::new()
                .get(url)
                .send()
                .await?
                .error_for_status()?
                .json()
                .await?;
            println!("{}", serde_json::to_string_pretty(&v)?);
            Ok(())
        }
    }
}
