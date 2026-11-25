/* 冷打开等待页的重连脚本（无依赖、原生 JS）。
 *
 * 只做一件事：同源探测 GET /api/app/ready，确认 HTTP 200 + JSON + ready === true
 * 之后才导航回管理页。页面本身不读 Cookie、不存用户数据、不重发任何业务请求。
 * 代理升级标记有效时，等待代理对这个探针直接回 {"ready": false}，不会把它排进
 * 180 秒队列；因此这里的退避探测是廉价的。
 */
(function () {
  'use strict';

  var READY_PATH = '/api/app/ready';
  var PROBE_TIMEOUT = 4000;
  var MIN_DELAY = 1000;
  var MAX_DELAY = 8000;
  var JITTER = 400;
  var ADMIN_PATH = '/admin';
  var ADMIN_PATH_SLASH = '/admin/';

  var timer = null;
  var attempt = 0;
  var probing = false;
  var stopped = false;
  var hint = document.getElementById('wb-waiting-hint');
  var button = document.getElementById('wb-waiting-retry');
  var meta = document.getElementById('wb-waiting-meta');

  /* 只保留安全的 tab/hash 选择：路径固定为 /admin 或 /admin/，tab 走白名单，
   * hash 只接受不含引号/尖括号/空白的短串。其它一律丢弃，绝不把本页当跳板。 */
  function safeTarget() {
    var target = location.pathname === ADMIN_PATH_SLASH ? ADMIN_PATH_SLASH : ADMIN_PATH;
    var tab = null;
    try {
      tab = new URLSearchParams(location.search).get('tab');
    } catch (error) {
      tab = null;
    }
    if (tab && /^[A-Za-z0-9_-]{1,32}$/.test(tab)) {
      target += '?tab=' + encodeURIComponent(tab);
    }
    var hash = location.hash;
    if (hash && /^#[A-Za-z0-9_=&%.-]{1,64}$/.test(hash)) {
      target += hash;
    }
    return target;
  }

  function setMeta(text) {
    if (!meta) return;
    if (!text) {
      meta.hidden = true;
      meta.textContent = '';
      return;
    }
    meta.hidden = false;
    meta.textContent = text;
  }

  function schedule() {
    if (stopped) return;
    attempt += 1;
    var delay = Math.min(MAX_DELAY, MIN_DELAY * Math.pow(2, attempt - 1));
    delay += Math.floor(Math.random() * JITTER);
    if (timer) clearTimeout(timer);
    timer = setTimeout(probe, delay);
  }

  function probe() {
    if (stopped || probing) return;
    probing = true;
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    var controller = new AbortController();
    var abort = setTimeout(function () { controller.abort(); }, PROBE_TIMEOUT);
    fetch(READY_PATH, {
      method: 'GET',
      cache: 'no-store',
      credentials: 'same-origin',
      redirect: 'error',
      headers: { Accept: 'application/json' },
      signal: controller.signal
    }).then(function (response) {
      if (!response.ok) return null;
      var type = response.headers.get('content-type') || '';
      if (type.indexOf('application/json') < 0) return null;
      return response.json();
    }).then(function (body) {
      clearTimeout(abort);
      var ready = !!body && typeof body === 'object' && body.ready === true;
      if (ready) {
        stopped = true;
        setMeta('');
        if (button) button.disabled = true;
        location.replace(safeTarget());
        return;
      }
      setMeta('已尝试 ' + attempt + ' 次，仍在等待服务恢复。');
      schedule();
    }).catch(function () {
      clearTimeout(abort);
      setMeta('已尝试 ' + attempt + ' 次，仍在等待服务恢复。');
      schedule();
    }).then(function () {
      probing = false;
    });
  }

  function retryNow() {
    if (stopped) return;
    attempt = 0;
    probe();
  }

  if (button) button.addEventListener('click', retryNow);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) retryNow();
  });
  window.addEventListener('online', retryNow);
  window.addEventListener('pagehide', function () {
    stopped = true;
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  });

  if (hint) hint.hidden = false;
  probe();
})();
