// sidecar.js -- Marvis uskey 签名侧车（Wine + Windows node.exe 常驻）
//
// 协议：
//   GET  /health   -> {ok, qimei36, calls, busId, appVer}
//   POST /uskey    body {agentTag, conversationId, responseId, scene, model, nonce, openId}
//                  -> {uskey, qimei36, busInfo, uskeyLen, ms}
//
// 为什么是常驻进程：wine 冷启 1.1s，但进程内 getUskey 只要 ~0.3ms。
// 为什么必须重定向 stdout：wine node.exe 的 stdout 若是管道，会
//   "Error: open EBADF" 秒崩 —— 启动脚本一律 >>log 2>&1。
const http = require('http');
const b = require('./beacon_napi.node');

const PORT = parseInt(process.env.SIDECAR_PORT || '8791', 10);
const HOST = process.env.SIDECAR_HOST || '127.0.0.1';
const BUS_ID = process.env.SIDECAR_BUS_ID || '7800255';
const APP_KEY = process.env.SIDECAR_APP_KEY || '0WIN06Y06DKV1IUQ';
const APP_VER = process.env.SIDECAR_APP_VER || '1.60.3400.35';
const FIELDS = ['agentTag', 'conversationId', 'responseId', 'scene', 'model', 'nonce', 'openId'];

const sleep = ms => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
function log() {
  const a = Array.prototype.slice.call(arguments);
  try { console.log.apply(console, [new Date().toISOString()].concat(a)); }
  catch (e) { /* 没有 stdout 也要活着 */ }
}

let QIMEI = '';
let CALLS = 0;

function boot() {
  try { log('init ret=' + b.init({ appKey: APP_KEY, appVer: APP_VER })); }
  catch (e) { log('init ERR ' + ((e && e.message) || e)); }
  for (let i = 0; i < 100; i++) {
    let q = null;
    try { q = b.getQimei(); } catch (e) { log('getQimei ERR ' + ((e && e.message) || e)); }
    if (q) {
      QIMEI = (typeof q === 'object') ? String(q.qimei36 || q.qimei || '') : String(q);
      if (QIMEI) { break; }
    }
    sleep(100);
  }
  log('qimei36=' + QIMEI + ' len=' + QIMEI.length);
}

// bus_info 七字段固定顺序 —— 与上游口径一致，顺序/分隔符都不能动
function buildBusInfo(p) {
  return 'marvisAgentTag=' + p.agentTag +
    '&marvisConversationID=' + p.conversationId +
    '&marvisResponseID=' + p.responseId +
    '&marvisScene=' + p.scene +
    '&model=' + p.model +
    '&nonce=' + p.nonce +
    '&originOpenID=' + p.openId;
}

function send(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
    'Connection': 'keep-alive'
  });
  res.end(body);
}

const srv = http.createServer((req, res) => {
  const path = String(req.url || '').split('?')[0];
  if (req.method === 'GET' && (path === '/health' || path === '/')) {
    return send(res, QIMEI ? 200 : 503,
      { ok: !!QIMEI, qimei36: QIMEI, calls: CALLS, busId: BUS_ID, appVer: APP_VER });
  }
  if (req.method !== 'POST' || path !== '/uskey') {
    return send(res, 404, { error: 'not found' });
  }
  let buf = '';
  req.setEncoding('utf8');
  req.on('data', c => { buf += c; if (buf.length > 65536) { req.destroy(); } });
  req.on('end', () => {
    const t0 = Date.now();
    try {
      const p = JSON.parse(buf || '{}');
      if (!QIMEI) { throw new Error('qimei not ready'); }
      const busInfo = p.busInfo || buildBusInfo(p);
      if (!p.busInfo) {
        const missing = FIELDS.filter(k => !p[k]);
        if (missing.length) { throw new Error('missing ' + missing.join(',')); }
      }
      const uskey = b.getUskey(BUS_ID, busInfo) || '';
      if (!uskey) { throw new Error('getUskey returned empty'); }
      CALLS++;
      send(res, 200, {
        uskey: uskey, qimei36: QIMEI, busInfo: busInfo,
        uskeyLen: uskey.length, ms: Date.now() - t0
      });
    } catch (e) {
      log('ERR ' + ((e && e.message) || e));
      send(res, 500, { error: String((e && e.message) || e) });
    }
  });
});

boot();
srv.listen(PORT, HOST, () => log('listening ' + HOST + ':' + PORT + ' busId=' + BUS_ID));
