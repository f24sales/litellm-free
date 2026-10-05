import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHash, timingSafeEqual } from "node:crypto";
import { spawn } from "node:child_process";

const root = path.dirname(fileURLToPath(import.meta.url));
const worker = path.join(root, "worker");
const manifest = JSON.parse(fs.readFileSync(path.join(root, "openclaw.plugin.json"), "utf8"));
const endpoint = "/plugins/litellm-free";
const MAX_BODY = 2 * 1024 * 1024;
let queue = Promise.resolve();

function reply(res, code, body) {
  res.writeHead(code, { "content-type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(body) + "\n");
}

function authorized(req) {
  const expected = process.env.OPENCLAW_GATEWAY_TOKEN?.trim();
  const supplied = /^Bearer ([^\s]+)$/i.exec(req.headers.authorization ?? "")?.[1];
  if (!expected || !supplied) return false;
  const digest = value => createHash("sha256").update(value).digest();
  return timingSafeEqual(digest(expected), digest(supplied));
}

function save(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const temporary = file + ".tmp";
  fs.writeFileSync(temporary, JSON.stringify(value, null, 2) + "\n", { mode: 0o600 });
  fs.renameSync(temporary, file);
}

function object(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (object(value)) return Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])]));
  return value;
}

const hash = value => createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex");
const clean = value => String(value ?? "").replace(/[\r\n\x00-\x1f]/g, " ").slice(0, 160);

const DISPLAY_NAMES = {
  groq: "Groq",
  kilo: "Kilo",
  nous: "Nous Portal",
  nvidia: "NVIDIA",
  openai: "OpenAI",
  opencode: "OpenCode",
  openrouter: "OpenRouter",
  deepseek: "DeepSeek",
  "deepseek-ai": "DeepSeek-AI",
  google: "Google",
  liquid: "Liquid",
  poolside: "Poolside",
  qwen: "Qwen",
  stealth: "Stealth",
  thinkingmachines: "Thinking Machines",
};

function displayModel(row) {
  const raw = clean(row?.model_name ?? row?.key ?? "Model");
  const parts = raw.split("/");
  return parts.map((part, index) => {
    if (index === parts.length - 1) return part;
    const key = part.toLowerCase();
    return DISPLAY_NAMES[key] ?? part.replace(/(^|[-_])([a-z])/g, (_, prefix, letter) => `${prefix}${letter.toUpperCase()}`);
  }).join(", ");
}

async function readBody(req) {
  let size = 0;
  const chunks = [];
  const timer = setTimeout(() => req.destroy(), 10_000);
  try {
    for await (const chunk of req) {
      size += chunk.length;
      if (size > MAX_BODY) throw new Error("body_too_large");
      chunks.push(chunk);
    }
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } finally { clearTimeout(timer); }
}

function validate(event) {
  if (!object(event) || event.schema_version !== 1) throw new Error("invalid_schema");
  const allowed = {
    "litellm-free-webui": ["scan_valid", "scan_failed"],
    "litellm-free": ["import_succeeded", "import_failed"],
  };
  if (!allowed[event.source]?.includes(event.event)) throw new Error("invalid_source_or_event");
  if (event.event === "scan_valid" && !["ok_changed", "ok_unchanged"].includes(event.scan_state)) {
    throw new Error("invalid_scan_state");
  }
  if (event.event === "import_succeeded") {
    if (!object(event.diff) || !["added", "removed", "updated"].every(k => Array.isArray(event.diff[k]))) {
      throw new Error("invalid_diff");
    }
    const changed = ["added", "removed", "updated"].some(k => event.diff[k].length > 0);
    if (event.diff.changed !== changed) throw new Error("inconsistent_diff");
  }
}

