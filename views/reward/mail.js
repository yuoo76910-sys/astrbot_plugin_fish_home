/* 信箱：右上角方形「信」按钮 → 左边按月分组，右边信纸。
   数据走 panel/letters、panel/letter_read、panel/letter_write，不碰 app.js 的状态。 */

(function () {
  "use strict";

  let VIEW = null;
  let D = { letters: [], unread: 0, cost: 20, balance: 0 };
  let curId = "";
  let busy = false;

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
    const host = $("mailBox") || $("mail");
    if (!host) return;
    const old = host.querySelector(".mail-toast");
    if (old) old.remove();
    const el = document.createElement("div");
    el.className = "mail-toast " + (kind || "");
    el.textContent = text;
    host.appendChild(el);
    setTimeout(() => el.remove(), 2600);
  }

  function monthLabel(m) {
    const parts = String(m || "").split("-");
    if (parts.length < 2) return "更早";
    return `${parts[0]} 年 ${Number(parts[1])} 月`;
  }

  function renderList() {
    const host = $("mailList");
    if (!host) return;
    host.replaceChildren();
    const rows = D.letters || [];
    if (!rows.length) {
      const p = document.createElement("p");
      p.className = "paper-empty";
      p.textContent = "信箱是空的。";
      host.appendChild(p);
      return;
    }
    let lastMonth = "";
    rows.forEach((r) => {
      if (r.month !== lastMonth) {
        lastMonth = r.month;
        const h = document.createElement("div");
        h.className = "mail-month";
        h.textContent = monthLabel(r.month);
        host.appendChild(h);
      }
      const b = document.createElement("button");
      b.type = "button";
      b.className = "mail-item" + (r.read ? " read" : "") + (r.id === curId ? " on" : "");
      b.dataset.id = r.id;

      const t = document.createElement("span");
      t.className = "t";
      t.textContent = r.title || "信";
      if (r.kind === "urgent") {
        const tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = "加急";
        t.appendChild(tag);
      }
      const d = document.createElement("span");
      d.className = "d";
      d.textContent = `${r.date} ${r.clock}`;
      const dot = document.createElement("i");
      dot.className = "dot";
      b.append(t, d, dot);
      b.addEventListener("click", () => openLetter(r.id));
      host.appendChild(b);
    });
  }

  function renderPaper(r) {
    const inner = $("paperInner");
    if (!inner) return;
    inner.replaceChildren();
    if (!r) {
      const p = document.createElement("p");
      p.className = "paper-empty";
      p.textContent = "左边点一封看。";
      inner.appendChild(p);
      return;
    }
    const head = document.createElement("div");
    head.className = "paper-head";
    head.textContent = `${r.date} ${r.clock} · ${r.title}`;
    const body = document.createElement("p");
    body.className = "paper-body";
    body.textContent = r.body || "";
    inner.append(head, body);

    if ((r.refs || []).length) {
      const refs = document.createElement("div");
      refs.className = "paper-refs";
      const b = document.createElement("b");
      b.textContent = "写这封信时她记着你说过：";
      refs.appendChild(b);
      (r.refs || []).forEach((q) => {
        const s = document.createElement("div");
        s.textContent = `「${q}」`;
        refs.appendChild(s);
      });
      inner.appendChild(refs);
    }
  }

  function renderSub() {
    const sub = $("mailSub");
    if (sub) {
      const n = Number(D.unread || 0);
      sub.innerHTML = n > 0 ? `<b>${n}</b> 封没读` : "都读过了";
    }
    const cost = $("mailCost");
    if (cost) cost.textContent = String(D.cost || 20);
    const w = $("mailWrite");
    if (w) w.disabled = busy || Number(D.balance || 0) < Number(D.cost || 20);
  }

  async function refresh() {
    try {
      D = await apiGet("panel/letters");
      renderSub();
      renderList();
      const hit = (D.letters || []).find((x) => x.id === curId);
      renderPaper(hit || (D.letters || [])[0] || null);
      paintBadge(D.unread);
    } catch (e) {
      toast(e.message || "读不到信箱", "err");
    }
  }

  function paintBadge(n) {
    const b = $("btnMail");
    if (b) b.classList.toggle("has-unread", Number(n || 0) > 0);
  }

  async function badgePoll() {
    if (!VIEW) return;
    try {
      const d = await apiGet("panel/letters");
      paintBadge(d.unread);
    } catch (e) { /* 下一轮 */ }
  }

  async function openLetter(id) {
    const r = (D.letters || []).find((x) => x.id === id);
    if (!r) return;
    curId = id;
    renderPaper(r);
    renderList();
    if (!r.read) {
      try {
        const res = await apiPost("panel/letter_read", { id });
        r.read = true;
        D.unread = Number(res.unread || 0);
        renderSub();
        renderList();
      } catch (e) { /* 读不到就算了 */ }
    }
  }

  async function writeOne() {
    if (busy) return;
    busy = true;
    renderSub();
    try {
      const res = await apiPost("panel/letter_write", {});
      D.balance = Number(res.balance || 0);
      D.letters = res.letters || D.letters;
      D.unread = (D.letters || []).filter((x) => !x.read).length;
      curId = (D.letters || [])[0] ? D.letters[0].id : curId;
      renderSub();
      renderList();
      renderPaper((D.letters || [])[0] || null);
      toast("她把信放在桌上了", "ok");
    } catch (e) {
      toast(e.message || "写不出来", "err");
    } finally {
      busy = false;
      renderSub();
    }
  }

  function open() {
    const box = $("mail");
    if (!box) return;
    box.hidden = false;
    refresh();
  }

  function close() {
    const box = $("mail");
    if (!box) return;
    box.hidden = true;
  }

  async function boot() {
    const box = $("mail");
    if (!box) return;
    VIEW = await waitBridge(4000);
    $("btnMail") && $("btnMail").addEventListener("click", open);
    $("mailClose") && $("mailClose").addEventListener("click", close);
    $("mailWrite") && $("mailWrite").addEventListener("click", writeOne);
    box.addEventListener("click", (ev) => { if (ev.target === box) close(); });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && !box.hidden) close();
    });
    badgePoll();
    setInterval(badgePoll, 60000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
