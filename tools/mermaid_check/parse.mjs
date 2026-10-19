import { JSDOM } from "jsdom";
import fs from "fs";
const dom = new JSDOM("<!DOCTYPE html><body></body>");
globalThis.window = dom.window; globalThis.document = dom.window.document;
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
const mermaid = (await import("mermaid")).default;
const blocks = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
let bad = 0;
for (const [name, text] of blocks) {
  try { await mermaid.parse(text); console.log("ok  ", name); }
  catch (e) { bad++; console.log("FAIL", name, String(e.message || e).split("\n")[0]); }
}
process.exit(bad ? 1 : 0);
