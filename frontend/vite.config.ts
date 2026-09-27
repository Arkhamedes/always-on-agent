import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base "./" -> relative asset URLs, so dashboard.py can serve dist/ from any
// path without rewriting.
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: {
    // `npm run dev` on the workstation proxies API calls to the VM's
    // dashboard over an SSH tunnel: ssh -L 8766:127.0.0.1:8766 <vm>
    proxy: { "/api": "http://127.0.0.1:8766" },
  },
});
