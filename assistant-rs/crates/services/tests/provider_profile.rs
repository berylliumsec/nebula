use nebula_assistant_domain::dependencies::{DependencyKind, StoredDependency};
use nebula_assistant_integrations::ModelRequest;
use nebula_assistant_services::provider_profile::*;
use serde_json::{Value, json};
fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../compatibility/python-provider-profile.json"
    ))
    .unwrap()
}
struct Inputs {
    case: Value,
    trace: Vec<Value>,
}
impl ManagedCredentialResolver for Inputs {
    fn resolve(&mut self, reference: &str) -> std::result::Result<Secret, CredentialFailure> {
        self.trace
            .push(json!({"kind":"managed","reference":reference}));
        match self.case["managed"].as_str() {
            Some("locked") => Err(CredentialFailure::Locked("fixture vault is locked".into())),
            Some("missing") => Err(CredentialFailure::Unavailable(
                "fixture credential is missing".into(),
            )),
            value => {
                Ok(Secret::new(value.unwrap_or("FIXTURE-ONLY-NOT-A-REAL-SECRET").into()).unwrap())
            }
        }
    }
}
impl EnvironmentInputs for Inputs {
    fn variable(&mut self, name: &str) -> Option<String> {
        self.trace.push(json!({"kind":"environment","name":name}));
        self.case["environment"][name].as_str().map(str::to_owned)
    }
}
fn profile(case: &Value) -> StoredDependency {
    StoredDependency::decode(
        DependencyKind::ProviderProfile,
        &serde_json::to_vec(&case["profile"]).unwrap(),
    )
    .unwrap()
}
fn observe(case: &Value) -> (Value, Vec<Value>) {
    let mut inputs = Inputs {
        case: case.clone(),
        trace: Vec::new(),
    };
    let provider = profile(case);
    let mut output = json!({});
    let result = (|| -> Result<(), ProfileError> {
        let resolver = if case["managed"] == "no-resolver" {
            None
        } else {
            Some(&mut inputs as &mut dyn ManagedCredentialResolver)
        };
        let bound = bound_metadata(&provider, case["profile_input_raw"].as_str().unwrap())?;
        assert!(!format!("{bound:?}").contains("FIXTURE-ONLY"));
        // Compare strings, not Values: this proves nested object insertion order.
        assert_eq!(
            bound.as_str(),
            case["metadata_raw"].as_str().unwrap(),
            "{}",
            case["name"]
        );
        let prepared = prepare(&provider, bound.as_str(), resolver)?;
        assert!(!format!("{prepared:?}").contains("FIXTURE-ONLY"));
        output["config"] = prepared.public_config().clone();
        output["managed_reference"] = prepared
            .managed_reference()
            .map(Value::from)
            .unwrap_or(Value::Null);
        output["adapter"] = prepared.public_config()["kind"].clone();
        let request: ModelRequest =
            serde_json::from_slice(&serde_json::to_vec(&case["request"]).unwrap()).unwrap();
        output["required_model"] = prepared.require(&request)?.into();
        if prepared.adapter_support() == AdapterSupport::OpenAiCompatible {
            let mode = if case["mode"] == "stream" {
                RequestMode::Stream
            } else {
                RequestMode::Complete
            };
            let config = prepared.openai_config(&request, mode, &mut inputs)?;
            output["headers"] = serde_json::to_value(config.headers()).unwrap();
            if mode == RequestMode::Complete {
                output["completion_timeout"] =
                    config.runtime().completion_timeout.as_secs_f64().into();
            }
            output["retry"] = json!({"attempts":config.runtime().retry_attempts,"backoff_seconds":config.runtime().retry_backoff.as_secs_f64()});
            output["allowed_providers"] = json!(config.runtime().openrouter_allowed_providers);
            assert!(!format!("{config:?}").contains("FIXTURE-ONLY"));
        } else {
            let before = inputs.trace.len();
            let failure = prepared
                .openai_config(&request, RequestMode::Complete, &mut inputs)
                .unwrap_err();
            assert_eq!(failure.details()["kind"], "AdapterUnavailable");
            assert_eq!(
                inputs.trace.len(),
                before,
                "unimplemented adapters must not select compatible credentials"
            );
        }
        Ok(())
    })();
    match result {
        Ok(()) => output["accepted"] = true.into(),
        Err(error) => {
            output["accepted"] = false.into();
            output["error"] = error.details().clone();
            assert!(!format!("{error:?} {error}").contains("FIXTURE-ONLY"));
        }
    }
    (output, inputs.trace)
}
#[test]
fn source_profile_configuration_catalog_credentials_and_require_match() {
    let fixture = fixture();
    for case in fixture["vectors"].as_array().unwrap() {
        let (actual, trace) = observe(case);
        assert_eq!(actual, case["expected"], "{}", case["name"]);
        assert_eq!(
            &trace,
            case["trace"].as_array().unwrap(),
            "{}",
            case["name"]
        );
    }
}
#[test]
fn profile_metadata_binding_and_secret_limits_are_explicit() {
    let fixture = fixture();
    let case = &fixture["vectors"][0];
    let provider = profile(case);
    let mut inputs = Inputs {
        case: case.clone(),
        trace: Vec::new(),
    };
    let error = prepare(
        &provider,
        r#"{"default_model":"unbound"}"#,
        Some(&mut inputs),
    )
    .unwrap_err();
    assert_eq!(error.details()["kind"], "BindingError");
    assert!(inputs.trace.is_empty());
    assert!(Secret::new("private".repeat(3000)).is_err());
    let mut large = case["profile"].clone();
    large["metadata"]["large"] = Value::String("private".repeat(160_000));
    let provider = StoredDependency::decode(
        DependencyKind::ProviderProfile,
        &serde_json::to_vec(&large).unwrap(),
    )
    .unwrap();
    let error = prepare(&provider, &large["metadata"].to_string(), None).unwrap_err();
    assert_eq!(error.details()["kind"], "ResourceBoundary");
    assert!(!format!("{error:?} {error}").contains("private"));

    // Ignored descriptor entries still consume work before a second DOM/row
    // vector is allocated. Admission limits cannot be bypassed with null rows.
    let mut many = case["profile"].clone();
    many["metadata"]["model_descriptors"] = json!(vec![Value::Null; 6_000]);
    let provider = StoredDependency::decode(
        DependencyKind::ProviderProfile,
        &serde_json::to_vec(&many).unwrap(),
    )
    .unwrap();
    let error = prepare(&provider, &many["metadata"].to_string(), None).unwrap_err();
    assert_eq!(error.details()["kind"], "ResourceBoundary");
    assert!(inputs.trace.is_empty());
}
#[test]
fn empty_response_schema_agrees_with_supported_adapter_require() {
    let fixture = fixture();
    let case = fixture["vectors"]
        .as_array()
        .unwrap()
        .iter()
        .find(|v| v["name"] == "empty-schema-no-capability")
        .unwrap();
    let provider = profile(case);
    let mut inputs = Inputs {
        case: case.clone(),
        trace: Vec::new(),
    };
    let prepared = prepare(&provider, case["metadata_raw"].as_str().unwrap(), None).unwrap();
    let request: ModelRequest =
        serde_json::from_slice(&serde_json::to_vec(&case["request"]).unwrap()).unwrap();
    let expected = prepared.require(&request).unwrap();
    let config = prepared
        .openai_config(&request, RequestMode::Complete, &mut inputs)
        .unwrap();
    assert_eq!(config.runtime().require(&request).unwrap(), expected);
}
