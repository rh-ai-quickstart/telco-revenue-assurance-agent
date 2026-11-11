import "@testing-library/jest-dom/vitest";
import { afterEach, beforeAll, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = () => undefined;
});
