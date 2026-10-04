import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import MainApp from "./MainApp";
import LandingPage from "./landing/LandingPage";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { MaintenanceGate } from "./components/MaintenanceGate";
import { BackendReadinessGate } from "./components/BackendReadinessGate";
import WaitlistConfirmed from "./WaitlistConfirmed";
import LegalTerms from "./LegalTerms";
import LegalPrivacy from "./LegalPrivacy";
import CookieBanner from "./components/CookieBanner";
import MaintenancePage from "./MaintenancePage";
import { MarketingOptInProcessor } from "./components/MarketingOptInProcessor";

export default function App() {
  return (
    <BrowserRouter>
      <CookieBanner />
      <MarketingOptInProcessor />
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route
          path="/app"
          element={
            <ProtectedRoute>
              <BackendReadinessGate>
                <MaintenanceGate>
                  <MainApp />
                </MaintenanceGate>
              </BackendReadinessGate>
            </ProtectedRoute>
          }
        />
        {/* The static demo is retired (DemoApp.tsx is kept); the studio has demo songs. */}
        <Route path="/demo/*" element={<Navigate to="/app" replace />} />
        <Route path="/maintenance" element={<MaintenancePage />} />
        <Route path="/waitlist/confirmed" element={<WaitlistConfirmed />} />
        <Route path="/legal/terms" element={<LegalTerms />} />
        <Route path="/legal/privacy" element={<LegalPrivacy />} />
      </Routes>
    </BrowserRouter>
  );
}
