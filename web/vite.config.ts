import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// dev: `npm run dev` proxies the API of a running coordinator (or worker with MESHRAM_API=http://127.0.0.1:7870)
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy: { "/api": process.env.MESHRAM_API || "http://127.0.0.1:7860" } },
});
