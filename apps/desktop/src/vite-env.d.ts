/// <reference types="vite/client" />

interface Window {
  craneDesktop?: {
    getAppInfo: () => Promise<{
      name: string;
      version: string;
      platform: string;
      runtimeConnected: boolean;
    }>;
  };
}