import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  base: "/supervision/",   // servi derrière l'edge sous /supervision/ (et à la racine de :3000 via le nginx du dashboard)
  plugins: [react(), tailwindcss()],
  server: { port: 5174, proxy: {
    "/bff": { target: "http://localhost:3000", changeOrigin: true },
    "/assistant": { target: "http://localhost:3000", changeOrigin: true },
  } },
});
