//! Native, bounded unlock flow for provider secrets owned by the local Core.

use tauri::AppHandle;

use crate::core_connection::configured_core_is_local;

const PROVIDER_SERVICE: &str = "io.berylliumsec.nebula.provider-credentials";
const VAULT_PREFIX: &str = "vault:";
const REFERENCE_HEX_LENGTH: usize = 32;

fn provider_identifier(reference: &str) -> Result<&str, String> {
    let identifier = reference
        .strip_prefix(VAULT_PREFIX)
        .ok_or_else(|| "Only saved provider-vault references can be unlocked.".to_string())?;
    if identifier.len() != REFERENCE_HEX_LENGTH
        || !identifier
            .bytes()
            .all(|value| value.is_ascii_digit() || (b'a'..=b'f').contains(&value))
    {
        return Err("The provider credential reference is invalid.".to_string());
    }
    Ok(identifier)
}

#[cfg(target_os = "linux")]
fn unlock_linux_provider(reference: &str) -> Result<(), String> {
    use std::collections::HashMap;

    use dbus_secret_service::{EncryptionType, SecretService};

    let identifier = provider_identifier(reference)?;
    let service = SecretService::connect_with_max_prompt_timeout(EncryptionType::Plain, 120)
        .map_err(|_| {
            "The host credential service is unavailable. Unlock the keyring on the Nebula host."
                .to_string()
        })?;
    let items = service
        .search_items(HashMap::from([
            ("service", PROVIDER_SERVICE),
            ("username", identifier),
        ]))
        .map_err(|_| {
            "The provider credential could not be found in the host keyring.".to_string()
        })?;
    if items.unlocked.is_empty() && items.locked.is_empty() {
        return Err(
            "The provider credential is no longer present in the host keyring.".to_string(),
        );
    }
    if !items.locked.is_empty() {
        let locked = items.locked.iter().collect::<Vec<_>>();
        service.unlock_all(&locked).map_err(|_| {
            "The keyring remained locked or its unlock prompt was dismissed.".to_string()
        })?;
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
fn unlock_linux_provider(reference: &str) -> Result<(), String> {
    provider_identifier(reference)?;
    Err("Interactive provider-vault unlock is currently available on Linux.".to_string())
}

#[tauri::command]
pub(crate) async fn unlock_provider_credential(
    app: AppHandle,
    reference: String,
) -> Result<(), String> {
    if !configured_core_is_local(&app)? {
        return Err(
            "This desktop is connected to a remote Core. Unlock the keyring on the Core host, then retry."
                .to_string(),
        );
    }
    tauri::async_runtime::spawn_blocking(move || unlock_linux_provider(&reference))
        .await
        .map_err(|_| "The native keyring unlock task stopped unexpectedly.".to_string())?
}

#[cfg(test)]
mod tests {
    use super::provider_identifier;

    #[test]
    fn provider_reference_is_bounded_and_opaque() {
        assert_eq!(
            provider_identifier("vault:0123456789abcdef0123456789abcdef").unwrap(),
            "0123456789abcdef0123456789abcdef"
        );
        assert!(provider_identifier("env:OPENROUTER_API_KEY").is_err());
        assert!(provider_identifier("vault:../keyring").is_err());
        assert!(provider_identifier("vault:0123").is_err());
    }
}
