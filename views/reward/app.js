/* 鱼的窝 · 鱼的窝前端
   布局：整屏鱼缸（鱼是唯一主角）+ 底部投喂台（约 10%）。
   数据来自插件注册的 /astrbot_plugin_fish_home/panel/state。
   支持 ?demo=1 本地预览（截图验收用），不依赖 AstrBot 桥接。 */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const DEMO = params.get("demo") === "1" || location.protocol === "file:";
  const API_STATE = "panel/state";
  const API_SEND = "panel/send";
  const API_READ = "panel/read";
  const API_TAP = "panel/tap";

  let VIEW = null;
  let DATA = null;
  let FILTER = "all";
  let BUSY = false;
  let pendingSig = "";

  const PARTICLE_CHARS = {
    heart: ["♥", "♡", "♥"],
    star: ["✦", "✧", "★"],
    bubble: ["○", "◦", "◌"],
    note: ["♪", "♫", "♬"],
    confetti: ["▪", "▫", "◆", "◇"],
    sparkle: ["✧", "✶", "✷"],
  };

  const METER_TINT = {
    intimacy: "#ff5c8a",
    joy: "#ff9f1c",
    attachment: "#8b5cf6",
    soothe: "#34d399",
    wish: "#38bdf8",
  };
  const RARITY_COLOR = {
    common: "#9db6c9", rare: "#7fd8e8", epic: "#b7a4ff", legendary: "#ffd166", secret: "#ff9ec4",
  };

  /* ---------------- 工具 ---------------- */

  const clamp = (v, lo = 0, hi = 100) => Math.max(lo, Math.min(hi, Number(v) || 0));
