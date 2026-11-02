// SPDX-License-Identifier: MIT
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { AuthProvider } from "./auth/AuthProvider";
import { RootErrorBoundary } from "./components/RootErrorBoundary";
import { App } from "./App";
import "./index.css";
import "./styles/fonts.css";
import "./styles/tailwind.css";

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("main.tsx: #root element not found in index.html");
}

createRoot(rootElement).render(
  <StrictMode>
    {/* Outside the router and the auth provider so a throw in either still renders a fallback. */}
    <RootErrorBoundary>
      <BrowserRouter>
        <AuthProvider>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </RootErrorBoundary>
  </StrictMode>,
);
