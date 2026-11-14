// Local-only (never committed): the default config with longer waits for a host at load ~45.
import base from "./playwright.config";
export default { ...base, expect: { ...base.expect, timeout: 60_000 }, use: { ...base.use, actionTimeout: 90_000, navigationTimeout: 120_000 } };
