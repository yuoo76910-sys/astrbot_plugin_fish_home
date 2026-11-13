/* 「她送我的」收藏模块（独立于 app.js，不共享状态）
   数据：/panel/her_gifts (GET) 与 /panel/her_gift_take (POST)
   本地预览：?hgdemo=1 或 file:// 打开时用内置示例数据
   验收参数：?hgopen=1 直接开收藏板；?hgtake=1 直接演一遍领取近景 */

(function () {
  "use strict";

  const DEMO = new URLSearchParams(location.search).get("hgdemo") === "1" || location.protocol === "file:";
  let VIEW = null;
  let DATA = null;
  let boardOpen = false;
  let busy = false;

  const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

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
    if (DEMO) return demoTake();
    if (!VIEW) throw new Error("没有桥接");
    return unwrap(await VIEW.apiPost(ep, body || {}));
  }

  /* ---------------- 示例数据（file:// 预览用） ---------------- */

  function demoPayload() {
    const G = (id, name, icon, rarity, hint, done, prog, conds, hidden) => ({
      id, name: hidden && !done ? "？？？" : name, icon, rarity,
      hint: hidden && !done ? "还不知道是什么。" : hint,
      line: "……给你。", done: !!done, ts: done ? Date.now() / 1000 - 7200 : 0,
      hidden: !!hidden, teased: !hidden || done || prog >= 0.5,
      progress: done ? 1 : prog, conditions: hidden && !done ? [] : (conds || []),
    });
    const items = [
      G("hg_shell", "一枚捡来的贝壳", "hg_shell.png", "common", "亲密攒到一定程度之后，她在沙滩上想到的第一件事。", true, 1),
      G("hg_ribbon", "分一半的发带", "hg_ribbon.png", "rare", "亲密再往上攒攒，她会多出一件不想留着的东西。", false, 0.6, [{ text: "亲密层数 >= 5", current: 3, target: 5 }]),
      G("hg_star_jar", "星星罐", "hg_star_jar.png", "epic", "听说亲密攒到两位数，她会把一种很亮的东西给你。", false, 0.3, [{ text: "亲密层数 >= 10", current: 3, target: 10 }]),
      G("hg_letter_back", "她写的手写信", "hg_letter_back.png", "epic", "心愿高到某个程度，她会回你一封一模一样的东西。", true, 1),
      G("hg_crane", "折纸千纸鹤", "hg_crane.png", "rare", "连着七天都来找她，她手里会多一只纸鹤。", true, 1),
      G("hg_photo", "一张合照", "hg_photo.png", "legendary", "很久很久的陪伴之后，她想把那天留下来。", false, 0.23, [{ text: "连续天数 >= 30", current: 7, target: 30 }]),
      G("hg_bento", "她做的便当", "hg_bento.png", "epic", "七样食物都喂过她之后，她会反过来喂你一次。", false, 0.57, [{ text: "尝过的食物 >= 7", current: 4, target: 7 }]),
      G("hg_first_day", "第一天的日历页", "hg_first_day.png", "rare", "她留着认识你那天的一页纸。", false, 0.4, [{ text: "认识天数 >= 30", current: 12, target: 30 }]),
      G("hg_lantern", "她留的灯", "hg_lantern.png", "rare", "好感度刷满的那个深夜，她好像多做了点什么。", false, 0, [{ text: "深夜事件 >= 1", current: 0, target: 1 }]),
      G("hg_key", "一把钥匙", "hg_key.png", "secret", "？？？", false, 0.86, [], true),
    ];
    return {
      items: items.map((i) => ({ ...i, claimed: !!i.done, ready: false })),
      done: items.filter((i) => i.done).length, ready: 0, total: items.length,
      pending: { id: "hg_shell", name: "一枚捡来的贝壳", icon: "hg_shell.png", rarity: "common", line: "潮水冲上来的，不是我准备的。" },
      queue: 1, character: { name: "她" },
    };
  }

  let demoPending = true;
  function demoTake() {
    if (!demoPending) throw new Error("她手里没有东西了");
    demoPending = false;
    return { id: "hg_shell", name: "一枚捡来的贝壳", icon: "hg_shell.png", rarity: "common", line: "潮水冲上来的，不是我准备的。", wish: 2 };
  }

  /* ---------------- 入口按钮 + 提示气泡 ---------------- */

  function mountButton() {
    if (document.getElementById("hgOpenBtn")) return;
    const host = document.querySelector(".scene-actions") || document.body;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.id = "hgOpenBtn";
    btn.className = "round-btn hg-open";
    btn.title = "她送我的";
    btn.textContent = "🎁";
    const dot = document.createElement("span");
    dot.className = "hg-dot";
    dot.textContent = "0";
    btn.appendChild(dot);
    btn.addEventListener("click", () => {
      if (DATA && DATA.pending) takeGift();
      else openBoard();
    });
    if (host === document.body) btn.style.position = "fixed";
    host.insertBefore(btn, host.firstChild);
    if (window.__bindTip) window.__bindTip(btn, "她送你的东西");
  }

  function setBadge(n) {
    const btn = document.getElementById("hgOpenBtn");
    if (!btn) return;
    const dot = btn.querySelector(".hg-dot");
    if (dot) dot.textContent = "1";
    btn.classList.toggle("has-new", n > 0);
    const hint = document.getElementById("hgHint");
    if (hint) {
      hint.hidden = !(n > 0) || boardOpen;
      if (!hint.hidden) placeHint();
    }
  }

  /* 「她手里好像藏着什么」别压在她说话的气泡上：往上让，没地方就暂时不提示 */
  function placeHint() {
    const hint = document.getElementById("hgHint");
    if (!hint || hint.hidden) return;
    hint.classList.remove("yield");
    hint.style.top = "";
    const talk = document.getElementById("talkBubble");
    if (!talk || talk.hidden) return;
    const tr = talk.getBoundingClientRect();
    if (!tr.width) return;
    const hr = hint.getBoundingClientRect();
    const overlap = tr.top < hr.bottom + 6 && tr.bottom > hr.top - 6 && tr.left < hr.right && tr.right > hr.left;
    if (!overlap) return;
    const up = tr.top - hr.height - 14;
    if (up > 8) hint.style.top = up + "px";
    else hint.classList.add("yield");
  }

  /* ---------------- 房间展示架：收到的东西摆在她的窝里 ---------------- */

  function mountShelf() {
    if (document.getElementById("hgShelf")) return;
    const tank = document.getElementById("tank");
    if (!tank) return;
    const el = document.createElement("div");
    el.id = "hgShelf";
    el.className = "hg-shelf";
    el.hidden = true;
    // 不要标题那行字：架子上摆的就是她送的东西，看一眼就知道
    tank.appendChild(el);
  }

  let shelfTipTimer = 0;
  function renderShelf() {
    const el = document.getElementById("hgShelf");
    if (!el || !DATA) return;
    const done = (DATA.items || []).filter((i) => i.claimed && i.icon);   // 没领的不摆出来
    el.hidden = done.length === 0;
    el.querySelectorAll("button").forEach((b) => b.remove());
    done.slice(0, 10).forEach((it) => {
      const b = document.createElement("button");
      b.type = "button";
      b.title = it.name;
      const img = document.createElement("img");
      img.src = "./assets/items/" + encodeURIComponent(it.icon);
      img.alt = "";
      img.addEventListener("error", () => {
        img.remove();
        b.textContent = "🎁";
      });
      b.appendChild(img);
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        showShelfTip(it);
      });
      el.appendChild(b);
    });
  }

  function showShelfTip(it) {
    const el = document.getElementById("hgShelf");
    if (!el) return;
    let tip = el.querySelector(".hg-shelf-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "hg-shelf-tip";
      el.appendChild(tip);
    }
    tip.textContent = `「${it.name}」${it.line ? " —— " + it.line : ""}`;
    tip.hidden = false;
    clearTimeout(shelfTipTimer);
    shelfTipTimer = setTimeout(() => { tip.hidden = true; }, 3200);
  }

  function mountHint() {
    if (document.getElementById("hgHint")) return;
    const el = document.createElement("div");
    el.id = "hgHint";
    el.className = "hg-hint";
    el.hidden = true;
    el.textContent = "她手里好像藏着什么 →";
    const tank = document.getElementById("tank") || document.body;
    tank.appendChild(el);
  }

  /* ---------------- 领取 ---------------- */

  async function takeGift() {
    if (busy) return;
    busy = true;
    try {
      const gift = await apiPost("panel/her_gift_take", {});
      if (window.__herCamera) window.__herCamera(gift);
      toast(`获得了：${gift.name}`);
      await reload();
    } catch (e) {
      toast(e && e.message ? e.message : "她摇头：现在不给");
      await reload();
    } finally {
      busy = false;
    }
  }

  let toastTimer = 0;
  function toast(text) {
    let el = document.getElementById("hgToast");
    if (!el) {
      el = document.createElement("div");
      el.id = "hgToast";
      el.className = "hg-toast";
      (document.getElementById("tank") || document.body).appendChild(el);
    }
    el.textContent = text;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
  }

  /* ---------------- 收藏板 ---------------- */

  function ensureBoard() {
    let el = document.getElementById("hgBoard");
    if (el) return el;
    el = document.createElement("div");
    el.id = "hgBoard";
    el.className = "hg-board";
    el.innerHTML = `
      <div class="hg-panel">
        <header class="hg-head">
          <h3>她送我的</h3>
          <span class="hg-sum"></span>
          <span class="hg-rule"></span>
          <button type="button" class="hg-take" ${""}>领取</button>
          <button type="button" class="hg-close">收起</button>
        </header>
        <div class="hg-body"></div>
      </div>`;
    document.body.appendChild(el);
    el.addEventListener("click", (e) => { if (e.target === el) closeBoard(); });
    el.querySelector(".hg-close").addEventListener("click", closeBoard);
    el.querySelector(".hg-take").addEventListener("click", takeGift);
    return el;
  }

  function tile(it) {
    const el = document.createElement("div");
    const claimed = it.claimed !== undefined ? it.claimed : !!it.done;
    const ready = !!it.ready && !claimed;
    el.className = `hg-tile ${claimed ? "is-done" : (ready ? "is-ready" : "is-locked")}`
      + `${it.hidden && !claimed ? " is-secret" : ""} hg-r-${esc(it.rarity || "common")}`;

    const ic = document.createElement("div");
    ic.className = "hg-i";
    if (it.hidden && !it.teased) ic.textContent = "？";
    else if (it.icon) {
      const img = document.createElement("img");
      img.src = "./assets/items/" + encodeURIComponent(it.icon);
      img.alt = "";
      img.addEventListener("error", () => { img.remove(); ic.textContent = "🎁"; });
      ic.appendChild(img);
    } else ic.textContent = "🎁";

    const nm = document.createElement("div");
    nm.className = "hg-n";
    nm.textContent = it.name || "？？？";

    el.append(ic, nm);

    if (claimed) {
      const ln = document.createElement("p");
      ln.className = "hg-line";
      ln.textContent = it.line || "";
      const dt = document.createElement("span");
      dt.className = "hg-date";
      if (it.ts) {
        const d = new Date(it.ts * 1000);
        dt.textContent = `${d.getMonth() + 1}月${d.getDate()}日`;
      }
      el.append(ln, dt);
    } else if (ready) {
      const rp = document.createElement("p");
      rp.className = "hg-line";
      rp.textContent = "她备好了，还没递给你。";
      el.appendChild(rp);
    } else {
      const pg = document.createElement("div");
      pg.className = "hg-p";
      const fill = document.createElement("i");
      fill.style.width = Math.round((it.progress || 0) * 100) + "%";
      pg.appendChild(fill);
      el.appendChild(pg);
      const hint = document.createElement("p");
      hint.className = "hg-hintx";
      const conds = (it.conditions || []).map((c) => `${c.text}（${c.current}/${c.target}）`).join("　");
      hint.textContent = it.hint || conds || "还不知道是什么。";
      el.appendChild(hint);
      if (conds && (it.hint || "")) {
        const c2 = document.createElement("p");
        c2.className = "hg-cond";
        c2.textContent = conds;
        el.appendChild(c2);
      }
    }
    if (it.hidden) {
      const tag = document.createElement("span");
      tag.className = "hg-tag";
      tag.textContent = "隐藏";
      el.appendChild(tag);
    }
    return el;
  }

  function render() {
    if (!DATA) return;
    const el = document.getElementById("hgBoard");
    if (!el) return;
    el.querySelector(".hg-sum").textContent = `${DATA.done} / ${DATA.total} 已收到`
      + (DATA.ready ? `　·　她手里还有 ${DATA.ready} 件等你领` : "");
    const takeBtn = el.querySelector(".hg-take");
    takeBtn.style.display = DATA.pending ? "" : "none";
    if (DATA.pending) takeBtn.textContent = `领取：${DATA.pending.name}`;
    const body = el.querySelector(".hg-body");
    body.replaceChildren();
    (DATA.items || []).forEach((it) => body.appendChild(tile(it)));
  }

  function openBoard() {
    ensureBoard();
    boardOpen = true;
    document.body.classList.add("hg-board-open");
    document.getElementById("hgBoard").classList.add("open");
    render();
    reload(true);
  }

  function closeBoard() {
    boardOpen = false;
    document.body.classList.remove("hg-board-open");
    const el = document.getElementById("hgBoard");
    if (el) el.classList.remove("open");
    setBadge(DATA && DATA.pending ? 1 : 0);
  }

  /* ---------------- 数据 ---------------- */

  async function reload(fromOpen) {
    try {
      DATA = await apiGet("panel/her_gifts");
      setBadge(DATA.pending ? 1 : 0);
      renderShelf();
      if (boardOpen || fromOpen) render();
    } catch (e) {
      if (DEMO) { DATA = demoPayload(); setBadge(1); renderShelf(); if (boardOpen) render(); }
    }
  }

  /* ---------------- 启动 ---------------- */

  async function boot() {
    mountButton();
    mountHint();
    const tbWatch = document.getElementById("talkBubble");
    if (tbWatch && !placeHint._w) {
      placeHint._w = true;
      try {
        new MutationObserver(() => placeHint())
          .observe(tbWatch, { attributes: true, attributeFilter: ["hidden", "style", "class"], childList: true, subtree: true });
      } catch (e) { /* 忽略 */ }
      window.addEventListener("resize", () => placeHint());
    }
    mountShelf();
    if (!DEMO) VIEW = await waitBridge(4000);
    await reload(false);
    if (!DEMO && !VIEW) return;
    setInterval(() => { if (!document.hidden) reload(false); }, 20000);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && boardOpen) closeBoard(); });

    const qs = new URLSearchParams(location.search);
    if (qs.get("hgopen") === "1") setTimeout(openBoard, 400);
    if (qs.get("hgtake") === "1") setTimeout(() => takeGift(), 700);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
