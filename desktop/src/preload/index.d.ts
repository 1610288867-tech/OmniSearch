import type { OmnisearchApi } from "../shared/contracts";

declare global {
  interface Window {
    omnisearch: OmnisearchApi;
  }
}

export {};
