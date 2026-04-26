import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";

type UpdateInfo = {
  version?: string;
  current_version?: string;
  body?: string | null;
  date?: string | null;
};

function messageForError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error);
  return /pubkey|signature/i.test(message) ? "Updater not configured for this build" : message;
}

export default function UpdateChecker() {
  const [checking, setChecking] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [update, setUpdate] = useState<UpdateInfo | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  async function checkForUpdates() {
    setChecking(true);
    setError("");
    setMessage("");
    setUpdate(null);
    try {
      const result = await invoke<UpdateInfo | null>("check_for_updates");
      if (result) {
        setUpdate(result);
      } else {
        setMessage("No updates available.");
      }
    } catch (err) {
      setError(messageForError(err));
    } finally {
      setChecking(false);
    }
  }

  async function installUpdate() {
    setInstalling(true);
    setError("");
    setMessage("");
    try {
      await invoke("install_update");
      setMessage("Installing update…");
    } catch (err) {
      setError(messageForError(err));
    } finally {
      setInstalling(false);
    }
  }

  return (
    <div className="card">
      <h2>About</h2>
      <div className="actions" style={{ marginTop: 0 }}>
        <button type="button" onClick={() => void checkForUpdates()} disabled={checking}>
          {checking ? "Checking…" : "Check for updates"}
        </button>
      </div>
      {message ? <p className="muted">{message}</p> : null}
      {error ? <div className="error">{error}</div> : null}
      {update ? (
        <div style={{ marginTop: 12 }}>
          <p>Version {update.version ?? "unknown"} is available.</p>
          {update.body ? <pre>{update.body}</pre> : null}
          <button type="button" className="primary" onClick={() => void installUpdate()} disabled={installing}>
            {installing ? "Installing…" : "Install"}
          </button>
        </div>
      ) : null}
    </div>
  );
}