function run(command, args, timeoutSeconds) {
  return new Promise(resolve => {
    const child = spawn(command, args, { stdio: "ignore", detached: true, env: process.env });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      try { process.kill(-child.pid, "SIGKILL"); } catch { /* Already exited. */ }
    }, timeoutSeconds * 1000);
    child.once("error", () => { clearTimeout(timer); resolve({ ok: false, error: "spawn_failed" }); });
    child.once("close", code => {
      clearTimeout(timer);
      resolve({ ok: code === 0 && !timedOut, exitCode: code, ...(timedOut ? { error: "timeout" } : {}) });
    });
  });
}

function configuration(api) {
  const file = api.pluginConfig?.configPath ?? (fs.existsSync(path.join(root, "config.json"))
    ? path.join(root, "config.json") : path.join(root, "config.json_example"));
  const config = JSON.parse(fs.readFileSync(file, "utf8"));
  for (const key of ["notifyRoute", "notifyScript", "refreshScript"]) {
    if (typeof config[key] !== "string" || !config[key]) throw new Error("missing_" + key);
  }
  for (const key of ["notifyScript", "refreshScript"]) {
    if (!path.isAbsolute(config[key])) throw new Error("absolute_path_required");
  }
  for (const [key, fallback] of [["refreshTimeoutSeconds", 360], ["notifyTimeoutSeconds", 90]]) {
    config[key] ??= fallback;
    if (!Number.isInteger(config[key]) || config[key] < 1 || config[key] > 600) throw new Error("invalid_timeout");
  }
  return config;
}

function message(event, refresh) {
  if (event.event === "scan_valid") {
    return event.scan_state === "ok_changed"
      ? "🟢 LiteLLM-Free: Scan valid. Model changes ..."
      : "🟢 LiteLLM-Free: Scan valid. No model changes.";
  }
  if (event.event === "scan_failed") return "🔴 LiteLLM-Free: Scan not valid.";
  if (event.event === "import_failed") return event.import_ok === true
    ? "🔴 LiteLLM-Free: Import valid; post-import check failed."
    : "🔴 LiteLLM-Free: Import failed; models not reloaded.";
  if (!refresh?.ok) return "🔴 LiteLLM-Free: Import valid; client model reload failed.";
  const lines = ["🟢 LiteLLM-Free: Update valid, models reloaded."];
  for (const [key, marker] of [["added", "➕"], ["removed", "➖"]]) {
    const rows = event.diff[key];
    const names = rows.slice(0, 6).map(row => `${marker} ${displayModel(row)}`);
    lines.push(...names);
    if (rows.length > 6) lines.push(`${marker} …`);
  }
  return lines.join("\n").slice(0, 3500);
}

// Retain metadata changes as well as route names, while removing credentials.
function diffAttachment(value) {
  if (Array.isArray(value)) return value.map(diffAttachment);
  if (object(value)) return Object.fromEntries(Object.entries(value).filter(([key]) => {
    const name = key.toLowerCase().replace(/[^a-z0-9]/g, "");
    return !(/apikey|password|secret|credential|accesskey/.test(name)
      || (name.endsWith("token") && !name.endsWith("pertoken"))
      || /authorization$|headers$|cookies?$/.test(name)
      || ["auth", "bearer", "privatekey", "serviceaccount"].includes(name));
  }).map(([key, item]) => [key, diffAttachment(item)]));
  if (typeof value === "string" && /^https?:\/\//i.test(value)) {
    try {
      const url = new URL(value);
      url.username = ""; url.password = ""; url.search = ""; url.hash = "";
      return url.toString();
    } catch { return "[invalid URL]"; }
  }
  return value;
}

