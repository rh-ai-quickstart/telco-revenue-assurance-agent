import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { DashboardPage } from "@/pages/DashboardPage";
import "@/styles.css";

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Missing root element.");
}

createRoot(rootElement).render(
  <StrictMode>
    <DashboardPage />
  </StrictMode>,
);