/* 走动边界：按“脚点 + 地板”算。BG 是 cover 铺的，先把脚点换算回背景图坐标再夹。 */
const WALK_BG={w:960,h:540};
const WALK_FLOOR={x0:0.02,y0:0.415,x1:0.98,y1:0.995};
function clampFootToFloor(el,nx,ny){
  const box=el.parentElement.getBoundingClientRect();
  if(!box.width||!box.height) return {x:nx,y:ny};
  const s=Math.max(box.width/WALK_BG.w, box.height/WALK_BG.h);
  const ox=(box.width-WALK_BG.w*s)/2, oy=(box.height-WALK_BG.h*s)/2;
  const h=(el.getBoundingClientRect().height)||80;
  const fx=(nx/100)*box.width;
  const fy=(ny/100)*box.height + h/2;
  let ix=(fx-ox)/(WALK_BG.w*s), iy=(fy-oy)/(WALK_BG.h*s);
  ix=Math.min(WALK_FLOOR.x1,Math.max(WALK_FLOOR.x0,ix));
  iy=Math.min(WALK_FLOOR.y1,Math.max(WALK_FLOOR.y0,iy));
  return {x:((ox+ix*WALK_BG.w*s)/box.width)*100, y:(((oy+iy*WALK_BG.h*s)-h/2)/box.height)*100};
}

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function fmt(v, nd = 1) {
    const n = Number(v) || 0;
    if (Math.abs(n - Math.round(n)) < 1e-6) return String(Math.round(n));
    return n.toFixed(nd);
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtClock(ts) {
    if (!ts) return "—";
    const d = new Date(Number(ts) * 1000);
    return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }

  function unwrap(data) {
    if (data && typeof data === "object" && data.data && data.gifts === undefined) return data.data;
    return data;
  }

  function waitForBridge(timeout = 2600) {
    return new Promise((resolve) => {
      const started = Date.now();
      const tick = () => {
        const v = window.AstrBotPluginView || window.AstrBotPluginPage || null;
        if (v) return resolve(v);
        if (Date.now() - started >= timeout) return resolve(null);
        setTimeout(tick, 40);
      };
      tick();
    });
  }

  function toast(msg) {
    const el = $("toast");
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
    requestAnimationFrame(() => el.classList.add("show"));
    clearTimeout(toast._t);
    toast._t = setTimeout(() => {
      el.classList.remove("show");
      setTimeout(() => { el.hidden = true; }, 240);
    }, 2600);
  }

  function applyTheme(view) {
    const forced = params.get("theme");
    if (forced === "light" || forced === "dark") {
      document.documentElement.dataset.theme = forced;
      return;
    }
    if (view && typeof view.onContext === "function") {
      view.onContext((ctx) => {
        if (ctx && typeof ctx.isDark === "boolean") {
          document.documentElement.dataset.theme = ctx.isDark ? "dark" : "light";
        }
      });
      return;
    }
    document.documentElement.dataset.theme =
      window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }

  /* ---------------- 鱼（站定不动，只有待机小动作） ---------------- */

  const FISH_X = 50;
  /* 新场景（落地窗）里没有地板，她站在窗台正下方那面砖墙上。
     57% ≈ 脚底落在窗台线下面一点点，和预览图 预览_night_clear.png 对齐；
     旧房间（白纸方格）是 72%，要换回去记得改回来。 */
  const FISH_Y = 57;

  class FishController {
    constructor(el) {
      this.el = el;
      this.x = FISH_X;
      this.y = FISH_Y;
      this.frame = 0;
      this.frameClock = 0;
      this.raf = 0;
      this.last = 0;
      this.paused = false;
      this.held = new Set();
      this.vx = 0;
      this.vy = 0;
      this.speed = 26;
      this.loop = this.loop.bind(this);
      this.applyPosition();
      this.setFrame(0);
      this.raf = requestAnimationFrame(this.loop);
      document.addEventListener("visibilitychange", () => {
        this.paused = document.hidden;
      });
    }

    applyPosition() {
      this.el.style.left = `${this.x}%`;
      this.el.style.top = `${this.y}%`;
    }

    setFrame(f) {
      this.frameNo = (f % 8 + 8) % 8;
      this.el.style.backgroundPosition = `${this.frameNo * (100 / 7)}% 0`;
    }

    setSpeed() {
      /* 站定不动，心情只影响待机节奏由 CSS 处理 */
    }

    /** 按住的键 -> 速度（% / 秒），松手会滑停 */
    tick(dt) {
      const h = this.held;
      let dx = (h.has("right") ? 1 : 0) - (h.has("left") ? 1 : 0);
      let dy = (h.has("down") ? 1 : 0) - (h.has("up") ? 1 : 0);
      if (dx && dy) {
        const inv = 1 / Math.SQRT2;
        dx *= inv;
        dy *= inv;
      }
      const k = Math.min(1, dt / 70);
      this.vx += (dx * this.speed - this.vx) * k;
      this.vy += (dy * this.speed - this.vy) * k;
      if (Math.abs(this.vx) < 0.5) this.vx = 0;
      if (Math.abs(this.vy) < 0.5) this.vy = 0;

      const moving = this.vx !== 0 || this.vy !== 0;
      if (moving) {
        const _p = clampFootToFloor(this.el, this.x + (this.vx * dt) / 1000, this.y + (this.vy * dt) / 1000);
        this.x = _p.x; this.y = _p.y;
        this.el.style.left = `${this.x}%`;
        this.el.style.top = `${this.y}%`;
        if (Math.abs(this.vx) > 1) this.el.classList.toggle("flip", this.vx < 0);
      }
      this.el.classList.toggle("walking", moving);
      return moving;
    }

    /** 键盘：true = 按下 */
    setKey(dir, down) {
      if (down) this.held.add(dir);
      else this.held.delete(dir);
    }

    clearKeys() {
      this.held.clear();
    }

    face(dx) {
      if (Math.abs(dx) < 4) return;
      this.el.classList.toggle("flip", dx < 0);
    }

    /** 按住 = 压下去，松手 = 回弹 */
    setPressed(on) {
      this.el.classList.toggle("pressed", !!on);
    }

    react() {
      this.el.classList.remove("happy");
      void this.el.offsetWidth;
      this.el.classList.add("happy");
      setTimeout(() => this.el.classList.remove("happy"), 1450);
    }

    /** 鱼嘴在视口里的位置（喂食落点） */
    mouth() {
      /* 换成正面小人以后没有「朝右的鱼嘴」了，落点就是脸中间偏下一点 */
      const r = this.el.getBoundingClientRect();
      return {
        x: r.left + r.width / 2,
        y: r.top + r.height * 0.46,
        rect: r,
      };
    }

    loop(t) {
      if (this.paused) {
        this.last = t;
        this.raf = requestAnimationFrame(this.loop);
        return;
      }
      const dt = Math.min(50, t - (this.last || t));
      this.last = t;
      const moving = this.tick(dt);
      if (typeof positionTalk === "function") positionTalk();
      this.frameClock += dt;
      if (this.frameClock > (moving ? 70 : 70)) {
        this.frameClock = 0;
        this.frame = (this.frame + 1) % 8;
        this.setFrame(this.frame);
      }
      this.raf = requestAnimationFrame(this.loop);
    }
  }

  const MOVE_KEYS = {
    ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
    w: "up", s: "down", a: "left", d: "right",
    W: "up", S: "down", A: "left", D: "right",
    up: "up", down: "down", left: "left", right: "right",
  };

  let fishCtl = null;


  function positionTalk() {
    const el = $("talkBubble");
    if (!el || !fishCtl) return;
    el.style.top = `${fishCtl.y - 10}%`;
    const tank = $("tank");
    if (!tank) { el.style.left = `${fishCtl.x}%`; return; }
    // 气泡是按中心定位的：她走到边上时可用宽度只剩几个字，会被挤成一条竖排纸
    // 所以先按百分比放，再用实测宽度把它拉回容器内
    el.style.left = `${fishCtl.x}%`;
    const tr = tank.getBoundingClientRect();
    const w = el.offsetWidth || 120;
    const half = w / 2 + 6;
    const cx = Math.min(Math.max((fishCtl.x / 100) * tr.width, half), Math.max(half, tr.width - half));
    el.style.left = `${Math.round(cx)}px`;
  }

  function hideTalkBubble() {
    const el = $("talkBubble");
    if (!el) return;
    el.hidden = true;
    el.classList.remove("typing");
  }

  /* 气泡只用来表示「她在回话」：三点跳动，不随机说话。 */
  function showTalkBubble() {
    const el = $("talkBubble");
    const txt = $("talkText");
    if (!el || !txt) return;
    txt.textContent = "";
    el.hidden = false;
    el.classList.add("typing");
    positionTalk();
  }

  /* ---------------- 演示数据 ---------------- */

  const DEMO_INFO = [
    ["candy", "food", "加倍佳棒棒糖", "🍬", "common", "她最爱的酸甜味，含在嘴里会安静一会儿", "亲密 +6 · 喜悦 +4 · 安抚 +1", "每天陪她聊满 10 句", 3, true, 2, { daily: true, target: 10, current: 10, claimed: true }],
    ["milk", "drink", "草莓牛奶", "🥛", "common", "草莓味的，喝完会把瓶子留给你", "亲密 +6 · 喜悦 +4 · 安抚 +1", "聊天累计 30 句", 2, true, 1, { cond: ["聊天总量 ≥ 30", 128, 30] }],
    ["fish_snack", "food", "小鱼干", "🐟", "common", "咸香的小鱼干，嘴上嫌弃，手很诚实", "亲密 +7 · 喜悦 +5 · 安抚 +1", "聊天累计 60 句", 1, true, 1, { cond: ["聊天总量 ≥ 60", 128, 60] }],
    ["cupcake", "food", "纸杯小蛋糕", "🧁", "rare", "奶油和糖霜，适合心情好的下午", "亲密 +8 · 喜悦 +6 · 安抚 +2", "聊天累计 100 句", 0, true, 0, { cond: ["聊天总量 ≥ 100", 128, 100] }],
    ["star_hairpin", "item", "星星发夹", "⭐", "rare", "别在头发上的星星，晚上会反一点光", "亲密 +9 · 喜悦 +6 · 依恋 +3", "历史最高亲密 50 + 当前心愿 40", 1, true, 1, { cond: ["历史最高亲密 ≥ 50", 61, 50], cond2: ["当前心愿 ≥ 40", 72, 40] }],
    ["letter", "item", "手写信", "💌", "rare", "手写的信，比消息慢，但能留很久", "亲密 +10 · 喜悦 +7 · 安抚 +3", "历史最高亲密 60 + 当前心愿 55", 0, true, 1, { cond: ["历史最高亲密 ≥ 60", 61, 60], cond2: ["当前心愿 ≥ 55", 72, 55] }],
    ["plush", "item", "胖鱼玩偶", "🧸", "rare", "一只软乎乎的胖鱼，抱起来刚好", "亲密 +11 · 喜悦 +8 · 依恋 +4", "聊天累计 200 句", 0, false, 0, { cond: ["聊天总量 ≥ 200", 128, 200] }],
    ["firework", "item", "一起看烟花", "🎆", "epic", "一起看的烟花，才算数", "亲密 +12 · 喜悦 +10 · 安抚 +4", "历史最高亲密 70 + 当前心愿 70", 0, false, 0, { cond: ["历史最高亲密 ≥ 70", 61, 70], cond2: ["当前心愿 ≥ 70", 72, 70] }],
    ["song", "item", "专属小情歌", "🎵", "epic", "只唱给你听的小调，歌词是她现编的", "亲密 +13 · 喜悦 +10 · 安抚 +5", "聊天累计 365 句", 0, false, 0, { cond: ["聊天总量 ≥ 365", 128, 365] }],
    ["balloon", "item", "一束气球", "🎈", "epic", "一束会飞的气球，幼稚但开心", "亲密 +14 · 喜悦 +8 · 安抚 +3", "聊天累计 520 句", 0, false, 0, { cond: ["聊天总量 ≥ 520", 128, 520] }],
    ["cake", "food", "生日蛋糕", "🎂", "epic", "蛋糕上写她的名字，许愿时替你也许了一个", "亲密 +16 · 喜悦 +12 · 安抚 +5", "历史最高亲密 80 + 当前心愿 80", 0, false, 0, { cond: ["历史最高亲密 ≥ 80", 61, 80], cond2: ["当前心愿 ≥ 80", 72, 80] }],
    ["key", "item", "她房间的钥匙", "🗝️", "legendary", "一把钥匙，意思是你可以随时回来", "亲密 +18 · 喜悦 +12 · 依恋 +8", "历史最高亲密 90 + 当前心愿 90", 0, false, 0, { cond: ["历史最高亲密 ≥ 90", 61, 90], cond2: ["当前心愿 ≥ 90", 72, 90] }],
    ["heart", "item", "一颗真心", "❤️", "legendary", "她最后能拿出来的东西，收不回去", "亲密 +25 · 喜悦 +20 · 依恋 +10", "？？？", 0, false, 0, { hidden: true }],
    ["lullaby", "item", "她等你的歌", "🎶", "secret", "她一个人等到很晚时哼的歌，你偶然听见了", "亲密 +12 · 喜悦 +6 · 依恋 +6", "？？？", 0, false, 0, { hidden: true }],
  ];

  function demoGifts() {
    return DEMO_INFO.map((row) => {
      const [id, kind, name, emoji, rarity, desc, effect, source, count, unlocked, sent, extra = {}] = row;
      const conditions = [];
      let progress = unlocked ? 1 : 0;
      if (extra.cond) {
        const [text, cur, target] = extra.cond;
        const met = cur >= target;
        conditions.push({ text, current: cur, target, met, progress: Math.min(1, cur / target) });
        progress = Math.min(1, cur / target);
      }
      if (extra.cond2) {
        const [text, cur, target] = extra.cond2;
        const met = cur >= target;
        conditions.push({ text, current: cur, target, met, progress: Math.min(1, cur / target) });
        progress = Math.min(progress, Math.min(1, cur / target));
      }
      const hidden = !!extra.hidden;
      const revealed = !hidden;
      const canSend = !!unlocked && count > 0;
      return {
        id, kind, name: hidden ? "？？？" : name, emoji: hidden ? "❓" : emoji,
        mood: ({ candy: "liked", milk: "bored", star_hairpin: "missed" }[id] || null)
          ? { state: ({ candy: "liked", milk: "bored", star_hairpin: "missed" }[id]), days: id === "milk" ? 2 : 0 }
          : null,
        icon: hidden ? "" : `${id}.png`,
        rarity, rarity_cn: hidden ? "未知" : ({ common: "普通", rare: "稀有", epic: "史诗", legendary: "传说", secret: "隐藏" }[rarity] || "普通"),
        desc: hidden ? "？？？" : desc,
        effect_text: hidden ? "？？？" : effect,
        source: hidden ? "？？？" : source,
        hidden, revealed,
        hint: hidden ? (id === "heart" ? "把很多心意都送出去之后，她会不会把心也给你" : "她等你等到很晚的时候，会一个人哼歌") : "",
        unlocked: !!unlocked, count, sent, daily: !!extra.daily, daily_target: extra.target || 0,
        daily_current: extra.current || 0, daily_claimed: !!extra.claimed,
        conditions: hidden ? [] : conditions, progress,
        can_send: canSend && !hidden, block_reason: canSend && !hidden ? "" : (unlocked ? "仓库里没有这份礼物" : "还没解锁"),
        gif: `./assets/reactions/${id}.gif`,
        animation: { particle: rarity === "legendary" ? "heart" : rarity === "secret" ? "note" : rarity === "epic" ? "confetti" : "star", color: rarity === "legendary" ? "#ff5c8a" : rarity === "secret" ? "#a78bfa" : rarity === "epic" ? "#ff8fab" : "#ffd166" },
      };
    });
  }

  function demoState() {
    return {
      version: "0.1.0-demo", now: Date.now() / 1000, enabled: true, title: "鱼的窝",
      character: { name: "她" },
      level: { n: 3, title: "", min: 0, next_at: null, next_title: null, progress: 0.42 },
      meters: {
        intimacy: { key: "intimacy", label: "亲密度", value: 42.4, level: 3, peak: 96.2, max: 100, feeds_heart: "intimacy", step: 10 },
        joy: { key: "joy", label: "喜悦", value: 78.2, level: 5, peak: 100, max: 100, feeds_heart: "joy", step: 10 },
        attachment: { key: "attachment", label: "依恋", value: 61.0, level: 2, peak: 88.4, max: 100, feeds_heart: "attachment", step: 10 },
        soothe: { key: "soothe", label: "安抚", value: 24.6, level: 1, peak: 71.0, max: 100, feeds_heart: "stress", step: 10 },
      },
      feed: { step: 10, total: 11, backlog: {} },
      intimacy: {
        current: 42.4, peak: 96.2, level: 3, source: "local", source_label: "攒满 100 反哺鱼的心 +10",
        mood: "黏人", mode: "calm", mode_cn: "平静", expectation: 43, attachment: 61, joy: 78.2, soothe: 24.6, stress: 12,
      },
      wish: {
        value: 72.4, peak: 81.0, trend: "up",
        events: [
          { delta: 8, reason: "及时回她", ts: Date.now() / 1000 - 600 },
          { delta: 3, reason: "解锁礼物：手写信", ts: Date.now() / 1000 - 2100 },
          { delta: 2, reason: "陪她聊天", ts: Date.now() / 1000 - 2900 },
          { delta: 18, reason: "完成今日心愿：想被你夸一句", ts: Date.now() / 1000 - 7200 },
        ],
      },
      daily_wish: { id: "praise", text: "想被你夸一句", hint: "夸夸她，说她哪里可爱、哪里厉害", done: false, line: "" },
      chat: {
        total: 128, today: 12, streak: 4,
        days: [
          { date: "", label: "10-27", count: 4 }, { date: "", label: "10-28", count: 7 },
          { date: "", label: "10-29", count: 3 }, { date: "", label: "10-30", count: 9 },
          { date: "", label: "10-31", count: 6 }, { date: "", label: "11-01", count: 8 },
          { date: "", label: "今天", count: 12 },
        ],
      },
      gifts: demoGifts(),
      radar: [
        { key: "intimacy", label: "亲密", value: 54 },
        { key: "joy", label: "喜悦", value: 86 },
        { key: "attachment", label: "依恋", value: 63 },
        { key: "curiosity", label: "好奇", value: 48 },
        { key: "reflection", label: "反刍", value: 31 },
        { key: "duty", label: "责任", value: 42 },
        { key: "social", label: "社交", value: 57 },
        { key: "fatigue", label: "疲惫", value: 23 },
        { key: "stress", label: "压力", value: 18 },
      ],
      moods: [
        { key: "calm", label: "平静", value: 86, active: true },
        { key: "wronged", label: "委屈", value: 7, active: false },
        { key: "afraid", label: "害怕", value: 0, active: false },
        { key: "shut", label: "回避", value: 0, active: false },
      ],
      furniture: [
        { id: "bowl", kind: "furniture", name: "食盆", emoji: "🍚", x: 43, y: 16, w: 8, unlocked: true, conditions: [], progress: 1, lines: ["盆里还剩一点，她舔干净了。"] },
        { id: "bili_badge", kind: "badge", svg: "bilibili", name: "B站观众牌", emoji: "📺", x: 76, y: 56, w: 5.4, unlocked: true, conditions: [], progress: 1, lines: ["她把这块牌子挂在墙上，说是在 B 站混过的证明。"] },
        { id: "patch_rug", kind: "furniture", name: "拼接地毯", emoji: "🟫", x: 50, y: 15, w: 20, unlocked: true, conditions: [], progress: 1, lines: ["四种颜色的布拼在一起，是她自己缝的。"] },
      ],
      pending: [{ kind: "unlock", text: "她翻出了一份「手写信」（历史最高亲密 60 + 当前心愿 55），去鱼的窝里给她吧。", ts: Date.now() / 1000 - 2100, notified: false }],
      history: [
        { ts: Date.now() / 1000 - 600, kind: "send", gift: "candy", name: "加倍佳棒棒糖", delta: 6, before: 36.4, after: 42.4, fed: ["joy"] },
        { ts: Date.now() / 1000 - 2100, kind: "unlock", gift: "letter", name: "手写信", source: "历史最高亲密 60 + 当前心愿 55" },
        { ts: Date.now() / 1000 - 7300, kind: "send", gift: "milk", name: "草莓牛奶", delta: 6, before: 30.4, after: 36.4, fed: [] },
      ],
      last_reaction: {
        gift: "candy", name: "加倍佳棒棒糖", line: "哼，还知道我爱吃这个\n糖我收下了，人也可以多留一会儿",
        delta: 6, before: 36.4, after: 42.4, fed: ["joy"], ts: Date.now() / 1000 - 600,
      },
      limits: { daily_limit: 5, sent_today: 2, cooldown_left: 0, sendable: 4 },
      clues: [
        "「一起看烟花」已经很近了：历史最高亲密 70 + 当前心愿 70",
        "她等你等到很晚的时候，会一个人哼歌",
      ],
      owner_configured: true,
      thoughts: [{ text: "想被你夸一句", kind: "praise" }],
    };
  }

  function demoSend(id) {
    const payload = JSON.parse(JSON.stringify(DATA));
    const gift = (payload.gifts || []).find((g) => g.id === id);
    if (!gift) throw new Error("没有这件礼物");
    if (!gift.can_send) throw new Error(gift.block_reason || "现在还不能送");
    const gain = { intimacy: 6, joy: 4, attachment: 1, soothe: 1 };
    const meters = payload.meters || (payload.meters = {});
    const fed = [];
    const changes = {};
    Object.keys(gain).forEach((key) => {
      const m = meters[key] || (meters[key] = { key, value: 0, level: 0, peak: 0, max: 100, step: 10 });
      const before = Number(m.value) || 0;
      let after = before + gain[key];
      let levels = 0;
      while (after >= 100) { after -= 100; levels += 1; }
      m.value = Math.round(after * 10) / 10;
      m.level = (m.level || 0) + levels;
      m.peak = Math.max(Number(m.peak) || 0, Math.min(100, before + gain[key]));
      if (levels) { fed.push(key); payload.feed.total = (payload.feed.total || 0) + levels; }
      changes[key] = { before: Math.round(before * 10) / 10, after: m.value, delta: gain[key], levels };
    });
    const im = changes.intimacy;
    payload.intimacy.current = meters.intimacy.value;
    payload.intimacy.peak = Math.max(payload.intimacy.peak, meters.intimacy.peak);
    payload.intimacy.level = meters.intimacy.level;
    payload.level = { ...payload.level, n: meters.intimacy.level, progress: meters.intimacy.value / 100 };
    gift.count = Math.max(0, gift.count - 1);
    gift.sent = (gift.sent || 0) + 1;
    gift.can_send = gift.count > 0;
    gift.block_reason = gift.can_send ? "" : "仓库里没有这份礼物";
    payload.wish.value = Math.min(100, Number(payload.wish.value) + 2);
    payload.wish.peak = Math.max(payload.wish.peak, payload.wish.value);
    payload.limits.sent_today = (payload.limits.sent_today || 0) + 1;
    const line = gift.id === "candy" ? "哼，还知道我爱吃这个\n糖我收下了，人也可以多留一会儿" : "……谢谢\n这次我记住了";
    payload.last_reaction = { gift: gift.id, name: gift.name, line, delta: im.delta, before: im.before, after: im.after, meters: changes, fed, ts: Date.now() / 1000 };
    payload.history = [{ ts: Date.now() / 1000, kind: "send", gift: gift.id, name: gift.name, delta: im.delta, before: im.before, after: im.after, meters: changes, fed }, ...(payload.history || [])];
    payload.result = { ok: true, gift: gift.id, name: gift.name, line, delta: im.delta, before: im.before, after: im.after, meters: changes, fed, newly_unlocked_names: [], daily_wish_done: false };
    DATA = payload;
    return payload;
  }

  /* ---------------- 渲染 ---------------- */

  function radarSvg(dims) {
    const W = 160;
    const C = 80;
    const R = 44;
    const LR = 62;
    const pt = (i, r) => {
      const a = ((-90 + i * (360 / Math.max(1, dims.length))) * Math.PI) / 180;
      return [C + Math.cos(a) * r, C + Math.sin(a) * r];
    };
    const ring = (k) => dims.map((_, i) => pt(i, R * k).map((n) => n.toFixed(1)).join(",")).join(" ");
    const shape = dims.map((d, i) => pt(i, (R * clamp(d.value)) / 100).map((n) => n.toFixed(1)).join(",")).join(" ");
    const axes = dims
      .map((_, i) => {
        const [x, y] = pt(i, R);
        return `<line x1="${C}" y1="${C}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}"/>`;
      })
      .join("");
    const dots = dims
      .map((d, i) => {
        const [x, y] = pt(i, (R * clamp(d.value)) / 100);
        return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="1.7"><title>${esc(d.label)} ${fmt(d.value)}</title></circle>`;
      })
      .join("");
    const labels = dims
      .map((d, i) => {
        const [x, y] = pt(i, LR);
        return `<text x="${x.toFixed(1)}" y="${y.toFixed(1)}">${esc(d.label)}</text>`;
      })
      .join("");
    return `<svg viewBox="0 0 ${W} ${W}" class="radar" role="img" aria-label="九维状态">
      <g class="grid"><polygon points="${ring(0.33)}"/><polygon points="${ring(0.66)}"/><polygon points="${ring(1)}"/>${axes}</g>
      <polygon class="shape" points="${shape}"/>
      <g class="dots">${dots}</g>
      <g class="labels">${labels}</g>
    </svg>`;
  }

  function renderPanel(data) {
    // 演示用：?demo=1&aff=-12 可以直接看负值/掉光的样子
    if (DEMO && params.get("aff") != null) {
      const v = Number(params.get("aff"));
      if (!Number.isNaN(v)) {
        data = Object.assign({}, data, {
          meters: Object.assign({}, data.meters, {
            intimacy: Object.assign({}, (data.meters || {}).intimacy || {}, { value: v }),
          }),
        });
      }
    }
    const intimacy = data.intimacy || {};
    const wish = data.wish || {};
    const level = data.level || {};
    const chat = data.chat || {};
    const meters = data.meters || {};
    const charName = (data.character && data.character.name) || "她";

    const name = $("charName");
    if (name) name.textContent = charName;
    // 标签页标题也跟着她的名字走，换了人不会还写着「鱼的窝」
    if (charName) document.title = `${charName}的窝`;
    const lv = $("levelBadge");
    if (lv) lv.textContent = `${level.n || 0}层`;
    const mood = $("moodBadge");
    if (mood) mood.textContent = intimacy.mood || "平常";
    const cr = $("creativeBadge");
    if (cr) cr.hidden = !data.creative;
    const wv = $("wishValue");
    if (wv) wv.textContent = fmt(wish.value);
    const tc = $("todayChat");
    if (tc) tc.textContent = String(chat.today || 0);

    // 左上角：工资条（按挂机时长算，满 target_hours 到 100）
    const wage = data.wage || {};
    const cur = Number(wage.value != null ? wage.value : 0) || 0;
    const maxV = 100;

    const fill = $("affFill");
    const numEl = $("affNum");
    const nameEl = $("affName");
    if (fill) {
      fill.classList.remove("neg");
      fill.style.width = `${Math.max(0, Math.min(100, cur)).toFixed(1)}%`;
    }
    if (numEl) numEl.textContent = String(Math.round(cur));
    if (nameEl) {
      nameEl.classList.remove("neg");
      nameEl.textContent = "工资";
    }

    // 三档直接跟着鱼心的行为档；徽章就写那五个字：日常/委屈/生气/害怕/回避
    const tier = String(((data.favor) || {}).tier || "normal");
    const tmode = String(((data.favor) || {}).mode || "");
    const MODE_CN = { calm: "日常", wronged: "委屈", angry: "生气", afraid: "害怕", shut: "回避" };
    if (mood) {
      mood.textContent = MODE_CN[tmode] || (tier === "gone" ? "回避" : (intimacy.mood || "平常"));
      mood.dataset.tier = tier;
      mood.dataset.mode = tmode;
      const hrs = Number(wage.hours || 0);
      const target = Number(wage.target_hours || 6);
      mood.title = `挂机 ${hrs.toFixed(1)} 小时（满 ${target} 小时算满勤）· 心情：${intimacy.mood || "平常"}`;
    }
    renderPurse(data);
    applyTier(data);
  }

  function renderMeterList(data) {
    const host = $("meterList");
    if (!host) return;
    const meters = data.meters || {};
    host.replaceChildren();
    ["intimacy", "joy", "attachment", "soothe"].forEach((key) => {
      const m = meters[key] || {};
      const tint = METER_TINT[key] || "#7fd8e8";
      const row = document.createElement("div");
      row.className = "meter-row";
      row.innerHTML =
        `<span>${esc(m.label || key)}</span>` +
        `<div class="bar"><i style="width:${clamp(m.value)}%;--c1:${tint}"></i></div>` +
        `<b>${fmt(m.value)} / 100 · ${m.level || 0}层</b>`;
      host.appendChild(row);
    });
    const note = $("feedNote");
    if (note) {
      const step = (data.feed && data.feed.step) == null ? 10 : data.feed.step;
      note.textContent = `吃的东西会直接补到九维图上｜今天已吃 ${(data.feed && data.feed.total) || 0} 次`;
    }
  }

  let hotSel = 0;
  let hotLocked = false;
  let hintDone = false;

  function hotbarGifts(data) {
    return ((data && data.gifts) || []).filter((g) => g.unlocked);
  }

  const HEART_MAP = [
    "..###.###..",
    ".#########.",
    "###########",
    "###########",
    "###########",
    ".#########.",
    "..#######..",
    "...#####...",
    "....###....",
    ".....#.....",
  ];
  // 左上角的高光（相对坐标）
  const HEART_GLINT = new Set(["2,1", "3,1", "2,2", "1,2"]);

  /** 像素爱心：broken=true 就是灰的、从中间裂开、右半掉下去 */
  function heartSvg(broken) {
    const cell = 2;
    const rows = HEART_MAP;
    const base = broken ? "#9e9ea8" : "#ff2d55";
    const shade = broken ? "#787883" : "#c81e3f";
    const glint = broken ? "#b9b9c2" : "#ff9cb0";
    let rects = "";
    rows.forEach((row, y) => {
      [...row].forEach((ch, x) => {
        if (ch !== "1" && ch !== "#") return;
        let rx = x;
        let ry = y;
        let fill = y >= 4 ? shade : base;
        if (!broken && HEART_GLINT.has(`${x},${y}`)) fill = glint;
        if (broken) {
          if (x === 5 || x === 6) return;
          if (x >= 7) {
            rx += 2;
            ry += 2;
          }
        }
        rects += `<rect x="${rx * cell}" y="${ry * cell}" width="${cell}" height="${cell}" fill="${fill}"/>`;
      });
    });
    const w = (rows[0].length + (broken ? 3 : 0)) * cell;
    const h = (rows.length + (broken ? 2 : 0)) * cell;
    return `<svg viewBox="0 0 ${w} ${h}" width="18" height="18" xmlns="http://www.w3.org/2000/svg" shape-rendering="crispEdges">${rects}</svg>`;
  }

  function slotTipText(g) {
    const mood = g.mood || {};
    if (mood.state === "bored") {
      return mood.days > 0 ? `吃腻了 · 还要 ${mood.days} 天` : "吃腻了";
    }
    if (mood.state === "missed") return "好久没喂这个了";
    if (mood.state === "liked") return "她喜欢的";
    return g.name || "";
  }

  /** 所有悬停提示共用一个像素气泡 */
  function showTip(el, text) {
    const tip = $("hintBubble");
    if (!tip || !el || !text) return;
    tip.textContent = text;
    tip.classList.remove("fade");
    tip.hidden = false;
    const r = el.getBoundingClientRect();
    const h = tip.offsetHeight || 26;
    const w = tip.offsetWidth || 120;
    const half = w / 2;
    // 气泡是 translate(-50%) 定位的，left 给的是中心；贴边时往回拉，别跑出屏幕
    const cx = Math.min(Math.max(r.left + r.width / 2, half + 8), innerWidth - half - 8);
    tip.style.left = `${cx}px`;

    // 她家具上那句白气泡（.furn-bubble）也在同一块地方，别叠上去：有它就让提示再往上让一层
    let top = r.top - h - 10;
    const fb = [...document.querySelectorAll(".furn-bubble")]
      .map((n) => n.getBoundingClientRect())
      .find((b) => b.bottom > r.top - h - 6 && b.left < r.right + 40 && b.right > r.left - 40);
    if (fb) top = Math.min(top, fb.top - h - 8);
    tip.style.top = `${top >= 4 ? top : r.bottom + 10}px`;
  }

  function hideTip() {
    const tip = $("hintBubble");
    if (tip) tip.hidden = true;
  }

  /** 给任意元素挂像素提示；text 可以是字符串或函数 */
  function bindTip(el, text) {
    if (!el) return;
    el.removeAttribute("title");
    const get = typeof text === "function" ? text : () => text;
    el.addEventListener("mouseenter", () => {
      const t = get();
      if (t) showTip(el, t);
    });
    el.addEventListener("mouseleave", hideTip);
  }
  // 给别的模块（成就板/今天/她送我的/她自己想的）也用同一套气泡
  try { window.__bindTip = bindTip; } catch (e) { /* 忽略 */ }

  function hoverSlot(g, slotEl) {
    const mood = slotTipText(g);
    const base = `${g.name} · ${g.effect_text || ""}`.trim();
    showTip(slotEl, mood ? `${g.name} · ${mood}` : base);
  }

  function unhoverSlot() {
    hideTip();
  }

  function moodBubble(g) {
    const mood = g.mood || {};
    if (!mood.state) return null;
    const b = document.createElement("span");
    b.className = `mood-bubble ${mood.state}`;
    b.innerHTML = heartSvg(mood.state === "bored");
    return b;
  }

  function slotIcon(g, box) {
    if (g.icon) {
      const img = document.createElement("img");
      img.src = `./assets/items/${encodeURIComponent(g.icon)}`;
      img.alt = g.name || "";
      img.addEventListener("error", () => {
        img.remove();
        const s = document.createElement("span");
        s.className = "emoji";
        s.textContent = g.emoji || "🎁";
        box.appendChild(s);
      });
      box.appendChild(img);
    } else {
      const s = document.createElement("span");
      s.className = "emoji";
      s.textContent = g.emoji || "🎁";
      box.appendChild(s);
    }
  }

  function slotBadge(g, btn) {
    // 三态角标：想吃/吃腻已有红心、裂心气泡；这里只补"没吃过"的问号
    const st = (g.mood && g.mood.state) || "";
    if (!g.ever_eaten && st !== "bored") {
      const b = document.createElement("span");
      b.className = "slot-badge b-new";
      b.textContent = "?";
      b.title = "她还没吃过——第一次给她";
      btn.appendChild(b);
    }
  }

  function renderHotbar(data) {
    const host = $("hotbar");
    if (!host) return;
    const gifts = hotbarGifts(data);
    if (gifts.length && hotSel >= gifts.length) hotSel = 0;
    host.replaceChildren();
    unhoverSlot();
    for (let i = 0; i < 10; i += 1) {
      const g = gifts[i];
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "slot";
      btn.dataset.index = String(i);
      if (g) btn.dataset.rarity = g.rarity || "common";
      if (!g) {
        btn.classList.add("empty");
        btn.disabled = true;
        btn.tabIndex = -1;
      } else {
        if (i === hotSel) btn.classList.add("active");
        if (!g.can_send) btn.classList.add("no-stock");
        bindTip(btn, () => (g.can_send ? `${g.name}（${g.count}）· 点她喂食` : `${g.name} · ${g.block_reason || "现在还不能送"}`));
        slotIcon(g, btn);
        const bubble = moodBubble(g);
        if (bubble) btn.appendChild(bubble);
        slotBadge(g, btn);
        if (g.count > 0) {
          const c = document.createElement("span");
          c.className = "count";
          c.textContent = String(g.count);
          btn.appendChild(c);
        }
        btn.addEventListener("click", () => selectSlot(i));
        g.__slot = i;
        bindTip(btn, () => {
          const mood = slotTipText(g);
          const base = `${g.name} · ${g.effect_text || ""}`.trim();
          return mood ? `${g.name} · ${mood}` : base;
        });
      }
      host.appendChild(btn);
    }
    updateHint();
  }

  function selectSlot(i) {
    const gifts = hotbarGifts(DATA);
    if (i < 0 || i >= gifts.length) return;
    hotSel = i;
    hotLocked = true;
    renderHotbar(DATA);
    updateHint();
  }

  function selectedGift() {
    return hotbarGifts(DATA)[hotSel] || null;
  }

  function updateHint() {
    const el = $("hintBubble");
    if (el) el.hidden = true;
  }

  function hotbarStep(dir) {
    const gifts = hotbarGifts(DATA);
    if (!gifts.length) return;
    hotSel = (hotSel + dir + gifts.length) % gifts.length;
    hotLocked = true;
    renderHotbar(DATA);
    updateHint();
  }

  function feedSelected() {
    const g = selectedGift();
    if (!g) {
      toast("还没有可送的礼物");
      return;
    }
    if (!hotLocked) {
      showTalkBubble();
      return;
    }
    hideTalkBubble();
    const slot = document.querySelectorAll("#hotbar .slot")[hotSel];
    feed(g, slot);
  }

  function renderSheet(data) {
    const intimacy = data.intimacy || {};
    const wish = data.wish || {};
    const level = data.level || {};
    const chat = data.chat || {};
    const daily = data.daily_wish || {};
    const charName = (data.character && data.character.name) || "她";

    const setText = (id, v) => { const el = $(id); if (el) el.textContent = v == null ? "" : String(v); };

    setText("sheetTitle", `${charName} · 小窝`);
    setText("subtitle", `亲密 ${fmt(intimacy.current)} · 今日 ${chat.today || 0} 句`);

    setText("dailyText", daily.text || "今天没什么特别想要的");
    setText("dailyHint", daily.hint || "");
    setText("dailyReward", "完成：心愿 +18");
    setText("dailyExtra", daily.done ? (daily.line || "今天的心愿已经完成了") : "");
    const dailyCard = document.querySelector(".daily-card");
    if (dailyCard) dailyCard.classList.toggle("done", !!daily.done);
    const status = $("dailyStatus");
    if (status) {
      status.textContent = daily.done ? "已完成" : "进行中";
      status.classList.toggle("done", !!daily.done);
    }

    setText("wishBig", fmt(wish.value));
    setText("wishPeak", `最高 ${fmt(wish.peak)}`);
    const trend = $("wishTrend");
    if (trend) {
      const t = wish.trend || "flat";
      trend.textContent = t === "up" ? "↑ 在涨" : t === "down" ? "↓ 在掉" : "→ 平稳";
      trend.className = `trend ${t}`;
    }
    renderWishEvents(wish.events || []);

    setText("statLevel", `${level.n || 0} 层`);
    setText("statIntimacy", `${fmt(intimacy.current)} / 100`);
    setText("statPeak", `${fmt(intimacy.peak)} / 100`);
    setText("statFed", `${(data.feed && data.feed.total) || 0} 轮`);
    setText("statToday", String(chat.today || 0));
    setText("statTotal", String(chat.total || 0));
    setText("statStreak", `${chat.streak || 0} 天`);
    setText("sourceLabel", intimacy.source_label || "本地积累，攒满反哺鱼的心");

    renderWeek(chat.days || []);
    renderHistory(data.history || []);
    if (data.ambient) applyAmbient(data.ambient);
    renderClues(data.clues || []);
    renderMeterList(data);
    renderGiftGrid();
  }

  function renderWeek(days) {
    const host = $("weekBars");
    if (!host) return;
    host.replaceChildren();
    const max = Math.max(1, ...days.map((d) => Number(d.count) || 0));
    days.forEach((d, i) => {
      const wrap = document.createElement("div");
      wrap.className = `day${i === days.length - 1 ? " today" : ""}`;
      const bar = document.createElement("i");
      bar.style.height = `${Math.max(3, Math.round(((Number(d.count) || 0) / max) * 30))}px`;
      const count = document.createElement("b");
      count.textContent = String(d.count || 0);
      const label = document.createElement("span");
      label.textContent = d.label || "";
      wrap.append(count, bar, label);
      host.appendChild(wrap);
    });
  }

  function renderWishEvents(events) {
    const host = $("wishEvents");
    if (!host) return;
    host.replaceChildren();
    if (!events.length) {
      const li = document.createElement("li");
      li.textContent = "还没有变化。陪她聊两句，或者送个礼物。";
      host.appendChild(li);
      return;
    }
    [...events].reverse().forEach((e) => {
      const li = document.createElement("li");
      const d = Number(e.delta) || 0;
      li.innerHTML = `<span>${esc(e.reason || "心愿变化")}</span><b class="d ${d >= 0 ? "up" : "down"}">${d >= 0 ? "+" : ""}${fmt(d)}</b>`;
      host.appendChild(li);
    });
  }

  function renderHistory(rows) {
    const host = $("history");
    if (!host) return;
    host.replaceChildren();
    if (!rows.length) {
      const li = document.createElement("li");
      li.textContent = "还没有记录。先从送她一颗糖开始吧。";
      host.appendChild(li);
      return;
    }
    [...rows].reverse().slice(0, 14).forEach((h) => {
      const li = document.createElement("li");
      const when = fmtClock(h.ts);
      if (h.kind === "send") {
        const fed = (h.fed || []).length ? " · 攒满反哺" : "";
        li.innerHTML = `<b>送她「${esc(h.name || h.gift)}」</b><span>亲密 +${fmt(h.delta)}（${fmt(h.before)} → ${fmt(h.after)}/100）${fed} · ${when}</span>`;
      } else if (h.kind === "her_gift") {
        li.innerHTML = `<b>她送你「${esc(h.name || h.gift)}」</b><span>${esc(h.line || "里程碑回礼")} · ${when}</span>`;
      } else {
        li.innerHTML = `<b>解锁「${esc(h.name || h.gift)}」</b><span>${esc(h.source || "里程碑")} · ${when}</span>`;
      }
      host.appendChild(li);
    });
  }

  function renderClues(clues) {
    const host = $("clues");
    const empty = $("cluesEmpty");
    if (!host) return;
    host.replaceChildren();
    (clues || []).forEach((c) => {
      const li = document.createElement("li");
      li.textContent = c;
      host.appendChild(li);
    });
    if (empty) empty.hidden = (clues || []).length > 0;
  }

  function giftCard(g) {
    const card = document.createElement("article");
    const locked = !g.unlocked;
    const hidden = g.hidden && !g.revealed;
    card.className = `gift${locked ? " locked" : ""}`;
    card.dataset.rarity = g.rarity || "common";
    card.dataset.id = g.id;

    const top = document.createElement("div");
    top.className = "gift-top";
    const icon = document.createElement("div");
    icon.className = "gift-icon";
    if (hidden || !g.icon) {
      const q = document.createElement("span");
      q.className = hidden ? "q" : "fallback";
      q.textContent = hidden ? "？" : (g.emoji || "🎁");
      icon.appendChild(q);
    } else {
      const img = document.createElement("img");
      img.src = `./assets/items/${encodeURIComponent(g.icon)}`;
      img.alt = g.name;
      img.loading = "lazy";
      img.addEventListener("error", () => {
        img.remove();
        const q = document.createElement("span");
        q.className = "fallback";
        q.textContent = g.emoji || "🎁";
        icon.appendChild(q);
      });
      icon.appendChild(img);
    }
    const rarity = document.createElement("span");
    rarity.className = "rarity";
    rarity.textContent = g.rarity_cn || "普通";
    top.append(icon, rarity);

    const title = document.createElement("h3");
    title.textContent = g.name;
    const desc = document.createElement("p");
    desc.className = "desc";
    desc.textContent = g.desc;

    const chips = document.createElement("div");
    chips.className = "effect-chips";
    (g.effect_text && g.effect_text !== "？？？" ? g.effect_text.split(" · ") : []).forEach((t) => {
      const s = document.createElement("span");
      s.textContent = t;
      chips.appendChild(s);
    });

    const source = document.createElement("div");
    source.className = "source";
    source.textContent = g.source || "获取途径未知";

    const progress = document.createElement("div");
    progress.className = "progress";
    const fill = document.createElement("i");
    fill.style.width = `${Math.round((Number(g.progress) || 0) * 100)}%`;
    progress.appendChild(fill);

    const conds = document.createElement("div");
    conds.className = "conditions";
    if (hidden) {
      const hint = document.createElement("p");
      hint.className = "secret-hint";
      hint.textContent = g.hint || "？？？";
      conds.appendChild(hint);
    } else if ((g.conditions || []).length) {
      g.conditions.forEach((c) => {
        const row = document.createElement("div");
        row.className = `cond${c.met ? " met" : ""}`;
        const left = document.createElement("span");
        left.textContent = c.text;
        const right = document.createElement("b");
        right.textContent = c.met ? "已满足" : `${fmt(c.current)} / ${fmt(c.target)}`;
        row.append(left, right);
        conds.appendChild(row);
      });
    } else {
      const row = document.createElement("div");
      row.className = "cond met";
      row.innerHTML = `<span>${esc(g.source || "已解锁")}</span><b>已满足</b>`;
      conds.appendChild(row);
    }

    const foot = document.createElement("div");
    foot.className = "gift-foot";
    const stock = document.createElement("span");
    stock.className = "stock";
    if (g.daily) {
      stock.textContent = `今日 ${g.daily_current || 0}/${g.daily_target || 0} · 库存 ${g.count || 0}`;
    } else {
      stock.textContent = `库存 ${g.count || 0} · 已送 ${g.sent || 0}`;
    }
    const btn = document.createElement("button");
    btn.className = "send-btn";
    btn.type = "button";
    btn.textContent = g.can_send ? "喂给她" : "还不行";
    btn.disabled = !g.can_send || BUSY;
    bindTip(btn, g.block_reason || (g.can_send ? "喂给她" : "还不行"));
    btn.addEventListener("click", () => {
      const gifts = hotbarGifts(DATA);
      const i = gifts.findIndex((x) => x.id === g.id);
      if (i >= 0) hotSel = i;
      renderHotbar(DATA);
      feed(g, document.querySelectorAll("#hotbar .slot")[hotSel] || btn);
    });
    foot.append(stock, btn);

    card.append(top, title, desc, chips, source, progress, conds, foot);
    return card;
  }

  function renderGiftGrid() {
    const host = $("giftGrid");
    if (!host || !DATA) return;
    const all = DATA.gifts || [];
    const list = all.filter((g) => {
      if (FILTER === "sendable") return g.can_send;
      if (FILTER === "locked") return !g.unlocked;
      if (FILTER === "sent") return (g.sent || 0) > 0;
      return true;
    });
    host.replaceChildren();
    if (!list.length) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = FILTER === "sendable" ? "现在没有可以送的礼物，去陪她聊聊或者完成今日心愿吧。" : "这里还没有东西。";
      host.appendChild(p);
      return;
    }
    list.forEach((g) => host.appendChild(giftCard(g)));
  }

  /* ---------------- 小窝家具 ---------------- */

  function emojiNode(o) {
    const s = document.createElement("span");
    s.className = "furn-emoji";
    s.textContent = o.emoji || "🎁";
    const px = ((o.w || 10) / 100) * (window.innerWidth || 1440) * 0.45;
    s.style.fontSize = `${Math.round(Math.min(150, Math.max(20, px)))}px`;
    return s;
  }

  const BILI_SVG = `<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <g fill="none" stroke="#fb7299" stroke-width="2.4" stroke-linecap="round">
      <path d="M25.5 19.5 L18.5 8.5"/><path d="M38.5 19.5 L45.5 8.5"/>
    </g>
    <rect x="9" y="18" width="46" height="33" rx="9.5" fill="#fb7299"/>
    <rect x="14.5" y="23.5" width="35" height="22" rx="6" fill="#fff"/>
    <circle cx="26.5" cy="32.5" r="2.5" fill="#fb7299"/>
    <circle cx="37.5" cy="32.5" r="2.5" fill="#fb7299"/>
    <path d="M27 38.5 q5 4.2 10 0" fill="none" stroke="#fb7299" stroke-width="2.2" stroke-linecap="round"/>
  </svg>`;

  function badgeNode(o) {
    const s = document.createElement("span");
    s.className = "bili-badge";
    s.innerHTML = o.svg === "bilibili" ? BILI_SVG : `<span class="furn-emoji">${o.emoji || "🏅"}</span>`;
    return s;
  }

  const FURN_SCALE = 1.45;   // 家具整体大小（相对角色 1 像素）想更大就调这个数

  const FURN_NATIVE = {
    bowl: [11, 8],
    bili_badge: [15, 13],
    patch_rug: [46, 26],
    table: [21, 16],
  };

  function furnNative(o) {
    if (Array.isArray(o.native) && o.native.length) return o.native;
    return FURN_NATIVE[o.id] || null;
  }

  function furnIcon(o) {
    /* 图标加载链：先按 payload 给的 icon，再按 id.png，两张都挂了才退回 emoji。
       以前只要 payload 里的 icon 是空的，就直接把 emoji 拉到 150px 塞进房间，
       看着就像家具被“选中”成一块蓝色方块。 */
    if (o.kind === "badge" && !o.icon) return badgeNode(o);
    /* 家具工坊的自定义件：icon 就是 data URI，直接用 */
    if (o.icon && String(o.icon).indexOf("data:") === 0) {
      const img = document.createElement("img");
      img.alt = o.name || "";
      img.src = String(o.icon);
      return img;
    }
    const candidates = [];
    if (o.icon) candidates.push(o.icon);
    if (o.id) candidates.push(`${o.id}.png`);
    if (!candidates.length) return emojiNode(o);
    const img = document.createElement("img");
    img.alt = o.name || "";
    let i = 0;
    const next = () => {
      if (i >= candidates.length) {
        const parent = img.parentElement;
        img.remove();
        const e = emojiNode(o);
        e.style.fontSize = "34px";
        if (parent) parent.appendChild(e);
        return;
      }
      img.src = `./assets/furniture/${encodeURIComponent(candidates[i++])}`;
    };
    img.addEventListener("error", next);
    next();
    return img;
  }

  /* 地板可用区间：物品栏占掉的那一条不能摆家具。
     .furniture-layer 整体 z-index 只有 2，压不过 .hotbar-wrap(20)，
     所以靠 z-index 解决不了，必须把家具的 bottom 抬到物品栏顶边之上。 */
  function floorBandMinPct() {
    const tank = $("tank");
    const hb = document.querySelector(".hotbar-wrap");
    if (!tank || !hb) return 16;
    const tr = tank.getBoundingClientRect();
    const hr = hb.getBoundingClientRect();
    if (!tr.height || !hr.height) return 16;
    const used = ((tr.bottom - hr.top) / tr.height) * 100;   // 物品栏顶边到房间底边占多少
    return Math.max(12, Math.min(42, Math.round(used + 1.5)));
  }

  /* 叠放规则（与 furngen.js 摆放模式一致）：摆过/压过的件带层号 z，z 大在前；
     从没动过层的走深度序（屏幕越靠下越近越前）。排序名次发 zIndex，同 furngen 档位。 */
  function furnStk(o) {
    return o.z == null ? 100 - (Number(o.y) || 0) : 1000 + Number(o.z);
  }

  function renderFurniture(data) {
    renderFurniture._last = data;
    const layer = $("furnitureLayer");
    if (!layer) return;
    layer.replaceChildren();
    const items = (data.furniture || [])
      .filter((o) => o.unlocked && o.kind !== "ability");
    // 地板上的家具整体往上抬到物品栏之上，但保持彼此的前后关系（不压成同一条线）
    const floorYs = items.filter((o) => (Number(o.y) || 0) < 45).map((o) => Number(o.y) || 0);
    const band = floorBandMinPct();
    const shift = floorYs.length ? Math.max(0, band - Math.min(...floorYs)) : 0;
    items
      .slice()
      // 该在后的先画；摆过的永远压过没摆过的
      .sort((a, b) => furnStk(a) - furnStk(b))
      .forEach((o, idx) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "furn-item";
        btn.dataset.id = o.id;
        btn.style.zIndex = String(500 + idx * 2);
        const yRaw = Number(o.y) || 0;
        btn.style.left = `${o.x}%`;
        // 墙上的（y 很大）不动；地板上的整组上移，免得被物品栏压在后面点不到
        btn.style.bottom = `${yRaw < 45 ? yRaw + shift : yRaw}%`;
        const nat = furnNative(o);
        if (nat && nat[0]) {
          const heroW = ($("fish") ? $("fish").getBoundingClientRect().width : 86) || 86;
          const px = Math.max(10, Math.round(nat[0] * (heroW / 34) * FURN_SCALE * (Number(o.s) || 1)));
          btn.style.width = `${px}px`;
        } else {
          btn.style.width = `${o.w}%`;
        }
        bindTip(btn, o.custom ? `${o.name}` : `${o.name} · 点一下`);
        btn.appendChild(furnIcon(o));
        btn.addEventListener("click", () => tapFurniture(o, btn));
        layer.appendChild(btn);
      });
  }

  function relayoutFloor() {
    try {
      if (renderFurniture._last) renderFurniture(renderFurniture._last);
    } catch (e) { /* 忽略 */ }
  }

  function renderAbilities(data) {
    const host = $("abilityBar");
    if (!host) return;
    host.replaceChildren();
    (data.furniture || [])
      .filter((o) => o.unlocked && o.kind === "ability")
      .forEach((o) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ability-btn";
        bindTip(card, `${o.name} · ${o.unlocked ? "已解锁" : "还没解锁"}`);
        btn.textContent = o.emoji || "✋";
        btn.addEventListener("click", () => tapFurniture(o, btn));
        host.appendChild(btn);
      });
  }

  function renderFurnitureCards(data) {
    const host = $("furnGrid");
    if (!host) return;
    host.replaceChildren();
    // 自定义家具不进「小窝」卡（那里只列成就家具）
    (data.furniture || []).filter((o) => !o.custom).forEach((o) => {
      const card = document.createElement("article");
      card.className = `furn-card${o.unlocked ? "" : " locked"}`;
      const img = furnIcon(o);
      const meta = document.createElement("div");
      meta.className = "meta";
      const name = document.createElement("b");
      name.textContent = o.name;
      const st = document.createElement("span");
      st.className = "st";
      st.textContent = o.unlocked ? "已在房间 · 可以点" : "还没解锁";
      meta.append(name, st);
      if (!o.unlocked) {
        (o.conditions || []).forEach((c) => {
          const row = document.createElement("span");
          row.className = `cond${c.met ? " met" : ""}`;
          const cur = c.current == null ? "" : `（${fmt(c.current)} / ${fmt(c.target)}）`;
          row.textContent = `${c.text}${cur}`;
          meta.appendChild(row);
        });
        const bar = document.createElement("div");
        bar.className = "mini-bar";
        const fill = document.createElement("i");
        fill.style.width = `${Math.round((Number(o.progress) || 0) * 100)}%`;
        bar.appendChild(fill);
        meta.appendChild(bar);
      }
      card.append(img, meta);
      host.appendChild(card);
    });
    const note = $("furnNote");
    const owned = (data.furniture || []).filter((o) => o.unlocked).length;
    const total = (data.furniture || []).length;
    if (note) note.textContent = total ? `${owned} / ${total} 件 · 点房间里的家具能互动` : "还没有可解锁的家具";
  }

  function furnBubble(node, text) {
    const layer = $("floatLayer");
    if (!layer || !node) return;
    const r = node.getBoundingClientRect();
    const box = layer.getBoundingClientRect();
    const b = document.createElement("div");
    b.className = "furn-bubble";
    b.textContent = text;
    b.style.left = `${r.left + r.width / 2 - box.left}px`;
    b.style.top = `${r.top - box.top - 6}px`;
    layer.appendChild(b);
    setTimeout(() => b.remove(), 3400);
  }

  async function tapFurniture(obj, node) {
    if (obj && obj.custom) return;   // 家具工坊的自定义件：纯装饰，点了没反应
    hideTip();                      // 点下去就别再顶着提示了，免得和她的那句白气泡叠一起
    if (node) {
      node.classList.remove("tapped");
      void node.offsetWidth;
      node.classList.add("tapped");
      setTimeout(() => node.classList.remove("tapped"), 480);
    }
    if (fishCtl) fishCtl.react();
    const lines = obj.lines || [];
    if (DEMO) {
      furnBubble(node, lines.length ? lines[Math.floor(Math.random() * lines.length)] : "……");
      return;
    }
    try {
      if (!VIEW) VIEW = await waitForBridge();
      if (!VIEW || typeof VIEW.apiPost !== "function") throw new Error("当前环境不支持");
      const data = unwrap(await VIEW.apiPost(API_TAP, { object: obj.id }));
      if (!data || !data.line) throw new Error("没有回应");
      const gain = Number(data.gain) || 0;
      furnBubble(node, gain > 0 ? `${data.line}（心愿 +${fmt(gain)}）` : data.line);
      if (gain > 0) {
        load();
      }
    } catch (e) {
      furnBubble(node, "……");
    }
  }

  /* 皮肤表：窗外那张按 skin.json 里的文件名走，换图不用改代码。
     玻璃分成左右两格，中间那根窗棂由屋里的图自己画。 */
  function applySkin(data) {
    const sk = (data && data.skin) || null;
    if (!sk) return;
    const win = sk.window || {};
    if (!win.dir) return;
    const wx = (data && data.wx) || {};
    const kind = String(wx.kind || "");
    const slot = String(wx.slot || "");
    // 窗外动画只有 clear / rain / snow / night 四套；
    // 夜里一律用 night（星空），阴雨雪再压暗一层，别把白天晴天画到半夜
    let key;
    if (slot === "night") key = "night";
    else if (kind === "snow") key = "snow";
    else if (kind === "rain" || kind === "thunder") key = "rain";
    else key = document.documentElement.dataset.bg || "clear";
    // 夜里阴/雾/雨/雪：别画星星（压暗之后会被当成雪花），直接铺一块干净夜空
    const plain = slot === "night" && !!kind && !["clear", "cloudy"].includes(kind);
    const file = win[key] || win.clear || "night.webp";
    const stem = String(file).replace(/\.webp$/i, "");
    const set = (sel, suffix) => {
      const el = document.querySelector(sel);
      if (!el) return;
      if (plain) {
        el.style.backgroundImage = "linear-gradient(#0a0f16, #141c26)";
        el.style.filter = "";
        return;
      }
      el.style.backgroundImage = `url("${win.dir}/${stem}${suffix}.webp?v=42")`;
      el.style.filter = "";
    };
    set(".tank-window-l", "-l");
    set(".tank-window-r", "-r");
  }

  /* 天气：她的窗必须和主人当地实况一致。
     后端每 10 分钟拉一次，算出「时段 × 天气」后现画一张背景，
     前端只把 url 塞进 --room-bg；拿不到就继续用静态那张。 */
  /* 天气牌跟着右上角按钮行走：按钮数量变了也不会叠在一起（原来两个都钉在 right:12px/top:12px） */
  function placeWxChip(chip) {
    const acts = document.querySelector(".scene-actions");
    const tank = document.getElementById("tank");
    if (!chip || !tank) return;
    let right = 14;
    if (acts) {
      const cr = chip.getBoundingClientRect();
      const ar = acts.getBoundingClientRect();
      if (ar.width > 0) right = Math.round((tank.getBoundingClientRect().right - ar.left) + 8);
      chip.style.top = Math.round(ar.top - tank.getBoundingClientRect().top + (ar.height - cr.height) / 2) + "px";
    }
    chip.style.right = Math.max(14, right) + "px";
  }

  function applyWeather(data) {
    const wx = (data && data.wx) || null;
    if (!wx || !wx.bg) return;
    const url = `url("${wx.bg}")`;
    if (wx.bg !== applyWeather._bg) {
      document.documentElement.style.setProperty("--room-bg", url);
      applyWeather._bg = wx.bg;
    }
    let chip = $("wxChip");
    if (!chip) {
      chip = document.createElement("div");
      chip.id = "wxChip";
      chip.className = "wx-chip";
      const tank = $("tank");
      if (tank) tank.appendChild(chip);
    }
    const text = `${wx.temp}℃ · ${wx.slot_cn || ""} · ${wx.kind_cn || ""}`;
    if (chip.textContent !== text) chip.textContent = text;
    chip.title = `${wx.city || ""} ${wx.sunrise || ""}–${wx.sunset || ""}`;
    placeWxChip(chip);
  }

  /* ---------------- 状态牌：好感度 / 余额 / 三档 ---------------- */

  let tierNow = "";
  let sulkHeld = false;

  function renderPurse(data) {
    const bal = (data && data.balance) || {};
    const coin = $("coinValue");
    if (coin) coin.textContent = String(Math.max(0, Math.round(Number(bal.value || 0))));
    const add = $("coinAdd");
    if (add) {
      const t = Math.round(Number(bal.today || 0));
      add.textContent = t > 0 ? `+${t}` : "";
    }
  }

  /* 三档：正常 / 赌气（pout 定格最后一帧）/ 走了（人整只不见）。
     只做一次性切换，不循环，免得每 30 秒重新播一遍。 */
  function applyTier(data) {
    const tier = String(((data && data.favor) || {}).tier || "normal");
    const tank = $("tank");
    if (tank) tank.classList.toggle("is-gone", tier === "gone");
    if (tier === "sulk" && sulkHeld && !actEl) { sulkHeld = false; tierNow = ""; }
    if (tier === tierNow) return;
    const prev = tierNow;
    tierNow = tier;
    if (tier === "gone") {
      if (sulkHeld) { stopAct(); sulkHeld = false; }
      return;
    }
    if (tier === "sulk") {
      setTimeout(() => {
        if (tierNow !== "sulk" || !$("fish")) return;
        playAct("pout", { hold: true, sticky: true });
        sulkHeld = true;
      }, 80);
      return;
    }
    if (prev === "sulk" && sulkHeld) {
      stopAct();
      sulkHeld = false;
    }
  }

  function render(data) {
    DATA = data;
    applyWeather(data);
    applySkin(data);
    const intimacy = data.intimacy || {};

    renderPanel(data);
    renderHotbar(data);
    renderAbilities(data);
    renderFurniture(data);
    renderFurnitureCards(data);
    renderSheet(data);

    renderTestList();

    const fresh = freshUnlock(data);
    if (fresh) showUnlockCamera(fresh);

    const thought = $("thoughtBubble");
    const t = (data.thoughts || [])[0];
    if (thought) {
      if (t && t.text) {
        thought.textContent = `她现在惦记着：${t.text}`;
        thought.hidden = false;
      } else {
        thought.hidden = true;
      }
    }

    if (fishCtl) fishCtl.setSpeed(intimacy.mood || "平常");

    const unread = (data.pending || []).filter((p) => !p.notified).length;
    const sig = `${unread}|${(data.pending || []).map((p) => p.ts).join(",")}`;
    if (sig !== pendingSig) {
      pendingSig = sig;
      if (unread > 0) toast(`她有 ${unread} 条新提醒，点右上角 ☰ 看看`);
    }

    const err = $("errorCard");
    if (err) err.hidden = true;
  }

  /* ---------------- 喂食动画 ---------------- */

  function flyToFish(fromRect, gift, done) {
    if (!fishCtl) { done(); return; }
    const mouth = fishCtl.mouth();
    const el = document.createElement("div");
    el.className = "fly-item";
    if (gift.icon) {
      const img = document.createElement("img");
      img.src = `./assets/items/${encodeURIComponent(gift.icon)}`;
      img.alt = "";
      img.addEventListener("error", () => {
        img.remove();
        const s = document.createElement("span");
        s.className = "emoji";
        s.textContent = gift.emoji || "🎁";
        el.appendChild(s);
      });
      el.appendChild(img);
    } else {
      const s = document.createElement("span");
      s.className = "emoji";
      s.textContent = gift.emoji || "🎁";
      el.appendChild(s);
    }

    const sx = fromRect.left + fromRect.width / 2;
    const sy = fromRect.top + fromRect.height / 2;
    el.style.left = `${sx}px`;
    el.style.top = `${sy}px`;
    el.style.transform = "translate(-50%, -50%)";
    document.body.appendChild(el);

    const dx = mouth.x - sx;
    const dy = mouth.y - sy;
    fishCtl.face(dx);

    const anim = el.animate(
      [
        { transform: "translate(-50%,-50%) translate(0,0) scale(1.05) rotate(0deg)", opacity: 1, offset: 0 },
        { transform: `translate(-50%,-50%) translate(${dx * 0.55}px, ${dy * 0.45 - 90}px) scale(0.92) rotate(12deg)`, opacity: 1, offset: 0.55 },
        { transform: `translate(-50%,-50%) translate(${dx}px, ${dy}px) scale(0.3) rotate(-8deg)`, opacity: 0.95, offset: 1 },
      ],
      { duration: 680, easing: "cubic-bezier(.42,0,.58,1)", fill: "forwards" },
    );
    anim.onfinish = () => {
      el.remove();
      done();
    };
  }

  const POP_CN = { intimacy: "亲密", joy: "喜悦", attachment: "依恋", soothe: "安抚" };

  const FX_TIER = (d) => (d >= 10 ? "high" : d >= 6 ? "mid" : "low");
  const FX_CFG = {
    low: { dots: 4, star: 0, size: [5, 8], spread: 150, num: 16 },
    mid: { dots: 12, star: 3, size: [7, 12], spread: 230, num: 20 },
    high: { dots: 16, star: 5, size: [9, 15], spread: 300, num: 28 },
  };
  const FX_COLORS = ["#ffd166", "#ffe08a", "#fff3c4", "#ff9ec4", "#ff5c8a", "#7fd8e8", "#c4b5fd"];

  function fxBurst(tier) {
    const tank = $("tank");
    if (!tank) return;
    const el = document.createElement("div");
    el.className = `fx-burst ${tier}`;
    const fish = $("fish");
    if (fish) {
      const tankRect = tank.getBoundingClientRect();
      const r = fish.getBoundingClientRect();
      el.style.left = `${r.left - tankRect.left + r.width / 2}px`;
      el.style.top = `${r.top - tankRect.top + r.height / 2}px`;
    }
    tank.appendChild(el);
    setTimeout(() => el.remove(), 780);
  }

  /** 爆点特效：星芒 + 菱形冲击环 + 细射线 + 像素光斑（B+C+D 合体，按档位缩放） */
  function spawnBurstFx(host, cx, cy, tier = "high") {
    if (!host) return;
    const cfgB = tier === "high" ? { star: 1, dia: 3, rings: 4, scale: 1 }
      : tier === "mid" ? { star: 1, dia: 2, rings: 2, scale: 0.72 }
        : { star: 0, dia: 1, rings: 1, scale: 0.62 };
    const scale = cfgB.scale;
    const wrap = document.createElement("div");
    wrap.className = `fx-high ${tier}`;
    wrap.style.left = `${cx}px`;
    wrap.style.top = `${cy}px`;
    wrap.style.transform = `scale(${scale})`;
    wrap.style.transformOrigin = "0 0";

    // C · 星芒
    if (cfgB.star) {
      const star = document.createElement("div");
      star.className = "fx-star8";
      wrap.appendChild(star);
    }

    // B · 菱形冲击波
    [170, 260, 350].slice(0, cfgB.dia).forEach((size, i) => {
      const dia = document.createElement("div");
      dia.className = "fx-diamond";
      dia.style.width = `${size}px`;
      dia.style.height = `${size}px`;
      dia.style.opacity = String(1 - i * 0.24);
      dia.style.animationDelay = `${i * 70}ms`;
      wrap.appendChild(dia);
    });

    // B · 八条细射线
    for (let i = 0; i < 8; i += 1) {
      const ray = document.createElement("div");
      ray.className = "fx-ray";
      ray.style.transform = `rotate(${i * 45}deg) scaleY(0.15)`;
      ray.style.animationDelay = `${(i % 2) * 40}ms`;
      wrap.appendChild(ray);
    }

    // D · 四圈像素光斑
    const rings = [
      { r: 50, n: 8, s: 14 },
      { r: 84, n: 12, s: 11 },
      { r: 116, n: 16, s: 8 },
      { r: 146, n: 20, s: 6 },
    ];
    rings.slice(0, cfgB.rings).forEach((ring, ri) => {
      for (let i = 0; i < ring.n; i += 1) {
        const a = (Math.PI * 2 * i) / ring.n + ri * 0.22;
        const mote = document.createElement("div");
        mote.className = "fx-mote";
        mote.style.width = `${ring.s}px`;
        mote.style.height = `${ring.s}px`;
        mote.style.left = `${Math.cos(a) * ring.r}px`;
        mote.style.top = `${Math.sin(a) * ring.r * 0.58}px`;
        mote.style.marginLeft = `${-ring.s / 2}px`;
        mote.style.marginTop = `${-ring.s / 2}px`;
        mote.style.opacity = String(1 - ri * 0.2);
        mote.style.animationDelay = `${40 + ri * 50}ms`;
        wrap.appendChild(mote);
      }
    });

    host.appendChild(wrap);
    setTimeout(() => wrap.remove(), 1000);
  }

  function splash(result, gift) {
    const layer = $("floatLayer");
    if (!layer || !fishCtl) return;
    const mouth = fishCtl.mouth();
    const box = layer.getBoundingClientRect();
    const mx = mouth.x - box.left;
    const my = mouth.y - box.top;

    const delta = Math.abs(Number(result.delta) || 0);
    const tier = FX_TIER(delta);
    const cfg = FX_CFG[tier];
    const anim = (gift && gift.animation) || {};
    const tint = anim.color || "#ff9ec4";

    // 星芒 + 冲击环 + 射线 + 光斑（都画在人物下面，档位决定大小）
    spawnBurstFx($("tank"), mx, my, tier);

    // 白闪 / 菱形闪光
    const flash = document.createElement("div");
    flash.className = `fx-flash ${tier}`;
    flash.style.left = `${mx}px`;
    flash.style.top = `${my}px`;
    // 白闪放到人物下面，别把她糊掉
    const tankEl = $("tank");
    (tankEl || layer).appendChild(flash);
    setTimeout(() => flash.remove(), 700);

    // 光环：低 1 圈、中 2 圈、高 3 圈
    const ringCount = tier === "high" ? 3 : tier === "mid" ? 2 : 1;
    for (let i = 0; i < ringCount; i += 1) {
      const ring = document.createElement("span");
      ring.className = "fx-ring";
      ring.style.left = `${mx}px`;
      ring.style.top = `${my}px`;
      ring.style.width = `${46 + i * 10}px`;
      ring.style.height = `${46 + i * 10}px`;
      ring.style.borderColor = tier === "high" ? "#ffffff" : tint;
      ring.style.animationDelay = `${i * 90}ms`;
      layer.appendChild(ring);
      setTimeout(() => ring.remove(), 900 + i * 90);
    }

    // 方粒 / 十字星 / 五角星 混着飞，带一点拖尾
    const dots = cfg.dots + cfg.star;
    for (let i = 0; i < dots; i += 1) {
      const kind = i % 9 === 0 ? "spark" : i >= cfg.dots ? "star" : "dot";
      const p = document.createElement("span");
      p.className = kind === "spark" ? "fx-spark" : kind === "star" ? "fx-star" : "fx-dot";
      const angle = (Math.PI * 2 * i) / dots + Math.random() * 0.6;
      const dist = cfg.spread * (0.5 + Math.random() * 0.95);
      const size = cfg.size[0] + Math.random() * (cfg.size[1] - cfg.size[0]) * (kind === "dot" ? 1 : 1.6);
      p.style.left = `${mx}px`;
      p.style.top = `${my}px`;
      p.style.width = `${size}px`;
      p.style.height = `${size}px`;
      p.style.background = kind === "dot" ? FX_COLORS[i % FX_COLORS.length] : tint;
      p.style.setProperty("--dx", `${Math.cos(angle) * dist}px`);
      p.style.setProperty("--dy", `${Math.sin(angle) * dist - 30}px`);
      p.style.setProperty("--rot", `${(Math.random() - 0.5) * 90}deg`);
      p.style.animationDelay = `${Math.random() * 0.07}s`;
      layer.appendChild(p);
      setTimeout(() => p.remove(), 1100);
    }

    // 属性跳字：一条条往上弹，档位越高字越大
    if (splash._group && splash._group.parentNode) splash._group.remove();
    const group = document.createElement("div");
    group.className = `pop-group fx-${tier}`;
    group.style.left = `${mx}px`;
    group.style.top = `${my - Math.max(34, mouth.rect.height * 0.3)}px`;

    const meters = result.meters || {};
    const rows = [];
    ["intimacy", "joy", "attachment", "soothe"].forEach((k) => {
      const d = Number((meters[k] || {}).delta) || 0;
      if (Math.abs(d) > 0.05) rows.push(`<span class="pop pop-${k}">${POP_CN[k]} +${fmt(d)}</span>`);
    });
    if (!rows.length) {
      rows.push(`<span class="pop pop-intimacy">亲密 +${fmt(Math.max(0, delta))}</span>`);
    }
    const fed = result.fed || [];
    if (fed.length) {
      const step = fmt((DATA && DATA.feed && DATA.feed.step) || 10);
      rows.push(`<span class="pop gold">攒满一轮 · 她的心 +${step}</span>`);
    }
    group.innerHTML = rows.join("");
    group.style.setProperty("--pop-size", `${cfg.num}px`);
    group.style.setProperty("--pop-glow", tint);
    layer.appendChild(group);
    splash._group = group;
    setTimeout(() => { if (group.parentNode) group.remove(); }, 2800);
  }

  let seenUnlocks = null;
  function freshUnlock(data) {
    // 自定义家具不该触发「新家具」解锁镜头
    const now = (data.furniture || []).filter((o) => o.unlocked && !o.custom).map((o) => o.id);
    if (!seenUnlocks) { seenUnlocks = new Set(now); return null; }
    const fresh = now.find((id) => !seenUnlocks.has(id)) || null;
    now.forEach((id) => seenUnlocks.add(id));
    return fresh;
  }

  /** 把整个房间克隆一份按坐标放大，塞进某个镜头窗 */
  function buildCameraWorld(worldId) {
    const world = $(worldId);
    const tank = $("tank");
    const fish = $("fish");
    if (!world || !tank || !fish) return null;
    const tr = tank.getBoundingClientRect();
    const fx = (fishCtl ? fishCtl.x : 50) / 100;
    const fy = (fishCtl ? fishCtl.y : 62) / 100;

    world.replaceChildren();
    world.style.width = `${tr.width}px`;
    world.style.height = `${tr.height}px`;

    const bg = document.createElement("div");
    bg.className = "tank-bg";

    const furn = $("furnitureLayer");
    const furnClone = furn ? furn.cloneNode(true) : null;
    if (furnClone) {
      furnClone.removeAttribute("id");
      furnClone.querySelectorAll("[id]").forEach((n) => n.removeAttribute("id"));
    }

    /* 深拷贝：她的身体现在是 .fish-gif 子元素，浅拷贝会得到一个空盒子，
       近景里就会「人不见」——这就是特写消失的原因。 */
    const fishClone = fish.cloneNode(true);
    fishClone.removeAttribute("id");
    fishClone.removeAttribute("title");
    fishClone.style.left = `${fx * 100}%`;
    fishClone.style.top = `${fy * 100}%`;
    fishClone.classList.toggle("flip", fish.classList.contains("flip"));

    world.append(bg);
    if (furnClone) world.append(furnClone);
    world.append(fishClone);
    return { world, fishClone, fx, fy, tr };
  }

  function aimCamera(built, cam, wx, wy, viewW, focusRatio = 0.5) {
    const r = cam.getBoundingClientRect();
    const k = (r.width || 264) / Math.max(40, viewW);
    built.world.style.transform =
      `translate(${r.width / 2}px, ${r.height * focusRatio}px) scale(${k}) ` +
      `translate(${-wx}px, ${-wy}px)`;
  }

  let feedState = { active: false, last: 0, total: 0, count: 0, hideTimer: 0 };

  function itemNode(gift, result) {
    const icon = (gift && gift.icon) || (result && result.gift ? `${result.gift}.png` : "");
    if (!icon) {
      const s = document.createElement("span");
      s.className = "furn-emoji";
      s.textContent = (gift && gift.emoji) || "🎁";
      return s;
    }
    const img = document.createElement("img");
    img.src = `./assets/items/${encodeURIComponent(icon)}`;
    img.alt = "";
    img.addEventListener("error", () => {
      img.remove();
      const s = document.createElement("span");
      s.className = "furn-emoji";
      s.textContent = (gift && gift.emoji) || "🎁";
      img.parentElement.appendChild(s);
    });
    return img;
  }

  /* ---------------- 吃进嘴里之后的小动作 ---------------- */
  const FOOD_ACT = {
    chew: { sfx: "咔嚓 咔嚓", glyphs: ["🍚", "·", "🍚", "·"], cls: "act-chew" },
    sweet: { sfx: "唔…甜", glyphs: ["💗", "♪", "✦", "💗"], cls: "act-sweet" },
    sour: { sfx: "！好酸！", glyphs: ["！", "💧", "！", "💧"], cls: "act-sour" },
    wobble: { sfx: "duang～", glyphs: ["🍮", "…", "✦", "🍮"], cls: "act-wobble" },
    steam: { sfx: "呼——", glyphs: ["♨", "♨", "～", "♨"], cls: "act-steam" },
    slurp: { sfx: "吸溜——", glyphs: ["🍜", "～", "！", "～"], cls: "act-slurp" },
    cheese: { sfx: "拉丝～", glyphs: ["🧀", "✦", "💛", "✦"], cls: "act-cheese" },
  };
  const ACT_CLASSES = Object.keys(FOOD_ACT).map((k) => "act-" + k).concat(["happy"]);

  function playFoodAction(gift, result) {
    const fish = $("fish");
    const tank = $("tank");
    if (!fish || !tank) return;
    const key = (gift && FOOD_ACT[gift.action]) ? gift.action : "chew";
    const conf = FOOD_ACT[key];

    // 她自己的动作：先清掉其它动作类，避免叠加
    ACT_CLASSES.forEach((c) => fish.classList.remove(c));
    void fish.offsetWidth;
    fish.classList.add(conf.cls);
    setTimeout(() => fish.classList.remove(conf.cls), 1500);

    // 拟声 + 小粒子：跟着她脑袋飘
    const tr = tank.getBoundingClientRect();
    const fr = fish.getBoundingClientRect();
    const wrap = document.createElement("div");
    wrap.className = `food-act ${conf.cls}`;
    wrap.style.left = `${fr.left - tr.left + fr.width / 2}px`;
    wrap.style.top = `${fr.top - tr.top + fr.height * 0.3}px`;

    const sfx = document.createElement("b");
    sfx.className = "food-act-sfx";
    sfx.textContent = conf.sfx;
    wrap.appendChild(sfx);

    conf.glyphs.forEach((g, i) => {
      const s = document.createElement("i");
      s.className = "food-act-bit";
      s.textContent = g;
      const spread = (i - (conf.glyphs.length - 1) / 2);
      s.style.setProperty("--x", `${Math.round(spread * 16)}px`);
      s.style.setProperty("--y", `${-30 - (i % 3) * 13}px`);
      s.style.setProperty("--r", `${(i % 2 ? 1 : -1) * (10 + i * 7)}deg`);
      s.style.setProperty("--d", `${i * 0.08}s`);
      wrap.appendChild(s);
    });

    tank.appendChild(wrap);
    setTimeout(() => wrap.remove(), 1600);
  }

  /* ---------------- 行为动画：GIF 覆盖层 + 闲时调度（坐下玩手机/哈欠/表情） ---------------- */
  const ACT_GIFS = ["sit", "phone", "watch_tv", "yawn", "disgust", "pout", "angry", "speechless",
    "surprise", "delight", "love", "shy", "doubt", "scorn",
    "eat_rice", "eat_candy", "eat_kiwi", "eat_pudding", "eat_ramen", "eat_pizza", "eat_dislike", "eat_oden", "drink_cocoa"];
  /* 每个动作 GIF 的画布和「人」在画布里占的格子（PIL 量出来的）。
     w/h = 画布；bh = 内容高；bb = 内容底边；bcx = 内容水平中心。
     目标：不管哪张 GIF，屏幕上人都和待机时一样大、脚踩同一条线。 */
  const ACT_FIT = {
    _default: { w: 96, h: 96, bh: 85, bb: 92, bcx: 42 },
    drink_cocoa: { w: 96, h: 96, bh: 85, bb: 92, bcx: 54 },
    eat_candy: { w: 96, h: 96, bh: 85, bb: 92, bcx: 42 },
    eat_pudding: { w: 96, h: 96, bh: 85, bb: 92, bcx: 43.5 },
    eat_ramen: { w: 96, h: 96, bh: 85, bb: 92, bcx: 42.5 },
    phone: { w: 96, h: 96, bh: 77, bb: 87, bcx: 47.5 },
    eat_kiwi: { w: 192, h: 128, bh: 85, bb: 128, bcx: 65 },
    eat_oden: { w: 128, h: 96, bh: 85, bb: 96, bcx: 52 },
    eat_pizza: { w: 192, h: 128, bh: 85, bb: 128, bcx: 107 },
    watch_tv: { w: 128, h: 96, bh: 68, bb: 94, bcx: 87.5 },
  };
  /* 每张 GIF 播完一圈的毫秒数——只播一次，到点就收，不给她循环第二遍。
     这些数字是量出来的（帧数 × 每帧毫秒），别再手填：以前披萨按 2280 收，
     实际一圈 4480，动画会在一半被掐断。 */
  const ACT_MS = {
    idle: 1000, walk: 1800, phone: 1800, watch_tv: 1800, sit: 1800,
    pout: 1800, shy: 1800, love: 1800, angry: 1800, disgust: 1800,
    delight: 1800, surprise: 1800, speechless: 1800, yawn: 1800, scorn: 1800,
    doubt: 1600, drink_cocoa: 1600, eat_candy: 1600, eat_dislike: 1800,
    eat_kiwi: 1800, eat_oden: 1600, eat_pizza: 4520, eat_pudding: 1600,
    eat_ramen: 1600, eat_rice: 1600,
  };
  /* 用户要求：正常状态只播待机，不做随机小动作。改成 true 可以恢复。 */
  const RANDOM_IDLE_ON = false;
  // 食物 id → 吃东西 GIF
  const EAT_GIF = { rice: "eat_rice", strawberry_candy: "eat_candy", kiwi: "eat_kiwi",
    pudding: "eat_pudding", ramen: "eat_ramen", pizza: "eat_pizza", hot_cocoa: "drink_cocoa", oden: "eat_oden" };
  const EAT_MS = { kiwi: 1000, pizza: 2300 };
  let actEl = null, actTimer = 0, actHoldTimer = 0;
  let idleSince = Date.now(), lastAuto = 0, actReported = "";
  // 后端作息引擎镜像：engine=true 时前端骰子只留微动作，手机/电视照播后端
  let engineOn = false, manualAct = false;
  let ambientStaged = { name: "", since: 0 };
  const REPORTED_ACTS = ["phone", "watch_tv"];

  function reportActivity(act, on) {
    if (DEMO) return;
    const send = (v) => { try { v.apiPost("panel/activity", { act: act, on: on }).catch(() => {}); } catch (e) {} };
    if (VIEW && typeof VIEW.apiPost === "function") return send(VIEW);
    waitForBridge(3000).then((v) => { if (v) { if (!VIEW) VIEW = v; send(v); } });
  }

  function stopAct() {
    clearTimeout(actTimer); clearTimeout(actHoldTimer);
    if (actReported) { reportActivity(actReported, false); actReported = ""; }
    if (actEl) { actEl.remove(); actEl = null; }
    ambientStaged = { name: "", since: 0 };
    const f = $("fish");
    if (f) f.classList.remove("act-hidden");
  }

  function playAct(key, opts) {
    opts = opts || {};
    const fish = $("fish"), tank = $("tank");
    if (!fish || !tank || ACT_GIFS.indexOf(key) < 0) return;
    stopAct();
    const fr = fish.getBoundingClientRect(), tr = tank.getBoundingClientRect();
    if (!fr.height) return;
    const img = document.createElement("img");
    img.className = "act-gif" + (fish.classList.contains("flip") ? " flip" : "");
    img.src = `./assets/char/act/${key}.gif`;
    img.alt = "";
    const fit = ACT_FIT[key] || ACT_FIT._default;
    const H = fr.height;
    /* 待机时她的内容高 = 0.695 × 盒子高（41/59 的旧图换算出来的常数）。
       按这个反推每张 GIF 要显示多大，所有人的素材就一样大了。 */
    const aw = Math.round(0.695 * H * fit.w / fit.bh);
    const ah = Math.round(aw * fit.h / fit.w);
    /* 脚底和待机踩同一条线：待机 GIF 的内容底边在盒子高 0.9833 处 */
    const feetY = fr.top - tr.top + H * 0.9833;
    const cy = feetY - (fit.bb / fit.h - 0.5) * ah;
    const cx = fr.left - tr.left + fr.width / 2 - (fit.bcx / fit.w - 0.5) * aw;
    img.style.left = `${cx}px`;
    img.style.top = `${cy}px`;
    img.style.width = `${aw}px`;
    fish.classList.add("act-hidden");
    tank.appendChild(img);
    actEl = img;
    img.addEventListener("error", () => {
      // 素材缺失：立刻还原本体，别让她凭空消失
      if (actEl === img) stopAct();
    });
    if (opts.hold) {
      if (!engineOn && REPORTED_ACTS.indexOf(key) >= 0) { actReported = key; reportActivity(key, true); }
      /* 持续行为（坐着看电视 / 玩手机）：先播一圈，然后定格在最后一帧。
         GIF 本身会循环，所以到点换成同尺寸的静帧 PNG，画面不动。 */
      const dur = ACT_MS[key] || 1500;
      actHoldTimer = setTimeout(() => {
        if (actEl !== img) return;
        img.dataset.frozen = "1";
        img.src = `./assets/char/act/freeze/${key}.png`;
      }, Math.max(400, dur - 40));
      actTimer = opts.sticky ? 0 : setTimeout(stopAct, 300000);   // 兜底：最长挂 5 分钟
    } else {
      /* 一次性动作：只播一圈，到点收掉回到待机。
         thenBefore 在收掉之前跑——近景要趁机把正在播的那张图留下来。 */
      const dur = ACT_MS[key] || 1500;
      actTimer = setTimeout(() => {
        if (opts.thenBefore) {
          try { opts.thenBefore(); } catch (e) { /* 别让她因为一个回调卡住 */ }
        }
        stopAct();
        if (opts.then) opts.then();
      }, Math.max(600, dur - 60));
    }
  }

  /* 调试用：控制台里 window.__playAct("eat_rice") 就能单独试一个动作 */
  try { window.__playAct = playAct; } catch (e) { /* 忽略 */ }

  function radarVal(key) {
    const r = (((DATA || {}).radar) || []).find((x) => x && x.key === key);
    return r ? Number(r.value) || 0 : 0;
  }  function moodVal(key) {
    const r = (((DATA || {}).moods) || []).find((x) => x && x.key === key);
    return r ? Number(r.value) || 0 : 0;
  }

  function markInteract() {
    idleSince = Date.now();
    if (sulkHeld) return;      // 赌气时不会因为点一下就变回平常
    stopAct();
  }

  /* 后端作息镜像：手机/电视由后端决定，前端只照播 */
  function applyAmbient(a) {
    if (!a || !a.engine) return;
    engineOn = true;
    if (manualAct) return;
    const name = String(a.name || "");
    if (!name) {                                   // 作息空档：让位，微动作骰子接管
      if (ambientStaged.name && actEl) stopAct();
      return;
    }
    if (ambientStaged.name === name && ambientStaged.since === a.since) return;  // 已在播这一段
    if (BUSY || testPreview) return;
    if (actEl && !ambientStaged.name) return;      // 喂食/表情一次性动作在演，不打断
    const s = $("sheet"); if (s && !s.hidden) return;
    if (document.body.classList.contains("av-board-open")) return;
    if (document.body.classList.contains("hg-board-open")) return;
    const rc = $("reactionCard"); if (rc && !rc.hidden) return;
    if (fishCtl && (fishCtl.vx || fishCtl.vy)) return;   // 走动中不盖
    if (ambientStaged.name) stopAct();             // 换场：旧作息收掉
    playAct(name, { hold: true });
    ambientStaged = { name: name, since: a.since };
  }

  async function ambientPoll() {
    if (DEMO || !VIEW || document.hidden) return;
    try { applyAmbient(unwrap(await VIEW.apiGet("panel/activity"))); } catch (e) { /* 下轮再来 */ }
  }

  /* 三点气泡只在她真的在回话时出现：后端在 LLM 请求那一刻打时间戳，
     回完就清掉，前端每秒问一次。
     回话的时候她在"玩手机"——播一圈手机动作，然后定格，答完再收。 */
  let typingOn = false;
  let phoneHeld = false;
  async function typingPoll() {
    if (DEMO || !VIEW || document.hidden) return;
    let on = false;
    try { on = !!(unwrap(await VIEW.apiGet("panel/typing")) || {}).typing; } catch (e) { return; }
    if (on === typingOn) return;
    typingOn = on;
    if (on) {
      const s = $("sheet"); if (s && !s.hidden) return;
      if (BUSY || testPreview) return;
      showTalkBubble();
      // 赌气 / 正在演别的动作时不要抢她的戏
      if (!sulkHeld && !actEl && tierNow !== "gone") {
        playAct("phone", { hold: true, sticky: true });
        phoneHeld = true;
      }
    } else {
      hideTalkBubble();
      if (phoneHeld) {
        phoneHeld = false;
        if (!actEl || actEl.dataset.frozen === "1") stopAct();
      }
    }
  }

  function idleTick() {
    try {
      /* 正常状态只播待机：不掷骰子、不自己找事做。
         想恢复随机小动作，把 RANDOM_IDLE_ON 改回 true。 */
      if (!RANDOM_IDLE_ON) return;
      if (document.hidden || BUSY || testPreview || actEl) return;
      const s = $("sheet"); if (s && !s.hidden) return;
      if (document.body.classList.contains("av-board-open")) return;
      if (document.body.classList.contains("hg-board-open")) return;
      const rc = $("reactionCard"); if (rc && !rc.hidden) return;
      const uc = $("unlockCard"); if (uc && !uc.hidden) return;
      const now = Date.now();
      if (now - idleSince < 18000) return;     // 18 秒没人理才轮到自己找事
      if (now - lastAuto < 42000) return;      // 两次自主行为之间至少隔 42 秒
      const hour = new Date().getHours();
      const night = hour >= 22 || hour < 6;
      const fat = radarVal("fatigue");
      const str = radarVal("stress");
      const joy = radarVal("joy");
      const wrong = moodVal("wronged");
      const pool = [
        ["phone", night ? 40 : 18, true],
        ["watch_tv", night ? 22 : 12, true],
        ["sit", 6, false],
        ["yawn", fat >= 55 ? 24 : 5, false],
        ["doubt", 5, false],
        ["love", joy >= 70 ? 10 : 1, false],
        ["pout", (str >= 60 || wrong >= 45) ? 16 : 0, false],
        ["speechless", wrong >= 60 ? 8 : 0, false],
        ["angry", wrong >= 75 ? 10 : 0, false],
      ].filter((x) => x[1] > 0 && !(engineOn && (x[0] === "phone" || x[0] === "watch_tv" || x[0] === "sit")));
      let sum = 0; pool.forEach((x) => { sum += x[1]; });
      let r = Math.random() * sum, pick = pool[0];
      for (const x of pool) { r -= x[1]; if (r <= 0) { pick = x; break; } }
      lastAuto = now;
      if (pick[2]) {
        // 持续行为（玩手机/看电视）：先坐下，再挂起该行为
        playAct("sit", { ms: 950, then: () => playAct(pick[0], { hold: true }) });
      } else {
        playAct(pick[0], { ms: 2100 });
      }
    } catch (e) { /* 行为是锦上添花，出错不碰主流程 */ }
  }

  function feedCaption(gift, result, merged) {
    const line = $("reactionLine");
    const delta = $("reactionDelta");
    if (line) line.textContent = result.line || "……谢谢";
    if (delta) {
      const fed = (result.fed || []).length ? " · 攒满一轮" : "";
      const total = Math.round(feedState.total * 10) / 10;
      const times = feedState.count > 1 ? `　累计 ${feedState.count} 次` : "";
      delta.textContent = `「${result.name || (gift && gift.name) || ""}」亲密 +${fmt(total)}${fed}${merged ? times : ""}`;
    }
  }

  function showReaction(result, gift, actKey) {
    // 正在播的那张动作图先留一份：停掉之后近景里还得是她吃东西的样子
    const keepAct = (actKey && actEl) ? actEl.cloneNode(true) : null;
    stopAct();
    const card = $("reactionCard");
    const cam = $("camera");
    const itemBox = $("cameraItem");
    const fishEl = $("fish");
    if (!card || !cam || !itemBox || !fishEl) return;

    const d = Number(result.delta) || 0;
    const now = performance.now();

    // 短时间内连续喂：不新开镜头，只把数字累加、名字换成最新那件
    if (feedState.active && now - feedState.last < 2800) {
      feedState.total += d;
      feedState.count += 1;
      feedState.last = now;
      feedCaption(gift, result, true);
      if (itemBox) {
        itemBox.replaceChildren(itemNode(gift, result));
        itemBox.classList.remove("fly");
        void itemBox.offsetWidth;
        itemBox.classList.add("fly");
      }
      const world = $("cameraWorld");
      const fEl = $("fish");
      const tEl = $("tank");
      if (world && fEl && tEl) {
        const fr = fEl.getBoundingClientRect();
        const tr = tEl.getBoundingClientRect();
        spawnBurstFx(world, fr.left - tr.left + fr.width / 2, fr.top - tr.top + fr.height * 0.52,
                     FX_TIER(Math.abs(d)));
      }
      const tag = cam.querySelector(".camera-tag");
      if (tag) tag.textContent = "近景 · 她";
      clearTimeout(feedState.hideTimer);
      feedState.hideTimer = setTimeout(() => {
        feedState.active = false;
        if ($("reactionCard")) $("reactionCard").hidden = true;
      }, 3200);
      return;
    }

    feedState = { active: true, last: now, total: d, count: 1, hideTimer: 0 };
    card.hidden = false;
    const built = buildCameraWorld("cameraWorld");
    if (!built) return;
    if (keepAct) {
      // 近景里保留吃东西那张图，别让她在特写里站着发呆
      built.fishClone.classList.add("act-hidden");
      built.world.appendChild(keepAct);
    }

    const fishRect = fishEl.getBoundingClientRect();
    const tankRect = built.tr;
    const camRect = cam.getBoundingClientRect();
    const cw = camRect.width || 264;
    const ch = camRect.height || 152;
    const aspect = cw / ch;
    const dir = fishEl.classList.contains("flip") ? -1 : 1;

    // 头部中心（相对房间左上角）：横向居中，纵向在头顶往下约 26%
    const headCx = fishRect.left - tankRect.left + fishRect.width / 2;
    const headCy = fishRect.top - tankRect.top + fishRect.height * 0.52;
    const bodyW = fishRect.width;
    const bodyH = fishRect.height;
    // 拍全身：以她的整体外框为准，留 25% 余量，再按画面比例取宽
    const viewW = Math.max(bodyW * 1.5, bodyH * 1.25 * aspect, 120);
    aimCamera(built, cam, headCx, headCy, viewW, 0.5);

    const k = cw / viewW;
    const mouth = fishCtl ? fishCtl.mouth() : null;
    const mouthX = mouth
      ? cw / 2 + (mouth.x - (tankRect.left + headCx)) * k
      : cw / 2 + dir * fishRect.width * 0.34 * k;
    const mouthY = mouth
      ? ch / 2 + (mouth.y - (tankRect.top + headCy)) * k
      : ch / 2 + fishRect.height * 0.12 * k;

    clearInterval(showReaction._frames);
    let f = 0;
    showReaction._frames = setInterval(() => {
      f = (f + 1) % 8;
      built.fishClone.style.backgroundPosition = `${f * (100 / 7)}% 0`;
    }, 165);

    // 近景里也来一份同样的特效（世界坐标和主房间一致）
    spawnBurstFx(built.world, headCx, headCy, FX_TIER(Math.abs(feedState.total || d)));

    const tag = cam.querySelector(".camera-tag");
    if (tag) tag.textContent = "近景 · 她";
    feedCaption(gift, result, false);
    itemBox.replaceChildren(itemNode(gift, result));
    itemBox.style.left = `${mouthX}px`;
    itemBox.style.top = `${mouthY}px`;
    cam.classList.remove("unlock", "play");
    void cam.offsetWidth;
    cam.classList.add("play");
    itemBox.classList.remove("fly");
    void itemBox.offsetWidth;
    itemBox.classList.add("fly");

    clearTimeout(feedState.hideTimer);
    feedState.hideTimer = setTimeout(() => {
      feedState.active = false;
      clearInterval(showReaction._frames);
      card.hidden = true;
    }, 3200);
  }

  // ---------------- 成就测试面板（仅调试用） ----------------

  let previewSnapshot = null;
  let testPreview = false;

  const TEST_KIND_CN = { furniture: "家具", ability: "动作", badge: "徽章" };

  function snapshotUnlocks() {
    if (previewSnapshot) return;
    previewSnapshot = new Map();
    ((DATA && DATA.furniture) || []).forEach((o) => previewSnapshot.set(o.id, !!o.unlocked));
  }

  function restoreUnlocks() {
    if (!previewSnapshot) return;
    ((DATA && DATA.furniture) || []).forEach((o) => {
      if (previewSnapshot.has(o.id)) o.unlocked = previewSnapshot.get(o.id);
    });
    refreshRoom();
  }

  function refreshRoom() {
    if (!DATA) return;
    renderAbilities(DATA);
    renderFurniture(DATA);
    renderFurnitureCards(DATA);
    renderTestList();
  }

  function previewObject(id) {
    const o = ((DATA && DATA.furniture) || []).find((x) => x.id === id);
    if (!o) return;
    snapshotUnlocks();
    o.unlocked = true;
    refreshRoom();
    showUnlockCamera(id);
  }

  function previewAll() {
    snapshotUnlocks();
    ((DATA && DATA.furniture) || []).forEach((o) => { o.unlocked = true; });
    refreshRoom();
    const last = (DATA.furniture || []).filter((o) => !o.custom).slice(-1)[0];
    if (last) showUnlockCamera(last.id);
  }

  function renderTestList() {
    const host = $("testList");
    if (!host || !DATA) return;
    host.replaceChildren();
    // 自定义家具没有解锁条件，不进成就测试表
    ((DATA.furniture) || []).filter((o) => !o.custom).forEach((o) => {
      const row = document.createElement("div");
      row.className = `test-row${o.unlocked ? " on" : ""}`;
      const cond = (o.conditions || [])[0];
      const condText = o.unlocked ? "已解锁" : (cond ? `${cond.text}（${fmt(cond.current)}/${fmt(cond.target)}）` : "未解锁");
      row.innerHTML = `<i class="dot"></i><span class="nm">${esc(o.name)}</span>` +
        `<span class="cond">${esc(TEST_KIND_CN[o.kind || "furniture"] || "")} · ${esc(condText)}</span>`;
      row.addEventListener("click", () => previewObject(o.id));
      host.appendChild(row);
    });
  }

  function toggleTestPanel(force) {
    const panel = $("testPanel");
    if (!panel) return;
    const open = force === undefined ? panel.hidden : force;
    panel.hidden = !open;
    testPreview = open;
    if (open) {
      renderTestList();
    } else {
      previewSnapshot = null;
      load();
    }
  }

  /** 成就解锁：另开一个镜头窗，摆在亲密近景旁边 */
  function showUnlockCamera(id) {
    stopAct();
    const obj = ((DATA && DATA.furniture) || []).find((o) => o.id === id);
    const card = $("unlockCard");
    const cam = $("unlockCam");
    const line = $("unlockLine");
    const tag = $("unlockTag");
    if (!obj || !card || !cam) return;

    card.hidden = false;
    const built = buildCameraWorld("unlockWorld");
    if (!built) return;

    const camRect = cam.getBoundingClientRect();
    const cw = camRect.width || 264;
    const ch = camRect.height || 152;
    const aspect = cw / ch;
    const tankRect = built.tr;
    const isAbility = obj.kind === "ability";

    // 优先按房间里那件东西的真实外框取景，保证整件都在画面内
    const el = document.querySelector(`#furnitureLayer .furn-item[data-id="${obj.id}"]`);
    let wx;
    let wy;
    let viewW;
    if (el && !isAbility) {
      const r = el.getBoundingClientRect();
      wx = r.left - tankRect.left + r.width / 2;
      wy = r.top - tankRect.top + r.height / 2;
      viewW = Math.max(r.width * 1.55, r.height * 1.55 * aspect, 74);
    } else {
      wx = isAbility ? built.fx * tankRect.width : (obj.x / 100) * tankRect.width;
      wy = isAbility ? built.fy * tankRect.height : (1 - (obj.y + 2) / 100) * tankRect.height;
      viewW = 150;
    }
    aimCamera(built, cam, wx, wy, viewW, 0.5);

    if (line) line.textContent = `解锁：${obj.name}`;
    if (tag) tag.textContent = `近景 · ${obj.name}`;
    cam.classList.remove("play", "unlock");
    void cam.offsetWidth;
    cam.classList.add("unlock");

    clearTimeout(showUnlockCamera._t);
    showUnlockCamera._t = setTimeout(() => { card.hidden = true; }, 3200);
  }

  /** 「她送我的」领取近景：她从手里掏出来 → 东西飞出镜头递向你 → 屏幕中央盖章。
      hergift.js 通过 window.__herCamera 调。 */
  function showHerGiftCamera(gift, line) {
    stopAct();
    const card = $("reactionCard");
    const cam = $("camera");
    const itemBox = $("cameraItem");
    const fishEl = $("fish");
    const tank = $("tank");
    if (!card || !cam || !itemBox || !fishEl || !tank || !gift) return;

    feedState.active = false;
    clearTimeout(feedState.hideTimer);
    card.hidden = false;
    const built = buildCameraWorld("cameraWorld");
    if (!built) return;

    const fishRect = fishEl.getBoundingClientRect();
    const tankRect = built.tr;
    const camRect = cam.getBoundingClientRect();
    const cw = camRect.width || 264;
    const ch = camRect.height || 152;
    const aspect = cw / ch;
    const headCx = fishRect.left - tankRect.left + fishRect.width / 2;
    const headCy = fishRect.top - tankRect.top + fishRect.height * 0.52;
    const viewW = Math.max(fishRect.width * 1.6, fishRect.height * 1.35 * aspect, 130);
    aimCamera(built, cam, headCx, headCy, viewW, 0.5);

    const tag = cam.querySelector(".camera-tag");
    if (tag) tag.textContent = "近景 · 她递给你";
    const rl = $("reactionLine");
    if (rl) rl.textContent = "……等一下。";
    const rd = $("reactionDelta");
    if (rd) rd.textContent = "";

    const icon = document.createElement("img");
    icon.alt = "";
    icon.src = `./assets/items/${encodeURIComponent(gift.icon || "")}`;
    icon.addEventListener("error", () => {
      icon.remove();
      const s = document.createElement("span");
      s.className = "furn-emoji";
      s.textContent = "🎁";
      itemBox.appendChild(s);
    });
    itemBox.replaceChildren(icon);
    // 手的位置：她身子右下、靠镜头这边
    itemBox.style.left = `${cw / 2 + fishRect.width * 0.34 * (cw / viewW)}px`;
    itemBox.style.top = `${ch / 2 + fishRect.height * 0.30 * (cw / viewW)}px`;
    cam.classList.remove("unlock", "play");
    void cam.offsetWidth;
    cam.classList.add("play");
    itemBox.classList.remove("fly", "hg-give");
    void itemBox.offsetWidth;
    itemBox.classList.add("hg-give");   // 从身后掏出来：小小地探出、停住
    if (fishCtl) fishCtl.react();

    clearInterval(showHerGiftCamera._frames);
    let f = 0;
    showHerGiftCamera._frames = setInterval(() => {
      f = (f + 1) % 8;
      built.fishClone.style.backgroundPosition = `${f * (100 / 7)}% 0`;
    }, 165);

    // 第二拍：东西离开镜头，朝屏幕外的你飞过来
    clearTimeout(showHerGiftCamera._t);
    showHerGiftCamera._t = setTimeout(() => {
      clearInterval(showHerGiftCamera._frames);
      const hand = itemBox.getBoundingClientRect();
      card.hidden = true;
      const flyer = document.createElement("img");
      flyer.alt = "";
      flyer.className = "hg-fly";
      flyer.src = icon.src;
      flyer.style.left = `${hand.left + hand.width / 2}px`;
      flyer.style.top = `${hand.top + hand.height / 2}px`;
      flyer.style.width = `${Math.max(34, hand.width)}px`;
      document.body.appendChild(flyer);
      const tr = tank.getBoundingClientRect();
      const dx = tr.left + tr.width / 2 - (hand.left + hand.width / 2);
      const dy = tr.top + tr.height * 0.42 - (hand.top + hand.height / 2);
      flyer.animate(
        [
          { transform: "translate(-50%, -50%) scale(1) rotate(0deg)", opacity: 1 },
          { transform: `translate(calc(-50% + ${dx * 0.4}px), calc(-50% + ${dy * 0.3 - 26}px)) scale(1.5) rotate(-10deg)`, opacity: 1, offset: 0.55 },
          { transform: `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px)) scale(2.5) rotate(6deg)`, opacity: 1 },
        ],
        { duration: 620, easing: "cubic-bezier(0.3, 0.7, 0.4, 1)", fill: "forwards" },
      );
      setTimeout(() => {
        flyer.remove();
        showGiftStamp(gift, line);
      }, 620);
    }, 1250);
  }

  /** 第三拍：屏幕中央的获得卡，点一下或几秒后收起。 */
  function showGiftStamp(gift, line) {
    const old = document.getElementById("hgStamp");
    if (old) old.remove();
    const el = document.createElement("div");
    el.id = "hgStamp";
    el.className = "hg-stamp";
    const card = document.createElement("div");
    card.className = "hg-stamp-card";
    const ic = document.createElement("div");
    ic.className = "hg-stamp-ic";
    const img = document.createElement("img");
    img.src = `./assets/items/${encodeURIComponent(gift.icon || "")}`;
    img.alt = "";
    img.addEventListener("error", () => { img.remove(); ic.textContent = "🎁"; });
    ic.appendChild(img);
    const cap = document.createElement("div");
    cap.className = "hg-stamp-cap";
    cap.textContent = "她递给你";
    const nm = document.createElement("div");
    nm.className = "hg-stamp-name";
    nm.textContent = `「${gift.name || ""}」`;
    const ln = document.createElement("p");
    ln.className = "hg-stamp-line";
    ln.textContent = (line && line.text) || gift.line || "";
    const ex = document.createElement("div");
    ex.className = "hg-stamp-ex";
    ex.textContent = "心愿 +2 · 已收进「她送我的」";
    card.append(ic, cap, nm, ln, ex);
    el.appendChild(card);
    el.addEventListener("click", () => el.remove());
    document.body.appendChild(el);
    setTimeout(() => { if (el.parentNode) el.classList.add("out"); }, 3400);
    setTimeout(() => { if (el.parentNode) el.remove(); }, 3900);
  }

  // 调试用：控制台调 __fx(4) / __fx(8) / __fx(12) 预览三档特效
  if (typeof window !== "undefined") window.__fx = (d) => splash({ delta: d, meters: { intimacy: { delta: d } } }, { animation: { color: "#ffd166" } });

  async function feed(gift, btnEl) {
    if (BUSY) return;
    if (!gift) return;
    if (!gift.can_send) {
      toast(gift.block_reason || "现在还不能送");
      playAct("doubt", { ms: 1600 });
      if (btnEl) {
        btnEl.animate(
          [{ transform: "translateX(0)" }, { transform: "translateX(-4px)" }, { transform: "translateX(4px)" }, { transform: "translateX(0)" }],
          { duration: 240 },
        );
      }
      return;
    }
    BUSY = true;
    const rect = btnEl ? btnEl.getBoundingClientRect() : null;
    try {
      let payload;
      if (DEMO) {
        await sleep(200);
        payload = demoSend(gift.id);
      } else {
        if (!VIEW) VIEW = await waitForBridge();
        if (!VIEW || typeof VIEW.apiPost !== "function") throw new Error("当前环境不支持喂食，请从 AstrBot 插件页面打开。");
        payload = unwrap(await VIEW.apiPost(API_SEND, { gift: gift.id }));
      }
      if (!payload) throw new Error("服务器没有返回结果");
      const result = payload.result || {};
      const fallbackRect = rect || { left: innerWidth / 2, top: innerHeight, width: 0, height: 0 };
      flyToFish(fallbackRect, gift, () => {
        hintDone = true;
        render(payload);
        if (result.mood && result.mood.state === "bored") {
          // 吃腻了：真动画「吃到不喜欢的吃的」，顶掉近景
          playAct("eat_dislike", { ms: 1700 });
          splash(result, gift);
        } else if (EAT_GIF[gift.id]) {
          // 有真吃东西动画：先吃，吃完再切近景
          splash(result, gift);
          playAct(EAT_GIF[gift.id], {
            ms: EAT_MS[gift.id] || 1600,
            thenBefore: () => showReaction(result, gift, EAT_GIF[gift.id]),
          });
        } else {
          if (fishCtl && !(gift && gift.action)) fishCtl.react();
          playFoodAction(gift, result);
          splash(result, gift);
          showReaction(result, gift);
        }
      });
    } catch (e) {
      toast(`没喂成：${e && e.message ? e.message : e}`);
    } finally {
      BUSY = false;
    }
  }

  /* ---------------- 加载 / 交互 ---------------- */

  function fail(error) {
    const msg = error && error.message ? error.message : String(error);
    const box = $("errorCard");
    const text = $("errorText");
    if (text) text.textContent = msg;
    if (box) box.hidden = false;
  }

  async function load() {
    try {
      if (DEMO) {
        render(demoState());
        return;
      }
      if (!VIEW) VIEW = await waitForBridge();
      if (!VIEW || typeof VIEW.apiGet !== "function") {
        throw new Error("等不到 AstrBot 的页面桥接脚本。通常是 AstrBot 版本不支持插件页面，或者这个页面被直接打开、没有经过面板。");
      }
      applyTheme(VIEW);
      const data = unwrap(await VIEW.apiGet(API_STATE));
      if (!data || !data.gifts) throw new Error("返回数据里没有礼物列表");
      render(data);
    } catch (e) {
      const msg = e && e.message ? e.message : String(e);
      if (msg.includes("未找到该路由") || /not found/i.test(msg)) {
        fail("插件还没有注册页面接口。请在插件管理里重载「鱼的窝」——接口是在插件加载时注册的，只刷新页面不会生效。");
      } else {
        fail(msg);
      }
    }
  }

  async function readAll() {
    if (DEMO) {
      DATA.pending = [];
      render(DATA);
      toast("提醒已清空");
      return;
    }
    try {
      if (!VIEW) VIEW = await waitForBridge();
      if (!VIEW || typeof VIEW.apiPost !== "function") throw new Error("当前环境不支持");
      const data = unwrap(await VIEW.apiPost(API_READ, {}));
      if (data && data.gifts) render(data);
      toast("提醒已清空");
    } catch (e) {
      toast(`清空失败：${e && e.message ? e.message : e}`);
    }
  }

  function openSheet(open) {
    const sheet = $("sheet");
    if (!sheet) return;
    sheet.hidden = !open;
    if (open) renderSheet(DATA || {});
  }

  function bind() {
    const refresh = $("btnRefresh");
    if (refresh) refresh.addEventListener("click", load);
    // 家具工坊摆完东西回来：重新拉一次状态，房间立刻按新摆位重画
    window.addEventListener("furngen:refresh", () => { load(); });
    const retry = $("btnRetry");
    if (retry) retry.addEventListener("click", load);
    const read = $("btnRead");
    if (read) read.addEventListener("click", readAll);
    const detail = $("btnDetail");
    if (detail) detail.addEventListener("click", () => openSheet(true));
    const close = $("btnCloseSheet");
    if (close) close.addEventListener("click", () => openSheet(false));
    const sheet = $("sheet");
    if (sheet) {
      sheet.addEventListener("click", (ev) => {
        if (ev.target === sheet) openSheet(false);
      });
    }
    const filters = $("filters");
    if (filters) {
      filters.addEventListener("click", (ev) => {
        const btn = ev.target.closest("button[data-filter]");
        if (!btn) return;
        FILTER = btn.dataset.filter;
        filters.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
        renderGiftGrid();
      });
    }

    // 点她 = 用当前选中的礼物喂食
    const fishEl = $("fish");
    if (fishEl) {
      const press = (on) => { if (fishCtl) fishCtl.setPressed(on); };
      fishEl.addEventListener("pointerdown", () => press(true));
      fishEl.addEventListener("pointerup", () => press(false));
      fishEl.addEventListener("pointercancel", () => press(false));
      fishEl.addEventListener("pointerleave", () => press(false));
      window.addEventListener("pointerup", () => press(false));
      fishEl.addEventListener("click", () => {
        press(false);
        if (hotLocked) feedSelected();
        else {
          showTalkBubble();
          if (!actEl && Math.random() < 0.35) playAct(Math.random() < 0.5 ? "shy" : "love", { ms: 1700 });
        }
      });
    }

    // 屏幕方向键已经撤掉，走动只留键盘方向键

    // iframe 里键盘要先拿到焦点
    try {
      document.body.tabIndex = -1;
      document.addEventListener("pointerdown", () => {
        try { window.focus(); document.body.focus({ preventScroll: true }); } catch (e) { /* 忽略 */ }
      }, true);
    } catch (e) { /* 忽略 */ }

    // 顶部按钮换成像素提示
    [["btnTest", "成就测试"], ["btnDetail", "详情"], ["btnRefresh", "刷新"]].forEach(([id, text]) => {
      bindTip($(id), text);
    });
    bindTip(document.querySelector(".panel-foot"), "心愿：她愿意对你打开的程度。聊天、完成今日心愿、及时回她都会涨，很多礼物的解锁条件要看它。");

    const btnTest = $("btnTest");
    if (btnTest) btnTest.addEventListener("click", () => toggleTestPanel());
    const testAll = $("testAll");
    if (testAll) testAll.addEventListener("click", previewAll);
    const testReset = $("testReset");
    if (testReset) testReset.addEventListener("click", () => { previewSnapshot = null; load(); });

    document.addEventListener("keydown", (ev) => {
      const s = $("sheet");
      const sheetOpen = s && !s.hidden;
      if (ev.key === "Escape" && sheetOpen) { openSheet(false); return; }
      if (sheetOpen) return;
      if (ev.key >= "1" && ev.key <= "9") { selectSlot(Number(ev.key) - 1); return; }
      if (ev.key === "0") { selectSlot(9); return; }
      if (ev.key === "q" || ev.key === "Q") { hotbarStep(-1); return; }
      if (ev.key === "e" || ev.key === "E") { hotbarStep(1); return; }
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        feedSelected();
        return;
      }
      const dir = MOVE_KEYS[ev.key];
      if (dir && fishCtl) {
        ev.preventDefault();
        fishCtl.setKey(dir, true);
      }
    });

    document.addEventListener("keyup", (ev) => {
      const dir = MOVE_KEYS[ev.key];
      if (dir && fishCtl) fishCtl.setKey(dir, false);
    });

    window.addEventListener("blur", () => { if (fishCtl) fishCtl.clearKeys(); });
  }

  function preloadChar() {
    /* 身体已经换成 act/idle.gif + act/walk.gif，由 CSS 直接播；
       旧的 idle.png / run.png 八帧横条不再使用。这里只预热两张 GIF。 */
    const idle = new Image();
    idle.src = "./assets/char/act/idle.gif";
    const walk = new Image();
    walk.src = "./assets/char/act/walk.gif";
  }

  /* 时段/天气背景：?bg=clear|rain|snow|night，不带就默认「今晚 · 夜微雨」 */
  function applyBg() {
    const bg = params.get("bg");
    if (bg && ["clear", "rain", "snow", "night"].includes(bg)) {
      document.documentElement.dataset.bg = bg;
    }
  }

  function boot() {
    preloadChar();
    applyBg();
    applyTheme(null);
    try { window.focus(); } catch (e) { /* 忽略 */ }
    bind();
    if (!DEMO) {
      waitForBridge().then((v) => {
        VIEW = v;
        if (v) applyTheme(v);
      });
    }
    if ($("fish")) {
      fishCtl = new FishController($("fish"));
    }
    load();
    // 闲时行为：任何交互都打断她；没事时自己找事做
    ["pointerdown", "keydown", "wheel"].forEach((ev) => window.addEventListener(ev, markInteract, { passive: true }));
    setInterval(idleTick, 6000);
    setInterval(ambientPoll, 8000);
    setInterval(typingPoll, 1200);
    window.addEventListener("av-banner", () => {
      if (!actEl && !BUSY) {
        const rc = $("reactionCard");
        if (rc && rc.hidden) playAct("surprise", { ms: 1500 });
      }
    });
    // 演示模式：?act=phone 直接播一个行为（sit/phone/yawn/disgust/pout/angry/speechless/surprise/delight/love/shy/doubt/scorn）
    if (DEMO && params.get("act")) {
      const k = params.get("act");
      manualAct = true;
      setTimeout(() => playAct(k, { hold: REPORTED_ACTS.indexOf(k) >= 0, ms: 2100 }), 600);
    }
    // 演示模式：?demo=1&feed=candy&feedat=600 自动喂一次，方便截图验收动画
    if (DEMO && params.get("feed")) {
      const giftId = params.get("feed");
      const at = Number(params.get("feedat") || 500);
      setTimeout(() => {
        const gifts = hotbarGifts(DATA);
        const i = gifts.findIndex((x) => x.id === giftId);
        if (i >= 0) hotSel = i;
        renderHotbar(DATA);
        const g = gifts[i >= 0 ? i : hotSel];
        if (g) feed(g, document.querySelectorAll("#hotbar .slot")[hotSel]);
      }, at);
    }
    // 演示模式：?demo=1&debug=1 把布局读数写进标题（排查用）
    if (DEMO && params.get("debug")) {
      setTimeout(() => {
        const f = $("fish").getBoundingClientRect();
        const t = $("tank").getBoundingClientRect();
        const c = fishCtl && typeof fishCtl.limits === "function" ? fishCtl.limits() : {};
        const box = document.createElement("pre");
        box.style.cssText = "position:fixed;left:0;top:220px;z-index:999;margin:0;padding:6px;background:#000;color:#0f0;font:11px monospace;white-space:pre-wrap;max-width:100%;";
        box.textContent = `fish ${Math.round(f.width)}x${Math.round(f.height)} left=${Math.round(f.left)} top=${Math.round(f.top)}\ntank ${Math.round(t.width)}x${Math.round(t.height)}\nx=${(fishCtl && fishCtl.x).toFixed(1)} y=${(fishCtl && fishCtl.y).toFixed(1)}\nlim ${JSON.stringify(c)}`;
        document.body.appendChild(box);
      }, 900);
    }
    // 演示模式：?demo=1&creative=1 显示创造模式徽章（截图验收用）
    if (DEMO && params.get("creative")) {
      setTimeout(() => { if (DATA) { DATA.creative = true; renderPanel(DATA); } }, 600);
    }
    // 演示模式：?demo=1&unlock=sofa 直接播一次解锁近景（截图验收用）
    if (DEMO && params.get("unlock")) {
      setTimeout(() => showUnlockCamera(params.get("unlock")), 900);
    }
    // 演示模式：?demo=1&sheet=1 直接展开详情抽屉（截图验收用）
    if (DEMO && params.get("sheet")) {
      setTimeout(() => openSheet(true), 300);
    }
    // 演示模式：?demo=1&arrive=candy 直接演一遍"到嘴边"的落点效果（截图验收用）
    if (DEMO && params.get("arrive")) {
      const giftId = params.get("arrive");
      setTimeout(() => {
        const g = ((DATA && DATA.gifts) || []).find((x) => x.id === giftId);
        if (!g) return;
        const payload = demoSend(g.id);
        const result = payload.result || {};
        render(payload);
        if (fishCtl) fishCtl.react();
        splash(result, g);
        showReaction(result, g);
      }, 400);
    }
    setInterval(() => {
      if (document.hidden || BUSY || testPreview) return;
      const s = $("sheet");
      if (s && !s.hidden) return;
      load();
    }, 30000);
  }

  /* 窗口尺寸变了，地板家具要重新按物品栏高度夹紧（防抖一下） */
  window.addEventListener("resize", () => {
    clearTimeout(relayoutFloor._t);
    relayoutFloor._t = setTimeout(relayoutFloor, 180);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  // 供 hergift.js 调用的领取近景
  window.__herCamera = showHerGiftCamera;
  // 供 life.js 调用：台阶/点单后她的小反应
  window.__lfFishReact = function () { if (fishCtl) fishCtl.react(); };
  /* DIY 才走表情：现成的每种吃的都有自己的吃东西动画，不抢它们的戏。
     后端给了 react（delight/eat/neutral/disgust/angry），没给就按分数兜底。 */
  window.__lfDiyReact = function (score, react) {
    const map = { delight: "delight", eat: "love", neutral: "shy", disgust: "disgust", angry: "angry" };
    const key = (react && map[react]) ? map[react]
      : (score >= 8 ? "delight" : score > 0 ? "love" : score === 0 ? "shy" : "disgust");
    playAct(key, { ms: (key === "angry" || key === "disgust") ? 1900 : 1700 });
  };
})();
