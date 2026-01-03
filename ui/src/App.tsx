import { BrowserRouter, Route, Routes } from "react-router-dom";
import MainApp from "./MainApp";
import LandingPage from "./landing/LandingPage";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/app" element={<MainApp />} />
      </Routes>
    </BrowserRouter>
  );
}
