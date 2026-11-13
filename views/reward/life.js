/* 生活模块（v0.33-0.35）：哄台阶 / DIY 点单 / 她自己想的成就板
   数据：panel/state 里的 band/steps/diy/notes + panel/step + panel/diy_feed + panel/notes
   独立于 app.js，不共享状态。 */

(function () {
  "use strict";

  const DEMO = new URLSearchParams(location.search).get("lifedemo") === "1" || location.protocol === "file:";
  let VIEW = null;
  let S = null;          // 最近一次 band/steps/diy 快照
  let notesOpen = false;
  let busy = false;

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
    if (DEMO) return demoState();
    if (!VIEW) throw new Error("没有桥接");
    return unwrap(await VIEW.apiGet(ep));
  }

  async function apiPost(ep, body) {
    if (DEMO) return demoAct(ep, body);
    if (!VIEW) throw new Error("没有桥接");
    return unwrap(await VIEW.apiPost(ep, body || {}));
  }

  /* ---------------- 本地预览兜底数据 ---------------- */

  let demoBand = "shut";
  function demoState() {
    return {
      band: { name: demoBand },
      steps: { available: demoBand === "shut", used: 1, left: 2, cooldown_min: 0, kinds: [
        { id: "knock", name: "敲敲门" }, { id: "note", name: "留张纸条" }, { id: "water", name: "递杯热水" }] },
      diy: { enabled: true, used_today: false, last: [] },
      notes: { count: 2 },
    };
  }

  function demoAct(ep, body) {
    if (ep === "panel/step") {
      const map = {
        knock: { name: "敲敲门", scene: "门缝底下透出一点光。", line: "……谁啊。" },
        note: { name: "留张纸条", scene: "纸条被拿走了，背面有一行铅笔字。", line: "……字真丑。" },
        water: { name: "递杯热水", scene: "杯子被她接过去了，没说话。", line: "手是暖的。人还没。" },
      };
      const d = map[(body || {}).kind] || map.knock;
      return { ok: true, name: d.name, scene: d.scene, line: d.line, band: "sulk", used: 2, left: 1 };
    }
    if (ep === "panel/diy_feed") {
      return { ok: true, line: "吃过了。辣的味道。你自己尝过没有。", score: Number((body || {}).score) || 0, tastes: ["辣"] };
    }
    return {};
  }

  async function demoNotes() {
    return { items: [
      { id: "ai_work_late", title: "她记住了那些加班的晚上", why: "你说过3次加班。有一次太晚了，她第一次没催你回消息。", evidence: "今天又加班到十一点", done: true, done_ts: Date.now() / 1000 - 3600, progress: 1 },
      { id: "ai_insomnia", title: "凌晨的份数", why: "你2次睡不着。那些小时她都替你数着。", evidence: "失眠了", done: false, progress: 0.5 },
    ], slots: 2 };
  }

  /* ---------------- 台阶条 ---------------- */

  function mountSteps() {
    if (document.getElementById("lfSteps")) return;
    const tank = document.getElementById("tank") || document.body;
    const bar = document.createElement("div");
    bar.id = "lfSteps";
    bar.className = "lf-steps";
    bar.hidden = true;
    const cap = document.createElement("div");
    cap.className = "lf-cap";
    cap.textContent = "她躲起来了。你可以——";
    bar.appendChild(cap);
    const row = document.createElement("div");
    row.className = "lf-row";
    [["knock", "🚪", "敲敲门"], ["note", "📝", "留张纸条"], ["water", "🫖", "递杯热水"]].forEach((k) => {
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.kind = k[0];
      b.innerHTML = `<i>${k[1]}</i><span>${k[2]}</span>`;
      b.addEventListener("click", () => takeStep(k[0]));
      row.appendChild(b);
    });
    bar.appendChild(row);
    const info = document.createElement("div");
    info.className = "lf-info";
    bar.appendChild(info);
    tank.appendChild(bar);
  }

  async function takeStep(kind) {
    if (busy) return;
    busy = true;
    try {
      const d = await apiPost("panel/step", { kind: kind });
      toast(`${d.scene} ${d.line}`);
      if (window.__lfFishReact) window.__lfFishReact();
      await load(true);
    } catch (e) {
      toast(e && e.message ? e.message : "她没应门");
    } finally {
      busy = false;
    }
  }

  function renderSteps() {
    const bar = document.getElementById("lfSteps");
    if (!bar || !S) return;
    const st = S.steps || {};
    const show = !!st.available;
    bar.hidden = !show;
    if (!show) return;
    bar.querySelectorAll("button").forEach((b) => {
      const cooling = st.cooldown_min > 0 && st.used > 0;
      b.disabled = cooling || st.left <= 0 || busy;
    });
    const info = bar.querySelector(".lf-info");
    if (info) {
      info.textContent = st.left <= 0 ? "今天三次用完了。她还在里面。"
        : (st.cooldown_min > 0 ? `下一次还要等 ${st.cooldown_min} 分钟` : `今天还能用 ${st.left} 次`);
    }
  }

  /* ---------------- DIY 点单（详情抽屉里的卡片） ---------------- */

  function mountDiy() {
    const host = document.querySelector(".sheet-body");
    if (!host || document.getElementById("lfDiy")) return;
    const card = document.createElement("article");
    card.className = "card lf-diy";
    card.id = "lfDiy";
    card.innerHTML = `
      <div class="card-head"><h2>亲手做一顿</h2><span class="hint" id="lfDiyNote">一天一次 · 不花好感度 · 分数你说了算</span></div>
      <input type="text" id="lfDiyText" maxlength="60" placeholder="给她一碗很辣的番茄牛腩面，汤要烫" />
      <div class="lf-score">
        <span>她得给这顿饭打</span>
        <input type="range" id="lfDiyScore" min="-10" max="20" step="1" value="0" />
        <b id="lfDiyNum">0</b>
      </div>
      <div class="lf-foot">
        <button class="ghost-btn" id="lfDiySend" type="button">端给她</button>
        <span class="hint" id="lfDiyResult"></span>
      </div>`;
    host.appendChild(card);
    const num = card.querySelector("#lfDiyNum");
    card.querySelector("#lfDiyScore").addEventListener("input", (e) => { num.textContent = e.target.value; });
    card.querySelector("#lfDiySend").addEventListener("click", sendDiy);
  }

  async function sendDiy() {
    if (busy) return;
    const text = document.getElementById("lfDiyText");
    const score = document.getElementById("lfDiyScore");
    const res = document.getElementById("lfDiyResult");
    if (!text || !text.value.trim()) { if (res) res.textContent = "总得写一句这是什么吧"; return; }
    busy = true;
    if (res) res.textContent = "她正在尝……";
    try {
      const d = await apiPost("panel/diy_feed", { text: text.value.trim(), score: Number(score.value) || 0 });
      if (res) res.textContent = d.line || "她吃了。";
      if (window.__lfDiyReact) window.__lfDiyReact(Number(d.score) || 0, d.react);
      await load(true);
    } catch (e) {
      if (res) res.textContent = (e && e.message) || "没接";
    } finally {
      busy = false;
    }
  }

  function renderDiy() {
    const card = document.getElementById("lfDiy");
    if (!card || !S) return;
    const d = S.diy || {};
    const btn = card.querySelector("#lfDiySend");
    const note = card.querySelector("#lfDiyNote");
    if (btn) btn.disabled = !!d.used_today || !d.enabled;
    if (note) note.textContent = d.used_today ? "今天已经做过一顿饭了。明天再来。"
      : "一天一次 · 不花好感度 · 分数你说了算";
  }

  /* ---------------- 她自己想的成就板 ---------------- */

  function mountButton() {
    if (document.getElementById("lfNoteBtn")) return;
    const host = document.querySelector(".scene-actions") || document.body;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.id = "lfNoteBtn";
    btn.className = "round-btn lf-note-open";
    btn.title = "她自己想的成就";
    btn.textContent = "🖋";
    const dot = document.createElement("span");
    dot.className = "lf-dot";
    dot.textContent = "0";
    btn.appendChild(dot);
    btn.addEventListener("click", openNotes);
    if (window.__bindTip) window.__bindTip(btn, "她自己想的成就");
    if (host === document.body) btn.style.position = "fixed";
    host.insertBefore(btn, host.firstChild);
  }

  function setBadge(n) {
    const btn = document.getElementById("lfNoteBtn");
    if (!btn) return;
    const dot = btn.querySelector(".lf-dot");
    if (dot) dot.textContent = String(n);
    btn.classList.toggle("has-new", n > 0);
  }

  async function openNotes() {
    notesOpen = true;
    let el = document.getElementById("lfNotes");
    if (!el) {
      el = document.createElement("div");
      el.id = "lfNotes";
      el.className = "lf-board";
      el.innerHTML = `
        <div class="lf-panel">
          <header class="lf-head">
            <h3>她自己想的成就</h3>
            <span class="lf-sub">条件是她提的，达没达成归程序说了算</span>
            <span class="lf-rule"></span>
            <button type="button" class="lf-close">收起</button>
          </header>
          <div class="lf-body"></div>
        </div>`;
      document.body.appendChild(el);
      el.addEventListener("click", (e) => { if (e.target === el) closeNotes(); });
      el.querySelector(".lf-close").addEventListener("click", closeNotes);
    }
    el.classList.add("open");
    await renderNotes();
  }

  function closeNotes() {
    notesOpen = false;
    const el = document.getElementById("lfNotes");
    if (el) el.classList.remove("open");
  }

  async function renderNotes() {
    const el = document.getElementById("lfNotes");
    if (!el) return;
    const body = el.querySelector(".lf-body");
    body.innerHTML = '<div class="lf-empty">正在看她的小本本…</div>';
    let data;
    try {
      data = DEMO ? await demoNotes() : unwrap(await VIEW.apiGet("panel/notes"));
    } catch (e) {
      body.innerHTML = '<div class="lf-empty">读不到了。</div>';
      return;
    }
    const items = (data && data.items) || [];
    if (!items.length) {
      body.innerHTML = '<div class="lf-empty">她还没想好要给自己立什么目标。<br>等你们之间多出几件记得住的事。</div>';
      return;
    }
    body.replaceChildren();
    items.forEach((it) => {
      const cell = document.createElement("div");
      cell.className = "lf-cell" + (it.done ? " is-done" : "");
      const t = document.createElement("div"); t.className = "lf-title"; t.textContent = it.title;
      const w = document.createElement("p"); w.className = "lf-why"; w.textContent = it.why;
      cell.append(t, w);
      if (it.evidence) {
        const ev = document.createElement("p");
        ev.className = "lf-ev";
        ev.textContent = "原话：" + it.evidence;
        cell.appendChild(ev);
      }
      if (!it.done) {
        const pg = document.createElement("div"); pg.className = "lf-p";
        const fill = document.createElement("i"); fill.style.width = Math.round((it.progress || 0) * 100) + "%";
        pg.appendChild(fill);
        cell.appendChild(pg);
      } else {
        const ok = document.createElement("span"); ok.className = "lf-ok"; ok.textContent = "✓ 她做到了";
        cell.appendChild(ok);
      }
      body.appendChild(cell);
    });
    const slots = document.createElement("div");
    slots.className = "lf-slotinfo";
    slots.textContent = `框：${items.length} / 6（满了三个都达成，她会再钉一个新框）`;
    body.appendChild(slots);
    setBadge(0);
  }

  /* ---------------- 数据 ---------------- */

  let toastTimer = 0;
  function toast(text) {
    let el = document.getElementById("lfToast");
    if (!el) {
      el = document.createElement("div");
      el.id = "lfToast";
      el.className = "lf-toast";
      (document.getElementById("tank") || document.body).appendChild(el);
    }
    el.textContent = text;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
  }

  async function load() {
    try {
      S = await apiGet("panel/state");
      S = { band: S.band || { name: "normal" }, steps: S.steps || {}, diy: S.diy || {}, notes: S.notes || {} };
    } catch (e) {
      if (DEMO) S = demoState();
      else return;
    }
    renderSteps();
    renderDiy();
    const doneNotes = (S.notes && S.notes.count) || 0;
    const btn = document.getElementById("lfNoteBtn");
    if (btn && !notesOpen) {
      const dot = btn.querySelector(".lf-dot");
      if (dot) dot.textContent = String(doneNotes);
      btn.classList.toggle("has-new", doneNotes > 0);
    }
  }

  async function boot() {
    mountSteps();
    mountDiy();
    mountButton();
    if (!DEMO) VIEW = await waitBridge(4000);
    await load();
    setInterval(() => { if (!document.hidden) load(); }, 10000);
    // 演示参数：?lfshut=1 演回避台阶（纯视觉）；?lfnote=1 直接开自成就板
    const qs = new URLSearchParams(location.search);
    if (DEMO && qs.get("lfshut") === "1") { demoBand = "shut"; await load(); }
    if (!DEMO && qs.get("lfshut") === "1" && S) {
      S.band = { name: "shut" };
      S.steps = { available: true, used: 1, left: 2, cooldown_min: 0, kinds: [
        { id: "knock", name: "敲敲门" }, { id: "note", name: "留张纸条" }, { id: "water", name: "递杯热水" }] };
      renderSteps();
    }
    if (qs.get("lfnote") === "1") setTimeout(openNotes, 500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
