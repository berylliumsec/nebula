import {test as base, expect} from "@playwright/test";
import {ResourceScope} from "./resource-scope";

/** Each case/retry owns its Core, model and request contexts, even if setup fails. */
export const test = base.extend<{resources: ResourceScope}>({
  resources: async ({}, use, testInfo) => {
    const scope = new ResourceScope();
    try {
      await use(scope);
    } finally {
      try {
        if (testInfo.status !== testInfo.expectedStatus) {
          await testInfo.attach("fixture-diagnostics", {body: await scope.diagnostics(), contentType: "text/plain"});
        }
      } finally {
        await scope.close();
      }
    }
  },
});
export {expect};
