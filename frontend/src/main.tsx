import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";

// Fonts del sistema (igual que Paco APP) — sin @fontsource.
import "./index.css";
import LoginPage from "./pages/LoginPage";
import DashboardPage from "./pages/DashboardPage";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<DashboardPage />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
