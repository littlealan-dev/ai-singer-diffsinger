import { BrowserRouter, Route, Routes } from "react-router-dom";
import MainApp from "./MainApp";
import DemoApp from "./DemoApp";
import LandingPage from "./landing/LandingPage";
import { ProtectedRoute } from "./components/ProtectedRoute";
import WaitlistConfirmed from "./WaitlistConfirmed";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route
          path="/app"
          element={
            <ProtectedRoute>
              <MainApp />
            </ProtectedRoute>
          }
        />
        <Route path="/demo" element={<DemoApp />} />
        <Route path="/waitlist/confirmed" element={<WaitlistConfirmed />} />
      </Routes>
    </BrowserRouter>
  );
}
