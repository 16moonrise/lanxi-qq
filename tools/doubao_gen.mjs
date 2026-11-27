#!/usr/bin/env node
/**
 * [fish-doubao 2026-11-18] 用网页版豆包生图 —— 命令行包装。
 *
 * 为什么要有这个：方舟（Ark/Seedream）被删掉后，她的画彻底断了。
 * 豆包网页版的生图能力已经在 DSH 插件 `dsh-doubao-image-free` 里跑通了
 * （纯 HTTP 反代，不需要开浏览器），这里把那套能力包成一条命令，
 * 让 AstrBot 的 draw_core 可以直接 subprocess 调用，不依赖 DSH 在不在。
 *
 * 用法：
 *   node doubao_gen.mjs --prompt "画面描述" --out "D:\path\out.jpg" [--size 1024x1024]
 *                       [--model "Seedream 5.0 Flash"] [--timeout 180000]
 *
 * 输出：一行 JSON（stdout）
 *   成功 {"ok":true,"path":...,"bytes":N,"mediaType":"image/jpeg","ratio":"1:1","model":"..."}
 *   失败 {"ok":false,"error":"..."}
 *
 * 登录态从 DSH 的凭据文件读（豆包账号 cookie / 设备身份 / msToken）。
 * cookie 过期时会返回 user invalid，需要在 DSH 设置页重新登录豆包。
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const PROF = process.env.DSH_PROFILE_MODULES
  || path.join(os.homedir(), ".dsh", "profiles", "desktop", "node_modules");
const CRED = process.env.DSH_CREDENTIALS || path.join(os.homedir(), ".dsh", ".credentials.yaml");
const LIB = path.join(PROF, "dsh-doubao-image-free/lib/doubao-image.mjs");

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith("--")) continue;
    const k = a.slice(2);
    const v = argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[++i] : "true";
    out[k] = v;
  }
  return out;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const prompt = String(args.prompt || "").trim();
  if (!prompt) throw new Error("缺少 --prompt");
  const out = path.resolve(String(args.out || "").trim() || `doubao-${Date.now()}.jpg`);

  const yaml = (await import(pathToFileURL(path.join(PROF, "js-yaml/dist/js-yaml.mjs")).href)).default;
  if (!fs.existsSync(CRED)) throw new Error(`找不到凭据文件：${CRED}`);
  const doc = yaml.load(fs.readFileSync(CRED, "utf8"));
  const payload = doc?.records?.["dsh-doubao-image/doubao-session"]?.payload || {};
  const accounts = Array.isArray(payload.accounts) ? payload.accounts : [];
  const acc = accounts.find((a) => a.id === payload.activeId) || accounts[0];
  if (!acc?.cookieHeader) throw new Error("没有豆包登录态：先在 DSH 设置页登录豆包");

  if (!fs.existsSync(LIB)) throw new Error(`找不到豆包生图库：${LIB}`);
  const { generateImage } = await import(pathToFileURL(LIB).href);

  const res = await generateImage({
    cookieHeader: acc.cookieHeader,
    prompt,
    size: args.size || "1024x1024",
    model: args.model || undefined,
    device: acc.device || {},
    msToken: acc.msToken || "",
    timeoutMs: Number(args.timeout || 180000),
    allowBrowserFallback: false,
  });

  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, res.data);
  process.stdout.write(JSON.stringify({
    ok: true,
    path: out,
    bytes: res.data.length,
    mediaType: res.mediaType,
    ratio: res.ratio,
    model: res.actualModel || "",
  }));
}

main().catch((e) => {
  const msg = String((e && e.message) || e)
    .replace(/\s+/g, " ")
    .slice(0, 300);
  process.stdout.write(JSON.stringify({ ok: false, error: msg }));
  process.exitCode = 1;
});
