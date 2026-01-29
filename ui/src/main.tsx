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
  } else if (host === "sightsinger.app") {
    const shortMap: Record<string, string> = {
      "/r/choir":
        "/?utm_source=reddit&utm_medium=social&utm_campaign=community_launch&utm_content=r_choir&utm_id=community_launch&utm_source_platform=reddit",
      "/r/singing":
        "/?utm_source=reddit&utm_medium=social&utm_campaign=community_launch&utm_content=r_singing&utm_id=community_launch&utm_source_platform=reddit",
      "/r/musescore":
        "/?utm_source=reddit&utm_medium=social&utm_campaign=community_launch&utm_content=r_musescore&utm_id=community_launch&utm_source_platform=reddit",
      "/li/post":
        "/?utm_source=linkedin&utm_medium=social&utm_campaign=community_launch&utm_content=post&utm_id=community_launch&utm_source_platform=linkedin",
    };
    const hasUtm = window.location.search.includes("utm_");
    const target = shortMap[window.location.pathname];
    if (target && !hasUtm) {
      window.location.replace(`https://sightsinger.app${target}`);
    }
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
