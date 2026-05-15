import { useEffect, useMemo, useState } from "react";
import {
  NavLink,
  Navigate,
  Route,
  BrowserRouter as Router,
  Routes,
  useLocation,
  useNavigate
} from "react-router-dom";
import { dataclawConfigGet, hfLoadToken } from "./lib/ipc";
import Auth from "./routes/Auth";
import Config from "./routes/Config";
import Dashboard from "./routes/Dashboard";
import Logs from "./routes/Logs";
import appIcon from "../src-tauri/icons/icon.png";

const navItems = [
  ["Dashboard", "/dashboard"],
  ["Config", "/config"],
  ["Auth", "/auth"],
  ["Logs", "/logs"]
] as const;

export default function App() {
  return (
    <Router>
      <AppShell />
    </Router>
  );
}

function AppShell() {
  const [authBlocked, setAuthBlocked] = useState(false);
  const [checkingAuth, setCheckingAuth] = useState(true);
  const location = useLocation();
  const navigate = useNavigate();

  async function refreshAuthGate() {
    setCheckingAuth(true);
    try {
      const [token, config] = await Promise.all([
        hfLoadToken(),
        dataclawConfigGet({ showSecrets: false })
      ]);
      const repo = typeof config.repo === "string" ? config.repo.trim() : "";
      const blocked = token === null && repo.length > 0;
      setAuthBlocked(blocked);
      if (blocked && location.pathname !== "/auth") {
        navigate("/auth", { replace: true });
      }
    } catch {
      setAuthBlocked(false);
    } finally {
      setCheckingAuth(false);
    }
  }

  useEffect(() => {
    void refreshAuthGate();
    const handler = () => void refreshAuthGate();
    window.addEventListener("dataclaw-auth-changed", handler);
    return () => window.removeEventListener("dataclaw-auth-changed", handler);
  }, []);

  useEffect(() => {
    if (authBlocked && location.pathname !== "/auth") {
      navigate("/auth", { replace: true });
    }
  }, [authBlocked, location.pathname, navigate]);

  const guardedRoutes = useMemo(
    () => ({
      dashboard: authBlocked ? <Navigate to="/auth" replace /> : <Dashboard />,
      config: authBlocked ? <Navigate to="/auth" replace /> : <Config />,
      logs: authBlocked ? <Navigate to="/auth" replace /> : <Logs />
    }),
    [authBlocked]
  );

  return (
    <main>
      <nav>
        {navItems.map(([label, path]) => (
          <NavLink key={path} to={path}>
            {label}
          </NavLink>
        ))}
        <img className="nav-logo" src={appIcon} alt="DataClaw" />
      </nav>
      <Routes>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={guardedRoutes.dashboard} />
        <Route path="/config" element={guardedRoutes.config} />
        <Route path="/findings" element={<Navigate to="/dashboard" replace />} />
        <Route path="/auth" element={<Auth />} />
        <Route path="/logs" element={guardedRoutes.logs} />
      </Routes>
      {authBlocked && location.pathname !== "/auth" && (
        <div role="dialog" aria-modal="true" className="modal-backdrop">
          <section className="modal">
            <h2>Hugging Face sign in required</h2>
            <p>Save a token before running DataClaw for the configured repository.</p>
            <div className="actions" style={{ marginTop: 12 }}>
              <button
                type="button"
                className="primary"
                onClick={() => navigate("/auth", { replace: true })}
              >
                Open Auth
              </button>
              <button type="button" onClick={() => void refreshAuthGate()} disabled={checkingAuth}>
                Recheck
              </button>
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
