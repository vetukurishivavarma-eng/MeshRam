import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import Dashboard from "./Dashboard";
import WorkerPage from "./WorkerPage";
import { api } from "./ui";

// Same bundle is served by the coordinator (cluster dashboard) and by each worker (owner's local controls).
function App() {
  const [mode, setMode] = useState<string | null>(null);
  useEffect(() => { api("/api/mode").then((j) => setMode(j.mode)).catch(() => setMode("coordinator")); }, []);
  useEffect(() => { document.title = mode === "worker" ? "MeshRAM Agent" : "MeshRAM Cluster"; }, [mode]);
  if (!mode) return null;
  return mode === "worker" ? <WorkerPage /> : <Dashboard />;
}

createRoot(document.getElementById("root")!).render(<App />);
