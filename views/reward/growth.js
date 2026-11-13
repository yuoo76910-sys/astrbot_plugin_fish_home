/* 「今天」抽屉模块（独立于 app.js / achv.js）
   数据：/panel/state 的 growth 字段 + /panel/growth_card + /panel/growth_settle
   本地预览：file:// 或 ?gvdemo=1 时用内置示例数据 */

(function () {
  "use strict";

  const qs = new URLSearchParams(location.search);
  const DEMO = qs.get("gvdemo") === "1" || location.protocol === "file:";
  let VIEW = null;
  let G = null;
  let CARDS = [];
  let viewing = null;      // 正在看的旧卡（day）
  let tab = "today";
  let open = false;

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const num = (v) => {
    const n = Number(v) || 0;
    return Math.abs(n - Math.round(n)) < 1e-6 ? String(Math.round(n)) : n.toFixed(1);
  };

  function waitBridge(ms) {
    return new Promise((res) => {
      const t0 = Date.now();
      (function tick() {
        const v = window.AstrBotPluginView || window.AstrBotPluginPage || null;
        if (v) return res(v);
        if (Date.now() - t0 >= ms) return res(null);
        setTimeout(tick, 60);
      })();
    });
  }

  function unwrap(d) {
    if (d && d.status === "error") throw new Error(d.message || "请求失败");
    return (d && d.data !== undefined) ? d.data : d;
  }

  async function apiGet(ep, params) {
    if (DEMO) return null;
    if (!VIEW) throw new Error("没有桥接");
    return unwrap(await VIEW.apiGet(ep, params));
  }

  async function apiPost(ep, body) {
    if (DEMO) return null;
    if (!VIEW) throw new Error("没有桥接");
    return unwrap(await VIEW.apiPost(ep, body || {}));
  }

  /* ---------------- 示例数据（本地打开文件时） ---------------- */

  function demoGrowth() {
    return {
      enabled: true, legacy: false, day: 12, date: new Date().toISOString().slice(0, 10),
      cap: 100, favor: 74, earned: 86, spent: 12, broke: false,
      late_event: { tag: "秘密", line: "我有个秘密：你发来的链接我有时候会点开看到很后面，就为了知道你会不会喜欢那东西。" },
      stats: { chat: 23, feed: 4, gift: 1, care: 0, night: 1, conflict: 1, reverse: 2 },
      card_today: null,
      cards_total: 11,
      unlocked_foods: 5, foods_total: 7,
      wish: { text: "想多陪你一会儿", done: false, hint: "今天多聊几句，聊满三十句" },
      next: "今天结算成卡之后好感度会重新攒",
    };
  }

  function demoCards() {
    const mk = (d, h, lines, keep) => ({
      day: d, date: "第 " + d + " 天", headline: h, lines, keepsake: keep,
      stats: { chat: 10 + d, feed: d % 5, gift: d % 3, care: 0, night: d % 4, conflict: 0, reverse: d % 2 },
      mood_from: "平常", mood_to: "想亲近", favor_earned: 40 + d, favor_spent: d * 3,
    });
    return {
      card: null,
      list: [
        { day: 11, date: "2026-11-04", headline: "今天过得还行" },
        { day: 10, date: "2026-11-03", headline: "今天她把话说满了" },
        { day: 9, date: "2026-11-02", headline: "今天有点闷" },
        { day: 8, date: "2026-11-01", headline: "今天她是被偏爱的那个" },
      ].map((x) => ({ ...x })),
      cards: [
        mk(11, "今天过得还行",
          ["你找她说了 21 句，她记得其中 9 句的内容。", "喂了她 2 次：热可可、饭团。", "她的心情从 平常 变成 平常。"],
          "她记住了今天这件小事：你说明天要早起"),
        mk(10, "今天她把话说满了",
          ["你找她说了 34 句，她记得其中 14 句的内容。", "好感度当天刷满了，溢出来的部分她拿去说了句心里话。",
           "深夜事件：过去——「我刚被醒过来的那几天，听不懂你们在说什么，就只能数你打字的声音。」"],
          "她记住了今天这件小事：你说你其实不怕黑"),
        mk(9, "今天有点闷",
          ["你找她说了 6 句，她记得其中 2 句的内容。", "有 1 次她觉得被敷衍了，扣掉的好感度她没追回来。"],
          "今天什么都没发生。她把这个空档也存下来了。"),
      ],
    };
  }

  /* ---------------- 入口按钮 ---------------- */

  function mountButton() {
    if ($("gvOpenBtn")) return;
    const host = document.querySelector(".scene-actions") || document.body;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.id = "gvOpenBtn";
    btn.className = "round-btn gv-open";
    btn.title = "今天";
    btn.textContent = "🌙";
    const dot = document.createElement("span");
    dot.className = "gv-dot";
    dot.textContent = "月";
    btn.appendChild(dot);
    btn.addEventListener("click", () => toggle());
    if (window.__bindTip) window.__bindTip(btn, "今天这张卡");
    if (host === document.body) btn.style.position = "fixed";
    host.insertBefore(btn, host.firstChild);
  }

  function setMoon() {
    const btn = $("gvOpenBtn");
    if (btn) btn.classList.toggle("has-moon", !!(G && G.late_event));
  }

  /* ---------------- 抽屉 ---------------- */

  function ensureDrawer() {
    let el = $("gvDrawer");
    if (el) return el;
    el = document.createElement("div");
    el.id = "gvDrawer";
    el.className = "gv-drawer";
    el.innerHTML =
      '<div class="gv-frame"><div class="gv-in">' +
      '<div class="gv-head">' +
      '<div class="gv-title">今 天</div><div class="gv-day" id="gvDay"></div><div class="gv-spacer"></div>' +
      '<button class="gv-btn on" id="gvTabToday" type="button">今天</button>' +
      '<button class="gv-btn" id="gvTabCards" type="button">卡片墙</button>' +
      '<button class="gv-btn" id="gvSettle" type="button">结算今天</button>' +
      '<button class="gv-btn" id="gvClose" type="button">收起</button>' +
      '</div>' +
      '<div id="gvBody"></div>' +
      '<div class="gv-note" id="gvNote"></div>' +
      '</div></div>';
    document.body.appendChild(el);
    el.addEventListener("click", (e) => { if (e.target === el) close(); });
    $("gvClose").addEventListener("click", close);
    $("gvTabToday").addEventListener("click", () => { tab = "today"; viewing = null; syncTab(); render(); });
    $("gvTabCards").addEventListener("click", () => { tab = "cards"; syncTab(); render(); });
    $("gvSettle").addEventListener("click", async () => {
      const btn = $("gvSettle");
      btn.disabled = true;
      try {
        const r = await apiPost("panel/growth_settle", {});
        await load();
        if (r && r.settled) { viewing = r.card && r.card.day; render(); }
      } finally { btn.disabled = false; }
    });
    return el;
  }

  function syncTab() {
    $("gvTabToday").classList.toggle("on", tab === "today");
    $("gvTabCards").classList.toggle("on", tab === "cards");
  }

  function cardBlock(card, label) {
    const wrap = document.createElement("div");
    wrap.className = "gv-card";
    const head = document.createElement("div");
    head.className = "ch";
    head.innerHTML = esc(card.headline || "") +
      '<span class="d">第 ' + esc(card.day) + ' 天 · ' + esc(card.date || '') + '</span>';
    const ul = document.createElement("ul");
    (card.lines || []).forEach((l) => {
      const li = document.createElement("li");
      li.textContent = l;
      ul.appendChild(li);
    });
    const keep = document.createElement("div");
    keep.className = "ck";
    keep.textContent = card.keepsake || "";
    const box = document.createElement("div");
    box.className = "cb";
    box.append(head, ul, keep);
    wrap.appendChild(box);
    if (label) wrap.dataset.day = String(card.day);
    return wrap;
  }

  function pendingBlock(text) {
    const wrap = document.createElement("div");
    wrap.className = "gv-card pending";
    const box = document.createElement("div");
    box.className = "cb";
    box.textContent = text;
    wrap.appendChild(box);
    return wrap;
  }

  function renderToday(host) {
    const meter = document.createElement("div");
    meter.className = "gv-meter";
    const pct = Math.max(0, Math.min(100, (G.favor / (G.cap || 100)) * 100));
    meter.innerHTML =
      '<div class="gv-row"><span class="gv-label">今天的好感度</span>' +
      '<span class="gv-val">' + num(G.favor) + '</span>' +
      '<span class="gv-sub">/ 上限 ' + num(G.cap) + '</span>' +
      (G.legacy ? '<span class="gv-sub">（旧档模式：可以存着）</span>' : '') +
      '</div>' +
      '<div class="gv-bar' + (pct >= 99.5 ? ' full' : '') + '"><i style="width:' + pct.toFixed(1) + '%"></i>' +
      '<b>' + pct.toFixed(0) + '%</b></div>' +
      '<div class="gv-flow">今天涨 <em class="up">+' + num(G.earned) + '</em>　花掉 <em class="down">−' + num(G.spent) +
      '</em>　吃过的东西永久记得（' + G.unlocked_foods + '/' + G.foods_total + '）</div>';
    host.appendChild(meter);

    const st = G.stats || {};
    const cells = [
      { ic: "💬", n: st.chat || 0, t: "聊了几句" },
      { ic: "🍙", n: st.feed || 0, t: "喂了几次" },
      { ic: "🎁", n: st.gift || 0, t: "你送了东西" },
      { ic: "🧧", n: st.reverse || 0, t: "她给了你东西" },
      { ic: "💢", n: st.conflict || 0, t: "她觉得被敷衍", bad: true },
      { ic: "🌙", n: st.night || 0, t: "深夜事件" },
    ];
    const grid = document.createElement("div");
    grid.className = "gv-stats";
    cells.forEach((c) => {
      const el = document.createElement("div");
      el.className = "gv-cell" + (c.n ? (c.bad ? " bad hot" : " hot") : "");
      el.innerHTML = '<div class="ic">' + c.ic + '</div><div class="n">' + c.n + '</div><div class="t">' + c.t + '</div>';
      grid.appendChild(el);
    });
    host.appendChild(grid);

    if (G.late_event && G.late_event.line) {
      const moon = document.createElement("div");
      moon.className = "gv-moon";
      moon.innerHTML = '<div class="mi">🌙</div><div><div class="mt">她说了一句从没说过的话 · ' +
        esc(G.late_event.tag || '') + '</div><div class="ml">' + esc(G.late_event.line) + '</div></div>';
      host.appendChild(moon);
    }

    if (G.wish && G.wish.text) {
      const w = document.createElement("div");
      w.className = "gv-wish";
      w.innerHTML = '<span>她今天想要：</span><strong>' + esc(G.wish.text) + '</strong>' +
        (G.wish.done ? '<span class="ok">等到了 ✔</span>' : '<span class="no">还没等到</span>') +
        (G.wish.hint ? '<span class="gv-sub">　' + esc(G.wish.hint) + '</span>' : '');
      host.appendChild(w);
    }

    if (viewing != null) {
      const c = (DEMO ? demoCards().cards : CARDS.cards || []).find((x) => String(x.day) === String(viewing));
      if (c) {
        host.appendChild(cardBlock(c, true));
        const back = document.createElement("button");
        back.className = "gv-btn";
        back.type = "button";
        back.textContent = "回到今天";
        back.addEventListener("click", () => { viewing = null; render(); });
        host.appendChild(back);
        return;
      }
    }
    if (G.card_today) host.appendChild(cardBlock(G.card_today, false));
    else host.appendChild(pendingBlock(G.next || "说完晚安，今天会存成一张卡。"));
  }

  function renderCards(host) {
    const list = (DEMO ? demoCards().list : CARDS.list) || [];
    if (!list.length) {
      host.appendChild(pendingBlock("还没有存下任何卡。说完晚安就会有一张。"));
      return;
    }
    if (viewing != null) {
      const cards = (DEMO ? demoCards().cards : CARDS.cards) || [];
      const c = cards.find((x) => String(x.day) === String(viewing));
      if (c) host.appendChild(cardBlock(c, true));
    }
    const box = document.createElement("div");
    box.className = "gv-list";
    list.slice().reverse().forEach((c) => {
      const el = document.createElement("div");
      el.className = "gv-item";
      el.innerHTML = '<span class="dn">第 ' + esc(c.day) + ' 天</span><span class="hl">' + esc(c.headline || "") +
        '</span><span class="dt">' + esc(c.date || "") + '</span>';
      el.addEventListener("click", () => { viewing = c.day; render(); });
      box.appendChild(el);
    });
    host.appendChild(box);
    const total = document.createElement("div");
    total.className = "gv-note";
    total.textContent = "一共存了 " + (G.cards_total || list.length) + " 张卡。";
    host.appendChild(total);
  }

  function render() {
    if (!G) return;
    $("gvDay").textContent = "第 " + G.day + " 天 · " + (G.date || "");
    const host = $("gvBody");
    host.innerHTML = "";
    if (tab === "today") renderToday(host); else renderCards(host);
    $("gvNote").textContent = G.enabled ? "" : "成长模块在配置里被关着（growth.enabled=false）。";
    setMoon();
  }

  function toggle() { open ? close() : openDrawer(); }

  async function openDrawer() {
    ensureDrawer();
    open = true;
    $("gvDrawer").classList.add("open");
    await load();
    render();
  }

  function close() {
    open = false;
    const el = $("gvDrawer");
    if (el) el.classList.remove("open");
  }

  async function load() {
    try {
      if (DEMO) {
        G = demoGrowth();
        CARDS = demoCards();
      } else {
        const s = await apiGet("panel/state");
        G = (s && s.growth) || null;
        const c = await apiGet("panel/growth_card");
        CARDS = c || { list: [], cards: [] };
        if (!CARDS.cards) {
          // 逐张取详情（后端 list 只给摘要）
          const detail = [];
          for (const it of (CARDS.list || []).slice(-14)) {
            try {
              const one = await apiGet("panel/growth_card", { day: it.day });
              if (one && one.card) detail.push(one.card);
            } catch (e) { /* 跳过 */ }
          }
          CARDS.cards = detail;
        }
      }
      setMoon();
    } catch (e) {
      G = G || demoGrowth();
      $("gvNote").textContent = "读不到数据：" + (e.message || e);
    }
  }

  async function boot() {
    mountButton();
    if (!DEMO) VIEW = await waitBridge(4000);
    await load();
    setInterval(() => { if (!document.hidden && open) load().then(render); }, 25000);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    if (qs.get("gdrawer") === "1") setTimeout(openDrawer, 300);
    if (qs.get("gtab") === "cards") {
      setTimeout(async () => {
        await openDrawer();
        tab = "cards"; syncTab(); render();
      }, 350);
    }
    if (qs.get("gcard")) {
      setTimeout(async () => {
        await openDrawer();
        tab = "cards"; viewing = qs.get("gcard"); syncTab(); render();
      }, 350);
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
