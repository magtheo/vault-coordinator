import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["manifest.json"],
      manifest: {
        name: "Vault",
        short_name: "Vault",
        start_url: "/",
        display: "standalone",
        background_color: "#1a1a1a",
        theme_color: "#1a1a1a",
        icons: [
          {
            src: "/icon-192.png",
            sizes: "192x192",
            type: "image/png",
          },
          {
            src: "/icon-512.png",
            sizes: "512x512",
            type: "image/png",
          },
        ],
      },
      workbox: {
        globPatterns: ["**/*.{js,css,html,ico,png}"],
        runtimeCaching: [
          {
            urlPattern: /^\/api\/tasks/,
            handler: "NetworkFirst",
            options: {
              cacheName: "tasks-cache",
              expiration: { maxAgeSeconds: 60 },
            },
          },
          {
            urlPattern: /^\/api\/schedule\/today/,
            handler: "NetworkFirst",
            options: {
              cacheName: "today-cache",
              expiration: { maxAgeSeconds: 30 },
            },
          },
        ],
      },
    }),
  ],
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8650",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
