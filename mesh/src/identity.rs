//! Persistent Ed25519 identity for a Prometeu mesh node.
//!
//! The same keypair will be used to:
//!   1. Derive the Iroh NodeId (peer identity in the P2P overlay).
//!   2. Sign work receipts that downstream tokenization will redeem.
//!   3. (Future) Sign on-chain transactions on the Prometeu token chain.
//!
//! For that to hold, this file is the single source of truth and must be backed
//! up by the operator. Loss of this file = loss of node identity = loss of
//! accumulated reputation/receipts/balance.

use anyhow::{Context, Result};
use ed25519_dalek::{SigningKey, SECRET_KEY_LENGTH};
use rand::rngs::OsRng;
use std::fs;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

/// Load identity from file, or generate + persist a fresh one.
pub fn load_or_create<P: AsRef<Path>>(path: P) -> Result<SigningKey> {
    let path = path.as_ref();
    if path.exists() {
        load(path)
    } else {
        let key = SigningKey::generate(&mut OsRng);
        save(path, &key)?;
        tracing::info!(
            path = %path.display(),
            node_id = %hex::encode(key.verifying_key().to_bytes()),
            "generated fresh mesh identity"
        );
        Ok(key)
    }
}

fn load(path: &Path) -> Result<SigningKey> {
    let bytes = fs::read(path).with_context(|| format!("reading identity {}", path.display()))?;
    let trimmed: Vec<u8> = bytes
        .into_iter()
        .filter(|b| !b.is_ascii_whitespace())
        .collect();
    let raw = hex::decode(&trimmed)
        .with_context(|| format!("identity {} is not hex-encoded", path.display()))?;
    if raw.len() != SECRET_KEY_LENGTH {
        anyhow::bail!(
            "identity {} has wrong length: expected {}, got {}",
            path.display(),
            SECRET_KEY_LENGTH,
            raw.len()
        );
    }
    let mut arr = [0u8; SECRET_KEY_LENGTH];
    arr.copy_from_slice(&raw);
    Ok(SigningKey::from_bytes(&arr))
}

fn save(path: &Path, key: &SigningKey) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).ok();
    }
    let hexed = hex::encode(key.to_bytes());
    let tmp = path.with_extension("tmp");
    {
        let mut f = fs::File::create(&tmp)
            .with_context(|| format!("creating {}", tmp.display()))?;
        f.write_all(hexed.as_bytes())?;
        f.write_all(b"\n")?;
        f.sync_all()?;
        // 0600 — owner read/write only.
        let mut perm = f.metadata()?.permissions();
        perm.set_mode(0o600);
        fs::set_permissions(&tmp, perm)?;
    }
    fs::rename(&tmp, path)
        .with_context(|| format!("renaming {} -> {}", tmp.display(), path.display()))?;
    Ok(())
}
