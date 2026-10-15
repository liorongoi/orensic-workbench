import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // The Help page bundles ../docs, which sits outside this folder.
  server: { proxy: { "/api": "http://127.0.0.1:8000" }, fs: { allow: [".."] } },
});
