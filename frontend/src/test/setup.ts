import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Vitest runs with `globals: false`, so Testing Library's own automatic
// cleanup hook is not installed. Register it here instead.
afterEach(() => {
  cleanup();
  try {
    globalThis.localStorage.clear();
  } catch {
    // Storage is not available in every environment; nothing to clear.
  }
});