async function processEvent(api, event) {
  const config = configuration(api);
  const id = hash([event.source, event.event, event.event_id || event.run_id || hash(event)]);
  const receiptPath = path.join(worker, "receipts", id + ".json");
  const digest = hash(event);
  const receipt = fs.existsSync(receiptPath) ? JSON.parse(fs.readFileSync(receiptPath, "utf8"))
    : { id, digest, event: event.event, run_id: event.run_id, receivedAt: new Date().toISOString() };
  if (receipt.digest !== digest) return { code: 409, body: { ok: false, error: "event_id_reused" } };
  if (receipt.delivered && receipt.result?.ok !== false) {
    return { code: 200, body: { ...receipt.result, duplicate: true } };
  }
  save(receiptPath, receipt);
  // A successful second hook confirms that clients have actually reloaded,
  // including when another importer already applied the announced changes.
  if (event.event === "import_succeeded" && (!receipt.refresh || !receipt.refresh.ok)) {
    // Reload readiness/config-watch races are retriable, not invalid imports.
    // Keep all attempts and notify only after bounded retries have completed.
    const deadline = Date.now() + config.refreshTimeoutSeconds * 1000;
    for (let attempt = 0; attempt < 3; attempt++) {
      const remaining = Math.floor((deadline - Date.now()) / 1000);
      if (remaining < 1) break;
      receipt.refresh = await run(config.refreshScript, [], remaining);
      receipt.refreshHistory = [...(receipt.refreshHistory ?? []),
        { ...receipt.refresh, at: new Date().toISOString() }].slice(-12);
      save(receiptPath, receipt);
      if (receipt.refresh.ok) break;
      api.logger.warn?.(`[litellm-free-hook] model reload attempt ${attempt + 1} failed, exit=${receipt.refresh.exitCode ?? "unknown"}`);
      if (attempt < 2) await new Promise(resolve => setTimeout(resolve, (attempt + 1) * 1000));
    }
  }
  const args = [config.notifyScript, "--route", config.notifyRoute, "--text", message(event, receipt.refresh),
    "--timeout", String(config.notifyTimeoutSeconds)];
  if (event.event === "import_succeeded" && receipt.refresh?.ok && event.diff.changed && config.attachDiff !== false) {
    const attachment = path.join(worker, "diffs", id, "litellm-free-diff.json");
    save(attachment, diffAttachment(event.diff));
    args.push("--attachment", attachment);
  }
  const delivery = await run("/usr/bin/python3", args, config.notifyTimeoutSeconds + 5);
  const refreshOk = receipt.refresh?.ok ?? true;
  const complete = delivery.ok && refreshOk;
  receipt.delivered = complete;
  receipt.result = { ok: complete, event: event.event,
    delivered: delivery.ok, refresh: receipt.refresh ?? { skipped: true }, event_id: id };
  receipt.updatedAt = new Date().toISOString();
  save(receiptPath, receipt);
  api.logger.info?.(`[litellm-free-hook] ${event.event} delivered=${delivery.ok} refresh=${receipt.refresh?.ok ?? "skipped"}`);
  return { code: complete ? 200 : 503, body: receipt.result };
}

export default {
  id: manifest.id,
  name: manifest.name,
  configSchema: manifest.configSchema,
  register(api) {
    api.registerHttpRoute({
      // This machine-to-machine endpoint authenticates its bearer directly.
      // It does not use proxy client-IP claims (rootless localhost forwarding).
      path: endpoint, auth: "plugin", match: "exact", replaceExisting: true,
      async handler(req, res) {
        if (!authorized(req)) {
          res.setHeader("www-authenticate", "Bearer");
          reply(res, 401, { ok: false, error: "unauthorized" });
          return true;
        }
        if (req.method !== "POST") {
          res.setHeader("allow", "POST");
          reply(res, 405, { ok: false, error: "method_not_allowed" });
          return true;
        }
        let event;
        try { event = await readBody(req); validate(event); }
        catch { reply(res, 400, { ok: false, error: "invalid_event" }); return true; }
        const work = queue.then(() => processEvent(api, event));
        queue = work.catch(() => {});
        try { const result = await work; reply(res, result.code, result.body); }
        catch (error) {
          api.logger.error?.(`[litellm-free-hook] ${error.name}: processing failed`);
          reply(res, 500, { ok: false, error: "processing_failed" });
        }
        return true;
      },
    });
  },
};
