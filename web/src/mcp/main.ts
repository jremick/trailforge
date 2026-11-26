import "maplibre-gl/dist/maplibre-gl.css";
import "./mcpStyles.css";
import { TrailForgeMcpApp } from "./mcpApp";
import { normalizeViewPayload, type HostCapabilityFlags } from "./types";

interface TrailForgeMcpPreviewHandle {
  renderPayload: (raw: unknown) => boolean;
  setTheme: (theme: "light" | "dark") => void;
  setCapabilities: (caps: Partial<HostCapabilityFlags>) => void;
  getState: () => ReturnType<TrailForgeMcpApp["getState"]>;
}

declare global {
  interface Window {
    __TRAILFORGE_MCP_APP__?: TrailForgeMcpPreviewHandle;
  }
}

const rootEl = document.getElementById("mcp-root");
const app = new TrailForgeMcpApp({ root: rootEl });

if (typeof window !== "undefined") {
  window.__TRAILFORGE_MCP_APP__ = {
    renderPayload(raw: unknown): boolean {
      const parsed = normalizeViewPayload(raw);
      if (!parsed) return false;
      app.applyPayload(parsed, false);
      return true;
    },
    setTheme(theme: "light" | "dark"): void {
      app.setTheme(theme);
    },
    setCapabilities(caps: Partial<HostCapabilityFlags>): void {
      app.setCapabilities(caps);
    },
    getState() {
      return app.getState();
    },
  };
}

void app.start();
