//! Kalshi request signing — RSA-PSS / SHA-256, the scheme used by both REST private
//! endpoints and the WS handshake (verified, see memory `kalshi-key-facts`).
//!
//! Sign string = `timestamp_ms + METHOD + path` (path includes `/trade-api/v2`,
//! EXCLUDES the query string). Headers (exact case):
//!   KALSHI-ACCESS-KEY        = the Key ID
//!   KALSHI-ACCESS-TIMESTAMP  = current time in ms since epoch, as a string
//!   KALSHI-ACCESS-SIGNATURE  = base64(RSA-PSS-SHA256(sign string)), salt len = 32
//!
//! Market data (GetMarkets / orderbook) is PUBLIC, so this is only needed once we add
//! the WS feed or place orders. It is implemented now so that path is ready.

use anyhow::{Context, Result};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use rsa::pkcs1::DecodeRsaPrivateKey;
use rsa::pkcs8::DecodePrivateKey;
use rsa::pss::SigningKey;
use rsa::signature::{RandomizedSigner, SignatureEncoding};
use rsa::RsaPrivateKey;
use sha2::Sha256;

#[derive(Clone)]
pub struct Signer {
    key_id: String,
    signing_key: SigningKey<Sha256>,
}

impl Signer {
    /// Load from a Key ID and an RSA private key PEM (PKCS#8 or PKCS#1).
    pub fn from_pem(key_id: impl Into<String>, pem: &str) -> Result<Self> {
        let key = RsaPrivateKey::from_pkcs8_pem(pem)
            .or_else(|_| RsaPrivateKey::from_pkcs1_pem(pem))
            .context("parse RSA private key (tried PKCS#8 then PKCS#1)")?;
        // SigningKey::new uses salt length = digest size (32 for SHA-256) — what Kalshi requires.
        let signing_key = SigningKey::<Sha256>::new(key);
        Ok(Self {
            key_id: key_id.into(),
            signing_key,
        })
    }

    pub fn key_id(&self) -> &str {
        &self.key_id
    }

    /// Sign one request. `path` must include `/trade-api/v2...` and exclude any `?query`.
    /// Returns `(timestamp_ms_string, base64_signature)`.
    pub fn sign(&self, timestamp_ms: i64, method: &str, path: &str) -> String {
        let msg = format!("{}{}{}", timestamp_ms, method, path);
        let mut rng = rand::thread_rng();
        let sig = self
            .signing_key
            .sign_with_rng(&mut rng, msg.as_bytes());
        STANDARD.encode(sig.to_bytes())
    }

    /// Build the three signed headers for a request.
    pub fn headers(&self, timestamp_ms: i64, method: &str, path: &str) -> [(&'static str, String); 3] {
        let sig = self.sign(timestamp_ms, method, path);
        [
            ("KALSHI-ACCESS-KEY", self.key_id.clone()),
            ("KALSHI-ACCESS-TIMESTAMP", timestamp_ms.to_string()),
            ("KALSHI-ACCESS-SIGNATURE", sig),
        ]
    }
}
