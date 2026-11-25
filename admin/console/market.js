/* ==========================================================================
   模型广场（参考 new-api 的 Model Square 卡片）
   --------------------------------------------------------------------------
   每张卡回答四个问题：**这个模型多少钱、现在能不能用、最近快不快、
   怎么调用**。数据分两路取：

     /api/stats/model-market     近 N 小时用量 + 定价 + 可用账号 + 时段状态轴
     /api/models/aggregate       跨平台归一、各平台的「完整调用名」

   为什么不在一个接口里全做完：聚合视图是模型配置页的既有能力（带区/别名
   归一逻辑），广场只借用它的 `client_id`，不重复实现一遍归一规则。

   ── 底部那排小方块（状态轴）到底是什么 ──────────────────────────────────
   一格 = 服务端切好的一段时间（24 小时档是 1 格 1 小时，7 天档是 1 格 6 小时）。
   格子边界按**北京时间整点**对齐，所以「早上 8 点那格」就是真的 8 点。

   格子的颜色不是写死的三色循环，而是这一格的实际结果：
     up 绿   有成功调用，成功率 ≥ 99.5%
     warn 黄 有调用但成功率 97%~99.5%（抖过）
     down 红 有调用但成功率 < 97%（这一格基本用不了）
     off 红描边 没有请求、但有账号在冷却（确定不可用）
     idle 灰 没有请求、也没有冷却记录（没流量 ≠ 坏了）

   判定顺序也是刻意的：**先用真实请求说话，再用冷却记录补空白**。反过来做会
   把「昨晚没人调用」误画成「昨晚挂了」。
   ========================================================================== */
