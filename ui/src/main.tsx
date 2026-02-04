import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";
import "./firebase";

import { AuthProvider } from "./hooks/useAuth.tsx";

if (typeof window !== "undefined") {
  const host = window.location.hostname;
  if (host === "sightsinger-app.web.app") {
    const target = `https://sightsinger.app${window.location.pathname}${window.location.search}${window.location.hash}`;
    window.location.replace(target);
  }
}

const app = (
  <AuthProvider>
    <App />
  </AuthProvider>
);

ReactDOM.createRoot(document.getElementById("root")!).render(
  import.meta.env.DEV ? app : <React.StrictMode>{app}</React.StrictMode>
);
