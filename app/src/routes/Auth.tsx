import { useEffect, useState } from "react";
import { hfDeleteToken, hfLoadToken, hfSaveToken, hfWhoami } from "../lib/ipc";

type AuthState = "checking" | "present" | "missing";

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

export default function Auth() {
  const [token, setToken] = useState("");
  const [state, setState] = useState<AuthState>("checking");
  const [username, setUsername] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState("");

  async function refreshState() {
    setState("checking");
    setMessage("");
    setError("");
    try {
      const storedToken = await hfLoadToken();
      if (!storedToken) {
        setUsername(null);
        setState("missing");
        return;
      }
      setState("present");
      try {
        const whoami = await hfWhoami();
        setUsername(typeof whoami.user === "string" ? whoami.user : null);
      } catch {
        setUsername(null);
      }
    } catch (err) {
      setUsername(null);
      setState("missing");
      setError(errorMessage(err));
    }
  }

  useEffect(() => {
    void refreshState();
  }, []);

  async function saveAndVerify() {
    setBusy(true);
    setMessage("");
    setError("");
    setPhase("Saving token…");
    try {
      await hfSaveToken(token);
      setState("present");
      setToken("");
      setMessage("Token saved. Verifying with Hugging Face…");
      window.dispatchEvent(new Event("dataclaw-auth-changed"));
      setPhase("Verifying with Hugging Face…");
      try {
        const whoami = await hfWhoami();
        const user = typeof whoami.user === "string" ? whoami.user : null;
        setUsername(user);
        if (whoami.ok === false) {
          setMessage("");
          setError(
            typeof whoami.error === "string"
              ? `Token saved but verification failed: ${whoami.error}`
              : "Token saved but verification failed."
          );
        } else {
          setMessage(user ? `Verified as ${user}.` : "Token saved and verified.");
        }
      } catch (err) {
        setUsername(null);
        setMessage("");
        setError(`Token saved but verification failed: ${errorMessage(err)}`);
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setPhase("");
      setBusy(false);
    }
  }

  async function signOut() {
    setBusy(true);
    setMessage("");
    setError("");
    try {
      await hfDeleteToken();
      setUsername(null);
      setToken("");
      setState("missing");
      setMessage("Signed out.");
      window.dispatchEvent(new Event("dataclaw-auth-changed"));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="page">
      <h1>Hugging Face auth</h1>

      <div className="card">
        <h2>Token</h2>
        <p className="muted">
          Paste a Hugging Face access token with <strong>write</strong> permission. It is stored in
          macOS Keychain and mirrored to <code>~/.cache/huggingface/token</code> (mode 600) so the CLI
          and scheduled runs find it. Get a token at{" "}
          <a href="https://huggingface.co/settings/tokens" target="_blank" rel="noreferrer">
            huggingface.co/settings/tokens
          </a>
          .
        </p>
        <p>
          State:{" "}
          {state === "checking" ? (
            <>
              <span className="spinner" /> checking…
            </>
          ) : state === "present" ? (
            <strong style={{ color: "var(--ok)" }}>token present</strong>
          ) : (
            <strong style={{ color: "var(--warn)" }}>not present</strong>
          )}
        </p>
        {username ? <p>Signed in as <strong>{username}</strong></p> : null}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void saveAndVerify();
          }}
          style={{ display: "grid", gap: 10, maxWidth: 480, marginTop: 10 }}
        >
          <label htmlFor="hf-token">Token</label>
          <input
            id="hf-token"
            type="password"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            autoComplete="off"
            placeholder="hf_…"
          />
          <div className="actions" style={{ marginTop: 0 }}>
            <button type="submit" className="primary" disabled={busy || token.trim().length === 0}>
              {busy ? (
                <>
                  <span className="spinner" />
                  {phase || "Saving…"}
                </>
              ) : (
                "Save & verify"
              )}
            </button>
            <button type="button" onClick={() => void signOut()} disabled={busy || state !== "present"}>
              Sign out
            </button>
            <button type="button" onClick={() => void refreshState()} disabled={busy}>
              Recheck
            </button>
          </div>
        </form>
        {message ? <div className="notice">{message}</div> : null}
        {error ? <div className="error">{error}</div> : null}
      </div>
    </section>
  );
}
