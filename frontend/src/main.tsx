import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { installLinkHandler } from "./desktop";
import "@fontsource/instrument-serif/latin-400-italic.css";
import "./styles.css";

installLinkHandler();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
