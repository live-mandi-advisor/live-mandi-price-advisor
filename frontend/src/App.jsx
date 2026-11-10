import { Navigate, Route, Routes } from "react-router-dom";
import Home from "./pages/Home.jsx";
import Forecast from "./pages/Forecast.jsx";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/forecast" element={<Forecast />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
