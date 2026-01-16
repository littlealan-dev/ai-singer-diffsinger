import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";
import "./firebase";

import { AuthProvider } from "./hooks/useAuth.tsx";

const app = (
  <AuthProvider>
    <App />
  </AuthProvider>
);

ReactDOM.createRoot(document.getElementById("root")!).render(
  import.meta.env.DEV ? app : <React.StrictMode>{app}</React.StrictMode>
);