(function () {
  "use strict";

  var STATE_TEXT = {
    up: "正常",
    warn: "抖动",
    down: "不可用",
    off: "冷却中",
    idle: "无流量",
  };
  var STATE_HINT = {
    up: "这一格有成功调用，成功率 ≥ 99.5%",
    warn: "这一格有调用，但成功率落在 97%~99.5%",
    down: "这一格有调用，成功率低于 97%",
    off: "这一格没有请求，但有账号在冷却 —— 这个时段确实用不了",
    idle: "这一格没有请求，也没有冷却记录（没流量不等于坏了）",
  };
  var STATE_ORDER = ["up", "warn", "down", "off", "idle"];

  var state = {
    hours: 24,
    platform: "",
    query: "",
    onlyAvailable: false,
    onlyTrouble: false,
    sort: "requests",     // requests | price | uptime | availability | name
    view: "cards",        // cards | list
    data: null,
    aggregate: null,
    loading: false,
    loaded: false,
    loadedAt: 0,
    seq: 0,
  };

  function el(id) { return document.getElementById(id); }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function toast(message, kind) {
    if (typeof window.toast === "function") window.toast(message, kind);
  }

  function num(value) {
    var n = Number(value) || 0;
    if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + "亿";
    if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(1) + "万";
    return n.toLocaleString("zh-CN");
  }

  function credit(value) {
    var n = Number(value);
    if (!Number.isFinite(n)) return "—";
    return n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function pct(value, digits) {
    if (value === null || value === undefined) return "—";
    return (Number(value) * 100).toFixed(digits == null ? 2 : digits) + "%";
  }

  function latency(ms) {
    var n = Number(ms) || 0;
    if (!n) return "—";
    if (n < 1000) return Math.round(n) + " ms";
    if (n < 60000) return (n / 1000).toFixed(2) + " 秒";
    return Math.floor(n / 60000) + " 分 " + Math.round((n % 60000) / 1000) + " 秒";
  }

  function clock(timestamp) {
    var date = new Date(timestamp || Date.now());
    if (isNaN(date.getTime())) return "—";
    return String(date.getHours()).padStart(2, "0") + ":" +
      String(date.getMinutes()).padStart(2, "0");
  }

  /**
   * 定价的三种形态，别只印一个数字：
   *   · 正倍率 → ×0.50（每千 Token 倍率）
   *   · 0 且全部平台都是 0 → 免费
   *   · 负倍率（本库里 -1 是「该平台没有倍率概念 / 按上游实际消耗扣积分」
   *     的哨兵，81 个模型在用）→ 按实际用量。直接把 -1 印成「×-1.00」
   *     既看不懂也不像价格。
   */
  function price(item) {
    if (item.free) return { text: "免费", hint: "0 倍率 · 本站不计费" };
    var prices = item.pricing || [];
    if (!prices.length) return { text: "未定价", hint: "还没配置倍率" };
    var min = Number(item.min_multiplier);
    if (!(min > 0)) return { text: "按实际用量", hint: "按上游返回的实际积分扣费" };
    return { text: "×" + min.toFixed(2), hint: "每千 Token 倍率" };
  }

  function priceText(item) { return price(item).text; }

  function rateClass(value) {
    if (value === null || value === undefined) return "wbd-neutral";
    if (value >= 0.995) return "wbd-ok";
    if (value >= 0.97) return "wbd-warn";
    return "wbd-danger";
  }

  /** 有没有配定价。老缓存里没有 `configured` 字段，用 pricing 兜底。 */
  function configured(item) {
    if (item.configured !== undefined && item.configured !== null) return !!item.configured;
    return !!(item.pricing && item.pricing.length);
  }

  /** 把 aggregate 的 client_id 按 canonical key 索引起来。 */
  function clientIds(aggregate) {
    var map = {};
    ((aggregate && aggregate.items) || []).forEach(function (item) {
      var key = item.key || item.name;
      var ids = (item.platforms || []).map(function (row) {
        return {
          platform: row.platform,
          platform_label: row.platform_label || row.platform,
          model_id: row.client_id || row.model_id,
          multiplier: row.multiplier,
        };
      });
      map[key] = ids;
    });
    return map;
  }

  function keyOf(item) {
    return String(item.key || item.name || "").toLowerCase();
  }

  function lookup(map, item) {
    var direct = map[item.key];
    if (direct) return direct;
    var target = keyOf(item);
    var found = null;
    Object.keys(map).forEach(function (key) {
      if (!found && key.toLowerCase() === target) found = map[key];
    });
    return found || [];
  }

  /**
   * 卡片左上角用什么图标：
   *   1) 模型品牌 logo（gpt / claude / deepseek / glm / kimi / qwen …）
   *   2) 只在单平台出现的模型 → 用平台真实 logo（那本来就是它的上游标识）
   *   3) 都认不出来 → 首字母
   *
   * `default` / `auto` 这两个 key 不能用：`default` 在图标表里是「本站默认图标」，
   * 画在模型卡上会被当成某个厂商的品牌（其实是通用兜底），所以当作「没认出来」。
   */
  function logoOf(item) {
    var haystack = [item.key, item.name].concat(item.models || []).join(" ");
    var brand = "";
    if (typeof window.modelIconKey === "function") {
      brand = window.modelIconKey(haystack) || "";
    }
    if (brand && brand !== "default" && typeof window.modelIcon === "function") {
      var markup = window.modelIcon(haystack, "wbm-logo-mark");
      if (markup) return { html: markup, brand: brand };
    }
    var platforms = item.platforms || [];
    if (platforms.length === 1 && window.WBPlatformIcons &&
        typeof window.WBPlatformIcons.has === "function" &&
        window.WBPlatformIcons.has(platforms[0])) {
      return { html: window.WBPlatformIcons.get(platforms[0]), brand: "" };
    }
    return { html: "", brand: "" };
  }

  function logoHtml(item) {
    var logo = logoOf(item);
    var letter = Array.from(String(item.name || "M"))[0].toUpperCase();
    return '<span class="wbm-logo" data-brand="' + esc(logo.brand || "none") +
      '" aria-hidden="true">' + (logo.html || "<b>" + esc(letter) + "</b>") + "</span>";
  }

  function chipIcon(platform) {
    if (!window.WBPlatformIcons || typeof window.WBPlatformIcons.has !== "function") return "";
    if (!window.WBPlatformIcons.has(platform)) return "";
    return window.WBPlatformIcons.get(platform);
  }

  /* ── 时段状态轴 ───────────────────────────────────────────────────────── */

  function buckets() {
    return (state.data && state.data.buckets) || [];
  }

  function cellOf(item, index) {
    var series = item.timeline || [];
    return series[index] || null;
  }

  function cellTip(item, cell, bucket) {
    if (!cell) return "";
    var parts = [STATE_TEXT[cell.state] || cell.state];
    if (cell.requests) {
      parts.push(num(cell.requests) + " 次");
      if (cell.success_rate != null) parts.push("成功率 " + pct(cell.success_rate, 1));
      if (cell.avg_latency_ms) parts.push("平均 " + latency(cell.avg_latency_ms));
      if (cell.cooling) parts.push(cell.cooling + " 个账号在冷却");
    } else if (cell.cooling) {
      parts.push(cell.cooling + " 个账号在冷却");
    } else {
      parts.push("没有请求");
    }
    var description = (bucket && bucket.tip ? bucket.tip : "") + " · " + parts.join(" · ") +
      "\n" + (STATE_HINT[cell.state] || "");
    if (cell.coverage != null && cell.coverage < 0.999) {
      description += "\n这一格只统计了窗口内的 " +
        Math.max(1, Math.round(cell.coverage * 100)) + "% 时长";
    }
    return { title: (bucket ? bucket.label : "") + " · " + parts[0], description: description };
  }

  function timelineCells(item, mini) {
    var list = buckets();
    if (!list.length) return '<div class="wbm-axis-empty">服务端未返回时段数据</div>';
    var html = "";
    for (var i = 0; i < list.length; i += 1) {
      var cell = cellOf(item, i);
      var kind = cell ? cell.state : "idle";
      var tip = cellTip(item, cell, list[i]);
      if (cell && !cell.requests && kind === "idle") {
        // 空格子也要能看出「这一格是空的」，但不必占用 tooltip 的注意力
        tip = { title: list[i].label + " · 无流量", description: (list[i].tip || "") + "\n" + STATE_HINT.idle };
      }
      html += '<i class="wbm-cell is-' + kind + (cell && cell.coverage < 0.999 ? " is-partial" : "") +
        '" data-tooltip="' + esc(tip.title) + '" data-tooltip-description="' +
        esc(tip.description) + '" style="--cov:' +
        (cell && cell.coverage != null ? cell.coverage : 1) + '"></i>';
    }
    return '<div class="wbm-cells' + (mini ? " is-mini" : "") + '" role="img" aria-label="' +
      esc(stripLabel(item)) + '">' + html + "</div>";
  }

  function stripLabel(item) {
    var cells = item.cells || {};
    var list = buckets();
    var parts = [];
    STATE_ORDER.forEach(function (key) {
      if (cells[key]) parts.push(cells[key] + " 格" + STATE_TEXT[key]);
    });
    return "近 " + windowLabel() + "时段状态，共 " +
      list.length + " 格：" + (parts.join("、") || "暂无数据");
  }

  function stripScale() {
    var list = buckets();
    if (list.length < 2) return "";
    var marks = [list[0], list[Math.floor((list.length - 1) / 2)], list[list.length - 1]];
    return '<div class="wbm-axis-scale">' + marks.map(function (bucket) {
      return "<span>" + esc(bucket.label) + "</span>";
    }).join("") + "</div>";
  }

  function uptimeTone(value) {
    if (value === null || value === undefined) return "wbd-neutral";
    if (value >= 0.999) return "wbd-ok";
    if (value >= 0.97) return "wbd-warn";
    return "wbd-danger";
  }

  /** 窗口的人话写法：24/72/168 小时 → 24 小时 / 3 天 / 7 天。 */
  function windowLabel() {
    var hours = Number(state.data.window_hours || state.hours) || 24;
    return hours % 24 === 0 && hours >= 48 ? (hours / 24) + " 天" : hours + " 小时";
  }

  /** 卡片小标题里的窗口标记：24h / 3d / 7d（窄，不抢数字的位置）。 */
  function windowTag() {
    var hours = Number(state.data.window_hours || state.hours) || 24;
    return hours % 24 === 0 && hours >= 48 ? (hours / 24) + "d" : hours + "h";
  }

  function axisBlock(item) {
    var list = buckets();
    if (!list.length) return "";
    var measured = item.measured_cells;
    var head = '<div class="wbm-axis-head"><span>近 ' + windowLabel() + " · 每格 " +
      Number(state.data.bucket_hours || 1) + " 小时</span>" +
      '<b class="' + uptimeTone(item.uptime) + '">' +
      (measured ? "时段可用 " + pct(item.uptime, item.uptime >= 0.9995 ? 0 : 1) : "窗口内无调用") +
      "</b></div>";
    return '<div class="wbm-axis">' + head + timelineCells(item, false) + stripScale() + "</div>";
  }

  /* ── 卡片 / 列表行 ───────────────────────────────────────────────────── */

  function platformChips(item) {
    var platforms = item.platforms || [];
    var labels = item.platform_labels || platforms;
    if (!platforms.length) return "";
    // 只铺 3 个，多出来的折成「+N」：chip 换行会把价格和数字整体顶下去，
    // 同一排卡片就对不齐了（这比「少显示两个平台名」严重得多）。
    var shown = platforms.slice(0, 3);
    var rest = platforms.length - shown.length;
    var html = shown.map(function (value, index) {
      return '<span class="wbm-chip" title="' + esc(labels[index] || value) + '">' +
        chipIcon(value) + "<span>" + esc(labels[index] || value) + "</span></span>";
    }).join("");
    if (rest > 0) {
      var extra = platforms.slice(3).map(function (value, index) {
        return labels[3 + index] || value;
      });
      html += '<span class="wbm-chip is-more" title="' + esc(extra.join(" · ")) + '">+' +
        rest + "</span>";
    }
    return html;
  }

  function availability(item) {
    var capacity = Math.max(0, item.active_accounts - item.exhausted_accounts);
    if (!configured(item)) return { tone: "neutral", text: "未配置价格" };
    if (item.available) return { tone: "ok", text: "可用账号 " + capacity };
    return { tone: "danger", text: "当前不可用" };
  }

  function card(item, ids) {
    var primary = ids[0] || { model_id: (item.models || [])[0] || item.key, platform_label: "" };
    var capacity = availability(item);
    var pricing = price(item);
    return '<article class="wbm-card' + (item.available ? "" : " is-off") + '">' +
      '<header class="wbm-card-head">' + logoHtml(item) +
        '<div class="wbm-title"><h3 title="' + esc(item.name) + '">' + esc(item.name) + "</h3>" +
          '<div class="wbm-chips">' + platformChips(item) + "</div></div>" +
        '<button type="button" class="wbm-copy" title="复制调用名" data-copy="' +
          esc(primary.model_id) + '">复制</button>' +
      "</header>" +
      '<div class="wbm-price"><b>' + esc(pricing.text) + "</b><span>" +
        esc(pricing.hint) + "</span>" +
        '<em title="窗口内消耗积分">' + credit(item.credits) + " 积分</em></div>" +
      '<dl class="wbm-stats">' +
        "<div><dt>" + windowTag() + " 请求</dt><dd>" + num(item.requests) + "</dd></div>" +
        "<div><dt>成功率</dt><dd class='" + rateClass(item.success_rate) + "'>" + pct(item.success_rate) + "</dd></div>" +
        "<div><dt>首字</dt><dd>" + latency(item.avg_ttfb_ms) + "</dd></div>" +
        "<div><dt>吞吐</dt><dd>" + (item.tps ? item.tps.toFixed(1) + " t/s" : "—") + "</dd></div>" +
      "</dl>" +
      axisBlock(item) +
      '<footer class="wbm-foot">' +
        '<span class="wbm-avail is-' + capacity.tone + '">' + esc(capacity.text) + "</span>" +
        (item.exhausted_accounts ? "<span>额度耗尽 " + item.exhausted_accounts + "</span>" : "") +
        '<code title="' + esc(primary.model_id) + '">' + esc(primary.model_id) + "</code>" +
      "</footer>" +
      "</article>";
  }

  function listRow(item, ids) {
    var primary = ids[0] || {};
    var capacity = availability(item);
    var tone = { ok: "wbd-ok", danger: "wbd-danger", neutral: "wbd-neutral" }[capacity.tone] || "wbd-neutral";
    return "<tr>" +
      '<td><div class="wbm-name">' + esc(item.name) + "</div>" +
        '<div class="wbm-sub">' + esc((item.platform_labels || []).join(" · ")) + "</div></td>" +
      "<td>" + esc(priceText(item)) + "</td>" +
      "<td class='num'>" + num(item.requests) + "</td>" +
      "<td class='num " + rateClass(item.success_rate) + "'>" + pct(item.success_rate) + "</td>" +
      "<td class='num'>" + latency(item.avg_ttfb_ms) + "</td>" +
      "<td class='num'>" + (item.tps ? item.tps.toFixed(1) : "—") + "</td>" +
      "<td class='num'>" + credit(item.credits) + "</td>" +
      '<td class="wbm-tl-cell">' + timelineCells(item, true) + "</td>" +
      "<td class='num " + uptimeTone(item.uptime) + "'>" +
        (item.measured_cells ? pct(item.uptime, item.uptime >= 0.9995 ? 0 : 1) : "—") + "</td>" +
      "<td class='" + tone + "'>" + esc(capacity.text) + "</td>" +
      "<td><code class='wbm-code'>" + esc(primary.model_id || "") + "</code></td>" +
      "</tr>";
  }

  /* ── 过滤 / 排序 / 汇总 ───────────────────────────────────────────────── */

  function troubleCells(item) {
    var cells = item.cells || {};
    return (cells.down || 0) + (cells.off || 0);
  }

  function visible(items) {
    var query = state.query.trim().toLowerCase();
    var out = items.filter(function (item) {
      if (state.onlyAvailable && !item.available) return false;
      if (state.onlyTrouble && !troubleCells(item)) return false;
      if (state.platform && (item.platforms || []).indexOf(state.platform) < 0) return false;
      if (!query) return true;
      var haystack = [item.name, item.key].concat(item.models || []).join(" ").toLowerCase();
      return haystack.indexOf(query) >= 0;
    });
    var sorters = {
      requests: function (a, b) { return b.requests - a.requests; },
      // 负倍率是「按实际用量」的哨兵，排序时当成未知排到最后，别占着最便宜的位置。
      price: function (a, b) {
        var left = Number(a.min_multiplier) > 0 ? Number(a.min_multiplier) : 1e9;
        var right = Number(b.min_multiplier) > 0 ? Number(b.min_multiplier) : 1e9;
        return left - right;
      },
      // 可用率排序把「最难用」的顶到最前面：null（窗口内没调用）排最后。
      uptime: function (a, b) {
        var left = a.uptime == null ? 2 : a.uptime;
        var right = b.uptime == null ? 2 : b.uptime;
        return left - right;
      },
      availability: function (a, b) {
        return (b.active_accounts - b.exhausted_accounts) - (a.active_accounts - a.exhausted_accounts);
      },
      name: function (a, b) { return String(a.name).localeCompare(String(b.name), "zh-CN"); },
    };
    return out.sort(sorters[state.sort] || sorters.requests);
  }

  function legend() {
    return '<span class="wbm-legend">' + STATE_ORDER.map(function (key) {
      return '<span class="wbm-legend-item"><i class="wbm-cell is-' + key + '"></i>' +
        STATE_TEXT[key] + "</span>";
    }).join("") + "</span>";
  }

  function summary(rows) {
    var data = state.data;
    var bucketHours = Number(data.bucket_hours || 1);
    var trouble = rows.filter(function (item) { return troubleCells(item); }).length;
    return '<div class="wbm-summary">' +
      "<span>窗口 " + windowLabel() + " · 每格 " + bucketHours + " 小时（北京时间）</span>" +
      "<b>" + rows.length + "</b><span>个模型 · 可用 " +
      rows.filter(function (item) { return item.available; }).length +
      (trouble ? " · <em>有故障时段 " + trouble + "</em>" : "") +
      " · 数据源 " + (data.source === "rollup" ? "小时汇总" : "原始日志") +
      (state.loadedAt ? " · 更新于 " + clock(state.loadedAt) : "") + "</span>" +
      legend() + "</div>";
  }

  function render() {
    var host = el("wbmRoot");
    if (!host || !state.data) return;
    var ids = clientIds(state.aggregate);
    var rows = visible(state.data.items || []);
    var head = summary(rows);

    if (!rows.length) {
      host.innerHTML = head + '<div class="wbd-empty">没有符合条件的模型</div>';
      return;
    }
    if (state.view === "list") {
      host.innerHTML = head + '<div class="wbm-table-wrap"><table class="tbl wbm-table"><thead><tr>' +
        "<th>模型</th><th>价格</th><th class='num'>请求</th><th class='num'>成功率</th>" +
        "<th class='num'>首字</th><th class='num'>吞吐</th><th class='num'>积分</th>" +
        "<th>时段</th><th class='num'>可用率</th><th>状态</th><th>调用名</th></tr></thead><tbody>" +
        rows.map(function (item) { return listRow(item, lookup(ids, item)); }).join("") +
        "</tbody></table></div>";
      return;
    }
    host.innerHTML = head + '<div class="wbm-grid">' +
      rows.map(function (item) { return card(item, lookup(ids, item)); }).join("") + "</div>";
  }

  async function load(force) {
    var host = el("wbmRoot");
    if (!host) return;
    if (!force && state.loaded) { render(); return; }
    var seq = ++state.seq;
    state.loading = true;
    if (!state.loaded) {
      host.innerHTML = '<div class="wbm-grid">' +
        new Array(7).join('<div class="wbm-card wbm-skeleton"></div>') + "</div>";
    }
    try {
      var results = await Promise.all([
        api("/api/stats/model-market?hours=" + state.hours),
        api("/api/models/aggregate").catch(function () { return null; }),
      ]);
      // 先读文本再解析：500 时服务端返回的是纯文本 "Internal Server Error"，
      // 直接 `response.json()` 会抛 SyntaxError，把真实错误盖成一句看不懂的话。
      var rawMarket = await results[0].text();
      var market;
      try { market = JSON.parse(rawMarket); }
      catch (_) { market = { detail: rawMarket.slice(0, 160) || ("HTTP " + results[0].status) }; }
      var aggregate = null;
      if (results[1]) {
        var rawAggregate = await results[1].text();
        try { aggregate = JSON.parse(rawAggregate); } catch (_) { aggregate = null; }
      }
      if (seq !== state.seq) return;
      if (!results[0].ok) throw new Error(market.detail || ("HTTP " + results[0].status));
      state.data = market;
      state.aggregate = aggregate;
      state.loaded = true;
      state.loadedAt = Date.now();
      render();
      bindFilters();
    } catch (error) {
      if (seq !== state.seq) return;
      host.innerHTML = '<div class="wbd-error">模型广场加载失败：' + esc(error.message) + "</div>";
      toast("模型广场加载失败", "err");
    } finally {
      if (seq === state.seq) state.loading = false;
    }
  }

  function setHours(hours) {
    state.hours = Number(hours) || 24;
    document.querySelectorAll("[data-wbm-hours]").forEach(function (button) {
      button.classList.toggle("active", Number(button.dataset.wbmHours) === state.hours);
    });
    load(true);
  }

  function setView(view) {
    state.view = view === "list" ? "list" : "cards";
    document.querySelectorAll("[data-wbm-view]").forEach(function (button) {
      button.classList.toggle("active", button.dataset.wbmView === state.view);
    });
    render();
  }

  function bindFilters() {
    var search = el("wbmSearch");
    if (search && !search.dataset.bound) {
      search.dataset.bound = "1";
      search.addEventListener("input", function () { state.query = search.value; render(); });
    }
    var available = el("wbmAvailable");
    if (available && !available.dataset.bound) {
      available.dataset.bound = "1";
      available.addEventListener("change", function () {
        state.onlyAvailable = available.checked;
        render();
      });
    }
    var trouble = el("wbmTrouble");
    if (trouble && !trouble.dataset.bound) {
      trouble.dataset.bound = "1";
      trouble.addEventListener("change", function () {
        state.onlyTrouble = trouble.checked;
        render();
      });
    }
    var platform = el("wbmPlatformSel");
    if (platform) {
      var values = [""];
      var labels = {};
      ((state.data && state.data.items) || []).forEach(function (item) {
        (item.platforms || []).forEach(function (value, index) {
          if (value && values.indexOf(value) < 0) values.push(value);
          labels[value] = (item.platform_labels || [])[index] || value;
        });
      });
      // 平台列表会随数据变化（新平台接入 / 某平台清空），所以按内容指纹决定要不要重建，
      // 而不是只在第一次绑定时渲染一次 —— 否则下拉里会一直缺新平台。
      var fingerprint = values.join("|");
      if (platform.dataset.fingerprint !== fingerprint) {
        platform.dataset.fingerprint = fingerprint;
        platform.innerHTML = uiSelectHTML("wbmPlatform", {
          value: state.platform,
          options: values.map(function (value) {
            return { value: value, label: value ? (labels[value] || value) : "全部平台" };
          }),
          onChange: function (value) { state.platform = value; render(); },
        });
      }
    }
    var sort = el("wbmSortSel");
    if (sort && !sort.dataset.bound) {
      sort.dataset.bound = "1";
      sort.innerHTML = uiSelectHTML("wbmSort", {
        value: state.sort,
        options: [
          { value: "requests", label: "按调用量" },
          { value: "price", label: "按价格" },
          { value: "uptime", label: "按时段可用率" },
          { value: "availability", label: "按可用账号" },
          { value: "name", label: "按名称" },
        ],
        onChange: function (value) { state.sort = value; render(); },
      });
    }
  }

  document.addEventListener("click", function (event) {
    var target = event.target.closest && event.target.closest("[data-copy]");
    if (!target) return;
    var text = target.dataset.copy;
    var write = navigator.clipboard && navigator.clipboard.writeText
      ? navigator.clipboard.writeText(text)
      : Promise.reject(new Error("no clipboard"));
    write.then(function () { toast("已复制 " + text, "ok"); })
      .catch(function () { toast("复制失败，请手动选择 " + text, "err"); });
  });

  window.WBModelMarket = {
    load: load,
    render: function () { bindFilters(); render(); },
    bindFilters: bindFilters,
    setHours: setHours,
    setView: setView,
    state: state,
    /** 供 UI 自检脚本断言用：当前渲染出的格子状态序列。 */
    debugStates: function () {
      return ((state.data && state.data.items) || []).map(function (item) {
        return { key: item.key, states: (item.timeline || []).map(function (cell) { return cell.state; }) };
      });
    },
  };
})();
