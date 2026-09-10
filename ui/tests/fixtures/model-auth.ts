import {readFile} from "node:fs/promises";

/** The compiled-candidate launcher stores only this disposable server's token. */
export async function modelAuthHeaders(): Promise<{Authorization: string}> {
  const file = process.env.NEBULA_MODEL_TEST_AUTH_FILE;
  const token = file ? JSON.parse(await readFile(file, "utf8")).token : "model-test-token";
  if (typeof token !== "string" || !token) throw new Error("Model candidate did not provide a test token");
  return {Authorization: `Bearer ${token}`};
}
