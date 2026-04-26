use minisign_verify::{PublicKey, Signature};

const PUBLIC_KEY: &str = r#"untrusted comment: minisign public key: 6466846C3CAA49F
RWSfpMrDRmhGBvQo2YXcNEE7YockHc/R2tuNSWHk4XMe6cAKfQ9FQCeP
"#;

const SIGNATURE: &str = r#"untrusted comment: signature from tauri secret key
RUSfpMrDRmhGBkKZ/Ud7J7xTuzsciA+QvkWq5d/HLfEWizCf+vXqqDRfFi9RZn5t33bS/Tet8SWp0H1M0v3DRp1iKbQ9i2ekYws=
trusted comment: timestamp:1777106209	file:dataclaw-updater-signature-payload.json
igZYzlvezdXitLmwTUnvJ8LifgvfKLaxGgYuAHZ939pa9MWcdTTazViAerB1fgiWP1gbn5vvURVT7DnnstGdAA==
"#;

#[test]
fn test_updater_signature_verification_rejects_tampered_json() {
    let public_key = PublicKey::decode(PUBLIC_KEY).expect("fixture public key decodes");
    let signature = Signature::decode(SIGNATURE).expect("fixture signature decodes");
    let original = br#"{"version":"1.0.0"}
"#;
    let tampered = br#"{"version":"1.0.1"}
"#;

    public_key
        .verify(original, &signature, false)
        .expect("fixture signature verifies the original payload");
    assert!(public_key.verify(tampered, &signature, false).is_err());
}
