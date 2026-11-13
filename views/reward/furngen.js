/* 家具工坊：① 一句话生成像素家具（后端：模型出施工图 → 代码渲染）
   ② 摆放模式（纯拖动，松手在哪落在哪，不吸附格子）。
   拖动体验：全程只有一份半透明落点虚影（没有实体影子、原件拖动时藏起），
   松手瞬间就位（本地先落位，后台串行存档，失败再对齐回服务器）。
   数据走 panel/furn_*，不碰 app.js 的状态；摆完 dispatch "furngen:refresh"。 */

(function () {
  "use strict";

  let VIEW = null;
  let D = null;            // panel/furn_lib 的数据
  let busy = false;
  let preview = null;      // {prompt, mode, spec?, name, native, preview}
  let placing = false;
  let band = 16;           // 地板可用下界：量物品栏（跟 app.js floorBandMinPct 同一来源，两模式坐标系必须一致，否则错位）
  let trayBand = 16;       // 托盘顶边（摆放时新落点要避开它，不然新件会藏到托盘底下）
  let shift = 0;           // 地板家具整体上移量（跟 app.js renderFurniture 同一套算法）
  let drag = null;
  let fishW = 86;          // 角色显示宽（算家具尺寸用，buildModeLayer 时刷新）
  let layoutQueue = Promise.resolve();   // 摆位写请求串行化，快速连拖不乱序

  const FURN_SCALE = 1.45;   // 跟 app.js 一致：家具相对角色 1 像素的大小

  function $(id) { return document.getElementById(id); }

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
    if (!VIEW) throw new Error("没有桥接");
    return unwrap(await VIEW.apiGet(ep));
  }

  async function apiPost(ep, body) {
    if (!VIEW) throw new Error("没有桥接");
    return unwrap(await VIEW.apiPost(ep, body || {}));
  }

  function toast(text, kind) {
    const host = $("furnBox") || $("furn");
    if (!host) return;
    const old = host.querySelector(".furn-toast");
    if (old) old.remove();
    const el = document.createElement("div");
    el.className = "furn-toast " + (kind || "");
    el.textContent = text;
    host.appendChild(el);
    setTimeout(() => el.remove(), 2600);
  }

  function roomToast(text) {
    // 摆放模式里没有工坊盒子，用页面自带的大 toast
    const t = $("toast");
    if (!t) return;
    t.textContent = text;
    t.hidden = false;
    requestAnimationFrame(() => t.classList.add("show"));
    clearTimeout(roomToast._t);
    roomToast._t = setTimeout(() => { t.hidden = true; t.classList.remove("show"); }, 2200);
  }

  /* ---------------- 工坊 ---------------- */

  async function open() {
    const box = $("furn");
    if (!box) return;
    box.hidden = false;
    await refresh();
  }

  function close() {
    const box = $("furn");
    if (box) box.hidden = true;
  }

  async function refresh() {
    if (!VIEW) {
      toast("要在 AstrBot 面板里打开才能生成家具", "err");
      return;
    }
    try {
      D = await apiGet("panel/furn_lib");
      renderSub();
      renderGrid();
    } catch (e) {
      toast(e.message || "读不到家具库", "err");
    }
  }

  function genLeft() {
    const g = (D && D.gen) || {};
    return Number(g.left == null ? 0 : g.left);
  }

  function renderSub() {
    const sub = $("furnSub");
    if (sub) {
      const g = (D && D.gen) || {};
      const tag = g.img ? " · 精绘模式" : "";
      if (g.unlimited) sub.textContent = "不限次生成" + tag;
      else sub.innerHTML = genLeft() > 0
        ? `今天还能画 <b>${genLeft()}</b> 次${tag}`
        : "今天的次数用完了" + tag;
    }
    const go = $("furnGo");
    if (go) go.disabled = busy || (!D ? true : genLeft() <= 0);
  }

  function renderGrid() {
    const host = $("furnMyGrid");
    if (!host) return;
    host.replaceChildren();
    const items = (D && D.items) || [];
    const count = $("furnCount");
    if (count) count.textContent = items.length ? `${items.length} 件` : "还空着";
    if (!items.length) {
      const p = document.createElement("p");
      p.className = "furn-empty";
      p.textContent = "还没有家具。上面说一句话，画第一件。";
      host.appendChild(p);
      return;
    }
    items.forEach((it) => {
      const cell = document.createElement("div");
      cell.className = "furn-cell" + (it.x == null ? "" : " placed");
      cell.appendChild(iconNode(it));
      const nm = document.createElement("b");
      nm.textContent = it.name;
      const st = document.createElement("span");
      st.className = "st";
      st.textContent = (it.x == null ? "在库里" : "在房间里") + (it.mode === "img" ? " · 精绘" : "");
      const del = document.createElement("button");
      del.type = "button";
      del.className = "furn-del";
      del.textContent = "×";
      del.title = "删掉这件";
      del.addEventListener("click", (ev) => { ev.stopPropagation(); dropItem(it.id, del); });
      cell.append(nm, st, del);
      host.appendChild(cell);
    });
  }

  function iconNode(it) {
    const src = iconSrc(it);
    if (src) {
      const img = document.createElement("img");
      img.alt = it.name || "";
      img.addEventListener("error", () => {
        const parent = img.parentElement;
        img.remove();
        if (!parent) return;
        const s = document.createElement("span");
        s.className = "furn-fallback-emoji";
        s.textContent = it.emoji || "🪑";
        parent.appendChild(s);
      });
      img.src = src;
      return img;
    }
    const s = document.createElement("span");
    s.className = "furn-fallback-emoji";
    s.textContent = it.emoji || "🪑";
    return s;
  }

  function iconSrc(it) {
    const c = String(it.icon || "");
    if (c.indexOf("data:") === 0) return c;
    if (c) return "./assets/furniture/" + encodeURIComponent(c);
    if (!it.custom && it.id) return "./assets/furniture/" + encodeURIComponent(it.id + ".png");
    return "";
  }

  async function generate() {
    if (busy) return;
    const input = $("furnPrompt");
    const prompt = ((input && input.value) || "").trim();
    if (!prompt) { toast("先写一句想要什么", "err"); return; }
    if (!VIEW) { toast("要在 AstrBot 面板里打开才能生成", "err"); return; }
    busy = true;
    renderBusy(true);
    renderSub();
    try {
      const data = await apiPost("panel/furn_generate", { prompt });
      preview = Object.assign({ prompt }, data);
      renderPreview();
      if (data.note) toast(data.note, "err");
    } catch (e) {
      toast(e.message || "没画成", "err");
    } finally {
      busy = false;
      renderBusy(false);
      renderSub();
    }
  }

  function renderBusy(on) {
    const b = $("furnBusy");
    if (b) {
      b.hidden = !on;
      if (on) b.textContent = (D && D.gen && D.gen.img) ? "AI 精绘中……可能要十几秒" : "正在画……";
    }
    const p = $("furnPreview");
    if (p) p.hidden = on || !preview;
    const go = $("furnGo");
    if (go) go.disabled = on;
  }

  function renderPreview() {
    const box = $("furnPreview");
    if (!preview) { if (box) box.hidden = true; return; }
    const img = $("furnPreviewImg");
    if (img) img.src = preview.preview || "";
    const nm = $("furnPreviewName");
    if (nm) nm.textContent = preview.name || (preview.spec && preview.spec.name) || "";
    const sz = $("furnPreviewSize");
    if (sz) sz.textContent = (preview.native || []).join(" × ") + " px"
      + (preview.mode === "img" ? " · 精绘" : "");
    if (box) box.hidden = false;
  }

  async function keep() {
    if (!preview || busy) return;
    busy = true;
    try {
      D = await apiPost("panel/furn_save", preview.mode === "img"
        ? { mode: "img", prompt: preview.prompt, name: preview.name, preview: preview.preview }
        : { mode: "spec", prompt: preview.prompt, spec: preview.spec });
      preview = null;
      renderPreview();
      renderSub();
      renderGrid();
      toast("收进家具库了", "ok");
    } catch (e) {
      toast(e.message || "没存上", "err");
    } finally {
      busy = false;
      renderSub();
    }
  }

  function dropItem(id, btn) {
    if (btn.dataset.armed === "1") {
      btn.dataset.armed = "";
      btn.classList.remove("armed");
      btn.textContent = "×";
      apiPost("panel/furn_delete", { id: "c_" + id }).then((d) => {
        D = d;
        renderSub();
        renderGrid();
        window.dispatchEvent(new CustomEvent("furngen:refresh"));
        toast("删掉了", "ok");
      }).catch((e) => toast(e.message || "没删掉", "err"));
      return;
    }
    btn.dataset.armed = "1";
    btn.classList.add("armed");
    btn.textContent = "删？";
    setTimeout(() => {
      if (btn.dataset.armed !== "1") return;
      btn.dataset.armed = "";
      btn.classList.remove("armed");
      btn.textContent = "×";
    }, 2400);
  }

  /* ---------------- 摆放模式（纯拖动） ---------------- */

  function trayList() {
    const olds = ((D && D.old) || []).map((o) => ({
      id: o.id, name: o.name, icon: o.icon, emoji: o.emoji,
      native: (o.native && o.native.length) ? o.native : [24, 24],
      x: o.x, y: o.y, s: Number(o.s) || 1, z: o.z == null ? null : Number(o.z), custom: false,
    }));
    const news = ((D && D.items) || []).map((it) => ({
      id: "c_" + it.id, name: it.name, icon: it.icon, emoji: "🪑",
      native: (it.native && it.native.length) ? it.native : [24, 24],
      x: it.x, y: it.y, s: Number(it.s) || 1, z: it.z == null ? null : Number(it.z), custom: true,
    }));
    return olds.concat(news);
  }

  function enterPlace() {
    if (placing) return;
    if (!D) { toast("还没读到家具库", "err"); return; }
    placing = true;
    close();
    document.body.classList.add("furn-placing");
    const tray = $("furnTray");
    if (tray) tray.hidden = false;
    const bar = $("furnModeBar");
    if (bar) bar.hidden = false;
    buildModeLayer();
  }

  async function exitPlace() {
    placing = false;
    document.body.classList.remove("furn-placing");
    const tray = $("furnTray");
    if (tray) tray.hidden = true;
    const bar = $("furnModeBar");
    if (bar) bar.hidden = true;
    const layer = $("furnRoomLayer");
    if (layer) layer.remove();
    clearZoneHint();
    clearLanding();
    // 等排队中的摆位写完再让 app.js 重画，避免它读到旧位置
    try { await layoutQueue; } catch (e) { /* 队列内部已兜底 */ }
    window.dispatchEvent(new CustomEvent("furngen:refresh"));
    refresh();
  }

  /* 量某个底部条占房间高度的百分比（公式与 app.js floorBandMinPct 完全一致） */
  function barPct(el) {
    const tank = $("tank");
    if (!tank || !el) return 16;
    const tr = tank.getBoundingClientRect();
    const hr = el.getBoundingClientRect();
    if (!tr.height || !hr.height) return 16;
    const used = ((tr.bottom - hr.top) / tr.height) * 100;
    return Math.max(12, Math.min(42, Math.round(used + 1.5)));
  }

  function buildModeLayer() {
    const tank = $("tank");
    if (!tank) return;
    let layer = $("furnRoomLayer");
    if (!layer) {
      layer = document.createElement("div");
      layer.id = "furnRoomLayer";
      layer.className = "furn-room-layer";
      tank.appendChild(layer);
    }
    const fish = $("fish");
    if (fish) {
      const w = fish.getBoundingClientRect().width;
      if (w) fishW = w;
    }
    band = barPct(document.querySelector(".hotbar-wrap"));   // 坐标系基准：与 app.js 同源，两模式必须一致
    trayBand = barPct($("furnTray"));                        // 托盘高度只用来夹新落点，不进坐标系
    const placed = trayList().filter((it) => it.x != null);
    const floorYs = placed.filter((it) => Number(it.y) < 45).map((it) => Number(it.y));
    shift = floorYs.length ? Math.max(0, band - Math.min(...floorYs)) : 0;
    renderModeLayer();
    renderTray();
  }

  function itemW(it) {
    const nat = (it.native && it.native.length) ? it.native : [24, 24];
    const s = Number(it.s) || 1;
    return Math.max(10, Math.round(nat[0] * (fishW / 34) * FURN_SCALE * s));
  }

  function showScale(it) {
    const el = $("furnScale");
    if (!el) return;
    el.hidden = false;
    el.textContent = "×" + (Number(it.s) || 1).toFixed(2);
  }

  function hideScale() {
    const el = $("furnScale");
    if (el) el.hidden = true;
  }

  /* 叠放规则：摆过/压过的件带层号 z，z 大在前；从没动过层的走深度序（屏幕越靠下越前）。
     排序后按名次发 zIndex，跟旧 1000-y*10 档位兼容（那档最高 980，这里 500+idx*2，idx≤30）。 */
  function stk(it) {
    return it.z == null ? 100 - (Number(it.y) || 0) : 1000 + it.z;
  }

  function renderModeLayer() {
    const layer = $("furnRoomLayer");
    if (!layer) return;
    layer.replaceChildren();
    trayList()
      .filter((it) => it.x != null)
      .slice()
      .sort((a, b) => stk(a) - stk(b))              // 该在后的先画，该在前的后画
      .forEach((it, idx) => {
        const el = document.createElement("div");
        el.className = "furn-mode-item";
        el.dataset.id = it.id;
        el.style.zIndex = String(500 + idx * 2);
        placeEl(el, it);
        const img = document.createElement("img");
        img.src = iconSrc(it) || "";
        img.alt = it.name || "";
        el.appendChild(img);
        startDrag(el, it);
        el.addEventListener("contextmenu", (ev) => {   // 右键（手机长按）= 压到最底
          ev.preventDefault();
          const zs = trayList().filter((t) => t.x != null && t.z != null).map((t) => t.z);
          setLocalZ(it, zs.length ? Math.min(...zs) - 1 : 0);
          buildModeLayer();
          pushLayout({ id: it.id, x: it.x, y: it.y, s: Number(it.s) || 1, back: true });
        });
        layer.appendChild(el);
      });
  }

  function placeEl(el, it) {
    const y = Number(it.y) || 0;
    el.style.left = `${Number(it.x) || 50}%`;
    el.style.bottom = `${y < 45 ? y + shift : y}%`;
    el.style.width = `${itemW(it)}px`;
  }

  function renderTray() {
    const tray = $("furnTray");
    if (!tray) return;
    const keepTop = tray.scrollTop;   // 行多滚过的话别跳回顶部
    tray.replaceChildren();
    trayList().forEach((it) => {
      const cell = document.createElement("div");
      cell.className = "furn-tray-cell" + (it.x == null ? "" : " out");
      cell.dataset.id = it.id;
      const img = document.createElement("img");
      img.src = iconSrc(it) || "";
      img.alt = it.name || "";
      const nm = document.createElement("span");
      nm.textContent = it.name || "";
      cell.append(img, nm);
      startDrag(cell, it);
      tray.appendChild(cell);
    });
    tray.scrollTop = keepTop;
  }

  /* ---------------- 拖动引擎：只跟一个落点虚影 + 松手即落 ---------------- */

  function startDrag(el, it) {
    el.addEventListener("pointerdown", (ev) => {
      if (!placing || drag || ev.button > 0) return;
      ev.preventDefault();
      drag = {
        item: it, pid: ev.pointerId, landing: null,
        moved: false, src: el, tankRect: null, zone: "",
      };
      const move = (e) => {
        if (!drag || e.pointerId !== drag.pid) return;
        if (!drag.landing) {
          const tank = $("tank");
          if (!tank) return;
          drag.tankRect = tank.getBoundingClientRect();
          // 只做一份虚影：就是落点预览本身，不再另跟一个实体影子
          drag.landing = makeLanding(it);
          tank.appendChild(drag.landing);
          if (drag.src && drag.src.classList.contains("furn-mode-item")) {
            drag.src.classList.add("dragging");   // 原件先藏起来，避免重影
          }
          showScale(drag.item);
        }
        drag.moved = true;
        updateLanding(e.clientX, e.clientY);
      };
      const finish = (e) => {
        detach();
        if (!drag) return;
        const d = drag;
        drag = null;
        hideScale();
        if (d.src) d.src.classList.remove("dragging");
        clearZoneHint();
        clearLanding();
        const cx = (e && e.clientX) != null ? e.clientX : -1;
        const cy = (e && e.clientY) != null ? e.clientY : -1;
        if (!d.moved) {                  // 只点了一下：不算拖动，滚轮改过的大小也退回去
          if (placing) buildModeLayer();
          return;
        }
        dropAt(cx, cy, d.item, d.tankRect);
      };
      const cancel = () => {
        detach();
        if (!drag) return;
        hideScale();
        if (drag.src) drag.src.classList.remove("dragging");
        drag = null;
        clearZoneHint();
        clearLanding();
        if (placing) buildModeLayer();
      };
      const detach = () => {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", finish);
        document.removeEventListener("pointercancel", cancel);
        document.removeEventListener("wheel", onWheel);
      };
      // 滚轮调大小：按住家具时向上放大、向下缩小（0.4～3 倍）
      const onWheel = (ev) => {
        if (!drag) return;
        ev.preventDefault();
        const cur = Number(drag.item.s) || 1;
        let next = Math.round(cur * (ev.deltaY < 0 ? 1.08 : 1 / 1.08) * 100) / 100;
        next = Math.max(0.4, Math.min(3, next));
        if (next === cur) return;
        drag.item.s = next;
        showScale(drag.item);
        if (drag.landing) drag.landing.style.width = `${itemW(drag.item)}px`;
        if (drag.src && drag.src.classList.contains("furn-mode-item")) {
          drag.src.style.width = `${itemW(drag.item)}px`;
        }
      };
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", finish);
      document.addEventListener("pointercancel", cancel);
      document.addEventListener("wheel", onWheel, { passive: false });
    });
  }

  /* 落点虚影：家具半透明地站在松手后将出现的位置上，拖到哪指到哪（只有这一份） */
  function makeLanding(it) {
    const el = document.createElement("div");
    el.id = "furnLanding";
    el.className = "furn-landing";
    el.style.width = `${itemW(it)}px`;
    const img = document.createElement("img");
    img.src = iconSrc(it) || "";
    img.alt = "";
    el.appendChild(img);
    return el;
  }

  function clearLanding() {
    const el = $("furnLanding");
    if (el) el.remove();
  }

  /* 拖动中随时算：现在松手会落在哪（显示坐标，已吸附分区） */
  function computeDrop(cx, cy, tankRect) {
    tankRect = tankRect || (drag && drag.tankRect);
    const tank = $("tank");
    if (!tankRect && tank) tankRect = tank.getBoundingClientRect();
    if (!tankRect) return null;
    const inTank = cx >= tankRect.left && cx <= tankRect.right && cy >= tankRect.top && cy <= tankRect.bottom;
    const tray = $("furnTray");
    let onTray = false;
    if (tray && !tray.hidden) {
      const trr = tray.getBoundingClientRect();
      onTray = cx >= trr.left - 10 && cx <= trr.right + 10 && cy >= trr.top - 10;
    }
    let x = ((cx - tankRect.left) / tankRect.width) * 100;
    let y = ((tankRect.bottom - cy) / tankRect.height) * 100;
    const wall = y >= 45;
    // 纯贴图：不吸附格子，松手在哪就落在哪；地板新落点底边不压进托盘区，只区分墙/地、夹在房间边界内
    const lo = floorLo();
    x = Math.max(2, Math.min(98, x));
    y = wall ? Math.max(46, Math.min(89, y)) : Math.max(lo, Math.min(44, y));
    return { inTank, onTray, x: round1(x), y: round1(y), wall };
  }

  function updateLanding(cx, cy) {
    if (!drag || !drag.landing) return;
    const d = computeDrop(cx, cy, drag.tankRect);
    if (!d) return;
    const el = drag.landing;
    if (!d.inTank || d.onTray) {
      el.classList.add("off");
      zoneHintZone("");
      return;
    }
    el.classList.remove("off");
    el.style.left = `${d.x}%`;
    el.style.bottom = `${d.y}%`;
    el.style.zIndex = "3000";   // 虚影永远浮在最上，叠到哪都看得清
    zoneHintZone(d.wall ? "wall" : "floor");
  }

  function zoneHintZone(zone) {
    if (!drag || zone === drag.zone) return;
    drag.zone = zone;
    let el = $("furnZone");
    if (!zone) { if (el) el.remove(); return; }
    if (!el) {
      const tank = $("tank");
      if (!tank) return;
      el = document.createElement("div");
      el.id = "furnZone";
      el.className = "furn-zone";
      tank.appendChild(el);
    }
    el.dataset.zone = zone;
    if (zone === "wall") {
      el.style.bottom = "45%";
      el.style.height = "45%";
    } else {
      el.style.bottom = `${floorLo()}%`;
      el.style.height = `${Math.max(6, 45 - floorLo())}%`;
    }
  }

  /* 地板可落区下界：躲开物品栏和托盘里更高的那个 */
  function floorLo() {
    return Math.max(band, trayBand) + 2;
  }

  function clearZoneHint() {
    const el = $("furnZone");
    if (el) el.remove();
  }

  /* 松手：本地先落位（画面立刻对），写请求排队进后台 */
  function dropAt(cx, cy, it, tankRect) {
    const d = computeDrop(cx, cy, tankRect);
    if (!d) return;
    if (!d.inTank || d.onTray) {
      if (it.x != null) {
        recall(it);
      } else if (!d.onTray) {
        roomToast("拖进房间里再松手");
      }
      return;
    }
    // 地板件存“原始 y”：app.js 渲染时会加回 shift
    const sy = d.wall ? d.y : Math.max(1, round1(d.y - shift));
    const sc = Number(it.s) || 1;
    const zs = trayList().filter((t) => t.x != null && t.z != null).map((t) => t.z);
    placeLocal(it, d.x, sy, sc, zs.length ? Math.max(...zs) + 1 : 1);   // 刚落的永远最顶
    buildModeLayer();                    // 立刻重画：松手即到位，无网络空窗
    pushLayout({ id: it.id, x: d.x, y: sy, s: sc });
  }

  function placeLocal(it, x, y, s, z) {
    const set = (hit) => {
      if (!hit) return;
      hit.x = x; hit.y = y; hit.s = s;
      if (z != null) hit.z = z;
    };
    if (it.custom) set(((D && D.items) || []).find((i) => "c_" + i.id === it.id));
    else set(((D && D.old) || []).find((i) => i.id === it.id));
  }

  function setLocalZ(it, z) {
    const hit = it.custom
      ? ((D && D.items) || []).find((i) => "c_" + i.id === it.id)
      : ((D && D.old) || []).find((i) => i.id === it.id);
    if (hit) hit.z = z;
  }

  function pushLayout(body) {
    layoutQueue = layoutQueue.then(async () => {
      let ok = true;
      try {
        const res = await apiPost("panel/furn_layout", body);
        applyFromPayload(res && res.furniture);
      } catch (e) {
        ok = false;
        roomToast(e.message || "有一个位置没存上，重新对齐一下");
      }
      if (!ok) {
        try { D = await apiGet("panel/furn_lib"); } catch (e) { /* 下次再对 */ }
      }
      if (!drag && placing) buildModeLayer();   // 没人拖着才重画
    });
    return layoutQueue;
  }

  async function recall(it) {
    // 先把房里那只藏掉（立刻有反馈），存档确认后重画
    const node = document.querySelector(`#furnRoomLayer .furn-mode-item[data-id="${it.id}"]`);
    if (node) node.style.display = "none";
    try {
      const res = await apiPost("panel/furn_layout", { id: it.id, remove: true });
      applyFromPayload(res && res.furniture);
    } catch (e) {
      roomToast(e.message || "没收回来");
    }
    if (!drag) buildModeLayer();
  }

  function applyFromPayload(furniture) {
    if (!Array.isArray(furniture) || !D) return;
    (D.items || []).forEach((it) => {
      const hit = furniture.find((f) => f.id === "c_" + it.id);
      it.x = hit ? hit.x : null;
      it.y = hit ? hit.y : null;
      if (hit && hit.s) it.s = hit.s;
      if (hit && hit.z != null) it.z = hit.z;
    });
    (D.old || []).forEach((o) => {
      const hit = furniture.find((f) => f.id === o.id);
      if (hit) { o.x = hit.x; o.y = hit.y; o.s = hit.s || 1; if (hit.z != null) o.z = hit.z; }
    });
  }

  function round1(v) {
    return Math.round(Number(v) * 10) / 10;
  }

  /* ---------------- 启动 ---------------- */

  function boot() {
    const box = $("furn");
    if (!box) return;
    waitBridge(4000).then((v) => { VIEW = v; });
    $("btnFurn") && $("btnFurn").addEventListener("click", open);
    $("furnClose") && $("furnClose").addEventListener("click", close);
    box.addEventListener("click", (ev) => { if (ev.target === box) close(); });
    $("furnGo") && $("furnGo").addEventListener("click", generate);
    const input = $("furnPrompt");
    if (input) {
      input.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") generate();
      });
    }
    $("furnKeep") && $("furnKeep").addEventListener("click", keep);
    $("furnReroll") && $("furnReroll").addEventListener("click", generate);
    $("furnDrop") && $("furnDrop").addEventListener("click", () => {
      preview = null;
      renderPreview();
    });
    $("furnPlace") && $("furnPlace").addEventListener("click", enterPlace);
    $("furnDone") && $("furnDone").addEventListener("click", exitPlace);
    const tray = $("furnTray");
    if (tray) {
      // 托盘行数溢出时，滚轮竖着滚（拖动中可以滚轮缩放，别抢）
      tray.addEventListener("wheel", (ev) => {
        if (drag) return;
        if (tray.scrollHeight <= tray.clientHeight + 2) return;
        tray.scrollTop += ev.deltaY;
        ev.preventDefault();
      }, { passive: false });
    }
    document.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape") return;
      if (placing) exitPlace();
      else if (!$("furn").hidden) close();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
