import { BrowserRouter, Route, Routes } from "react-router-dom";
import MainApp from "./MainApp";
import DemoApp from "./DemoApp";
import LandingPage from "./landing/LandingPage";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { MaintenanceGate } from "./components/MaintenanceGate";
import WaitlistConfirmed from "./WaitlistConfirmed";
import LegalTerms from "./LegalTerms";
import LegalPrivacy from "./LegalPrivacy";
import CookieBanner from "./components/CookieBanner";
import MaintenancePage from "./MaintenancePage";

export default function App() {
  return (
    <BrowserRouter>
      <CookieBanner />
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route
          path="/app"
          element={
            <ProtectedRoute>
              <MaintenanceGate>
                <MainApp />
              </MaintenanceGate>
            </ProtectedRoute>
          }
        />
        <Route path="/demo" element={<DemoApp />} />
        <Route path="/maintenance" element={<MaintenancePage />} />
        <Route path="/waitlist/confirmed" element={<WaitlistConfirmed />} />
        <Route path="/legal/terms" element={<LegalTerms />} />
        <Route path="/legal/privacy" element={<LegalPrivacy />} />
      </Routes>
    </BrowserRouter>
  );
}
