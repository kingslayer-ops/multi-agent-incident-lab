import { createReadStream, statSync } from "node:fs";
import { createServer, request as proxyRequest } from "node:http";
import { extname, join, normalize } from "node:path";

const root = join(process.cwd(), "dist");
const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml"
};

function proxy(incoming, outgoing) {
  const upstream = proxyRequest({
    hostname: "api",
    port: 8000,
    method: incoming.method,
    path: incoming.url,
    headers: { ...incoming.headers, host: "api:8000" }
  }, response => {
    outgoing.writeHead(response.statusCode ?? 502, {
      ...response.headers,
      "x-accel-buffering": "no"
    });
    response.pipe(outgoing);
  });
  upstream.on("error", error => {
    if (!outgoing.headersSent) outgoing.writeHead(502, { "content-type": "text/plain" });
    outgoing.end(`Upstream unavailable: ${error.message}`);
  });
  incoming.pipe(upstream);
}

function staticFile(urlPath, outgoing) {
  const requested = normalize(decodeURIComponent(urlPath.split("?")[0])).replace(/^(\.\.[/\\])+/, "");
  let candidate = join(root, requested === "/" ? "index.html" : requested);
  try {
    if (!statSync(candidate).isFile()) candidate = join(root, "index.html");
  } catch {
    candidate = join(root, "index.html");
  }
  outgoing.writeHead(200, {
    "content-type": contentTypes[extname(candidate)] ?? "application/octet-stream",
    "cache-control": candidate.endsWith("index.html") ? "no-store" : "public, max-age=31536000, immutable"
  });
  createReadStream(candidate).pipe(outgoing);
}

createServer((incoming, outgoing) => {
  const url = incoming.url ?? "/";
  if (url === "/health" || url.startsWith("/api/")) proxy(incoming, outgoing);
  else staticFile(url, outgoing);
}).listen(4173, "0.0.0.0");
