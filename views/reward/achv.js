/* 成就板 + 解锁动画模块（独立于 app.js，不共享状态）
   数据：/panel/achv (GET) 与 /panel/achv_pop (POST)
   本地预览：?avdemo=1 或 file:// 打开时用内置示例数据 */

(function () {
  "use strict";

  const DEMO = new URLSearchParams(location.search).get("avdemo") === "1" || location.protocol === "file:";
  let VIEW = null;
  let DATA = null;
  let boardOpen = false;
  let filter = "all";
  let known = null;          // 已见过的成就 id，用来判断「刚刚解锁」
  let bannerQueue = [];
  let bannerBusy = false;
  let pollTimer = 0;

  /* ---------------- 工具 ---------------- */

  const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const fmt = (v) => {
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

  async function apiGet(ep) {
    if (DEMO) return demoPayload();
    if (!VIEW) throw new Error("没有桥接");
    return unwrap(await VIEW.apiGet(ep));
  }

  async function apiPost(ep, body) {
    if (DEMO) { demoConsume(); return null; }
    if (!VIEW) throw new Error("没有桥接");
    return unwrap(await VIEW.apiPost(ep, body || {}));
  }

  /* ---------------- 示例数据（本地预览用） ---------------- */

  function demoPayload() {
    const F = (id, name, desc, icon, done, prog, conds, hidden) => ({
      id, name: hidden && !done && prog < 0.5 ? "？？？" : name,
      desc: hidden && !done && prog < 0.5 ? "还不知道是什么。" : desc,
      icon: hidden && !done && prog < 0.5 ? "？" : icon,
      icon_kind: /\.png$/.test(icon) ? "furniture" : "emoji",
      done, ts: done ? Date.now() / 1000 - 3600 : 0,
      hidden: !!hidden, teased: !hidden || done || prog >= 0.5,
      progress: done ? 1 : prog,
      conditions: (hidden && !done && prog < 0.5) ? [] : (conds || []),
    });
    const C = (s, t) => ({ text: s + " ≥ " + t, stat: s, current: 0, target: t, met: false, progress: 0 });
    const cats = [
      { key: "feed", title: "喂食", done: 4, total: 6, items: [
        F("feed_1", "第一口", "喂她吃第一样东西。", "achv_feed_1.png", true, 1),
        F("feed_10", "投喂十次", "累计喂满 10 次。", "achv_feed_10.png", true, 1),
        F("feed_30", "喂了三十次", "累计喂满 30 次，她已经记住你的口味。", "achv_feed_30.png", false, .4, [C("fed_total", 30)]),
        F("feed_all7", "都尝过了", "七种食物每种都喂过一次。", "achv_feed_all7.png", false, .71, [C("foods_distinct", 7)]),
        F("feed_streak7", "每天那根小鱼干", "连续 7 天都来找她。", "achv_feed_streak7.png", true, 1),
        F("feed_spend40", "舍得花钱", "一天为她花掉 40 点好感度。", "achv_feed_spend40.png", true, 1),
      ]},
      { key: "chat", title: "陪伴", done: 3, total: 5, items: [
        F("chat_10", "聊满十句", "累计聊天 10 句。", "achv_chat_10.png", true, 1),
        F("chat_100", "一百句", "累计聊天 100 句。", "achv_chat_100.png", true, 1),
        F("chat_500", "话痨", "累计聊天 500 句。", "achv_chat_500.png", false, .34, [C("chat_total", 500)]),
        F("chat_streak7", "一星期没缺席", "连续 7 天都来找她。", "achv_chat_streak7.png", true, 1),
        F("chat_streak30", "一个月没缺席", "连续 30 天都来找她。", "achv_chat_streak30.png", false, .23, [C("streak", 30)]),
      ]},
      { key: "meters", title: "亲密与积累", done: 4, total: 6, items: [
        F("m_round1", "攒满一轮", "任意一条进度攒满一次。", "achv_m_round1.png", true, 1),
        F("m_int3", "三层亲密", "亲密度槽攒到 3 层。", "achv_m_int3.png", true, 1),
        F("m_int10", "十层亲密", "亲密度槽攒到 10 层。", "achv_m_int10.png", false, .3, [C("level", 10)]),
        F("m_joy5", "她很开心", "喜悦槽攒到 5 层。", "achv_m_joy5.png", true, 1),
        F("m_soothe3", "被她信任", "安抚槽攒到 3 层。", "achv_m_soothe3.png", false, .33, [C("level_soothe", 3)]),
        F("m_wish90", "心愿很高", "心愿值最高冲到过 90。", "achv_m_wish90.png", true, 1),
      ]},
      { key: "room", title: "小窝", done: 5, total: 7, items: [
        F("r_bowl", "食盆", "第一次喂她，她就有了这个盆。", "achv_r_bowl.png", true, 1),
        F("r_rug", "拼接地毯", "四条槽各攒满一次，布是她自己缝的。", "achv_r_rug.png", true, 1),
        F("r_bili", "B站观众牌", "让她刷到过一条视频。", "achv_r_bili.png", true, 1),
      ]},
      { key: "favor", title: "好感与回赠", done: 4, total: 6, items: [
        F("f_100", "好感度满格", "好感度冲到过 100。", "achv_f_100.png", false, .88, [C("favor_peak", 100)]),
        F("f_60", "她挺喜欢你", "好感度达到 60。", "achv_f_60.png", true, 1),
        F("f_wish1", "猜中心愿", "完成一次「她今天想要」。", "achv_f_wish1.png", true, 1),
        F("f_wish7", "一周都猜中", "累计完成 7 次今日心愿。", "achv_f_wish7.png", false, .57, [C("daily_wishes_done", 7)]),
        F("f_rev1", "她送你东西", "她主动回赠过一次礼物。", "achv_f_rev1.png", true, 1),
        F("f_revgood", "送的是好东西", "她心情不错的时候送了你一份。", "achv_f_revgood.png", true, 1),
      ]},
      { key: "secret", title: "藏起来的东西", done: 1, total: 4, items: [
        F("s_pet100", "摸了一百次头", "点她摸头累计 100 次。", "achv_s_pet100.png", false, .4, [C("pet_total", 100)], true),
        F("s_night3", "深夜还聊", "晚上十点后陪她说过 3 个不同的夜晚。", "achv_s_night3.png", true, 1, [], true),
        F("s_blame3", "裂掉的心", "连续被她记下的敷衍/假安慰攒到 3 次。", "achv_s_blame3.png", false, .33, [], true),
        F("s_all", "小窝的全部", "把上面所有成就都点亮。", "achv_s_all.png", false, .62, [C("ach_done_others", 33)], true),
      ]},
    ];
    return {
      title: "鱼的窝", character: { name: "她" },
      summary: { done: 21, total: 34, pct: 21 / 34 },
      counters: { favor: 62, favor_peak: 88, level: 3, chat_total: 168, streak: 7, furniture: 5, fed_total: 12 },
      categories: cats,
      pop: DEMO ? [] : [],
    };
  }

  function demoConsume() { /* 本地预览没有存档 */ }

  /* ---------------- 入口按钮 ---------------- */

  function mountButton() {
    if (document.getElementById("avOpenBtn")) return;
    const host = document.querySelector(".scene-actions") || document.body;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.id = "avOpenBtn";
    btn.className = "round-btn av-open";
    btn.title = "成就板";
    btn.textContent = "🏆";
    const dot = document.createElement("span");
    dot.className = "av-dot";
    dot.textContent = "0";
    btn.appendChild(dot);
    btn.addEventListener("click", openBoard);
    if (window.__bindTip) window.__bindTip(btn, "成就板");
    if (host === document.body) btn.style.position = "fixed";
    host.insertBefore(btn, host.firstChild);
  }

  function setBadge(n) {
    const btn = document.getElementById("avOpenBtn");
    if (!btn) return;
    const dot = btn.querySelector(".av-dot");
    if (dot) dot.textContent = String(n);
    btn.classList.toggle("has-new", n > 0);
  }

  // 板内动画：所有「板里没看过」的都播，间隔 320ms；超过 10 个才把多余的静默标记
  const BOARD_MAX = 10;
  const BOARD_STEP = 320;
  let pendingAnim = new Set();   // 这次要「从锁着演到解锁」的格子
  const LOG = [];
  function avlog(msg) {
    try {
      if (!new URLSearchParams(location.search).get("avdebug")) return;
      LOG.push(Math.round(performance.now()) + "ms " + msg);
      document.title = "AVLOG " + LOG.slice(-7).join(" | ");
    } catch (e) { /* 忽略 */ }
  }



  function boardPending() {
    return ((DATA && DATA.pop) || []).filter((p) => p && !p.board_seen);
  }

  function latestDoneId() {
    let best = null;
    ((DATA && DATA.categories) || []).forEach((c) => {
      (c.items || []).forEach((it) => {
        if (it && it.done && (!best || (it.ts || 0) > (best.ts || 0))) best = it;
      });
    });
    return best ? best.id : null;
  }

  function playBoardQueue() {
    const rows = boardPending();
    avlog("playBoardQueue 未播 " + rows.length + " 条");
    if (!rows.length) return;   // 没有新解锁就不播：只给真的刚解锁的格子做动画
    const show = rows.slice(-BOARD_MAX);
    const overflow = rows.slice(0, Math.max(0, rows.length - BOARD_MAX));
    const ids = show.map((p) => p.id);
    pendingAnim = new Set(ids);
    render();                       // 先画成「还锁着」
    ids.forEach((id, i) => playTileUnlock(id, 340 + i * BOARD_STEP));
    setTimeout(() => { pendingAnim = new Set(); }, 340 + ids.length * BOARD_STEP + 2600);
    // 播过的 + 排不下的都算看过，下次开板不重播
    apiPost("panel/achv_pop", { ids: rows.map((p) => p.id), channel: "board" }).catch(() => {});
    (DATA.pop || []).forEach((p) => { if (p) p.board_seen = true; });
    setBadge(0);
  }

  async function testUnlock(wantId) {
    avlog("testUnlock 进入 wantId=" + (wantId || "自动"));
    const btn = document.getElementById("avTest");
    if (btn) btn.disabled = true;
    try {
      if (DEMO) {
        // 本地样稿：直接把第一个没解锁的点亮
        let pick = null;
        ((DATA && DATA.categories) || []).forEach((c) => {
          (c.items || []).forEach((it) => { if (!pick && !it.done) { it.done = true; it.progress = 1; pick = it; } });
        });
        if (pick) {
          pendingAnim = new Set([pick.id]);
          render();
          playTileUnlock(pick.id, 340);
          setTimeout(() => { pendingAnim = new Set(); }, 3200);
        }
        return;
      }
      const r = await apiPost("panel/achv_test", wantId ? { id: String(wantId) } : {});
      avlog("achv_test 返回 " + JSON.stringify(r));
      const next = await apiGet("panel/achv");
      DATA = next;
      const nowDone = new Set();
      (next.categories || []).forEach((c) => (c.items || []).forEach((i) => { if (i.done) nowDone.add(i.id); }));
      known = nowDone;
      const id = (r && r.id) || latestDoneId();
      avlog("testUnlock 目标 " + id + " replay=" + !!(r && r.replay));
      if (id) pendingAnim.delete(id);   // 重播前先摘掉，避免被画回锁着
      render();
      if (!id) return;
      if (r && r.replay) {
        // 这个成就本来就已解锁：只闪一下做回顾，不假装它还是锁着的
        const el = document.querySelector('#avBoard .av-ach[data-id="' + id + '"]');
        if (el) {
          el.classList.add('justunlocked');
          confetti(el);
          const rb = document.getElementById('avRibbon');
          if (rb) { rb.classList.remove('show'); void rb.offsetWidth; rb.classList.add('show'); setTimeout(() => rb.classList.remove('show'), 1700); }
          setTimeout(() => el.classList.remove('justunlocked'), 2600);
        }
        return;
      }
      pendingAnim = new Set([id]);
      render();                 // 真新解锁：先画成锁着，再翻成解锁
      playTileUnlock(id, 340);
      setTimeout(() => { pendingAnim = new Set(); }, 3200);
    } catch (e) {
      avlog('testUnlock 异常 ' + (e && e.message ? e.message : String(e)));
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  /* ---------------- 场景 A：主界面解锁弹窗 ---------------- */

  function ensureBanner() {
    let el = document.getElementById("avBanner");
    if (el) return el;
    el = document.createElement("div");
    el.id = "avBanner";
    el.className = "av-banner";
    el.innerHTML =
      '<div class="av-plaque"><div class="av-body">' +
      '<div class="av-cap">成 就 解 锁</div>' +
      '<div class="av-ic" id="avBnIc"></div>' +
      '<div><div class="av-t1" id="avBnName"></div><div class="av-t2" id="avBnDesc"></div></div>' +
      '<div class="av-shine"></div><div class="av-fx" id="avBnFx"></div>' +
      '</div></div>';
    document.body.appendChild(el);
    return el;
  }

  let seed = 20261105;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);

  function stars(host, n, spread) {
    host.innerHTML = "";
    for (let i = 0; i < n; i++) {
      const s = document.createElement("span");
      s.className = "av-st";
      s.textContent = ["✦", "✧", "★"][i % 3];
      const a = (i / n) * Math.PI * 2 + rnd() * .4;
      const d = spread * (.55 + rnd() * .65);
      s.style.setProperty("--x", (Math.cos(a) * d).toFixed(1) + "px");
      s.style.setProperty("--y", (Math.sin(a) * d - 18).toFixed(1) + "px");
      s.style.setProperty("--r", (rnd() * 220 - 110).toFixed(0) + "deg");
      s.style.setProperty("--d", (0.4 + rnd() * .5).toFixed(2) + "s");
      s.style.fontSize = (11 + rnd() * 9).toFixed(0) + "px";
      host.appendChild(s);
    }
  }

  function iconInto(box, item) {
    box.innerHTML = "";
    const kind = (item.icon_kind || (/\.(png|gif)$/i.test(item.icon || "") ? "furniture" : "emoji"));
    if (kind === "furniture" && /\.(png|gif)$/i.test(item.icon)) {
      const img = document.createElement("img");
      img.src = "./assets/furniture/" + item.icon;
      img.alt = "";
      img.addEventListener("error", () => { img.remove(); box.textContent = ""; });
      box.appendChild(img);
    } else {
      box.textContent = item.icon || "🏆";
    }
  }

  function pumpBanner() {
    if (bannerBusy || !bannerQueue.length) return;
    if (document.hidden || boardOpen) return;   // 板开着时不播木牌，避免和板内横幅重复
    const item = bannerQueue.shift();
    const el = ensureBanner();
    iconInto(document.getElementById("avBnIc"), item);
    document.getElementById("avBnName").textContent = item.name || "成就解锁";
    document.getElementById("avBnDesc").textContent = item.desc || "";
    stars(document.getElementById("avBnFx"), 8, 84);
    el.classList.remove("show");
    void el.offsetWidth;
    el.classList.add("show");
    try { window.dispatchEvent(new CustomEvent("av-banner", { detail: { id: item.id } })); } catch (e) {}
    bannerBusy = true;
    setTimeout(() => { bannerBusy = false; pumpBanner(); }, 2750);
    apiPost("panel/achv_pop", { ids: [item.id] }).catch(() => {});
    setBadge(((DATA && DATA.pop) || []).filter((x) => x && !x.board_seen).length);
  }

  const BANNER_MAX = 2;   // 一次最多弹 2 条，其余静默标记，避免攒了一堆连播半分钟

  function feedBannerQueue(pop) {
    const fresh = (pop || []).filter((p) => p && !p.popped && !bannerQueue.some((q) => q.id === p.id));
    if (!fresh.length) return;
    // 后端按时间正序返回，取最新的两条来弹
    const show = fresh.slice(-BANNER_MAX);
    const rest = fresh.slice(0, Math.max(0, fresh.length - BANNER_MAX));
    show.forEach((p) => bannerQueue.push(p));
    if (rest.length) apiPost("panel/achv_pop", { ids: rest.map((p) => p.id) }).catch(() => {});
    pumpBanner();
  }

  /* ---------------- 成就板 ---------------- */

  const tip = (() => {
    let el = null;
    return {
      show(ev, item) {
        if (!el) { el = document.createElement("div"); el.className = "av-tip"; document.body.appendChild(el); }
        el.innerHTML = "";
        const b = document.createElement("b");
        b.textContent = item.name;
        const d = document.createElement("div");
        d.textContent = item.desc;
        el.append(b, d);
        if ((item.conditions || []).length) {
          const ul = document.createElement("ul");
          item.conditions.forEach((c) => {
            const li = document.createElement("li");
            li.textContent = c.text + (c.met ? " ✔" : `（${fmt(c.current)} / ${fmt(c.target)}）`);
            ul.appendChild(li);
          });
          el.appendChild(ul);
        }
        const p = document.createElement("div");
        p.className = "av-pg" + (item.done ? " ok" : "");
        p.textContent = item.done ? "已解锁 ✔" : "完成度 " + Math.round((item.progress || 0) * 100) + "%";
        el.appendChild(p);
        el.style.display = "block";
        const w = el.offsetWidth, h = el.offsetHeight;
        let x = ev.clientX + 14, y = ev.clientY + 14;
        if (x + w > innerWidth - 8) x = ev.clientX - w - 12;
        if (y + h > innerHeight - 8) y = ev.clientY - h - 12;
        el.style.left = Math.max(6, x) + "px";
        el.style.top = Math.max(6, y) + "px";
      },
      hide() { if (el) el.style.display = "none"; },
    };
  })();

  function ensureBoard() {
    let el = document.getElementById("avBoard");
    if (el) return el;
    el = document.createElement("div");
    el.id = "avBoard";
    el.className = "av-board";
    el.innerHTML =
      '<div class="av-frame"><div class="av-in">' +
      '<div class="av-head">' +
      '<div class="av-title">成 就</div>' +
      '<div class="av-note" id="avNote"></div><div class="av-spacer"></div>' +
      '<button class="av-btn" id="avTest" type="button" title="强制点亮一个成就，用来验收动画">🧪 测试解锁</button>' +
      '<button class="av-btn on" id="avFAll" type="button">全部</button>' +
      '<button class="av-btn" id="avFDone" type="button">只看已解锁</button>' +
      '<button class="av-btn" id="avClose" type="button">收起</button>' +
      '</div>' +
      '<div class="av-sum"><div class="av-bar"><i id="avSumBar"></i><b id="avSumText">— / —</b></div>' +
      '<div class="av-nums" id="avNums"></div></div>' +
      '<div id="avBody"></div>' +
      '<div class="av-ribbon" id="avRibbon"><span>成 就 解 锁</span></div>' +
      '</div></div>';
    document.body.appendChild(el);
    el.addEventListener("click", (e) => { if (e.target === el) closeBoard(); });
    el.querySelector("#avClose").addEventListener("click", closeBoard);
    el.querySelector("#avTest").addEventListener("click", testUnlock);
    el.querySelector("#avFAll").addEventListener("click", () => { filter = "all"; syncFilter(); render(); });
    el.querySelector("#avFDone").addEventListener("click", () => { filter = "done"; syncFilter(); render(); });
    return el;
  }

  function syncFilter() {
    const a = document.getElementById("avFAll"), b = document.getElementById("avFDone");
    if (a) a.classList.toggle("on", filter === "all");
    if (b) b.classList.toggle("on", filter === "done");
  }

  function confetti(host) {
    const colors = ["#ffd24a", "#ff9ec4", "#7fd8e8", "#6ec25a", "#b7a4ff"];
    for (let i = 0; i < 14; i++) {
      const c = document.createElement("i");
      c.className = "av-conf";
      const a = (i / 14) * Math.PI * 2;
      const d = 60 + rnd() * 80;
      c.style.setProperty("--x", (Math.cos(a) * d).toFixed(1) + "px");
      c.style.setProperty("--y", (Math.sin(a) * d - 24).toFixed(1) + "px");
      c.style.setProperty("--r", (rnd() * 720 - 360).toFixed(0) + "deg");
      c.style.setProperty("--d", (rnd() * .3).toFixed(2) + "s");
      c.style.background = colors[i % colors.length];
      host.appendChild(c);
    }
  }

  function tile(item) {
    const el = document.createElement("div");
    const replay = pendingAnim.has(item.id) && item.done;      // 数据已解锁，但先演「还锁着」
    const shownDone = item.done && !replay;
    el.className = "av-ach " + (shownDone ? "is-done" : "is-locked") + (item.hidden && !item.done ? " is-secret" : "");
    el.dataset.id = item.id;

    const ic = document.createElement("div"); ic.className = "av-i";
    if (item.hidden && !item.teased) ic.textContent = "？";
    else iconInto(ic, item);

    const nm = document.createElement("div"); nm.className = "av-n"; nm.textContent = item.name;
    const pg = document.createElement("div"); pg.className = "av-p";
    const pw = replay ? Math.min(0.74, Math.max(0.2, (item.progress || 0.6) - 0.26)) : (item.progress || 0);
    const fill = document.createElement("i"); fill.style.width = Math.round(pw * 100) + "%";
    pg.appendChild(fill);

    const lock = document.createElement("span"); lock.className = "av-lock"; lock.textContent = "锁";
    const ok = document.createElement("span"); ok.className = "av-ok"; ok.textContent = "✔";
    const ring = document.createElement("span"); ring.className = "av-ring";
    const flash = document.createElement("span"); flash.className = "av-flash";

    el.append(ic, nm, pg, lock, ok, ring, flash);
    if (item.hidden) { const t = document.createElement("span"); t.className = "av-tag"; t.textContent = "隐藏"; el.appendChild(t); }
    el.addEventListener("mousemove", (e) => tip.show(e, item));
    el.addEventListener("mouseleave", () => tip.hide());
    return el;
  }

  function render() {
    if (!DATA) return;
    const host = document.getElementById("avBody");
    host.innerHTML = "";
    const s = DATA.summary || { done: 0, total: 0 };
    document.getElementById("avSumBar").style.width = Math.round((s.pct || 0) * 100) + "%";
    document.getElementById("avSumText").textContent = s.done + " / " + s.total + " 已解锁";
    const c = DATA.counters || {};
    document.getElementById("avNums").innerHTML =
      "好感度 <em>" + fmt(c.favor) + "</em>　亲密 <em>" + fmt(c.level) + "</em> 层　聊天 <em>" + fmt(c.chat_total) +
      "</em> 句　连续 <em>" + fmt(c.streak) + "</em> 天　家具 <em>" + fmt(c.furniture) + "</em> 件";
    document.getElementById("avNote").textContent = (DATA.character && DATA.character.name ? DATA.character.name + "的" : "") +
      "成就板 · 共 " + s.total + " 项";

    (DATA.categories || []).forEach((cat) => {
      const head = document.createElement("div"); head.className = "av-cat";
      const h = document.createElement("h3"); h.textContent = cat.title;
      const rule = document.createElement("span"); rule.className = "av-rule";
      const cnt = document.createElement("span"); cnt.className = "av-cnt";
      cnt.textContent = cat.done + " / " + cat.total;
      head.append(h, rule, cnt);

      const grid = document.createElement("div"); grid.className = "av-grid";
      (cat.items || []).forEach((it) => {
        if (filter === "done" && !it.done) return;
        grid.appendChild(tile(it));
      });
      host.append(head, grid);
    });
    if (!(DATA.categories || []).length) {
      host.innerHTML = '<div class="av-empty">还没有成就表（assets/achievements.json）。</div>';
    }
  }

  // 场景 B：面板内解锁动画
  function playTileUnlock(id, delay) {
    const el = document.querySelector('#avBoard .av-ach[data-id="' + id + '"]');
    avlog("playTileUnlock " + id + " el=" + (el ? "有" : "无"));
    if (!el) return;
    setTimeout(() => {
      pendingAnim.delete(id);   // 翻完就退出待演名单，否则下次 render 会把它画回锁着
      el.classList.remove("is-locked");
      el.classList.add("is-done", "justunlocked");
      confetti(el);
      const bar = el.querySelector(".av-p i");
      if (bar) { bar.style.width = "100%"; }
      const ribbon = document.getElementById("avRibbon");
      if (ribbon) {
        ribbon.classList.remove("show"); void ribbon.offsetWidth; ribbon.classList.add("show");
        setTimeout(() => ribbon.classList.remove("show"), 1700);
      }
      const num = document.getElementById("avSumText");
      if (num) { num.classList.add("tick"); setTimeout(() => num.classList.remove("tick"), 600); }
      setTimeout(() => el.classList.remove("justunlocked"), 2600);
    }, delay || 0);
  }

  function diffAndPlay(next) {
    if (!known) { known = new Set(); (next.categories || []).forEach((c) => (c.items || []).forEach((i) => known.add(i.id))); return []; }
    const nowDone = new Set();
    (next.categories || []).forEach((c) => (c.items || []).forEach((i) => { if (i.done) nowDone.add(i.id); }));
    const fresh = [...nowDone].filter((id) => !known.has(id));
    known = nowDone;
    return fresh;
  }

  async function openBoard() {
    ensureBoard();
    boardOpen = true;
    document.body.classList.add("av-board-open");
    document.getElementById("avBoard").classList.add("open");
    await reload(true);
    playBoardQueue();
  }

  function closeBoard() {
    boardOpen = false;
    document.body.classList.remove("av-board-open");
    const el = document.getElementById("avBoard");
    if (el) el.classList.remove("open");
    tip.hide();
    // 木牌接着把剩下的播完
    feedBannerQueue(((DATA && DATA.pop) || []).filter((p) => p && !p.popped));
  }

  async function reload(fromOpen) {
    try {
      const next = await apiGet("panel/achv");
      const fresh = diffAndPlay(next);
      DATA = next;
      const unpopped = (next.pop || []).filter((p) => !p.popped);
      setBadge(((next.pop || []).filter((p) => p && !p.board_seen)).length);
      if (boardOpen) {
        render();
        playBoardQueue();
      } else if (!fromOpen) {
        feedBannerQueue(unpopped);
      }
    } catch (e) {
      if (DEMO) { DATA = demoPayload(); known = null; diffAndPlay(DATA); setBadge(0); if (boardOpen) render(); }
    }
  }

  /* ---------------- 启动 ---------------- */

  async function boot() {
    mountButton();
    const qs = new URLSearchParams(location.search);
    if (!DEMO) VIEW = await waitBridge(4000);
    await reload(false);
    if (!DEMO && !VIEW) return;
    pollTimer = setInterval(() => { if (!document.hidden) reload(false); }, 20000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) reload(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && boardOpen) closeBoard(); });

    // 验收参数：?avopen=1 直接开面板；?avplay=成就id 播格子解锁动画；?avpop=1 播主界面弹窗
    const avtest = qs.get("avtest");
    avlog("boot avtest=" + (avtest || "无") + " DEMO=" + DEMO + " VIEW=" + (VIEW ? "有" : "无"));
    if (avtest) {
      setTimeout(async () => {
        try {
          await openBoard();
          avlog("开板完成，准备测试解锁");
          setTimeout(() => testUnlock(avtest === "1" ? "" : avtest), 900);
        } catch (e) {
          avlog("调度异常 " + (e && e.message));
        }
      }, 300);
    }
    if (qs.get("avopen") === "1" || qs.get("avplay")) {
      setTimeout(async () => { await openBoard(); }, 300);
    }
    const play = qs.get("avplay");
    if (play) setTimeout(() => playTileUnlock(play, 200), 1200);
    if (qs.get("avpop") === "1") {
      setTimeout(() => {
        bannerQueue = [];
        pumpBanner();
      }, 500);
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
