import { parentPort } from "node:worker_threads";
import { parentFrame } from "./protocol.js";
import { Runtime } from "./runtime.js";
import { createTransport } from "./transport.js";

const port = parentPort;
if (!port) process.exit(1);
const runtime = new Runtime({ factory: createTransport, write: line => port.postMessage(line), exit: () => {
  port.postMessage(null);
  port.close();
} });
port.on("message", (raw: unknown) => {
  try {
    if (raw === null) void runtime.stop("shutdown");
    else runtime.receive(parentFrame.parse(raw));
  } catch { void runtime.stop("transport_failed"); }
});
process.on("uncaughtException", () => runtime.stop("transport_failed"));
process.on("unhandledRejection", () => runtime.stop("transport_failed"));
