use std::{env, fs, path::Path};

use base64::{Engine as _, engine::general_purpose::STANDARD};
use minisign_verify::{PublicKey, Signature};

fn decoded_text(value: &str, label: &str) -> Result<String, String> {
    let bytes = STANDARD
        .decode(value.trim())
        .map_err(|error| format!("invalid base64 {label}: {error}"))?;
    String::from_utf8(bytes).map_err(|_| format!("decoded {label} is not UTF-8"))
}

fn verify(artifact: &Path, signature_path: &Path, encoded_public_key: &str) -> Result<(), String> {
    let public_key = PublicKey::decode(&decoded_text(encoded_public_key, "public key")?)
        .map_err(|error| format!("invalid updater public key: {error}"))?;
    let encoded_signature = fs::read_to_string(signature_path)
        .map_err(|error| format!("cannot read updater signature: {error}"))?;
    let signature = Signature::decode(&decoded_text(&encoded_signature, "signature")?)
        .map_err(|error| format!("invalid updater signature: {error}"))?;
    let content =
        fs::read(artifact).map_err(|error| format!("cannot read updater artifact: {error}"))?;
    public_key
        .verify(&content, &signature, true)
        .map_err(|error| format!("updater signature verification failed: {error}"))
}

fn main() {
    let arguments: Vec<String> = env::args().collect();
    if arguments.len() != 4 {
        eprintln!("usage: verify_update_signature ARTIFACT SIGNATURE PUBLIC_KEY");
        std::process::exit(2);
    }
    if let Err(error) = verify(
        Path::new(&arguments[1]),
        Path::new(&arguments[2]),
        &arguments[3],
    ) {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
