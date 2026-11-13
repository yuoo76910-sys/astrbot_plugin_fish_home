"""鱼的窝 —— 鱼的窝 / 心愿值 / 像素房间收礼动画。

设计约束
--------
1. 亲密度只读、只改内存：礼物效果直接作用于「鱼的心」运行中的 state，
   调它自己的 _save_state 落盘；不改它的代码、不直接写它的存档文件。
   拿不到运行实例时退回本地成长值，绝不碰它的文件。
2. 心愿值（wish）是奖励插件自己的成长值：聊天、及时回她、送礼、完成她
   当天的心愿都会涨；长时间不理她、她委屈、你忽略她主动说的话会掉。
   商品条件可以同时要求「历史最高亲密」「当前心愿」「聊天总量」「送出
   礼物数」等，所以是复合条件，也可以藏起来不告诉你。
3. 面板和聊天 GIF 共用同一套礼物表（assets/gifts.json）和像素素材
   （views/reward/assets/），不重复维护。
4. 运行期只用标准库；Pillow 只在 tools/make_reward_assets.py 生成素材时用。
5. 任何外部依赖失败都只降级，不让插件起不来。

作者: zexuan604   版本: 0.1.0   许可: MIT
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import request as web_request
from astrbot.core.config.astrbot_config import AstrBotConfig

PLUGIN_NAME = "astrbot_plugin_fish_home"
VERSION = "0.2.1"
PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------- #
# 天气 → 房间背景
# 房间里的窗必须和用户当地实况对得上，不然一眼穿帮：
# 杭州下着雨，窗外挂着大太阳，这插件就废了。
# 做法：拉 open-meteo 的实况 → 用官方那套生成器（bg_scene.py，480×270 原生
# ×4 放大）现画一张 1920×1080，按「时段 × 天气」存成固定文件名，前端只换 url。
# --------------------------------------------------------------------------- #
WMO_CN = {
    "clear": "晴", "cloudy": "多云", "overcast": "阴", "fog": "雾",
    "rain": "雨", "thunder": "雷阵雨", "snow": "雪",
}
SLOT_CN = {"day": "白天", "dusk": "黄昏", "night": "夜里"}


def wmo_kind(code: int) -> str:
    """WMO 天气代码 → 生成器认的 7 种天气。"""
    c = int(code or 0)
    if c in (0, 1):
        return "clear"
    if c == 2:
        return "cloudy"
    if c == 3:
        return "overcast"
    if c in (45, 48):
        return "fog"
    if c in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    if c in (71, 73, 75, 77, 85, 86):
        return "snow"
    if c in (95, 96, 99):
        return "thunder"
    return "cloudy"


def season_of(month: int) -> str:
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "autumn"


def moon_phase_of(d: datetime) -> float:
    """0 = 新月，0.5 = 满月。拿 2000-01-06 那次新月做基准，误差够小。"""
    base = datetime(2000, 1, 6, 18, 14)
    return ((d - base).total_seconds() / 86400.0 % 29.530588853) / 29.530588853
STATE_VERSION = 4

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(PLUGIN_DIR, "assets")
GIF_DIR = os.path.join(PLUGIN_DIR, "views", "reward", "assets", "reactions")
ACT_DIR = os.path.join(PLUGIN_DIR, "views", "reward", "assets", "char", "act")
# 食物 id → 吃东西 GIF（聊天端喂后主动消息配图）
EAT_GIF_BY_ID = {
    "rice": "eat_rice.gif", "strawberry_candy": "eat_candy.gif", "kiwi": "eat_kiwi.gif",
    "pudding": "eat_pudding.gif", "ramen": "eat_ramen.gif", "pizza": "eat_pizza.gif",
    "hot_cocoa": "drink_cocoa.gif", "oden": "eat_oden.gif",
}
# 送礼口味反馈 → 表情 GIF（聊天端送礼回执配图，优先于旧合成 GIF）
MOOD_GIF_BY_STATE = {"liked": "love.gif", "bored": "disgust.gif", "missed": "speechless.gif"}
GIFTS_FILE = os.path.join(ASSETS_DIR, "gifts.json")
OBJECTS_FILE = os.path.join(ASSETS_DIR, "objects.json")
ACHV_FILE = os.path.join(ASSETS_DIR, "achievements.json")
LATE_NIGHTS_FILE = os.path.join(ASSETS_DIR, "late_nights.json")
CARDS_FILE = os.path.join(ASSETS_DIR, "growth_cards.json")
HER_GIFTS_FILE = os.path.join(ASSETS_DIR, "gifts_from_her.json")
# 美术皮肤清单：换图只改这张表，不动代码
SKIN_FILE = os.path.join(PLUGIN_DIR, "views", "reward", "assets", "skin", "skin.json")

# ---------------- 家具工坊：AI 出施工图，代码渲染像素家具 ----------------
# 路线：用户一句话 → 她的聊天模型出「施工图」JSON（调色板 + 图元指令）
#      → 代码（PIL）平涂渲染 + 自动 1px 深墨描边 → 透明小 PNG。
# 画风铁律由渲染器写死：平涂、无抗锯齿、色板 ≤8、画布 ≤48；模型只决定形状配色。
FURN_OUTLINE = (42, 35, 51, 255)     # 自动描边色（深墨）
FURN_MAX_CANVAS = 48                 # 画布最长边
FURN_MAX_PALETTE = 8                 # 调色板颜色数上限
FURN_MAX_OPS = 120                   # 图元指令条数上限
FURN_OP_KINDS = ("rect", "ellipse", "tri", "line", "mirror")
FURN_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

FURN_DESIGN_SYSTEM = (
    "你是像素素材施工图设计师。用户想画什么都行——家具、动物、食物、一张脸、一件道具——"
    "你严格按他说的主体来画，绝不自作主张换成别的东西，输出一份 JSON 施工图，"
    "由程序渲染成 48 像素以内的像素画。\n"
    "规则：\n"
    "1. 只输出一个 JSON 对象，不要代码块、不要解释、不要多余文字。\n"
    "2. 字段：\n"
    '   "name": 物件名，2~8 个字，照用户说的主体起；\n'
    '   "size": [宽,高]，画布（4~48），按实际比例，盆栽竖长、地毯扁宽；\n'
    '   "palette": 颜色数组，1~8 个 "#rrggbb"，由浅到深；\n'
    '   "ops": 指令数组，最多 60 条，按给出的顺序执行（先画大色块，再画细节）。\n'
    "3. 指令（坐标都是整数，原点在左上角，画出画布会被裁掉）：\n"
    '   {"op":"rect","x":..,"y":..,"w":..,"h":..,"c":..} 矩形；\n'
    '   {"op":"ellipse","x":..,"y":..,"w":..,"h":..,"c":..} 椭圆（外接框）；\n'
    '   {"op":"tri","x":..,"y":..,"w":..,"h":..,"c":..} 等腰三角（顶点在上中）；\n'
    '   {"op":"line","x":..,"y":..,"x2":..,"y2":..,"c":..} 1px 直线；\n'
    '   {"op":"mirror","axis":"x"} 把已画内容左右镜像（对称的主体——脸、椅子、正对的猫——先画一半再镜像）。\n'
    "   c 是调色板下标，从 0 开始。\n"
    "4. 描边、阴影、地面都不要画（程序会自动描 1px 深墨边）；只用平涂色块，"
    "细节用 1~2px 的小 rect/line。\n"
    "示例（棕色小食盆）：\n"
    '{"name":"小食盆","size":[16,10],"palette":["#c89a6b","#8a5a33","#5a3a1f"],'
    '"ops":[{"op":"ellipse","x":1,"y":2,"w":14,"h":6,"c":1},'
    '{"op":"ellipse","x":3,"y":3,"w":10,"h":3,"c":0},'
    '{"op":"rect","x":1,"y":7,"w":14,"h":2,"c":2}]}'
)

# 命令/别名：计数时排除，避免把「/奖励 送 糖果」也当成聊天
COMMAND_WORDS = {
    "奖励", "礼物", "心", "reset", "help", "帮助", "新对话", "清空对话",
    "persona", "人格", "model", "模型", "provider", "tts", "websearch",
    "plugin", "插件", "sid", "name", "改名", "状态",
}

PRAISE_WORDS = (
    "真棒", "真厉害", "好厉害", "太强了", "好聪明", "真聪明", "真乖",
    "好可爱", "真可爱", "最喜欢你", "喜欢你", "夸你", "漂亮", "可爱",
    "厉害", "不错", "真行", "靠谱",
)
GOODNIGHT_WORDS = ("晚安", "睡了", "睡啦", "去睡", "睡觉了", "先睡")

# 这几样不该有「想吃 / 吃腻」的脸色：白米饭每天白给，喂多少次都照吃
NO_MOOD_IDS = {"rice"}

# 说话态度直接决定好感度：凶她会掉，软话会涨
MEAN_WORDS = (
    "滚", "闭嘴", "烦死了", "讨厌你", "别烦我", "不想理你", "不理你了",
    "傻逼", "傻狗", "废物", "去死", "烦不烦",
)
GENTLE_WORDS = (
    "谢谢", "辛苦", "对不起", "抱歉", "陪我", "抱抱", "别生气", "原谅",
    "我错了", "哄你", "乖乖",
)

STAT_CN = {
    "intimacy": "当前亲密",
    "intimacy_peak": "历史最高亲密",
    "wish": "当前心愿",
    "wish_peak": "心愿最高",
    "expectation": "她的期待",
    "expectation_peak": "她等过你",
    "attachment": "依恋",
    "joy": "喜悦",
    "stress": "压力",
    "blame": "责怪",
    "chat_total": "聊天总量",
    "chat_today": "今日聊天",
    "streak": "连续天数",
    "gifts_sent": "送出礼物",
    "gifts_distinct": "送过的礼物种类",
    "daily_wishes_done": "完成心愿次数",
    "hour": "时间",
    "mode": "状态",
    "level": "亲密层数",
    "day_index": "认识天数",
    "gap_days": "离开天数",
    "foods_distinct": "尝过的食物",
    "late_nights": "深夜事件",
    "ach_done_others": "解锁成就",
    "her_gift_count": "她送出的回礼",
}

# ---------------- 线索抽取：从主人消息里读"他今天怎么样" ----------------
# 白名单 tag → 关键词。只抽主人说自己的事；命中即记原句（evidence），
# 延迟回礼与手写信都靠原句说话——读错也能被原文救回来。
SIGNAL_TAGS: dict[str, tuple[str, ...]] = {
    # 身体
    "sick": ("生病了", "生病", "感冒", "发烧", "头疼", "头痛", "肚子疼", "胃疼", "不舒服", "难受", "医院"),
    "insomnia": ("失眠", "睡不着", "睡不好", "整夜没睡", "一宿没睡"),
    "no_meal": ("没吃饭", "没吃早饭", "没吃午饭", "没吃晚饭", "忘了吃饭", "还没吃饭", "没来得及吃"),
    "tired": ("好累", "太累", "累死", "疲惫", "困死", "没睡够", "睡眠不足"),
    # 工作学业
    "work_late": ("加班", "赶方案", "赶报告", "改需求", "996"),
    "exam": ("考试", "期末", "答辩", "面试", "考研", "考公", "刷题"),
    "scolded": ("被骂", "挨骂", "被批", "被说了一顿", "被怼"),
    "job_change": ("换工作", "辞职", "离职", "跳槽", "新工作", "入职"),
    # 关系
    "fight": ("吵架", "吵了一架", "闹掰", "冷战了"),
    "miss_you": ("想你", "想你了", "惦记", "挂念"),
    "cold_shoulder": ("被冷落", "没人理", "孤单", "一个人吃饭", "一个人待着"),
    "makeup": ("和好了", "不吵了", "和解了"),
    # 作息
    "stay_up": ("熬夜", "通宵", "两点睡", "三点睡", "凌晨一点", "凌晨两点", "凌晨三点"),
    "early_rise": ("早起", "五点起", "六点起", "天没亮就起"),
    "weekend": ("周末", "放假", "休假", "调休"),
    # 节日纪念
    "birthday": ("生日", "今天是我生日"),
    "anniversary": ("纪念日", "一周年", "两周年", "周年"),
    "festival": ("中秋", "春节", "元旦", "圣诞", "端午", "清明", "国庆"),
    # 危险组：只用于触发关心/加急信，不参与任何负面判定
    "self_neglect": ("不想活", "活不下去", "没意思透了", "人间不值得", "消失算了"),
    "extreme": ("崩溃", "大哭", "哭了一晚", "心态崩", "受够了"),
}

SIGNAL_TAG_CN = {
    "sick": "身体不舒服", "insomnia": "失眠", "no_meal": "没吃饭", "tired": "很累",
    "work_late": "加班", "exam": "考试赶due", "scolded": "被骂", "job_change": "换工作",
    "fight": "吵架", "miss_you": "想她", "cold_shoulder": "被冷落", "makeup": "和好",
    "stay_up": "熬夜", "early_rise": "早起", "weekend": "放假",
    "birthday": "生日", "anniversary": "纪念日", "festival": "节日",
    "self_neglect": "说丧气话", "extreme": "情绪很崩",
}

DANGER_TAGS = ("self_neglect", "extreme")

for _t in SIGNAL_TAGS:
    STAT_CN[f"sig_{_t}_7d"] = f"近7天提到「{SIGNAL_TAG_CN[_t]}」"
STAT_CN["sig_danger_7d"] = "近7天情绪很崩的次数"
STAT_CN["panel_seen"] = "打开过小窝"

RARITY_CN = {
    "common": "普通",
    "rare": "稀有",
    "epic": "史诗",
    "legendary": "传说",
    "secret": "隐藏",
}

# 本地积累槽：每个值 0~100，攒满一轮就把溢出的部分留下，
# 同时往「鱼的心」对应项反哺一次、自己的等级 +1（等级只是数字，没有数值效果、没有上限）。
BILI_PLUGIN_NAME = "astrbot_plugin_bilibili_ai_bot"

# 喂/送之后主动说的那一句：先按「吃 / 喝 / 收下」分，再按性格分。{name} 会替换成东西的名字
POST_FEED_PERSONAS = ("傲娇", "温柔", "冷淡", "元气", "黏人", "毒舌", "天然", "无口")

_POST_FEED_COMMON = {
    "傲娇": {
        "food": ["{name}……还行吧，我又没说喜欢。", "哼，{name} 也不是不能吃，别多想。", "你给的？那、那就勉强吃掉好了。"],
        "drink": ["哼，{name} 还行……别以为我馋。", "{name}？喝就喝，谁怕谁。"],
        "item": ["{name}？我、我收下了，别多想。", "哼，{name} 也不是不能要。", "给我 {name}……那、那就谢了。"],
    },
    "温柔": {
        "food": ["{name} 很好吃，谢谢你。", "刚吃完 {name}，心情好了一点。", "嗯，{name} 我记住了。"],
        "drink": ["{name} 很好喝，谢谢。", "喝完 {name} 了，甜甜的。"],
        "item": ["谢谢你的 {name}，我会好好收着。", "{name} 收到了，很喜欢。"],
    },
    "冷淡": {
        "food": ["{name}，吃完了。", "嗯，{name}。", "{name} 可以。"],
        "drink": ["{name}，喝完了。", "嗯，{name}。"],
        "item": ["{name}，收下了。", "嗯，{name}。"],
    },
    "元气": {
        "food": ["{name}！超好吃的！还有吗还有吗？", "刚吃完 {name}，现在浑身是劲！"],
        "drink": ["{name}！一口下去超爽！", "喝完 {name}，满血复活！"],
        "item": ["哇，{name}！我要把它放最显眼的地方！", "{name} 收到！超开心！"],
    },
    "黏人": {
        "food": ["{name} 吃完了……你还在吗？", "吃完 {name} 了，再陪我说会儿话嘛。"],
        "drink": ["{name} 喝完了……你别走啊。", "喝完 {name}，能再坐一会儿吗。"],
        "item": ["{name} 我收下了……你还会再来吗？", "拿着 {name}，好像你就在旁边。"],
    },
    "毒舌": {
        "food": ["{name} 也就能吃，别指望我夸你。", "就 {name}？行吧，总比没有强。"],
        "drink": ["{name}？凑合喝吧。", "就一杯 {name}，你倒是会省。"],
        "item": ["{name}？行吧，勉强收下。", "就 {name}？下次记得用点心。"],
    },
    "天然": {
        "food": ["{name} 是什么味道来着……啊，已经吃完了。", "刚才那个 {name}，好好吃！"],
        "drink": ["{name}……甜甜的，是什么味道来着。", "喝完 {name}，好像更渴了？"],
        "item": ["{name}……放哪儿好呢。", "{name}，我先拿着。"],
    },
    "无口": {
        "food": ["……{name}。", "{name}。（点了点头）"],
        "drink": ["……{name}。", "{name}。（喝完放下）"],
        "item": ["……{name}。", "{name}。（收进怀里）"],
    },
}


def post_feed_pool(persona: str, kind: str) -> list:
    table = _POST_FEED_COMMON.get(persona) or _POST_FEED_COMMON["温柔"]
    return table.get(kind) or table["item"]


# 工资：写死的两档（设置里不再暴露）
WAGE_HOURS = 6.0     # 挂满 6 小时
WAGE_MAX = 100       # 满勤 100 块

METER_MAX = 100.0
FEED_STEP = 10.0
METER_KEYS = ("intimacy", "joy", "attachment", "soothe")
METER_LABELS = {
    "intimacy": "亲密度",
    "joy": "喜悦",
    "attachment": "依恋",
    "soothe": "安抚",
}
# 本地槽 -> (鱼的心 drives 字段, 方向)。安抚是「被安抚」，攒满反哺成压力 -10。
METER_FEED = {
    "intimacy": ("intimacy", 1.0),
    "joy": ("joy", 1.0),
    "attachment": ("attachment", 1.0),
    "soothe": ("stress", -1.0),
}
HEART_FIELD_CN = {"intimacy": "亲密", "joy": "喜悦", "attachment": "依恋", "stress": "压力"}


# 九维中文名（鱼的心 drives）
RADAR_CN = {
    "attachment": "依恋", "curiosity": "好奇", "reflection": "反刍",
    "duty": "责任", "social": "社交", "fatigue": "疲惫",
    "intimacy": "亲密", "stress": "压力", "joy": "喜悦",
}
# 鱼的心数据目录（用于兜底读取）
PLUGIN_DIRS_HEART = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugin_data", "astrbot_plugin_fish_heart",
)

MODE_CN = {"calm": "平静", "wronged": "委屈", "afraid": "害怕", "shut": "回避"}


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


def now_ts() -> float:
    return time.time()


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def hour_now() -> int:
    return datetime.now().hour


def mode_of(blame: float) -> str:
    if blame >= 80.0:
        return "shut"
    if blame >= 55.0:
        return "afraid"
    if blame >= 25.0:
        return "wronged"
    return "calm"


def mood_label(drives: dict) -> str:
    """和「鱼的心」面板同一套心情判断，只用于展示。"""
    g = lambda k: float(drives.get(k, 0.0) or 0.0)  # noqa: E731
    if g("stress") >= 50:
        return "紧绷"
    if g("fatigue") >= 65:
        return "累了"
    if g("attachment") >= 80 and g("intimacy") >= 80:
        return "黏人"
    if g("attachment") >= 80:
        return "想亲近"
    if g("joy") >= 85:
        return "开心"
    if g("reflection") >= 65:
        return "走神"
    if g("curiosity") >= 70:
        return "好奇"
    return "平常"


def level_info(n: int, progress: float = 0.0) -> dict:
    """等级 = 攒满的轮数，从 0 开始、无上限，纯数字不带来任何效果。"""
    return {
        "n": int(n),
        "title": "",
        "min": 0,
        "next_at": None,
        "next_title": None,
        "progress": round(clamp(progress), 4),
    }


def fmt_num(v: float, nd: int = 1) -> str:
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return f"{v:.{nd}f}"


def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# 礼物表 / 每日心愿
# --------------------------------------------------------------------------- #

_DEFAULT_GIFTS = [
    {
        "id": "candy",
        "name": "加倍佳棒棒糖",
        "emoji": "🍬",
        "icon": "candy.png",
        "rarity": "common",
        "desc": "她最爱的酸甜味",
        "effect": {"intimacy": 6, "joy": 4, "attachment": 1, "stress": -1, "wish": 2},
        "unlock": {
            "kind": "daily",
            "conditions": [{"stat": "chat_today", "op": ">=", "value": 10}],
        },
        "source": "每天陪她聊满 10 句",
        "lines": ["哼，还知道我爱吃这个"],
        "animation": {"particle": "heart", "color": "#ff9ec4", "motion": "nibble"},
    },
    {
        "id": "milk",
        "name": "草莓牛奶",
        "emoji": "🥛",
        "icon": "milk.png",
        "rarity": "common",
        "desc": "草莓味的",
        "effect": {"intimacy": 6, "joy": 4, "attachment": 1, "stress": -1, "wish": 2},
        "unlock": {"kind": "milestone", "conditions": [{"stat": "chat_total", "op": ">=", "value": 30}]},
        "source": "聊天累计 30 句",
        "lines": ["草莓味的……算你有心"],
        "animation": {"particle": "bubble", "color": "#a8e6ff", "motion": "float"},
    },
]


def load_gift_file() -> tuple[list[dict], list[dict], dict]:
    """读礼物表；文件坏了就用内置的两件，不让插件起不来。"""
    try:
        with open(GIFTS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        gifts = [g for g in (raw.get("gifts") or []) if isinstance(g, dict) and g.get("id")]
        wishes = [w for w in (raw.get("daily_wishes") or []) if isinstance(w, dict) and w.get("id")]
        mode_lines = raw.get("mode_lines") if isinstance(raw.get("mode_lines"), dict) else {}
        if not gifts:
            gifts = list(_DEFAULT_GIFTS)
        return gifts, wishes, mode_lines
    except Exception as e:
        logger.warning("[鱼的窝] 读礼物表失败，使用内置礼物: %s", e)
        return list(_DEFAULT_GIFTS), [], {}


def load_objects() -> list[dict]:
    """读小窝家具表；读不到就不给家具，不影响主流程。"""
    try:
        with open(OBJECTS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        rows = [o for o in (raw.get("objects") or []) if isinstance(o, dict) and o.get("id")]
        return rows
    except Exception as e:
        logger.warning("[鱼的窝] 读家具表失败: %s", e)
        return []


def furn_parse_spec(text: str) -> dict:
    """从模型回复里抠出施工图 JSON；抠不出就抛 ValueError。"""
    t = str(text or "").strip()
    if not t:
        raise ValueError("模型没回话")
    t = re.sub(r"```(?:json)?", "", t).strip().strip("`").strip()
    if not t.startswith("{"):
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise ValueError("回复里没有 JSON")
        t = m.group(0)
    try:
        raw = json.loads(t)
    except Exception:
        raise ValueError("JSON 解析失败")
    if not isinstance(raw, dict):
        raise ValueError("不是 JSON 对象")
    return raw


def furn_validate_spec(raw: dict, prompt: str = "") -> dict:
    """把施工图修成能渲染的样子：字段夹范围、非法指令丢弃。修不动就抛 ValueError。"""
    name = str(raw.get("name") or "").strip()
    if not name:
        name = str(prompt or "").strip()[:8] or "小家具"
    name = name[:12]

    size = raw.get("size") or []
    if not isinstance(size, (list, tuple)) or len(size) != 2:
        raise ValueError("size 不对")
    try:
        w = int(size[0])
        h = int(size[1])
    except Exception:
        raise ValueError("size 不是数字")
    w = max(6, min(FURN_MAX_CANVAS, w))
    h = max(6, min(FURN_MAX_CANVAS, h))

    pal_raw = raw.get("palette") or []
    if not isinstance(pal_raw, list) or not pal_raw:
        raise ValueError("没有调色板")
    palette = []
    for c in pal_raw[:FURN_MAX_PALETTE]:
        c = str(c or "").strip().upper()
        if not FURN_HEX_RE.match(c):
            raise ValueError(f"颜色「{c}」不是 #rrggbb")
        palette.append(c)
    if not palette:
        raise ValueError("调色板是空的")

    ops_raw = raw.get("ops") or []
    if not isinstance(ops_raw, list) or not ops_raw:
        raise ValueError("一条画图指令都没有")
    ops: list[dict] = []
    for o in ops_raw[:FURN_MAX_OPS]:
        if not isinstance(o, dict):
            continue
        kind = str(o.get("op") or "").strip()
        if kind not in FURN_OP_KINDS:
            continue
        try:
            if kind == "mirror":
                ops.append({"op": "mirror"})
                continue
            c = int(o.get("c", 0) or 0)
            if c < 0 or c >= len(palette):
                c = 0
            if kind == "line":
                ops.append({
                    "op": "line",
                    "x": int(o.get("x", 0) or 0), "y": int(o.get("y", 0) or 0),
                    "x2": int(o.get("x2", 0) or 0), "y2": int(o.get("y2", 0) or 0),
                    "c": c,
                })
            else:
                ops.append({
                    "op": kind,
                    "x": int(o.get("x", 0) or 0), "y": int(o.get("y", 0) or 0),
                    "w": max(1, int(o.get("w", 1) or 1)),
                    "h": max(1, int(o.get("h", 1) or 1)),
                    "c": c,
                })
        except Exception:
            continue
    if not ops:
        raise ValueError("没有一条能用的画图指令")
    return {"name": name, "size": [w, h], "palette": palette, "ops": ops}


def furn_render_png(spec: dict) -> tuple[bytes, list[int]]:
    """按施工图施工：平涂图元（无抗锯齿）→ 剪影自动描 1px 深墨边 → 透明 PNG。
    返回 (png 字节, [宽, 高])；画出来是空的算失败。"""
    from io import BytesIO
    from PIL import Image, ImageDraw

    w, h = int(spec["size"][0]), int(spec["size"][1])
    pal = spec["palette"]
    colors = [tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) + (255,) for c in pal]
    # 四周各留 1px 给描边
    canvas = Image.new("RGBA", (w + 2, h + 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    off = 1

    for op in spec["ops"]:
        kind = op["op"]
        if kind == "mirror":
            # 镜像是「补一份」，不是搬家：左半画完镜像出右半，原图保留
            flipped = canvas.transpose(Image.FLIP_LEFT_RIGHT)
            canvas.paste(flipped, (0, 0), flipped.getchannel("A"))
            draw = ImageDraw.Draw(canvas)
            continue
        col = colors[op["c"]]
        x = int(op["x"]) + off
        y = int(op["y"]) + off
        if kind == "rect":
            draw.rectangle([x, y, x + int(op["w"]) - 1, y + int(op["h"]) - 1], fill=col)
        elif kind == "ellipse":
            draw.ellipse([x, y, x + int(op["w"]) - 1, y + int(op["h"]) - 1], fill=col)
        elif kind == "tri":
            draw.polygon([
                (x + int(op["w"]) // 2, y),
                (x, y + int(op["h"]) - 1),
                (x + int(op["w"]) - 1, y + int(op["h"]) - 1),
            ], fill=col)
        elif kind == "line":
            draw.line([x, y, int(op["x2"]) + off, int(op["y2"]) + off], fill=col, width=1)

    # 自动描边：透明像素只要四邻有一个不透明，就填深墨
    furn_apply_outline(canvas)
    W, H = canvas.size

    buf = BytesIO()
    canvas.save(buf, "PNG", optimize=True)
    return buf.getvalue(), [W, H]


def furn_apply_outline(canvas) -> int:
    """在RGBA画布上给剪影自动描1px深墨边（就地改）。返回描的像素数。"""
    alpha = canvas.getchannel("A")
    solid = alpha.load()
    W, H = canvas.size
    edge: list[tuple[int, int]] = []
    for yy in range(H):
        for xx in range(W):
            if solid[xx, yy]:
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = xx + dx, yy + dy
                if 0 <= nx < W and 0 <= ny < H and solid[nx, ny]:
                    edge.append((xx, yy))
                    break
    if not edge:
        raise ValueError("画出来是空的")
    for xx, yy in edge:
        canvas.putpixel((xx, yy), FURN_OUTLINE)
    return len(edge)


# --------- 精绘模式：AI 生图 → 本地抠图量化，产出同一套像素家具规格 ---------

FURN_IMG_PROBE = 192                     # 先降到这个分辨率再抠图（像素安全 + 快）
FURN_IMG_BG_TOL2 = 78 * 78               # 背景色键的容差（色距平方）
FURN_IMG_MIN_BG_RATIO = 0.30             # 四边里至少三成是同一个底色才敢抠
FURN_IMG_RAW_CAP = 16 * 1024 * 1024      # 回图字节上限


def furn_img_prompt(user: str) -> str:
    """把用户那句中文包成像素素材提示词：主体完全跟着用户那句走，这里只加像素与抠图约束。"""
    obj = " ".join(str(user or "").split())[:60] or "一个像素小物件"
    return (
        f"{obj}，pixel art 像素画素材 / 游戏 sprite：严格按上面的描述画这个主体本身；"
        "单个物件居中，纯亮品红色纯色背景（#FF00FF），"
        "背景平涂无渐变、无地板、无投影、无反光、无光晕，像素颗粒分明、硬边缘、"
        "不抗锯齿不抖动，物件带深色描边，平涂上色，全图不超过 8 种颜色，"
        "无文字、无水印、无签名"
    )


def furn_prep_image(raw: bytes) -> tuple[bytes, list[int]]:
    """AI 回图 → 能进房间的家具 PNG。

    步骤：降采样到 192 → 四边洪泛抠底色（只抠和边缘连通的近底色像素，
    物件内部的同色块不误伤）→ 裁到包围盒 → NEAREST 缩到 ≤48 → 二值 alpha
    → 调色板量化 ≤8 色 → 补边距 → 复用自动描边。失败抛 ValueError。"""
    from collections import Counter, deque
    from io import BytesIO
    from PIL import Image

    if not raw or len(raw) > FURN_IMG_RAW_CAP:
        raise ValueError("回图太大或为空")
    try:
        img = Image.open(BytesIO(raw))
        img.load()
    except Exception as e:
        raise ValueError(f"回图不是有效图片（{type(e).__name__}）") from e
    img = img.convert("RGBA")
    w, h = img.size
    if max(w, h) > FURN_IMG_PROBE:
        f = float(FURN_IMG_PROBE) / max(w, h)
        img = img.resize((max(2, int(w * f)), max(2, int(h * f))), Image.LANCZOS)
        w, h = img.size
    px = img.load()

    # 底色 = 四边像素的多数色；若边缘本来就大片透明，说明是抠好的图，跳过键控
    step = max(1, min(w, h) // 48)
    ring: list[tuple[int, int, int]] = []
    n_ring = 0
    n_hole = 0
    for xx in range(0, w, step):
        for yy in (0, h - 1):
            ring.append(px[xx, yy][:3])
            n_ring += 1
            n_hole += 1 if px[xx, yy][3] < 128 else 0
    for yy in range(0, h, step):
        for xx in (0, w - 1):
            ring.append(px[xx, yy][:3])
            n_ring += 1
            n_hole += 1 if px[xx, yy][3] < 128 else 0
    alpha = img.getchannel("A")
    if n_hole < max(4, int(n_ring * 0.05)):
        bg = Counter(ring).most_common(1)[0][0]
        bg_hits = sum(1 for c in ring if furn_px_near(c, bg, FURN_IMG_BG_TOL2)) / max(1, len(ring))
        if bg_hits < FURN_IMG_MIN_BG_RATIO:
            raise ValueError("背景不是纯色，抠不出来，换一张")

        # 从四边洪泛：近底色且与边界连通 → 透明
        solid = alpha.load()
        seen = bytearray(w * h)
        dq = deque()
        for xx in range(w):
            for yy in (0, h - 1):
                i = yy * w + xx
                if not seen[i] and solid[xx, yy] and furn_px_near(px[xx, yy][:3], bg, FURN_IMG_BG_TOL2):
                    seen[i] = 1
                    dq.append((xx, yy))
        for yy in range(h):
            for xx in (0, w - 1):
                i = yy * w + xx
                if not seen[i] and solid[xx, yy] and furn_px_near(px[xx, yy][:3], bg, FURN_IMG_BG_TOL2):
                    seen[i] = 1
                    dq.append((xx, yy))
        while dq:
            x0, y0 = dq.popleft()
            solid[x0, y0] = 0
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x0 + dx, y0 + dy
                if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx] and solid[nx, ny] \
                        and furn_px_near(px[nx, ny][:3], bg, FURN_IMG_BG_TOL2):
                    seen[ny * w + nx] = 1
                    dq.append((nx, ny))
        img.putalpha(alpha)
        # 抠完再看四边：还有一半以上不透 = 背景太花（棋盘/渐变/纹理），别硬产出垃圾
        n_ring2 = 0
        n_left = 0
        for xx in range(0, w, step):
            for yy in (0, h - 1):
                n_ring2 += 1
                n_left += 1 if solid[xx, yy] else 0
        for yy in range(0, h, step):
            for xx in (0, w - 1):
                n_ring2 += 1
                n_left += 1 if solid[xx, yy] else 0
        if n_ring2 and n_left / n_ring2 > 0.45:
            raise ValueError("背景太花，抠不出来，换一张")

    box = alpha.getbbox()
    if not box:
        raise ValueError("整张图都是背景，什么也没画出来")
    img = img.crop(box)
    w, h = img.size
    if max(w, h) > FURN_MAX_CANVAS:
        f = FURN_MAX_CANVAS / float(max(w, h))
        img = img.resize((max(4, int(round(w * f))), max(4, int(round(h * f)))), Image.NEAREST)
        w, h = img.size

    # 二值 alpha + ≤8 色量化（透明处填物件均色，免得底色混进调色板）
    alpha = img.getchannel("A").point(lambda v: 255 if v >= 128 else 0)
    data = list(img.convert("RGB").getdata())
    amask = list(alpha.getdata())
    op = [c for c, a in zip(data, amask) if a]
    if not op:
        raise ValueError("抠完没剩下什么")
    avg = tuple(sum(c[i] for c in op) // len(op) for i in range(3))
    rgb = Image.new("RGB", (w, h), avg)
    rgb.paste(img.convert("RGB"), (0, 0), alpha)
    mapped = rgb.quantize(colors=FURN_MAX_PALETTE, method=Image.MEDIANCUT,
                          dither=Image.NONE).convert("RGB")
    canvas = Image.new("RGBA", (w + 2, h + 2), (0, 0, 0, 0))
    canvas.paste(mapped, (1, 1), alpha)
    furn_apply_outline(canvas)

    buf = BytesIO()
    canvas.save(buf, "PNG", optimize=True)
    return buf.getvalue(), [w + 2, h + 2]


def furn_px_near(c1, c2, tol2: int) -> bool:
    return (c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2 <= tol2


# --------------------------------------------------------------------------- #
# 插件
# --------------------------------------------------------------------------- #


@register(
    PLUGIN_NAME,
    "zexuan604",
    "鱼的窝：喂她吃点东西、攒进度、解锁房间里的小家具",
    VERSION,
)
class FishHome(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.conf: dict = config if isinstance(config, dict) else {}
        self._task: asyncio.Task | None = None

        self.catalog, self.daily_wishes, self.mode_lines = load_gift_file()
        self.gift_map = {g["id"]: g for g in self.catalog}
        self.foods, self.reverse_gifts = self._load_foods()
        self.furniture = load_objects()
        self.furniture_map = {str(o["id"]): o for o in self.furniture}
        self.ach_cats = self._load_achievements()
        self.ach_items = [it for cat in self.ach_cats for it in (cat.get("items") or [])]
        self.ach_map = {str(it.get("id")): it for it in self.ach_items}
        self.ach_cat_of = {
            str(it.get("id")): str(cat.get("title") or cat.get("key") or "")
            for cat in self.ach_cats for it in (cat.get("items") or [])
        }
        self.late_events = self._load_json_list(LATE_NIGHTS_FILE, "items")
        self.card_tpl = self._load_json_dict(CARDS_FILE)
        _hg_raw = self._load_json_dict(HER_GIFTS_FILE)
        self.her_gifts = [x for x in (_hg_raw.get("gifts") or []) if isinstance(x, dict) and x.get("id")]
        self.signal_gifts = [x for x in (_hg_raw.get("signal_gifts") or []) if isinstance(x, dict) and x.get("id")]
        self.her_gift_map = {str(g.get("id")): g for g in self.her_gifts + self.signal_gifts}

        try:
            self.data_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as e:
            logger.warning("[鱼的窝] 获取数据目录失败，退回插件内目录: %s", e)
            self.data_dir = os.path.join(PLUGIN_DIR, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        # 家具工坊：自定义家具的库和图片都在 plugin_data，插件整包升级不丢
        self.furn_lib_path = os.path.join(self.data_dir, "custom_furniture.json")
        self.furn_img_dir = os.path.join(self.data_dir, "furniture_imgs")
        self.furn_lib = self._furn_lib_load()
        self.creative = os.path.exists(self._creative_flag_path())
        self._post_feed_task = None
        self._last_umo = str(self._cfg("owner_session", "") or "")
        self.state_path = os.path.join(self.data_dir, "state.json")
        self.state = self._load_state()
        # 从"好感度每天清零"的老版本升上来：先给一个正常值，别一进门她就在赌气
        if not self.state.get("favor_seeded"):
            self.state["favor_seeded"] = True
            if float(self.state.get("favor", 0) or 0) < 10:
                self.state["favor"] = 30.0
            self.state["favor_tier"] = "normal"
            self._save_state()

        logger.info(
            "[鱼的窝] v%s 已载入，礼物=%d，心愿=%.1f，喂完主动说话=%s(%s)，数据目录=%s",
            VERSION,
            len(self.catalog),
            float(self.state.get("wish", 0) or 0),
            "开" if bool(self._cfg("post_feed.enabled", True)) else "关",
            self._post_feed_persona(),
            self.data_dir,
        )
        # 作息心跳：不依赖面板开着，账本自己走（离线也"刚才在干嘛"有真数据）
        self._ambient_task = None
        try:
            self._ambient_task = asyncio.create_task(self._ambient_heartbeat())
        except RuntimeError:
            pass

    async def _ambient_heartbeat(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                now = now_ts()
                # 主动推进：先掷骰（面板不开账本也走），再取快照
                self._ambient_idle_roll(now)
                self._ambient_now(now)
                self._check_decay(now)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("[鱼的窝] 作息心跳异常: %s", e)

    # ---------------- 配置 / 状态 ----------------

    def _cfg(self, path: str, default: Any = None) -> Any:
        cur: Any = self.conf
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur if cur is not None else default

    def _dbg(self, msg: str, *args) -> None:
        if self._cfg("debug", False):
            logger.info("[鱼的窝] " + msg, *args)

    def _default_state(self) -> dict:
        return {
            "version": STATE_VERSION,
            "initialized": False,
            "created_at": 0.0,
            "chat": {"total": 0, "days": {}, "last_ts": 0.0},
            "gifts": {},
            "unlocked": {},
            "revealed": {},
            "wish": 0.0,
            "wish_peak": 0.0,
            "wish_day": "",
            "wish_gain_day": 0.0,
            "wish_events": [],
            "daily_wish": {"day": "", "id": "", "done": False, "done_ts": 0.0, "hinted": False},
            "daily_wishes_done": 0,
            "intimacy_peak": 0.0,
            "expectation_peak": 0.0,
            "local_affection": 0.0,
            "meters": {"intimacy": 0.0, "joy": 0.0, "attachment": 0.0, "soothe": 0.0},
            "meter_levels": {"intimacy": 0, "joy": 0, "attachment": 0, "soothe": 0},
            "meter_peak": {"intimacy": 0.0, "joy": 0.0, "attachment": 0.0, "soothe": 0.0},
            "fed_total": 0,
            "heart_backlog": {},
            "meter_model": 2,
            "furniture": {},
            "furniture_tap": {"day": "", "count": 0},
            "send": {"day": "", "count": 0, "last_ts": 0.0},
            "history": [],
            "pending": [],
            "last_reaction": {},
            "level_seen": 0,
            "blame_seen": 0.0,
            "silence_blocks": 0,
            "punished_proactive_ts": 0.0,
            "timely_ts": 0.0,
            "timely_day": "",
            "timely_count": 0,
            "first_day": "",
            "thought_day": "",
            "thought_count": 0,
            "thought_sigs": [],
            "starter_done": False,
            "daily_gift_day": "",
            "achievements": {},
            "ach_pop": [],
            "favor_peak": 0.0,
            "fed_kinds": [],
            "spent_day": "",
            "spent_today": 0,
            "pet_total": 0,
            "tap_total": 0,
            "late_nights": 0,
            "late_day": "",
            "blame_days": 0,
            "negative_days": 0,
            "negative_flag": False,
            "reverse_total": 0,
            "reverse_good": 0,
            "her_gifts": {"given": {}, "claimed": {}, "missed": [], "pending": [], "count": 0},
            "activity": {},
            "ambient": {"phone_since": 0.0, "phone_until": 0.0},
            "signals": [],
            "letter_last_ts": 0.0,
            "letter_week_key": "",
            "letter_week_count": 0,
            "pending_letter_beats": [],
            "letters": [],
            "ai_achv": [],
            "ai_achv_last_gen": 0.0,
            "diy_day": "",
            "diy_log": [],
        "taste": {},
            "steps": {"day": "", "used": 0, "last_ts": 0.0},
            "panel_seen": 0,
            "events": [],
            "event_days": {},
            "day_index": 1,
            "day_started": "",
            "daily_cards": [],
            "last_settle": 0.0,
            "last_settle_day": "",
            "unlocked_foods": [],
            "late_events_used": [],
            "late_event_today": None,
            "late_event_sent": "",
            "favor_earned_today": 0.0,
            "favor_spent_today": 0.0,
            "favor_broke_through": False,
            "favor_day_mood": "",
        }

    def _load_state(self) -> dict:
        base = self._default_state()
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    base = deep_merge(base, raw)
                    base["version"] = STATE_VERSION
                    # 旧版把「鱼的心」的亲密当成本地面板值；换成本地积累槽后从 0 重新攒。
                    if int(raw.get("meter_model", 0) or 0) < 2:
                        base["intimacy_peak"] = 0.0
                        base["meters"] = {
                            "intimacy": 0.0, "joy": 0.0, "attachment": 0.0, "soothe": 0.0,
                        }
                        base["meter_levels"] = {
                            "intimacy": 0, "joy": 0, "attachment": 0, "soothe": 0,
                        }
                        base["meter_peak"] = {
                            "intimacy": 0.0, "joy": 0.0, "attachment": 0.0, "soothe": 0.0,
                        }
                        base["fed_total"] = 0
                        base["heart_backlog"] = {}
                        base["meter_model"] = 2
        except Exception as e:
            logger.warning("[鱼的窝] 读状态失败，已备份并重建: %s", e)
            try:
                if os.path.exists(self.state_path):
                    os.replace(self.state_path, self.state_path + f".bak-{int(now_ts())}")
            except Exception:
                pass
        # 类型兜底
        for key, typ in (
            ("chat", dict), ("gifts", dict), ("unlocked", dict), ("revealed", dict),
            ("wish_events", list), ("history", list), ("pending", list),
            ("daily_wish", dict), ("send", dict), ("thought_sigs", list),
            ("meters", dict), ("meter_levels", dict), ("meter_peak", dict), ("heart_backlog", dict),
            ("furniture", dict), ("furniture_tap", dict),
            ("achievements", dict), ("ach_pop", list), ("fed_kinds", list),
            ("events", list), ("event_days", dict), ("daily_cards", list),
            ("unlocked_foods", list), ("late_events_used", list),
        ):
            if not isinstance(base.get(key), typ):
                base[key] = self._default_state()[key]
        return base

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as e:
            logger.warning("[鱼的窝] 保存状态失败: %s", e)

    # ---------------- 身份 ----------------

    def _owner_umo(self) -> str:
        return str(self._cfg("owner_session", "") or "").strip()

    # ---------------- 离线补看：bot 不在线时你发的消息 ----------------

    def _cq_client(self):
        """拿到 aiocqhttp（NapCat）的客户端，能调 OneBot 扩展接口。"""
        try:
            p = self.context.get_platform("aiocqhttp")
            if p is None:
                return None
            if hasattr(p, "get_client"):
                return p.get_client()
            return getattr(p, "bot", None)
        except Exception:
            return None

    def _owner_qq(self) -> str:
        umo = self._owner_umo() or str(getattr(self, "_last_umo", "") or "")
        if not umo:
            return ""
        tail = umo.rsplit(":", 1)[-1].strip()
        return tail if tail.isdigit() else ""

    async def _offline_fetch(self) -> None:
        """她不在线的时候你说的话，上线后补看一遍（走 NapCat 的历史消息接口）。"""
        if not bool(self._cfg("offline.enabled", True)):
            return
        if getattr(self, "_offline_busy", False):
            return
        qq = self._owner_qq()
        bot = self._cq_client()
        if not qq or bot is None:
            return
        now = now_ts()
        last = float((self.state.get("chat") or {}).get("last_ts", 0) or 0)
        if not last or now - last < 600:       # 刚聊过，不用翻
            return
        if now - float(self.state.get("offline_last_fetch", 0) or 0) < 300:
            return
        self._offline_busy = True
        try:
            try:
                count = max(5, min(30, int(self._cfg("offline.max_msgs", 12) or 12)))
            except Exception:
                count = 12
            ret = await bot.call_action("get_friend_msg_history", user_id=int(qq), count=count)
            data = (ret or {}).get("data") if isinstance(ret, dict) else None
            msgs = (data or ret or {}).get("messages") or []
            rows = []
            for msg in msgs:
                if not isinstance(msg, dict):
                    continue
                t = float(msg.get("time", 0) or 0)
                if t <= last:
                    continue
                sender = msg.get("sender") or {}
                if str(sender.get("user_id") or "") != qq:
                    continue
                segs = msg.get("message") or []
                text = "".join(
                    str(s.get("data", {}).get("text") or "")
                    for s in segs if isinstance(s, dict) and s.get("type") == "text"
                ).strip()
                if not text:
                    continue
                rows.append({"t": t, "text": text[:100]})
            rows.sort(key=lambda r: r["t"])
            self.state["offline_last_fetch"] = now
            if rows:
                self.state["offline"] = rows[-count:]
                self._life(f"你不在的时候他说了 {len(rows)} 句话，她翻到了。", "offline")
                logger.info("[鱼的窝] 补看到 %d 条离线消息", len(rows))
            self._save_state()
        except Exception as e:
            logger.debug("[鱼的窝] 离线补看失败（不影响聊天）: %s", e)
        finally:
            self._offline_busy = False

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        try:
            obj = getattr(event, "message_obj", None)
            sender = getattr(obj, "sender", None) if obj is not None else None
            if sender is not None:
                for attr in ("user_id", "id"):
                    v = getattr(sender, attr, None)
                    if v:
                        return str(v).strip()
            getter = getattr(event, "get_sender_id", None)
            if callable(getter):
                v = getter()
                if v:
                    return str(v).strip()
        except Exception:
            pass
        return ""

    def _remember_umo(self, event: AstrMessageEvent) -> None:
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if umo:
                self._last_umo = umo
        except Exception:
            pass

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        owner = self._owner_umo()
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        sid = self._sender_id(event)
        if owner:
            if umo == owner:
                # 私聊会话本身是主人的，但发送者 ID 能拿到时必须也对上，
                # 防止同会话里被塞进别人的消息。
                tail = owner.rsplit(":", 1)[-1].strip()
                if not sid or not tail:
                    return True
                return sid == tail
            tail = owner.rsplit(":", 1)[-1].strip()
            if tail and sid and tail == sid:
                return True
            return False
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    @staticmethod
    def _is_command(text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return True
        if t.startswith("/") or t.startswith("／"):
            return True
        first = re.split(r"\s+", t, maxsplit=1)[0]
        return first in COMMAND_WORDS

    # ---------------- 鱼的心桥接 ----------------

    def _heart(self):
        name = "astrbot_plugin_fish_heart"
        if not name:
            return None
        try:
            getter = getattr(self.context, "get_registered_star", None)
            if not callable(getter):
                return None
            meta = getter(name)
            heart = getattr(meta, "star_cls", None) if meta is not None else None
            if heart is None:
                return None
            st = getattr(heart, "state", None)
            if isinstance(st, dict) and isinstance(st.get("drives"), dict):
                return heart
        except Exception as e:
            self._dbg("拿鱼的心失败: %s", e)
        return None

    @staticmethod
    def _heart_func(heart, name: str):
        """取「鱼的心」模块里的判定函数，心情/模式以它为准，避免两边各算一套。"""
        try:
            mod = sys.modules.get(type(heart).__module__)
            fn = getattr(mod, name, None)
            return fn if callable(fn) else None
        except Exception:
            return None

    def _bili_stats(self) -> dict:
        """从 B 站插件的 watch_log.json 读她看过多少视频（60 秒缓存）。"""
        now = time.time()
        cache = getattr(self, "_bili_cache", None)
        if cache and now - cache[0] < 60:
            return dict(cache[1])
        out = {"bili_watched": 0, "bili_today": 0, "bili_distinct": 0, "bili_bangumi": 0}
        try:
            base = str(StarTools.get_data_dir(BILI_PLUGIN_NAME))
        except Exception:
            base = ""
        if base:
            def _load(name):
                path = os.path.join(base, name)
                if not os.path.exists(path):
                    return []
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    return data if isinstance(data, list) else []
                except Exception:
                    return []

            log = [x for x in _load("watch_log.json") if isinstance(x, dict)]
            today = datetime.now().strftime("%Y-%m-%d")
            out["bili_watched"] = len(log)
            out["bili_today"] = len([x for x in log if str(x.get("time") or "").startswith(today)])
            out["bili_distinct"] = len({str(x.get("bvid") or "") for x in log if x.get("bvid")})
            out["bili_bangumi"] = len([x for x in _load("bangumi_watch_log.json") if isinstance(x, dict)])
        self._bili_cache = (now, dict(out))
        return out

    def _heart_snapshot(self) -> dict:
        heart = self._heart()
        if heart is not None:
            st = heart.state
            d = st.get("drives") or {}
            blame = float(st.get("blame_pressure", 0) or 0)
            fn_mode = self._heart_func(heart, "mode_of")
            fn_mood = self._heart_func(heart, "mood_label")
            mode = str(st.get("mode") or (fn_mode(blame) if fn_mode else mode_of(blame)))
            mood = str(fn_mood(d) if fn_mood else mood_label(d))
            thoughts = st.get("thoughts") or []
            all_thoughts = [t for t in thoughts if isinstance(t, dict)]
            pending_thoughts = [t for t in all_thoughts if bool(t.get("pending", True))]
            satisfied = [
                f"{t.get('kind')}|{t.get('mood')}"
                for t in all_thoughts
                if (not t.get("pending", True)) and t.get("mood")
            ]
            return {
                "source": "heart",
                "intimacy": float(d.get("intimacy", 0) or 0),
                "attachment": float(d.get("attachment", 0) or 0),
                "joy": float(d.get("joy", 0) or 0),
                "stress": float(d.get("stress", 0) or 0),
                "curiosity": float(d.get("curiosity", 0) or 0),
                "expectation": float(st.get("expectation", 0) or 0),
                "blame": blame,
                "mode": mode,
                "mood": mood,
                "drives": {str(k): float(v or 0) for k, v in d.items()},
                "thoughts": pending_thoughts,
                "satisfied_thoughts": satisfied,
                "awaiting_reply": bool(st.get("awaiting_reply")),
                "last_proactive": float(st.get("last_proactive", 0) or 0),
            }
        local = float(self.state.get("local_affection", 0) or 0)
        return {
            "source": "local",
            "intimacy": local,
            "attachment": 0.0,
            "joy": 0.0,
            "stress": 0.0,
            "curiosity": 0.0,
            "expectation": 0.0,
            "blame": 0.0,
            "mode": "calm",
            "mood": "平常",
            "drives": {},
            "thoughts": [],
            "satisfied_thoughts": [],
            "awaiting_reply": False,
            "last_proactive": 0.0,
        }

    RADAR_DIMS = (
        ("intimacy", "亲密"),
        ("joy", "喜悦"),
        ("attachment", "依恋"),
        ("curiosity", "好奇"),
        ("reflection", "反刍"),
        ("duty", "责任"),
        ("social", "社交"),
        ("fatigue", "疲惫"),
        ("stress", "压力"),
    )

    @classmethod
    def _radar_payload(cls, snap: dict) -> list[dict]:
        """九维：直接取「鱼的心」的 drives，缺的维度补 0。"""
        drives = snap.get("drives") or {}
        return [
            {
                "key": key,
                "label": label,
                "value": round(clamp(float(drives.get(key, 0) or 0)), 1),
            }
            for key, label in cls.RADAR_DIMS
        ]

    @staticmethod
    def _mood_payload(snap: dict) -> list[dict]:
        """把「鱼的心」的 blame 拆成四种情绪的强度，面板上用这个替代原来的四条槽。"""
        blame = clamp(float(snap.get("blame", 0) or 0))
        active = str(snap.get("mode") or "calm")
        rows = [
            ("calm", "平静", clamp(100 - blame)),
            ("wronged", "委屈", clamp((blame - 25) / 30 * 100)),
            ("afraid", "害怕", clamp((blame - 55) / 25 * 100)),
            ("shut", "回避", clamp((blame - 80) / 20 * 100)),
        ]
        return [
            {
                "key": k,
                "label": label,
                "value": round(v, 1),
                "active": (k == active),
            }
            for k, label, v in rows
        ]

    def _update_peaks(self, snap: dict | None = None) -> None:
        meters = self.state.get("meters") or {}
        cur = clamp(float(meters.get("intimacy", 0) or 0))
        peaks = self.state.setdefault("meter_peak", {})
        for key in METER_KEYS:
            val = clamp(float(meters.get(key, 0) or 0))
            peaks[key] = max(float(peaks.get(key, 0) or 0), val)
        self.state["intimacy_peak"] = max(
            float(self.state.get("intimacy_peak", 0) or 0),
            cur,
            clamp(float(peaks.get("intimacy", 0) or 0)),
        )
        if snap is None:
            snap = self._heart_snapshot()
        self.state["expectation_peak"] = max(
            float(self.state.get("expectation_peak", 0) or 0), float(snap.get("expectation", 0) or 0)
        )

    # ---------------- 本地积累槽 -> 鱼的心 ----------------

    def _feed_heart(self, stat: str, amount: float) -> None:
        """本地槽攒满一轮后，把奖励写进「鱼的心」运行中的 state（不顺带改它的代码/存档）。"""
        if abs(float(amount)) < 1e-9:
            return
        heart = self._heart()
        if heart is None:
            backlog = self.state.setdefault("heart_backlog", {})
            backlog[stat] = round(float(backlog.get(stat, 0) or 0) + float(amount), 1)
            self._dbg("鱼的心不在，先记账 %s %+.1f", stat, float(amount))
            return
        try:
            st = heart.state
            drives = st.setdefault("drives", {})
            before = float(drives.get(stat, 0) or 0)
            drives[stat] = clamp(before + float(amount))
            rows = list(st.get("recent_events") or [])
            rows.append({
                "n": "gift_feed",
                "cn": "收到礼物（攒满一轮）",
                "ts": now_ts(),
                "d": {stat: round(float(drives[stat]) - before, 1)},
            })
            st["recent_events"] = rows[-8:]
            self.state["heart_backlog"] = {
                k: v for k, v in (self.state.get("heart_backlog") or {}).items() if k != stat
            }
            save = getattr(heart, "_save_state", None)
            if callable(save):
                try:
                    save()
                except Exception as e:
                    logger.warning("[鱼的窝] 鱼的心存盘失败: %s", e)
        except Exception as e:
            logger.warning("[鱼的窝] 反哺鱼的心失败: %s", e)

    def _drain_heart_backlog(self) -> None:
        backlog = dict(self.state.get("heart_backlog") or {})
        if not backlog or self._heart() is None:
            return
        for stat, amount in backlog.items():
            self._feed_heart(stat, float(amount or 0))

    def _add_meter(self, key: str, delta: float) -> dict:
        """往一个本地槽里加值；满了就扣掉 100、等级 +1、反哺鱼的心 +10，溢出保留。"""
        meters = self.state.setdefault("meters", {})
        levels = self.state.setdefault("meter_levels", {})
        peaks = self.state.setdefault("meter_peak", {})
        before = clamp(float(meters.get(key, 0) or 0))
        delta = float(delta or 0)
        if abs(delta) < 1e-9:
            return {"before": round(before, 1), "after": round(before, 1), "delta": 0.0, "levels": 0}
        after = before + delta
        gained = 0
        if delta > 0:
            while after >= METER_MAX - 1e-9:
                after -= METER_MAX
                gained += 1
            peaks[key] = max(float(peaks.get(key, 0) or 0), min(METER_MAX, before + delta))
        else:
            after = max(0.0, after)
        meters[key] = round(after, 3)
        if gained:
            levels[key] = int(levels.get(key, 0) or 0) + gained
            self.state["fed_total"] = int(self.state.get("fed_total", 0) or 0) + gained
            target, sign = METER_FEED[key]
            self._feed_heart(target, sign * FEED_STEP * gained)
            self._dbg("%s 攒满 %d 轮，反哺鱼的心 %s %+.1f", METER_LABELS[key], gained, target, sign * FEED_STEP * gained)
        return {
            "before": round(before, 1),
            "after": round(meters[key], 1),
            "delta": round(delta, 1),
            "levels": gained,
        }

    def _apply_gift(self, gift: dict) -> dict:
        """把礼物效果记进本地积累槽；攒满的槽已经在这一步反哺给「鱼的心」。"""
        eff = gift.get("effect") or {}
        scale = 1.0
        self._drain_heart_backlog()
        out: dict[str, dict] = {}
        for key in ("intimacy", "joy", "attachment"):
            out[key] = self._add_meter(key, float(eff.get(key, 0) or 0) * scale)
        # 礼物里的 stress 是负值（让她放松），记进「安抚」槽
        out["soothe"] = self._add_meter("soothe", -float(eff.get("stress", 0) or 0) * scale)
        self._update_peaks()
        return out

    # ---------------- 心愿（已砍掉，只保留空壳，别再被调用） ----------------

    def _wish_gain(self, delta: float, reason: str) -> float:
        return 0.0

    def _wish_gain_chat(self) -> None:
        return

    def _daily_wish_def(self) -> dict | None:
        return None

    def _pick_daily_wish(self, today: str) -> dict:
        return {"day": today, "id": "", "done": True, "done_ts": 0.0, "hinted": True}

    def _roll_day(self) -> None:
        today = today_str()
        dw = self.state.get("daily_wish") or {}
        if dw.get("day") == today:
            return
        prev_day = str(dw.get("day") or "")
        prev_id = str(dw.get("id") or "")
        if prev_day and prev_id and not dw.get("done"):
            prev_def = next((w for w in self.daily_wishes if w.get("id") == prev_id), None)
            text = str((prev_def or {}).get("text") or prev_id)
            miss = float(self._cfg("wish.daily_wish_miss", 6) or 0)
            if miss:
                self._wish_gain(-miss, f"昨天的心愿没等到：{text}")
            self._pending("missed_wish", f"她昨天的心愿「{text}」没等到，今天有点安静。")
        self.state["daily_wish"] = self._pick_daily_wish(today)
        self.state["wish_day"] = today
        self.state["wish_gain_day"] = 0.0
        self.state["timely_day"] = today
        self.state["timely_count"] = 0
        self.state["thought_day"] = today
        self.state["thought_count"] = 0
        self.state["blame_seen"] = 0.0
        # 最近 30 天
        days = self.state.setdefault("chat", {}).setdefault("days", {})
        cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        for d in [k for k in list(days.keys()) if k < cutoff]:
            days.pop(d, None)
        self._growth_roll_day(today)
        try:
            self._letters_check(now_ts())
        except Exception as e:
            logger.warning("[鱼的窝] 例行信件检查失败: %s", e)

    def _complete_daily_wish(self, reason: str = "") -> bool:
        return False

    def _check_daily_wish_text(self, text: str) -> None:
        return

    def _check_daily_wish_gift(self, gift_id: str) -> None:
        return


    # ---------------- 计数 / 衰减 ----------------

    def _chat_today(self) -> int:
        days = (self.state.get("chat") or {}).get("days") or {}
        return int(days.get(today_str(), 0) or 0)

    def _streak(self) -> int:
        days = (self.state.get("chat") or {}).get("days") or {}
        if not days:
            return 0
        d = datetime.now().date()
        if int(days.get(d.isoformat(), 0) or 0) <= 0:
            d -= timedelta(days=1)
        n = 0
        while int(days.get(d.isoformat(), 0) or 0) > 0:
            n += 1
            d -= timedelta(days=1)
        return n

    def _check_decay(self, now: float) -> None:
        # 心愿已经砍掉，这里只留「她主动开口你一直没回」的记录，别的都别再算了
        snap = self._heart_snapshot()
        last_p = float(snap.get("last_proactive", 0) or 0)
        if last_p > 0 and (now - last_p) >= 6 * 3600:
            if float(self.state.get("punished_proactive_ts", 0) or 0) != last_p:
                self.state["punished_proactive_ts"] = last_p

    def _check_timely_reply(self, now: float) -> None:
        snap = self._heart_snapshot()
        last_p = float(snap.get("last_proactive", 0) or 0)
        if last_p <= 0 or (now - last_p) > 1800:
            return
        if float(self.state.get("timely_ts", 0) or 0) == last_p:
            return
        if int(self.state.get("timely_count", 0) or 0) >= 3:
            return
        self.state["timely_ts"] = last_p
        self.state["timely_count"] = int(self.state.get("timely_count", 0) or 0) + 1

    def _check_thoughts(self) -> None:
        snap = self._heart_snapshot()
        sigs = list(self.state.get("thought_sigs") or [])
        new = [s for s in (snap.get("satisfied_thoughts") or []) if s not in sigs]
        if not new:
            return
        self.state["thought_sigs"] = (sigs + new)[-20:]
        if int(self.state.get("thought_count", 0) or 0) >= 2:
            return
        self._wish_gain(6, "你满足了她的小心愿")
        self.state["thought_count"] = int(self.state.get("thought_count", 0) or 0) + 1

    # ---------------- 条件引擎 ----------------

    def _stats(self) -> dict:
        snap = self._heart_snapshot()
        meters = self.state.get("meters") or {}
        levels = self.state.get("meter_levels") or {}
        gifts = self.state.get("gifts") or {}
        sent = sum(int((g or {}).get("sent", 0) or 0) for g in gifts.values())
        distinct = sum(1 for g in gifts.values() if int((g or {}).get("sent", 0) or 0) > 0)
        return {
            "intimacy": clamp(float(meters.get("intimacy", 0) or 0)),
            "intimacy_peak": float(self.state.get("intimacy_peak", 0) or 0),
            "joy": clamp(float(meters.get("joy", 0) or 0)),
            "attachment": clamp(float(meters.get("attachment", 0) or 0)),
            "soothe": clamp(float(meters.get("soothe", 0) or 0)),
            "level": int(levels.get("intimacy", 0) or 0),
            "level_joy": int(levels.get("joy", 0) or 0),
            "level_attachment": int(levels.get("attachment", 0) or 0),
            "level_soothe": int(levels.get("soothe", 0) or 0),
            "furniture_count": len([
                o for o in (self.state.get("furniture") or {})
                if str((self.furniture_map.get(str(o)) or {}).get("kind") or "furniture") == "furniture"
            ]),
            "fed_total": int(self.state.get("fed_total", 0) or 0),
            "expectation": float(snap.get("expectation", 0) or 0),
            "expectation_peak": float(self.state.get("expectation_peak", 0) or 0),
            **self._bili_stats(),
            "stress": float(snap.get("stress", 0) or 0),
            "blame": float(snap.get("blame", 0) or 0),
            "chat_total": int((self.state.get("chat") or {}).get("total", 0) or 0),
            "chat_today": self._chat_today(),
            "streak": self._streak(),
            "gifts_sent": sent,
            "gifts_distinct": distinct,
            "daily_wishes_done": int(self.state.get("daily_wishes_done", 0) or 0),
            "favor": round(self._favor(), 1),
            "favor_peak": float(self.state.get("favor_peak", 0) or 0),
            "foods_distinct": len(self.state.get("fed_kinds") or []),
            "spent_today": int(self.state.get("spent_today", 0) or 0),
            "pet_total": int(self.state.get("pet_total", 0) or 0),
            "tap_total": int(self.state.get("tap_total", 0) or 0),
            "late_nights": int(self.state.get("late_nights", 0) or 0),
            "blame_days": int(self.state.get("blame_days", 0) or 0),
            "negative_days": int(self.state.get("negative_days", 0) or 0),
            "reverse_total": int(self.state.get("reverse_total", 0) or 0),
            "reverse_good": int(self.state.get("reverse_good", 0) or 0),
            "ach_done_others": len([
                k for k in self._ach_done()
                if k in self.ach_map and str(self.ach_map[k].get("id")) != "s_all"
            ]),
            **{f"furn_{oid}": (1 if oid in (self.state.get("furniture") or {}) else 0)
               for oid in self.furniture_map},
            "day_index": int(self.state.get("day_index", 1) or 1),
            "gap_days": self._gap_days(),
            "her_gift_count": len(((self.state.get("her_gifts") or {}).get("claimed") or {})),
            "her_gift_ready": len([g for g in ((self.state.get("her_gifts") or {}).get("given") or {})
                                  if g not in ((self.state.get("her_gifts") or {}).get("claimed") or {})]),
            "panel_seen": int(self.state.get("panel_seen", 0) or 0),
            **self._signal_stats(),
            "hour": hour_now(),
            "mode": str(snap.get("mode") or "calm"),
        }

    def _gap_days(self) -> int:
        """距上次聊天过了几天（没有记录按 0 算，避免刚装就触发重逢礼）。"""
        last = float((self.state.get("chat") or {}).get("last_ts", 0) or 0)
        if not last:
            return 0
        return max(0, int((now_ts() - last) // 86400))

    @staticmethod
    def _cond_value(cond: dict) -> tuple[str, str, Any]:
        return (
            str(cond.get("stat") or ""),
            str(cond.get("op") or ">="),
            cond.get("value"),
        )

    def _eval_cond(self, cond: dict, stats: dict) -> bool:
        if not isinstance(cond, dict):
            return False
        if "all" in cond:
            return all(self._eval_cond(c, stats) for c in (cond.get("all") or []))
        if "any" in cond:
            return any(self._eval_cond(c, stats) for c in (cond.get("any") or []))
        if "not" in cond:
            return not self._eval_cond(cond.get("not") or {}, stats)
        stat, op, value = self._cond_value(cond)
        cur = stats.get(stat, 0)
        try:
            if op in (">=", "≥"):
                return float(cur) >= float(value)
            if op in ("<=", "≤"):
                return float(cur) <= float(value)
            if op == ">":
                return float(cur) > float(value)
            if op == "<":
                return float(cur) < float(value)
            if op in ("==", "="):
                return str(cur) == str(value)
            if op == "!=":
                return str(cur) != str(value)
            if op == "in":
                return cur in (value or [])
            if op == "not_in":
                return cur not in (value or [])
        except (TypeError, ValueError):
            return False
        return False

    def _eval_conditions(self, conditions: list, stats: dict) -> bool:
        return all(self._eval_cond(c, stats) for c in (conditions or []))

    def _leaf_views(self, cond: dict, stats: dict, prefix: str = "") -> list[dict]:
        if not isinstance(cond, dict):
            return []
        if "all" in cond or "any" in cond:
            join = "全部满足" if "all" in cond else "任一满足"
            out = []
            for c in cond.get("all") or cond.get("any") or []:
                out.extend(self._leaf_views(c, stats, f"{prefix}{join} · "))
            return out
        if "not" in cond:
            return self._leaf_views(cond.get("not") or {}, stats, prefix + "不满足 · ")
        stat, op, value = self._cond_value(cond)
        cur = stats.get(stat, 0)
        try:
            target = float(value)
            prog = min(1.0, max(0.0, float(cur) / target)) if target > 0 else 1.0
        except (TypeError, ValueError):
            target = value
            prog = 1.0 if cur == value else 0.0
        met = self._eval_cond(cond, stats)
        if met:
            prog = 1.0
        label = str(cond.get("label") or STAT_CN.get(stat) or stat)
        if op == "in":
            text = f"{prefix}{label} 属于 {value}"
        else:
            text = f"{prefix}{label} {op} {fmt_num(target) if isinstance(target, float) else target}"
        return [{
            "text": text,
            "stat": stat,
            "current": round(float(cur), 1) if isinstance(cur, (int, float)) else str(cur),
            "target": target,
            "met": met,
            "progress": round(prog, 4),
        }]

    def _condition_progress(self, cond: dict, stats: dict) -> float:
        if not isinstance(cond, dict):
            return 0.0
        if "all" in cond:
            parts = [self._condition_progress(c, stats) for c in (cond.get("all") or [])]
            return min(parts) if parts else 0.0
        if "any" in cond:
            parts = [self._condition_progress(c, stats) for c in (cond.get("any") or [])]
            return max(parts) if parts else 0.0
        if "not" in cond:
            return 0.0 if self._eval_cond(cond, stats) else 1.0
        leaf = self._leaf_views(cond, stats)
        return float(leaf[0]["progress"]) if leaf else 0.0

    # ---------------- 礼物 ----------------

    def _gift_key(self, gift: dict) -> str:
        return str(gift.get("id") or "")

    def _is_daily_gift(self, gift: dict) -> bool:
        return str((gift.get("unlock") or {}).get("kind") or "") == "daily"

    def _test_mode(self) -> bool:
        """创造模式 / 测试模式：所有礼物直接可用、库存无限、不占每日次数和冷却。"""
        return bool(self.creative) or bool(self._cfg("test_mode", False))

    # ---------------- 创造模式（实验档与正式档分开） ----------------

    def _creative_flag_path(self) -> str:
        return os.path.join(self.data_dir, "creative.flag")

    def _state_path_for(self, creative: bool) -> str:
        name = "state_creative.json" if creative else "state.json"
        return os.path.join(self.data_dir, name)

    def _switch_mode(self, creative: bool) -> None:
        """只切「无限」开关，存档始终是同一个 state.json。"""
        if bool(creative) == bool(self.creative):
            return
        self.creative = bool(creative)
        try:
            if creative:
                with open(self._creative_flag_path(), "w", encoding="utf-8") as f:
                    f.write("1")
            elif os.path.exists(self._creative_flag_path()):
                os.remove(self._creative_flag_path())
        except Exception as e:
            logger.warning("[鱼的窝] 写创造模式标记失败: %s", e)
        logger.info("[鱼的窝] 创造模式=%s（存档不变）", self.creative)

    def _gift_unlocked(self, gift: dict) -> bool:
        if self._test_mode():
            return True
        if self._is_daily_gift(gift):
            return True
        return self._gift_key(gift) in (self.state.get("unlocked") or {})

    def _gift_stock(self, gift: dict) -> dict:
        gid = self._gift_key(gift)
        row = (self.state.get("gifts") or {}).get(gid) or {}
        if self._test_mode():
            return {"count": 9999, "sent": int(row.get("sent", 0) or 0)}
        return {"count": int(row.get("count", 0) or 0), "sent": int(row.get("sent", 0) or 0)}

    def _grant_gift(self, gift: dict, source: str, repeat: bool = False) -> None:
        gid = self._gift_key(gift)
        rows = self.state.setdefault("gifts", {})
        row = rows.setdefault(gid, {"count": 0, "sent": 0, "last_sent_ts": 0.0})
        row["count"] = int(row.get("count", 0) or 0) + 1
        key = self._gift_key(gift)
        if not repeat:
            self.state.setdefault("unlocked", {})[key] = now_ts()
        name = str(gift.get("name") or gid)
        self.state.setdefault("gift_mood", {})
        self.state.setdefault("favor", 0.0)
        self.state.setdefault("favor_day", "")
        self.state.setdefault("favor_today", 0)
        self.state.setdefault("favor_log", [])
        self.state.setdefault("food_stock", {})
        self.state.setdefault("life_log", [])
        self.state.setdefault("reverse", {"day": "", "count": 0, "items": []})
        self.state.setdefault("history", []).append({
            "ts": now_ts(), "kind": "unlock", "gift": gid, "name": name, "source": source,
        })
        self.state["history"] = self.state["history"][-200:]
        self._pending("unlock", f"她翻出了一份「{name}」（{source}），去鱼的窝里给她吧。", gid)
        self._wish_gain(3, f"解锁礼物：{name}")
        self._dbg("解锁礼物 %s（%s）", gid, source)

    # ---------------- 小窝家具（成就解锁） ----------------

    def _furniture_payload(self, stats: dict | None = None) -> list[dict]:
        stats = stats if isinstance(stats, dict) else self._stats()
        owned = self.state.get("furniture") or {}
        layout = self.state.get("furniture_layout") or {}
        out: list[dict] = []
        for obj in self.furniture:
            oid = str(obj.get("id"))
            conds = obj.get("unlock") or []
            met = bool(conds) and self._eval_conditions(conds, stats)
            leafs = self._leaf_views({"all": conds}, stats) if conds else []
            progress = self._condition_progress({"all": conds}, stats) if conds else 1.0
            unlocked = oid in owned or met
            x = float(obj.get("x", 50) or 50)
            y = float(obj.get("y", 8) or 8)
            s = 1.0
            ov = layout.get(oid)
            oz = None
            if isinstance(ov, dict):        # 家具工坊里挪过的位置/大小/层级优先
                try:
                    x = float(ov.get("x", x) or x)
                    y = float(ov.get("y", y) or y)
                    s = float(ov.get("s", 1) or 1)
                    if isinstance(ov.get("z"), (int, float)):
                        oz = int(ov["z"])
                except Exception:
                    pass
            out.append({
                "id": oid,
                "kind": str(obj.get("kind") or "furniture"),
                "name": str(obj.get("name") or oid),
                "icon": str(obj.get("icon") or ""),
                "emoji": str(obj.get("emoji") or "🎁"),
                "x": x,
                "y": y,
                "s": s,
                "z": oz,
                "w": float(obj.get("w", 14) or 14),
                "native": [int(v) for v in (obj.get("native") or [])][:2],
                "unlocked": unlocked,
                "conditions": [] if unlocked else leafs,
                "progress": 1.0 if unlocked else round(float(progress), 4),
                "lines": [str(x) for x in (obj.get("lines") or []) if str(x).strip()],
            })
        # 家具工坊的自定义件：只有摆进房间的才渲染；纯装饰，不接台词
        for item in self.furn_lib:
            try:
                if item.get("x") is None or item.get("y") is None:
                    continue
                out.append({
                    "id": "c_" + str(item.get("id")),
                    "kind": "furniture",
                    "name": str(item.get("name") or "家具"),
                    "icon": self._furn_icon_b64(str(item.get("id"))),
                    "emoji": "🪑",
                    "x": float(item.get("x") or 50),
                    "y": float(item.get("y") or 8),
                    "s": float(item.get("s") or 1),
                    "z": int(item["z"]) if isinstance(item.get("z"), (int, float)) else None,
                    "w": 12.0,
                    "native": [int(v) for v in (item.get("native") or [24, 24])][:2],
                    "unlocked": True,
                    "conditions": [],
                    "progress": 1.0,
                    "lines": [],
                    "custom": True,
                })
            except Exception:
                continue
        return out

    def _check_furniture(self) -> list[str]:
        stats = self._stats()
        owned = self.state.setdefault("furniture", {})
        newly: list[str] = []
        for obj in self.furniture:
            oid = str(obj.get("id"))
            if oid in owned:
                continue
            conds = obj.get("unlock") or []
            if conds and self._eval_conditions(conds, stats):
                owned[oid] = now_ts()
                newly.append(oid)
                name = str(obj.get("name") or oid)
                self._pending("furniture", f"她往房间里添了「{name}」，去看看。")
                self.state.setdefault("history", []).append({
                    "ts": now_ts(), "kind": "furniture", "gift": oid, "name": name, "source": "达成成就",
                })
                self.state["history"] = self.state["history"][-200:]
                self._dbg("解锁家具 %s", oid)
        return newly

    def _check_gifts(self) -> list[str]:
        stats = self._stats()
        unlocked_now: list[str] = []
        before = set((self.state.get("unlocked") or {}).keys())
        for gift in self.catalog:
            gid = self._gift_key(gift)
            unlock = gift.get("unlock") or {}
            conds = unlock.get("conditions") or []
            if self._is_daily_gift(gift):
                if self._eval_conditions(conds, stats):
                    if str(self.state.get("daily_gift_day") or "") != today_str():
                        self.state["daily_gift_day"] = today_str()
                        self._grant_gift(gift, "今日心愿奖励", repeat=True)
                        unlocked_now.append(gid)
                continue
            if gid in (self.state.get("unlocked") or {}):
                continue
            if self._eval_conditions(conds, stats):
                self._grant_gift(gift, str(gift.get("source") or "里程碑"))
                unlocked_now.append(gid)
                continue
            # 隐藏款：条件快到了，露出一角线索
            if unlock.get("hidden") and gid not in (self.state.get("revealed") or {}):
                reveal = unlock.get("reveal") or []
                if reveal and self._eval_conditions(reveal, stats):
                    self.state.setdefault("revealed", {})[gid] = now_ts()
                    self._pending(
                        "clue",
                        f"她好像藏着一件东西，你隐约看见了：{unlock.get('hint') or '？'}",
                        gid,
                    )
        after = set((self.state.get("unlocked") or {}).keys())
        return unlocked_now or list(after - before)

    def _gift_send_block(self, gift: dict, stats: dict) -> str:
        if not bool(self._cfg("enabled", True)):
            return "插件已关闭"
        if self._test_mode():
            return ""
        if not self._gift_unlocked(gift):
            return "还没解锁"
        stock = self._gift_stock(gift)["count"]
        if stock <= 0:
            if self._is_daily_gift(gift):
                if self._chat_today() < self._daily_target(gift):
                    return f"今天还差 {max(0, self._daily_target(gift) - self._chat_today())} 句聊天"
                return "今天的份已经领过了"
            return "仓库里没有这份礼物"
        limit = 5
        send = self.state.get("send") or {}
        sent_today = int(send.get("count", 0) or 0) if str(send.get("day") or "") == today_str() else 0
        if limit > 0 and sent_today >= limit:
            return "今天送得够多了，明天再送"
        cooldown = 20
        last = float(send.get("last_ts", 0) or 0) if str(send.get("day") or "") == today_str() else 0.0
        remain = cooldown - (now_ts() - last) if last else 0
        if remain > 0:
            return f"再等 {int(remain) + 1} 秒"
        return ""

    def _daily_target(self, gift: dict) -> int:
        for c in (gift.get("unlock") or {}).get("conditions") or []:
            if c.get("stat") == "chat_today":
                try:
                    return int(c.get("value") or 0)
                except (TypeError, ValueError):
                    return 0
        return 0

    def _reaction_line(self, gift: dict, mode: str) -> str:
        lines = [str(x) for x in (gift.get("lines") or []) if str(x).strip()]
        if not lines:
            lines = ["……谢谢"]
        line = random.choice(lines)
        if mode in self.mode_lines and mode in ("wronged", "afraid", "shut"):
            prefix = str(self.mode_lines.get(mode) or "")
            if prefix and random.random() < 0.55:
                return f"{prefix}\n{line}"
        return line

    def _match_gift(self, query: str) -> dict | None:
        """按 id / 全名 / 别名匹配；完全匹配优先，其次包含关系。"""
        q = str(query or "").strip()
        if not q:
            return None
        if q in self.gift_map:
            return self.gift_map[q]
        best = None
        best_score = 0
        for gift in self.catalog:
            name = str(gift.get("name") or "")
            aliases = [str(a) for a in (gift.get("aliases") or []) if str(a).strip()]
            score = 0
            if q == name or q in aliases:
                score = 4
            elif q and q in name:
                score = 2
            elif name and name in q:
                score = 1
            else:
                for a in aliases:
                    if q and q in a:
                        score = max(score, 2)
                    elif a in q:
                        score = max(score, 1)
            if score > best_score:
                best_score = score
                best = gift
        return best

    def _send_gift(self, gift_id: str) -> dict:
        self._roll_day()
        gift = self._match_gift(gift_id)
        if not gift:
            return {"ok": False, "error": "没有这件礼物，发 /奖励 看看仓库里有什么。"}
        if self._band() == "shut" and not self._test_mode():
            self._record_reject(str(gift.get("name") or self._gift_key(gift)))
            return {"ok": False, "error": random.choice(self.BAND_REJECT_LINES)}
        stats = self._stats()
        block = self._gift_send_block(gift, stats)
        if block:
            return {"ok": False, "error": block}
        gid = self._gift_key(gift)
        heart_before = self._heart_snapshot()
        meters = self._apply_gift(gift)
        cur = meters.get("intimacy") or {}
        before = float(cur.get("before", 0) or 0)
        after = float(cur.get("after", 0) or 0)
        delta = float(cur.get("delta", 0) or 0)
        fed = [k for k in METER_KEYS if int((meters.get(k) or {}).get("levels", 0) or 0) > 0]
        rows = self.state.setdefault("gifts", {})
        row = rows.setdefault(gid, {"count": 0, "sent": 0, "last_sent_ts": 0.0})
        if not self._test_mode():
            row["count"] = max(0, int(row.get("count", 0) or 0) - 1)
        row["sent"] = int(row.get("sent", 0) or 0) + 1
        self._touch_gift_mood(gid)
        row["last_sent_ts"] = now_ts()
        send = self.state.setdefault("send", {"day": "", "count": 0, "last_ts": 0.0})
        if str(send.get("day") or "") != today_str():
            send["day"] = today_str()
            send["count"] = 0
        send["count"] = int(send.get("count", 0) or 0) + 1
        send["last_ts"] = now_ts()
        self.growth_record("gift", quote=str(gift.get("name") or gid), source="panel")
        self._wish_gain(float((gift.get("effect") or {}).get("wish", 0) or 0), f"送她{gift.get('name')}")
        self._check_daily_wish_gift(gid)
        if fed:
            levels = self.state.get("meter_levels") or {}
            bits = "、".join(
                f"{METER_LABELS[k]} {int(levels.get(k, 0) or 0)}层" for k in fed
            )
            self._pending(
                "feed",
                f"你送的「{gift.get('name')}」让她攒满了{'、'.join(METER_LABELS[k] for k in fed)}，"
                f"现在是 {bits}；她心里那点好感跟着暖了一点。",
            )
        self._update_peaks(self._heart_snapshot())
        newly = self._check_gifts()
        new_furniture = self._check_furniture()
        line = self._reaction_line(gift, str(heart_before.get("mode") or "calm"))
        self.state["last_reaction"] = {
            "gift": gid,
            "name": str(gift.get("name") or gid),
            "line": line,
            "delta": round(delta, 1),
            "before": round(before, 1),
            "after": round(after, 1),
            "meters": meters,
            "fed": fed,
            "ts": now_ts(),
        }
        self.state.setdefault("history", []).append({
            "ts": now_ts(), "kind": "send", "gift": gid, "name": str(gift.get("name") or gid),
            "delta": round(delta, 1), "before": round(before, 1), "after": round(after, 1),
            "meters": meters, "fed": fed,
        })
        self.state["history"] = self.state["history"][-200:]
        self._save_state()
        self._post_feed(str(gift.get("name") or gid), str(gift.get("kind") or "item"), float(delta), gid)
        return {
            "ok": True,
            "gift": gid,
            "kind": str(gift.get("kind") or "item"),
            "name": str(gift.get("name") or gid),
            "line": line,
            "delta": round(delta, 1),
            "before": round(before, 1),
            "after": round(after, 1),
            "meters": meters,
            "fed": fed,
            "unlocked": newly,
            "furniture_unlocked": new_furniture,
            "daily_wish_done": bool((self.state.get("daily_wish") or {}).get("done")),
            "newly_unlocked_names": [str((self.gift_map.get(x) or {}).get("name") or x) for x in newly],
            "new_furniture_names": [
                str((self.furniture_map.get(x) or {}).get("name") or x) for x in new_furniture
            ],
        }

    # ---------------- 待读 / 提示 ----------------

    # ---------------- 喂完之后主动说一句 ----------------

    async def _persona_sync(self, umo: str = "") -> None:
        """性格不在这里填：直接读 AstrBot 里她的人设，缓存半小时。"""
        cache = self.state.get("persona_cache") or {}
        if cache and now_ts() - float(cache.get("ts", 0) or 0) < 1800:
            return
        try:
            pm = getattr(self.context, "persona_manager", None)
            if pm is None:
                return
            persona = await pm.get_default_persona_v3(umo or self._post_feed_umo() or None)
            # AstrBot 4.x 的 Personality 是 TypedDict（运行时就是 dict），
            # 用 getattr 取值永远拿到空串，人设正文会整个丢掉；两种形态都要兼容。
            if isinstance(persona, dict):
                name = str(persona.get("name") or "").strip()
                prompt = str(persona.get("prompt") or "").strip()
            else:
                name = str(getattr(persona, "name", "") or "").strip()
                prompt = str(getattr(persona, "prompt", "") or "").strip()
            self.state["persona_cache"] = {
                "ts": now_ts(), "name": name, "prompt": prompt[:600],
            }
        except Exception as e:
            logger.debug("[鱼的窝] 读人设失败: %s", e)

    def _persona_block(self) -> tuple[str, str]:
        """返回 (人设名, 人设正文)；拿不到就是空的。"""
        cache = self.state.get("persona_cache") or {}
        return str(cache.get("name") or "").strip(), str(cache.get("prompt") or "").strip()

    def _post_feed_persona(self) -> str:
        """只有兜底文案还用得到：人设名里带某个词就挑对应口吻，否则用温柔。"""
        name, _ = self._persona_block()
        for k in _POST_FEED_COMMON:
            if k and k in name:
                return k
        return "温柔"

    def _post_feed_system(self, name: str, kind: str) -> str:
        who = str(self._cfg("panel.character_name", "她") or "她")
        pname, pblock = self._persona_block()
        persona = pblock or pname or "（按你原本的人设来，不要另外编性格）"
        snap = self._heart_snapshot()
        mood = str(snap.get("mood") or "平常")
        meters = self.state.get("meters") or {}
        levels = self.state.get("meter_levels") or {}
        lv = int(levels.get("intimacy", 0) or 0)
        cur = round(clamp(float(meters.get("intimacy", 0) or 0)), 1)
        if kind == "furniture":
            return (
                f"你是{who}，你的人设是：{persona}\n"
                f"刚刚发生的事实：你按对方说的一句描述，亲手画出了一个「{name}」，收进了工坊的库，还没摆进房间。\n"
                f"你现在的状态：心情{mood}，亲密度 {cur}/100（第 {lv} 层）。\n"
                "现在你自己主动说一句话，就像顺手拿起手机发的那种：\n"
                "- 一句话，口语，12～24 个字，不要分行\n"
                "- 是你自己动笔画的东西：可以点评画得怎么样、可以说哪里难画、可以问对方想摆哪儿\n"
                "- 完全按你的性格来：傲娇就嘴硬、冷淡就短、黏人就想多聊、毒舌就损一句\n"
                "- 不要解释、不要括号动作、不要引号、不要提面板/AI/系统/插件/施工图/提示词\n"
                "直接输出这句话本身。"
            )
        if kind == "furnplace":
            return (
                f"你是{who}，你的人设是：{persona}\n"
                f"刚刚发生的事实：对方在房间里摆弄了一番，把「{name}」摆到了现在这个位置，屋里有它了。\n"
                f"你现在的状态：心情{mood}，亲密度 {cur}/100（第 {lv} 层）。\n"
                "现在你自己主动说一句话，就像顺手拿起手机发的那种：\n"
                "- 一句话，口语，12～24 个字，不要分行\n"
                "- 可以评价摆的位置、可以说挡不挡道、也可以问句为什么这么放\n"
                "- 完全按你的性格来：傲娇就嘴硬、冷淡就短、黏人就想多聊、毒舌就损一句\n"
                "- 不要解释、不要括号动作、不要引号、不要提面板/AI/系统/插件/施工图/提示词\n"
                "直接输出这句话本身。"
            )
        act = {"food": "吃掉了", "drink": "喝掉了", "item": "收下了"}.get(kind, "收下了")
        feel = {
            "food": "嘴里还留着味道",
            "drink": "喉咙里凉凉的",
            "item": "东西就在手边",
        }.get(kind, "东西就在手边")
        return (
            f"你是{who}，你的人设是：{persona}\n"
            f"刚刚发生的事实：对方把「{name}」给了你，你{act}。{feel}。\n"
            f"你现在的状态：心情{mood}，亲密度 {cur}/100（第 {lv} 层）。\n"
            "现在你自己主动说一句话，就像顺手拿起手机发的那种：\n"
            "- 一句话，口语，12～24 个字，不要分行\n"
            "- 要让你自己听起来是真的吃到了/收到了（可以提味道、手感、或者反应）\n"
            "- 完全按你的性格来：傲娇就嘴硬、冷淡就短、黏人就想多聊、毒舌就损一句\n"
            "- 不要解释、不要括号动作、不要引号、不要提面板/AI/系统/插件\n"
            "- 不要每次都夸，可以抱怨、可以敷衍、可以说别的事\n"
            "直接输出这句话本身。"
        )

    async def _post_feed_say(self, name: str, kind: str, umo: str) -> str:
        """优先让模型用她自己的语气说；失败再退回模板。"""
        await self._persona_sync(umo)
        if not bool(self._cfg("post_feed.use_llm", True)):
            return self._post_feed_line(name, kind)
        try:
            provider = self.context.get_using_provider(umo)
            if provider is None:
                raise RuntimeError("no provider")
            resp = await provider.text_chat(
                prompt="（刚刚喂完，你主动发一句）",
                system_prompt=self._post_feed_system(name, kind),
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            text = text.strip("\"'“”「」")
            text = re.sub(r"^[（(].*?[）)]\s*", "", text).strip()
            text = text.splitlines()[0].strip() if text else ""
            if 2 <= len(text) <= 40:
                return text
        except Exception as e:
            logger.warning("[鱼的窝] 生成主动消息失败，用模板兜底: %s", e)
        return self._post_feed_line(name, kind)

    def _post_feed_line(self, name: str, kind: str = "item") -> str:
        if kind in ("furniture", "furnplace"):
            return ""      # 家具的话只许她自己说：模型挂了就不吭声，不拿喂食模板凑
        persona = self._post_feed_persona()
        if kind not in ("food", "drink", "item"):
            kind = "item"
        pool = post_feed_pool(persona, kind)
        named = [x for x in pool if "{name}" in x] or pool
        try:
            return str(random.choice(named)).format(name=name)
        except Exception:
            return f"{name}……谢谢你。"

    # ---------------- 喜好 / 腻了 ----------------

    def _pref_ids(self) -> set:
        """配置里填的「她喜欢的东西」：逗号/空格分隔，可以填 id 也可以填名字。"""
        raw = ""          # 口味列表不再开放
        out = set()
        for part in re.split(r"[,，、\s]+", raw):
            p = part.strip()
            if not p:
                continue
            out.add(self._gift_key_name(p))
        return out

    def _gift_key_name(self, token: str) -> str:
        for gid, g in (self.gift_map or {}).items():
            if token == gid or token == str(g.get("name") or ""):
                return gid
        for f in (self.foods or []):
            if token == str(f.get("id") or "") or token == str(f.get("name") or ""):
                return str(f.get("id"))
        return token

    def _mood_row(self, gid: str) -> dict:
        rows = self.state.setdefault("gift_mood", {})
        row = rows.get(gid)
        if not isinstance(row, dict):
            row = {}
            rows[gid] = row
        row.setdefault("streak", 0)
        row.setdefault("last_ts", 0.0)
        row.setdefault("sick_until", 0.0)
        return row

    def _touch_gift_mood(self, gid: str) -> None:
        """喂了某件东西：它自己 streak +1，其他归零；腻了按规则加/减天数。"""
        if str(gid) in NO_MOOD_IDS:
            return                       # 白米饭这种每天白给的，不计喜欢也不计腻
        rows = self.state.setdefault("gift_mood", {})
        now = now_ts()
        for other in list(rows.keys()):
            if other == gid:
                continue
            row = self._mood_row(other)
            row["streak"] = 0
            # 喂别的东西 = 帮她散散腻：每次减 1 天（最低到今天）
            until = float(row.get("sick_until", 0) or 0)
            if until > now:
                row["sick_until"] = max(now, until - 86400.0)
        row = self._mood_row(gid)
        row["streak"] = int(row.get("streak", 0) or 0) + 1
        row["last_ts"] = now
        try:
            bored_at = 3
        except Exception:
            bored_at = 3
        if row["streak"] >= bored_at:
            extra = (row["streak"] - bored_at) // 2
            days = 1 + extra
            try:
                days = 1 + extra
            except Exception:
                pass
            row["sick_until"] = max(float(row.get("sick_until", 0) or 0), now + days * 86400.0)

    def _gift_mood_state(self, gid: str) -> dict:
        if str(gid) in NO_MOOD_IDS:
            return {"state": "", "streak": 0, "days": 0}
        known = gid in (self.gift_map or {}) or gid in self._food_map()
        row = self._mood_row(gid) if known else {}
        now = now_ts()
        streak = int(row.get("streak", 0) or 0)
        until = float(row.get("sick_until", 0) or 0)
        last = float(row.get("last_ts", 0) or 0)
        liked = gid in self._pref_ids()
        if until > now:
            left = max(1, int(round((until - now) / 86400.0)))
            return {"state": "bored", "streak": streak, "days": left}
        try:
            miss_days = 2
        except Exception:
            miss_days = 2
        if liked and last > 0 and (now - last) > miss_days * 86400:
            return {"state": "missed", "streak": streak, "days": 0}
        if liked:
            return {"state": "liked", "streak": streak, "days": 0}
        return {"state": "", "streak": streak, "days": 0}

    def _post_feed_umo(self) -> str:
        # 优先主人的私聊，避免喂完在群里刷一句
        umo = str(self._cfg("owner_session", "") or "").strip()
        if not umo:
            umo = str(getattr(self, "_last_umo", "") or "").strip()
        return umo

    # ---------------- 她会饿：一天三顿，时间每天都不一样 ----------------

    HUNGER_FALLBACK = {
        "傲娇": ["……不是饿了，就是随便说说。", "喂，你不管我饭的吗。"],
        "温柔": ["……有点饿了。", "你那边忙吗，我好像该吃饭了。"],
        "冷淡": ["饿了。", "……饭。"],
        "元气": ["我饿啦！今天吃什么！", "肚子在叫，你听得到吗！"],
        "黏人": ["饿了……你不在我就懒得吃。", "想吃你喂的那一口。"],
        "毒舌": ["你的鱼快饿死了，惊喜吗。", "再不来喂我，我就自己点外卖。"],
    }

    def _hunger_plan(self) -> dict:
        """按配置的三个基准时刻，各偏移 ±N 分钟，排今天的饿点；跨天重排。"""
        day = today_str()
        plan = self.state.get("hunger") or {}
        if str(plan.get("day") or "") == day and len(plan.get("slots") or []) >= 1:
            return plan
        base = str(self._cfg("hunger.slots", "08:00,12:30,19:00") or "08:00,12:30,19:00")
        try:
            jitter = max(0, min(59, int(self._cfg("hunger.jitter_minutes", 30) or 30))) * 60
        except Exception:
            jitter = 1800
        today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        slots = []
        for part in [p.strip() for p in base.split(",") if p.strip()][:3]:
            bits = part.split(":")
            try:
                hh = int(bits[0])
                mm = int(bits[1]) if len(bits) > 1 else 0
            except Exception:
                continue
            t = today0 + timedelta(hours=hh, minutes=mm,
                                   seconds=random.randint(-jitter, jitter))
            slots.append(round(t.timestamp()))
        slots.sort()
        plan = {"day": day, "slots": slots,
                "fired": [False] * len(slots), "fed": [False] * len(slots)}
        self.state["hunger"] = plan
        return plan

    async def _hunger_line(self, umo: str) -> str:
        """她饿了会怎么说——按人格让模型写一句；写不出来就用模板。"""
        persona = self._post_feed_persona()
        pool = self.HUNGER_FALLBACK.get(persona) or ["……饿了。"]
        fallback = random.choice(pool)
        if not bool(self._cfg("post_feed.use_llm", True)):
            return fallback
        try:
            provider = self.context.get_using_provider(umo) if umo else None
            if provider is None:
                return fallback
            who = str(self._cfg("panel.character_name", "她") or "她")
            await self._persona_sync(umo)
            pname, pblock = self._persona_block()
            persona = pblock or pname or "按你原本的人设"
            sys = (
                f"你是{who}，你的人设是：{persona}\n"
                "你现在有点饿了，给主人发一条很短的消息，"
                "不超过 25 个字，像随手发的，不要提系统/插件/AI，不要解释规则，别用书面语。"
            )
            resp = await provider.text_chat(prompt="（发一条消息）", system_prompt=sys)
            text = (getattr(resp, "completion_text", "") or "").strip().strip("\"'“”")
            if 3 <= len(text) <= 60:
                return text.replace("\n", " ")
        except Exception as e:
            logger.debug("[鱼的窝] 生成「饿了」失败，用模板: %s", e)
        return fallback

    async def _hunger_tick(self) -> None:
        """到她的饭点了就发一条消息；一天最多三顿，喂过就不再催。"""
        if not bool(self._cfg("hunger.enabled", True)):
            return
        plan = self._hunger_plan()
        now = now_ts()
        umo = self._post_feed_umo()
        if not umo:
            return
        slots = plan.get("slots") or []
        fired = plan.get("fired") or []
        fed = plan.get("fed") or []
        for i, slot in enumerate(slots):
            if now < float(slot):
                continue
            if i < len(fired) and fired[i]:
                continue
            if i < len(fed) and fed[i]:
                continue
            if now - float(slot) > 3 * 3600:      # 错过太久就不喊了，免得半夜补一句
                fired[i] = True
                if i < len(fed):
                    fed[i] = True                 # 这顿当作已经过去，别让喂米饭记成"刚好赶上"
                self.state["hunger"] = plan
                return
            fired[i] = True
            self.state["hunger"] = plan
            self.state["hunger_at"] = now
            line = await self._hunger_line(umo)
            try:
                await self.context.send_message(umo, MessageChain([Plain(line)]))
            except Exception as e:
                logger.warning("[鱼的窝] 发「饿了」失败: %s", e)
            self._life(f"她饿了，给你发了：{line}", "hunger")
            self._save_state()
            return

    async def _hunger_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(45)
                if not bool(self._cfg("enabled", True)):
                    continue
                await self._hunger_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("[鱼的窝] 饥饿循环异常: %s", e)
                await asyncio.sleep(30)

    @staticmethod
    def _rel_time(ts: float) -> str:
        mins = max(0.0, (now_ts() - float(ts or 0)) / 60.0)
        if mins < 1:
            return "刚刚"
        if mins < 60:
            return f"{int(mins)} 分钟前"
        hours = mins / 60.0
        if hours < 24:
            return f"{int(hours)} 小时前"
        return f"{int(hours / 24)} 天前"

    def _remember_feed(self, name: str, kind: str, delta: float, fid: str = "") -> None:
        """把这次喂食/画画记进她的记忆。两袋分装：吃饭 feed_memory（8 格）、
        画画 furn_memory（6 格），互不挤占（会注入上下文，让她真知道自己吃了什么、画过什么）。"""
        furn = kind in ("furniture", "furnplace")
        bag = "furn_memory" if furn else "feed_memory"
        rows = list(self.state.get(bag) or [])
        row: dict = {
            "ts": now_ts(),
            "name": str(name),
            "kind": str(kind),
            "delta": round(float(delta or 0), 1),
        }
        if fid:
            row["fid"] = str(fid)
        rows.append(row)
        self.state[bag] = rows[-(6 if furn else 8):]

    def _feed_memory_lines(self) -> list[str]:
        """注入她上下文的两本近账：吃过的（袋尾 3 条）+ 画过的（袋尾 3 条）。
        旧版混进饭袋的家具行也捞回画袋，不会在饭桌上说画过书架。"""
        out: list[str] = []
        food = [r for r in (self.state.get("feed_memory") or [])
                if isinstance(r, dict) and str(r.get("name") or "").strip()
                and str(r.get("kind") or "item") not in ("furniture", "furnplace")][-3:]
        for row in food:
            act = {"food": "吃掉", "drink": "喝掉", "item": "收下"}.get(
                str(row.get("kind") or "item"), "收下")
            out.append(
                f"- {self._rel_time(row.get('ts'))}，对方给了你「{row.get('name')}」，你{act}了，"
                f"亲密 +{fmt_num(float(row.get('delta', 0) or 0))}"
            )
        drawn = [r for r in (self.state.get("furn_memory") or [])
                 if isinstance(r, dict) and str(r.get("name") or "").strip()]
        drawn += [r for r in (self.state.get("feed_memory") or [])
                  if isinstance(r, dict) and str(r.get("kind") or "") == "furniture"
                  and str(r.get("name") or "").strip()]
        drawn = sorted(drawn, key=lambda r: float(r.get("ts") or 0))[-3:]
        for row in drawn:
            out.append(
                f"- {self._rel_time(row.get('ts'))}，对方说了一句想画的东西，你亲手画出了「{row.get('name')}」，收进了工坊的库"
            )
        return out

    def _reaction_gif(self, gid: str) -> str:
        """聊天送礼回执配图：有明确口味反馈用新表情动画，否则退回旧合成 GIF；都没有返回空。"""
        state = ""
        try:
            state = str((self._gift_mood_state(gid) or {}).get("state") or "")
        except Exception:
            state = ""
        pick = MOOD_GIF_BY_STATE.get(state)
        if pick:
            p = os.path.join(ACT_DIR, pick)
            if os.path.isfile(p):
                return p
        p = os.path.join(GIF_DIR, f"{gid}.gif")
        return p if os.path.isfile(p) else ""

    def _post_feed(self, name: str, kind: str = "item", delta: float = 0.0, gid: str = "",
                   fid: str = "") -> None:
        """喂完/画完/摆完排一句主动消息；连着来只保留最后一句。"""
        if kind != "furnplace":      # 摆放不记事件行——「房间里现在有什么」清单本身就是她的记忆
            self._remember_feed(name, kind, delta, fid)
        if not bool(self._cfg("post_feed.enabled", True)):
            return
        umo = self._post_feed_umo()
        if not umo:
            return
        try:
            chance = float(self._cfg("post_feed.chance", 1.0))
        except Exception:
            chance = 1.0
        if chance < 1.0 and random.random() > chance:
            return
        old = getattr(self, "_post_feed_task", None)
        if old is not None and not old.done():
            old.cancel()
        try:
            delay = max(0.0, float(self._cfg("post_feed.delay_seconds", 3) or 0))
        except Exception:
            delay = 3.0
        self._post_feed_task = asyncio.create_task(self._post_feed_send(name, kind, umo, delay, gid))

    async def _post_feed_send(self, name: str, kind: str, umo: str, delay: float, gid: str = "") -> None:
        try:
            if delay:
                await asyncio.sleep(delay)
            try:
                gap = max(0, int(self._cfg("post_feed.min_gap_seconds", 0) or 0))
            except Exception:
                gap = 0
            if gap:
                last = float(self.state.get("last_post_feed_ts", 0) or 0)
                if now_ts() - last < gap:
                    return
            text = await self._post_feed_say(name, kind, umo)
            if not text:
                return      # 家具这类她说不出人话就不说
            chain: list = [Plain(text)]
            gif_name = EAT_GIF_BY_ID.get(gid or "")
            if gif_name:
                p = os.path.join(ACT_DIR, gif_name)
                if os.path.isfile(p):
                    chain.append(Image.fromFileSystem(p))
            ok = await self.context.send_message(umo, MessageChain(chain))
            if ok is False:
                return
            self.state["last_post_feed_ts"] = now_ts()
            self.state.setdefault("history", []).append(
                {"ts": now_ts(), "kind": "post_feed", "gift": "", "name": name, "line": text}
            )
            self.state["history"] = self.state["history"][-200:]
            self._save_state()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[鱼的窝] 喂完主动说话失败: %s", e)

    def _pending(self, kind: str, text: str, gift: str | None = None) -> None:
        rows = list(self.state.get("pending") or [])
        rows.append({
            "kind": kind, "text": str(text), "gift": gift, "ts": now_ts(), "notified": False,
        })
        self.state["pending"] = rows[-20:]

    def _gift_payload(self, gift: dict, stats: dict) -> dict:
        gid = self._gift_key(gift)
        stock = self._gift_stock(gift)
        unlock = gift.get("unlock") or {}
        hidden = bool(unlock.get("hidden"))
        revealed = (not hidden) or (gid in (self.state.get("revealed") or {})) or self._gift_unlocked(gift)
        conditions = unlock.get("conditions") or []
        leafs = self._leaf_views({"all": conditions}, stats) if conditions else []
        if hidden and not revealed:
            leafs = []
        progress = self._condition_progress({"all": conditions}, stats) if conditions else 1.0
        if hidden and not revealed:
            progress = 0.0
        block = self._gift_send_block(gift, stats)
        effect = gift.get("effect") or {}
        effect_parts = []
        for key, label in (("intimacy", "亲密"), ("joy", "喜悦"), ("attachment", "依恋")):
            v = float(effect.get(key, 0) or 0)
            if v:
                effect_parts.append(f"{label} {'+' if v > 0 else ''}{fmt_num(v)}")
        soothe = -float(effect.get("stress", 0) or 0)
        if soothe:
            effect_parts.append(f"安抚 +{fmt_num(soothe)}")
        return {
            "id": gid,
            "kind": str(gift.get("kind") or "item"),
            "name": "？？？" if (hidden and not revealed) else str(gift.get("name") or gid),
            "emoji": "❓" if (hidden and not revealed) else str(gift.get("emoji") or "🎁"),
            "icon": "" if (hidden and not revealed) else str(gift.get("icon") or ""),
            "rarity": str(gift.get("rarity") or "common"),
            "rarity_cn": "未知" if (hidden and not revealed) else RARITY_CN.get(str(gift.get("rarity") or "common"), "普通"),
            "desc": "？？？" if (hidden and not revealed) else str(gift.get("desc") or ""),
            "effect_text": "？？？" if (hidden and not revealed) else " · ".join(effect_parts),
            "source": "？？？" if (hidden and not revealed) else str(gift.get("source") or ""),
            "hidden": hidden,
            "revealed": revealed,
            "hint": str(unlock.get("hint") or "") if (hidden and not revealed and bool(self._cfg("panel.show_hidden_hints", True))) else "",
            "unlocked": self._gift_unlocked(gift),
            "mood": self._gift_mood_state(gid),
            "count": stock["count"],
            "sent": stock["sent"],
            "daily": self._is_daily_gift(gift),
            "daily_target": self._daily_target(gift) if self._is_daily_gift(gift) else 0,
            "daily_current": self._chat_today() if self._is_daily_gift(gift) else 0,
            "daily_claimed": str(self.state.get("daily_gift_day") or "") == today_str() if self._is_daily_gift(gift) else False,
            "conditions": leafs,
            "progress": round(float(progress), 4),
            "can_send": not block,
            "block_reason": block,
            "gif": f"./assets/reactions/{gid}.gif",
            "animation": gift.get("animation") or {},
        }

    # ---------------- 好感度（货币） ----------------

    def _load_foods(self):
        path = os.path.join(PLUGIN_DIR, "assets", "foods.json")
        try:
            with open(path, encoding="utf-8-sig") as f:
                raw = json.load(f)
            rows = [r for r in (raw.get("foods") or []) if isinstance(r, dict) and r.get("id")]
            rev = raw.get("reverse_gifts") or {}
            return rows, rev
        except Exception as e:
            logger.warning("[鱼的窝] 读食物表失败: %s", e)
            return [], {}

    def _favor(self) -> float:
        return float(self.state.get("favor", 0) or 0)

    def _favor_add(self, delta: float, reason: str) -> float:
        v = self._favor() + float(delta)
        v = min(v, 100.0)
        self.state["favor"] = round(v, 1)
        if v > float(self.state.get("favor_peak", 0) or 0):
            self.state["favor_peak"] = round(v, 1)
        if self._g_on():
            if float(delta) > 0:
                self.state["favor_earned_today"] = round(
                    float(self.state.get("favor_earned_today", 0) or 0) + float(delta), 1)
            elif float(delta) < 0:
                self.state["favor_spent_today"] = round(
                    float(self.state.get("favor_spent_today", 0) or 0) - float(delta), 1)
            self._growth_check_breakthrough(float(delta))
        if v < 0 and not self.state.get("negative_flag"):
            self.state["negative_flag"] = True
            self.state["negative_days"] = int(self.state.get("negative_days", 0) or 0) + 1
        elif v >= 0:
            self.state["negative_flag"] = False
        rows = list(self.state.get("favor_log") or [])
        rows.append({"ts": now_ts(), "delta": round(float(delta), 2), "reason": str(reason)})
        self.state["favor_log"] = rows[-40:]
        return round(v, 1)

    # ---------------- 余额（工资存这里，买吃的花这里） ----------------

    def _balance(self) -> int:
        return max(0, int(round(float(self.state.get("balance", 0) or 0))))

    def _balance_add(self, delta: int, reason: str) -> int:
        v = max(0, self._balance() + int(delta))
        self.state["balance"] = v
        rows = list(self.state.get("balance_log") or [])
        rows.append({"ts": now_ts(), "delta": int(delta), "reason": str(reason), "after": v})
        self.state["balance_log"] = rows[-60:]
        return v

    def _balance_can(self, cost: int) -> bool:
        return self._balance() >= int(cost)

    # ---------------- 好感度：只说给话的人听 ----------------

    def _favor_mood_factor(self) -> float:
        """鱼心的状态决定她"吃不吃你这一套"：心情好一夸就上天，委屈时怎么说都听不进。"""
        try:
            s = self._heart_snapshot()
        except Exception:
            return 1.0
        if str(s.get("source") or "") != "heart":
            return 1.0
        mode = str(s.get("mode") or "calm")
        if mode == "shut":
            return 0.0
        if mode in ("wronged", "afraid"):
            return 0.4
        joy = float(s.get("joy", 0) or 0)
        stress = float(s.get("stress", 0) or 0)
        blame = float(s.get("blame", 0) or 0)
        f = 1.0
        if joy >= 60:
            f += 0.2
        elif joy <= 20:
            f -= 0.2
        if stress >= 60:
            f -= 0.2
        if blame >= 50:
            f -= 0.2
        return max(0.2, min(1.4, round(f, 2)))

    def _favor_from_chat(self, text: str) -> float:
        """每说一句结算一次：认真说话涨、敷衍涨得少、凶她会掉。
        涨多少还要乘一个「她此刻的心情系数」——同句话，她高兴时涨得多，委屈时几乎不涨。"""
        t = str(text or "")
        n = len(t)
        cap = 15.0          # 每天最多涨 15（写死，设置里不再暴露）
        earned = float(self.state.get("favor_earned_today", 0) or 0)
        if any(w in t for w in MEAN_WORDS):
            delta = -1.5
            reason = "你凶她了"
        elif n <= 3:
            return 0.0          # "嗯""哦""在"这种不算话
        else:
            delta = 1.0 if n >= 12 else 0.6
            if any(w in t for w in PRAISE_WORDS) or any(w in t for w in GENTLE_WORDS):
                delta += 0.4
            if ("想你" in t) or ("喜欢你" in t) or ("爱你" in t):
                delta += 0.1
            delta = min(delta, 1.5)
            factor = self._favor_mood_factor()
            if factor <= 0:
                return 0.0      # 她躲起来了，这时候说什么都听不进
            delta = round(delta * factor, 1)
            reason = "你和她说的话"
            if delta <= 0:
                return 0.0
            if earned >= cap:
                return 0.0      # 今天已经涨满了，先不加
        delta = round(float(delta), 1)
        if not delta:
            return 0.0
        self._favor_add(delta, reason)
        return delta

    def _life(self, text: str, kind: str = "life") -> None:
        rows = list(self.state.get("life_log") or [])
        rows.append({"ts": now_ts(), "kind": str(kind), "text": str(text)})
        self.state["life_log"] = rows[-60:]

    def _heart_inst(self):
        try:
            from astrbot.core.star.star import star_map
            for meta in star_map.values():
                name = str(getattr(meta, "name", "") or "")
                if "fish_heart" not in name:
                    continue
                inst = getattr(meta, "star_cls", None)
                if inst is not None and getattr(inst, "state", None):
                    return inst
        except Exception:
            pass
        return None

    def _heart_drives(self) -> dict:
        inst = self._heart_inst()
        if inst is not None:
            try:
                d = inst.state.get("drives") or {}
                if d:
                    return dict(d)
            except Exception:
                pass
        try:
            path = os.path.join(PLUGIN_DIRS_HEART, "state.json")
            with open(path, encoding="utf-8-sig") as f:
                return dict((json.load(f).get("drives") or {}))
        except Exception:
            return {}

    def _apply_radar(self, effects: dict) -> None:
        inst = self._heart_inst()
        if inst is None:
            self._dbg("没找到鱼心插件，这次的效果只记在面板上，没进她的九维")
            return
        if not effects:
            return
        try:
            drives = inst.state.setdefault("drives", {})
            for k, v in effects.items():
                if k not in drives:
                    continue
                drives[k] = round(max(0.0, min(100.0, float(drives.get(k, 50) or 50) + float(v))), 3)
            inst.state["last_tick"] = now_ts()
            save = getattr(inst, "_save_state", None)
            if callable(save):
                save()
        except Exception as e:
            logger.warning("[鱼的窝] 反哺九维失败: %s", e)

    def _online_bump(self) -> None:
        """面板开着就算在线：每次轮询累加一段，间隔太长的不算（人不在）。"""
        now = now_ts()
        day = today_str()
        on = self.state.get("online") or {}
        if not on:
            self.state["online"] = {"day": day, "sec": 0.0, "last": now}
            return
        if str(on.get("day") or "") != day:
            return                      # 跨天先等结算，别把昨天的时长冲掉
        last = float(on.get("last", 0) or 0)
        if last and now - last <= 120:
            on["sec"] = round(float(on.get("sec", 0) or 0) + min(now - last, 60), 1)
        on["last"] = now
        self.state["online"] = on

    def _wage_bonus_add(self, delta: float) -> float:
        """临时加减工资进度（DIY 用），结算时一起算进去。"""
        v = float(self.state.get("wage_bonus", 0) or 0) + float(delta or 0)
        v = max(-40.0, min(40.0, v))
        self.state["wage_bonus"] = round(v, 1)
        return self.state["wage_bonus"]

    def _wage_progress(self) -> dict:
        """今天的工资进度：挂满 target_hours 就是 100。"""
        hours = WAGE_HOURS
        top = WAGE_MAX
        on = self.state.get("online") or {}
        sec = float(on.get("sec", 0) or 0) if str(on.get("day") or "") == today_str() else 0.0
        bonus = float(self.state.get("wage_bonus", 0) or 0)
        pct = min(100.0, max(0.0, sec / (hours * 3600.0) * 100.0 + bonus))
        return {
            "value": round(pct, 1),
            "hours": round(sec / 3600.0, 2),
            "target_hours": hours,
            "max_money": top,
            "bonus": round(bonus, 1),
        }

    def _settle_favor(self) -> None:
        """每天结算一次：她的心决定今天加多少；被敷衍/假安慰会扣。"""
        day = today_str()
        if str(self.state.get("favor_day") or "") == day:
            return
        self.state["favor_day"] = day
        d = self._heart_drives()
        pos_keys = ("joy", "intimacy", "attachment", "curiosity", "reflection", "duty", "social")
        pos = sum(float(d.get(k, 50) or 0) for k in pos_keys) / max(1, len(pos_keys))
        neg = (float(d.get("stress", 0) or 0) + float(d.get("fatigue", 0) or 0)) / 2.0
        net = pos - neg
        # 工资按「在线时长」算：挂满 target_hours 拿满，其余按比例
        target_h = WAGE_HOURS
        top = WAGE_MAX
        on = self.state.get("online") or {}
        sec = float(on.get("sec", 0) or 0)          # 这时 online 里是刚过去那一天的时长
        bonus = float(self.state.get("wage_bonus", 0) or 0)
        wage = int(round(max(0.0, min(1.0, sec / (target_h * 3600.0)) * top + bonus)))
        wage = max(0, min(int(top), wage))
        self.state["wage_bonus"] = 0.0
        self.state["wage_today"] = wage
        self.state["wage_net"] = round(sec / 3600.0, 2)      # 换算成"今天挂了几小时"
        self.state["wage_day_mood"] = str(self._heart_snapshot().get("mood") or "平常")
        self.state["favor_day_mood"] = self.state["wage_day_mood"]
        self.state["online"] = {"day": day, "sec": 0.0, "last": 0.0}   # 新的一天重新计时
        if wage:
            self._balance_add(wage, f"挂机 {round(sec / 3600.0, 1)} 小时")
            self._life(f"昨天挂了 {round(sec / 3600.0, 1)} 小时，工资 {wage} 块进余额", "wage")
        else:
            self._life("昨天基本没在线，没挣到工资", "wage")

        # 被敷衍 / 假安慰：从她的心里读今天的责难
        inst = self._heart_inst()
        blame = 0
        try:
            if inst is not None:
                log = inst.state.get("blame_log") or []
                start = now_ts() - 86400
                blame = len([e for e in log if isinstance(e, dict) and float(e.get("ts", 0) or 0) >= start])
        except Exception:
            blame = 0
        if blame:
            self.state["blame_days"] = int(self.state.get("blame_days", 0) or 0) + 1
            self._growth_bump("conflict", blame)
            self._favor_add(-5 * blame, f"今天被敷衍 {blame} 次")
            self._life(f"今天被敷衍/假安慰 {blame} 次，好感度 -{5 * blame}", "favor")

        # 白给的东西按时补货（白米饭 12 小时一份），不在这里一天一发
        self._free_food_tick(force=True)
        self._save_state()

    # ---------------- 食物 ----------------

    def _food_map(self) -> dict:
        return {str(f["id"]): f for f in self.foods}

    def _free_food_tick(self, force: bool = False) -> None:
        """白给的那几样每天补满固定份数（白米饭 3 份，不累计、不过夜带）。"""
        today = today_str()
        if str(self.state.get("free_day") or "") == today and not force:
            return
        self.state["free_day"] = today
        stock = self.state.setdefault("food_stock", {})
        for f in self.foods:
            n = int(f.get("free_daily", 0) or 0)
            if n <= 0:
                continue
            stock[f["id"]] = n          # 每天重置成固定份数，不会越攒越多
        self._save_state()

    def _food_stock(self) -> dict:
        return self.state.setdefault("food_stock", {})

    def _food_payload(self) -> list[dict]:
        stock = self._food_stock()
        bal = self._balance()
        out = []
        for f in self.foods:
            fid = str(f["id"])
            n = int(stock.get(fid, 0) or 0)
            price = int(f.get("price", 0) or 0)
            radar = f.get("radar") or {}
            effect = " · ".join(f"{RADAR_CN.get(k, k)} {('+' if v > 0 else '')}{v}" for k, v in radar.items())
            base_price = price
            price = self._food_price(f)
            out.append({
                "id": fid,
                "kind": "food",
                "name": str(f.get("name") or fid),
                "emoji": str(f.get("emoji") or "🍙"),
                "icon": str(f.get("icon") or ""),
                "rarity": str(f.get("rarity") or "common"),
                "rarity_cn": str(f.get("rarity_cn") or {"common": "普通", "rare": "精良", "epic": "稀有"}.get(
                    str(f.get("rarity") or "common"), "普通")),
                "action": str(f.get("action") or ""),
                "action_text": str(f.get("action_text") or ""),
                "free_daily": int(f.get("free_daily", 0) or 0),
                "desc": str(f.get("desc") or ""),
                "effect_text": effect,
                "price": price,
                "price_base": base_price,
                "ever_eaten": fid in (self.state.get("unlocked_foods") or []),
                "count": n,
                "sent": 0,
                "unlocked": True,
                "can_send": n > 0 or bal >= price,
                "block_reason": "" if (n > 0 or bal >= price) else f"钱不够（要 {price}，你有 {bal}）",
                "mood": self._gift_mood_state(fid),
            })
        return out

    def _feed_food(self, fid: str) -> dict:
        foods = self._food_map()
        f = foods.get(str(fid))
        if not f:
            return {"ok": False, "error": "没有这种吃的"}
        # 一天最多喂三顿（白米饭也算在内）
        day = today_str()
        if str(self.state.get("fed_day") or "") != day:
            self.state["fed_day"] = day
            self.state["fed_count_today"] = 0
        try:
            limit = 3
        except Exception:
            limit = 3
        if limit > 0 and int(self.state.get("fed_count_today", 0) or 0) >= limit:
            return {"ok": False, "error": f"今天已经喂过 {limit} 顿啦，明天再来。"}
        if self._band() == "shut" and not self._test_mode():
            self._record_reject(str(f.get("name") or fid))
            return {"ok": False, "error": random.choice(self.BAND_REJECT_LINES)}
        stock = self._food_stock()
        n = int(stock.get(str(fid), 0) or 0)
        price = self._food_price(f)
        if n <= 0:
            if not self._balance_can(price):
                return {"ok": False, "error": f"钱不够，这个要 {price}，你现在有 {self._balance()}"}
            self._balance_add(-price, f"买了{f['name']}")
            self._spent_add(price)
        else:
            stock[str(fid)] = n - 1
        # 白米饭：喂了就算把这顿饿解决了
        if str(fid) == "rice":
            plan = self._hunger_plan()
            fired = plan.get("fired") or []
            fed = plan.get("fed") or []
            for i in range(len(fired) - 1, -1, -1):
                if fired[i] and not (i < len(fed) and fed[i]):
                    fed[i] = True
                    self.state["hunger"] = plan
                    self._life("你喂了她白米饭，这顿正好赶上。", "hunger")
                    break
        unlocked = list(self.state.get("unlocked_foods") or [])
        if fid not in unlocked:
            unlocked.append(fid)
            self.state["unlocked_foods"] = unlocked[-40:]
            self._pending("food_first", f"她第一次吃到「{f['name']}」，以后这个价她记得给你打折。")
        self.growth_record("feed", quote=str(f.get("name") or fid), source="panel")
        radar = f.get("radar") or {}
        self._apply_radar(radar)
        self._life(f"主人喂你吃了「{f['name']}」", "food")
        self._touch_gift_mood(fid)
        self.state["fed_total"] = int(self.state.get("fed_total", 0) or 0) + 1
        kinds = list(self.state.get("fed_kinds") or [])
        if fid not in kinds:
            kinds.append(fid)
        self.state["fed_kinds"] = kinds[-40:]
        self.state["fed_count_today"] = int(self.state.get("fed_count_today", 0) or 0) + 1
        self._check_achievements()
        self._save_state()
        eff = "、".join(f"{RADAR_CN.get(k, k)} {'+' if v > 0 else ''}{v}" for k, v in radar.items())
        return {
            "ok": True,
            "name": str(f.get("name")),
            "line": str(f.get("line") or "……嗯。"),
            "delta": sum(abs(float(v)) for v in radar.values()),
            "meters": {},
            "fed": [],
            "effect": eff,
            "mood": self._gift_mood_state(fid),
        }


    # ---------------- 「今天」的闭环：事件流 / 当日额度 / 深夜事件 / 今日卡 ----------------

    def _load_json_list(self, path: str, key: str) -> list[dict]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            rows = raw.get(key) if isinstance(raw, dict) else raw
            return [x for x in (rows or []) if isinstance(x, dict)]
        except Exception as e:
            logger.warning("[鱼的窝] 读 %s 失败: %s", os.path.basename(path), e)
            return []

    def _load_json_dict(self, path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return raw if isinstance(raw, dict) else {}
        except Exception as e:
            logger.warning("[鱼的窝] 读 %s 失败: %s", os.path.basename(path), e)
            return {}

    def _g_on(self) -> bool:
        return True

    def _g_cap(self) -> float:
        try:
            return 100.0
        except Exception:
            return 100.0

    def growth_record(self, kind: str, topics=None, quote: str = "", source: str = "") -> None:
        """唯一的事件入口：所有互动都往这里落一行，其它模块只读它。"""
        if not self._g_on():
            return
        day = today_str()
        rows = list(self.state.get("events") or [])
        rows.append({
            "ts": now_ts(), "day": day, "kind": str(kind),
            "topics": [str(t) for t in (topics or []) if str(t)][:4],
            "quote": str(quote or "")[:140], "source": str(source or ""),
        })
        self.state["events"] = rows[-4000:]
        self._growth_bump(kind, 1)
        try:
            keep = 30
        except Exception:
            keep = 30
        cutoff = (datetime.now() - timedelta(days=max(1, keep))).strftime("%Y-%m-%d")
        for r in self.state["events"]:
            if str(r.get("day") or "") < cutoff and r.get("quote"):
                r["quote"] = ""

    def _growth_bump(self, kind: str, n: int = 1) -> None:
        day = today_str()
        ed = self.state.setdefault("event_days", {})
        st = ed.setdefault(day, {"chat": 0, "feed": 0, "gift": 0, "care": 0,
                                 "night": 0, "conflict": 0, "reverse": 0})
        if kind in st:
            st[kind] = int(st.get(kind, 0) or 0) + int(n)
        for k in [k for k in list(ed.keys()) if str(k) < (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")]:
            ed.pop(k, None)

    def _growth_today(self) -> dict:
        return (self.state.get("event_days") or {}).get(today_str()) or {}

    def _growth_roll_day(self, today: str) -> None:
        """跨天：先把昨天结算成卡，再决定今天好感度要不要归零。"""
        if not self._g_on():
            return
        last = str(self.state.get("day_started") or "")
        if not last:
            self.state["day_started"] = today
            self.state.setdefault("day_index", 1)
            return
        if last == today:
            return
        self._growth_settle("跨天", day=last,
                            day_no=int(self.state.get("day_index", 1) or 1))
        self.state["day_index"] = int(self.state.get("day_index", 1) or 1) + 1
        self.state["day_started"] = today
        self.state["favor_earned_today"] = 0.0
        self.state["favor_spent_today"] = 0.0
        self.state["favor_broke_through"] = False
        self.state["late_event_today"] = None
        # 好感度不再每日归零：它是实时的，能攒上去，也能掉到负数
        self._save_state()

    def _growth_day_events(self, day: str) -> list[dict]:
        return [e for e in (self.state.get("events") or []) if isinstance(e, dict) and str(e.get("day")) == day]

    def _growth_build_card(self, day: str, day_no: int | None = None) -> dict:
        """今日卡：全部用真实统计 + 模板拼，不调模型。"""
        ed = (self.state.get("event_days") or {}).get(day) or {}
        chat = int(ed.get("chat", 0) or 0)
        feed = int(ed.get("feed", 0) or 0)
        gift = int(ed.get("gift", 0) or 0)
        care = int(ed.get("care", 0) or 0)
        night = int(ed.get("night", 0) or 0)
        conflict = int(ed.get("conflict", 0) or 0)
        reverse = int(ed.get("reverse", 0) or 0)
        tpl = self.card_tpl or {}
        lines_tpl = tpl.get("lines") or {}
        evs = self._growth_day_events(day)
        keeps = len([e for e in evs
                     if str(e.get("kind")) == "chat" and str(e.get("quote") or "").strip()])
        broke = bool(self.state.get("favor_broke_through")) if day == today_str() else any(
            str(e.get("kind")) == "night" for e in evs
        )
        dw = self.state.get("daily_wish") or {}
        wdef = self._daily_wish_def() or {}
        wish_text = str(wdef.get("text") or dw.get("id") or "")

        def L(key: str, **kw) -> str:
            raw = str(lines_tpl.get(key) or "")
            try:
                return raw.format(**kw) if raw else ""
            except Exception:
                return raw

        lines: list[str] = []
        if chat:
            lines.append(L("chat", chat=chat, keeps=keeps))
        else:
            lines.append(L("chat_zero"))
        if feed:
            names = "、".join(self._growth_feed_names(day)[:6])
            lines.append(L("feed", feed=feed, feed_names=names))
        if gift:
            lines.append(L("gift", gift=gift))
        if care:
            lines.append(L("care", care=care))
        if reverse:
            rr = (tpl.get("reverse_reason") or {})
            good = any(str(e.get("kind")) == "reverse" and "不错" in str(e.get("quote") or "") for e in evs)
            lines.append(L("reverse", reverse=reverse, reverse_reason=str(rr.get("good" if good else "bad") or "")))
        if conflict:
            lines.append(L("conflict", conflict=conflict))
        if wish_text:
            lines.append(L("wish_done" if dw.get("done") else "wish_miss", wish=wish_text))
        if broke:
            lines.append(L("broke"))
        le = self.state.get("late_event_today") if day == today_str() else None
        if not isinstance(le, dict):
            nline = next((str(e.get("quote") or "") for e in evs if str(e.get("kind")) == "night"), "")
            if nline:
                lines.append(L("night", night_tag="心里话", night_line=nline))
        elif le:
            lines.append(L("night", night_tag=str(le.get("tag") or ""), night_line=str(le.get("line") or "")))
        spent = float(self.state.get("favor_spent_today", 0) or 0) if day == today_str() else 0.0
        if spent:
            lines.append(L("spend", spent=fmt_num(spent)))
        achv_names = [
            str(self.ach_map.get(k, {}).get("name") or k)
            for k, ts in (self.state.get("achievements") or {}).items()
            if str((datetime.fromtimestamp(float(ts or 0))).strftime("%Y-%m-%d")) == day
        ]
        if achv_names:
            lines.append(L("achv", achv=len(achv_names), achv_names="、".join(achv_names[:4])))
        furn = len(self.state.get("furniture") or {})
        if furn:
            lines.append(L("furniture", furniture=furn))
        mood_to = str(self._heart_snapshot().get("mood") or "平常")
        mood_from = str(self.state.get("favor_day_mood") or mood_to)
        lines.append(L("mood", mood_from=mood_from, mood_to=mood_to))
        lines = [x for x in lines if x]

        # headline：按今天的形状挑一句
        def pick_head() -> str:
            heads = {str(h.get("when")): str(h.get("text")) for h in (tpl.get("headline") or [])}
            if not (self.state.get("daily_cards") or []) and int(self.state.get("day_index", 1) or 1) <= 1:
                return heads.get("first") or heads.get("normal") or "今天"
            if broke:
                return heads.get("broke") or heads.get("normal") or "今天"
            if conflict:
                return heads.get("conflict") or heads.get("normal") or "今天"
            if wish_text and dw.get("done") and feed:
                return heads.get("wish_and_feed") or heads.get("normal") or "今天"
            if chat == 0:
                return heads.get("quiet") or heads.get("normal") or "今天"
            return heads.get("normal") or "今天过得还行"

        quotes = [str(e.get("quote") or "").strip() for e in evs
                  if str(e.get("kind")) == "chat" and str(e.get("quote") or "").strip()]
        if not quotes:
            quotes = [str(e.get("quote") or "").strip() for e in evs if str(e.get("quote") or "").strip()]
        keeptpl = tpl.get("keepsake") or []
        if quotes:
            q = random.choice(quotes[-6:])
            body = str(keeptpl[random.randrange(len(keeptpl))].format(quote=q)) if keeptpl else q
        else:
            body = str(tpl.get("empty_day") or "今天什么都没发生。")
        return {
            "day": int(day_no if day_no is not None else (self.state.get("day_index", 1) or 1)),
            "date": day,
            "ts": now_ts(),
            "headline": pick_head(),
            "lines": lines,
            "keepsake": body,
            "stats": {"chat": chat, "feed": feed, "gift": gift, "care": care,
                      "night": night, "conflict": conflict, "reverse": reverse},
            "mood_from": mood_from,
            "mood_to": mood_to,
            "favor_earned": round(float(self.state.get("favor_earned_today", 0) or 0), 1),
            "favor_spent": round(float(self.state.get("favor_spent_today", 0) or 0), 1),
            "late_event": le if isinstance(le, dict) else None,
        }

    def _growth_feed_names(self, day: str) -> list[str]:
        out = []
        for e in self._growth_day_events(day):
            if str(e.get("kind")) == "feed":
                q = str(e.get("quote") or "")
                if q and q not in out:
                    out.append(q)
        return out

    def _growth_settle(self, reason: str = "晚安", day: str | None = None,
                     day_no: int | None = None) -> dict | None:
        if not self._g_on():
            return None
        day = str(day or today_str())
        if str(self.state.get("last_settle_day") or "") == day:
            return None
        self.state["last_settle_day"] = day
        card = self._growth_build_card(day, day_no)
        cards = list(self.state.get("daily_cards") or [])
        cards.append(card)
        self.state["daily_cards"] = cards[-180:]
        self.state["last_settle"] = now_ts()
        # 卡上写的内容，她上下文里也得知道——不然面板有、她没记住，两边就对不上
        try:
            brief = "；".join(str(x) for x in (card.get("lines") or []) if str(x).strip())
            brief = brief[:80].rstrip("。；; ")
        except Exception:
            brief = ""
        keep = str(card.get("keepsake") or "")[:50].rstrip("。；; ")
        text = f"今天结算成一张卡：{card.get('headline')}"
        if brief:
            text += f"。写着：{brief}"
        if keep:
            text += f"。留了一句：{keep}"
        self._life(text, "card")
        self._save_state()
        logger.info("[鱼的窝] 今日卡生成 day=%s %s（%s）", card.get("day"), card.get("headline"), reason)
        return card

    def _pick_late_event(self) -> dict | None:
        used = set(str(x) for x in (self.state.get("late_events_used") or []))
        pool = [x for x in (self.late_events or []) if str(x.get("id")) not in used]
        if not pool:
            return None
        return dict(random.choice(pool))

    def _growth_check_breakthrough(self, delta: float) -> None:
        """当天好感度第一次涨到上限 -> 溢出一个深夜事件，而不是撞墙。"""
        if not self._g_on():
            return
        if self.state.get("favor_broke_through"):
            return
        cap = 15.0          # 每天最多涨 15（写死，设置里不再暴露）
        earned = float(self.state.get("favor_earned_today", 0) or 0)
        if earned < cap or float(delta) <= 0:
            return
        self.state["favor_broke_through"] = True
        if False:
            return
        item = self._pick_late_event()
        if not item:
            return
        lid = str(item.get("id") or "")
        used = list(self.state.get("late_events_used") or [])
        used.append(lid)
        self.state["late_events_used"] = used[-64:]
        self.state["late_event_today"] = {
            "id": lid, "tag": str(item.get("tag") or ""), "line": str(item.get("line") or ""),
            "ts": now_ts(),
        }
        line = str(item.get("line") or "")
        self._pending("late_night", f"她说：{line}")
        self.growth_record("night", topics=[str(item.get("tag") or "")], quote=line, source="breakthrough")
        self._life("好感度当天刷满，她说了句从没说过的话", "late_night")
        self._save_state()
        self._push_late_event(line)

    def _push_late_event(self, line: str) -> None:
        """把这句心里话真的推给你（复用主动发言那条通道）。"""
        if False:
            return
        umo = self._post_feed_umo()
        if not umo:
            return
        try:
            loop = asyncio.get_running_loop()
        except Exception:
            return
        try:
            loop.create_task(self._push_late_send(umo, line))
        except Exception as e:
            logger.warning("[鱼的窝] 深夜事件推送排程失败: %s", e)

    async def _push_late_send(self, umo: str, line: str) -> None:
        try:
            await asyncio.sleep(20.0)
            if str(self.state.get("late_event_sent") or "") == str(self.state.get("late_event_today", {}).get("id") or ""):
                return
            ok = await self.context.send_message(umo, MessageChain([Plain(f"……等一下，我跟你说个事。\n{line}")]))
            if ok is not False:
                self.state["late_event_sent"] = str((self.state.get("late_event_today") or {}).get("id") or "")
                self._save_state()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[鱼的窝] 深夜事件推送失败: %s", e)

    def _growth_today_context(self) -> str:
        """她送你东西时引用的「今天为什么」。"""
        ed = self._growth_today()
        feed = int(ed.get("feed", 0) or 0)
        chat = int(ed.get("chat", 0) or 0)
        gift = int(ed.get("gift", 0) or 0)
        if feed:
            return f"因为你今天喂了我 {feed} 次"
        if gift:
            return f"因为你今天给我带了东西"
        if chat >= 8:
            return f"因为你今天跟我说了 {chat} 句"
        if chat:
            return "因为你今天来找我说话了"
        return "因为今天什么都没发生，我想自己制造一点"

    def _food_price(self, f: dict) -> int:
        """永久吃过的东西打八折——每日归零不该把你退回起点。"""
        base = int(f.get("price", 0) or 0)
        if base <= 0:
            return 0
        if str(f.get("id")) in (self.state.get("unlocked_foods") or []):
            return max(1, int(round(base * 0.8)))
        return base

    def _growth_payload(self) -> dict:
        day = today_str()
        ed = self._growth_today()
        cards = [c for c in (self.state.get("daily_cards") or []) if isinstance(c, dict)]
        today_card = next((c for c in reversed(cards) if str(c.get("date")) == day), None)
        le = self.state.get("late_event_today")
        dw = self.state.get("daily_wish") or {}
        wdef = self._daily_wish_def() or {}
        foods = self.foods or []
        unlocked = self.state.get("unlocked_foods") or []
        return {
            "enabled": self._g_on(),
            "day": int(self.state.get("day_index", 1) or 1),
            "date": day,
            "cap": self._g_cap(),
            "favor": round(self._favor(), 1),
            "earned": round(float(self.state.get("favor_earned_today", 0) or 0), 1),
            "spent": round(float(self.state.get("favor_spent_today", 0) or 0), 1),
            "broke": bool(self.state.get("favor_broke_through")),
            "late_event": le if isinstance(le, dict) else None,
            "stats": {k: int(ed.get(k, 0) or 0) for k in ("chat", "feed", "gift", "care", "night", "conflict", "reverse")},
            "card_today": today_card,
            "cards_total": len(cards),
            "unlocked_foods": len(unlocked),
            "foods_total": len(foods),
            "next": "好感度会一直存着，聊得好就涨，凶她就会掉",
        }

    def _card_brief(self, card: dict | None) -> str:
        if not isinstance(card, dict):
            return "今天还没有卡。"
        body = "\n".join(f"· {x}" for x in (card.get("lines") or [])[:9])
        return f"第 {card.get('day')} 天 · {card.get('headline')}\n{body}\n{card.get('keepsake') or ''}"

    async def _web_growth_card(self, **kwargs) -> dict:
        try:
            day = str(kwargs.get("day") or "").strip()
            if not day:
                try:
                    day = str(web_request.query.get("day") or "").strip()
                except Exception:
                    day = ""
            cards = [c for c in (self.state.get("daily_cards") or []) if isinstance(c, dict)]
            card = None
            if day:
                card = next((c for c in cards if str(c.get("date")) == day or str(c.get("day")) == day), None)
            elif cards:
                card = cards[-1]
            return {"ok": True, "data": {
                "card": card,
                "list": [{"day": c.get("day"), "date": c.get("date"), "headline": c.get("headline")} for c in cards[-60:]],
            }}
        except Exception as e:
            logger.warning("[鱼的窝] 今日卡读取失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    async def _web_growth_settle(self, **kwargs) -> dict:
        try:
            card = self._growth_settle("手动")
            if card is None:
                return {"ok": True, "data": {"settled": False, "reason": "今天已经结算过，或者成长模块关着"}}
            return {"ok": True, "data": {"settled": True, "card": card}}
        except Exception as e:
            logger.warning("[鱼的窝] 手动结算失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}


    # ---------------- 她送给主人的礼物 ----------------

    def _reverse_tick(self) -> None:
        day = today_str()
        st = self.state.get("reverse") or {}
        if str(st.get("day") or "") != day:
            st = {"day": day, "count": 0, "items": []}
        items = list(st.get("items") or [])
        if int(st.get("count", 0) or 0) >= 2:
            self.state["reverse"] = st
            return
        last = float(items[-1].get("ts", 0) or 0) if items else 0.0
        if last and now_ts() - last < 10 * 3600:
            self.state["reverse"] = st
            return
        if self._g_on() and not self._test_mode():
            try:
                need = 40.0
            except Exception:
                need = 40.0
            if need and self._favor() < need:
                self.state["reverse"] = st
                return
            try:
                chance = 0.55
            except Exception:
                chance = 0.55
            if random.random() > chance:
                self.state["reverse"] = st
                return
        d = self._heart_drives()
        joy = float(d.get("joy", 50) or 50)
        stress = float(d.get("stress", 0) or 0)
        good = joy >= 55 and stress < 45
        pool = (self.reverse_gifts or {}).get("good" if good else "bad") or []
        if not pool:
            return
        gift = dict(random.choice(pool))
        gift["ts"] = now_ts()
        gift["good"] = bool(good)
        gift["context"] = self._growth_today_context()
        items.append(gift)
        st["items"] = items[-10:]
        st["count"] = int(st.get("count", 0) or 0) + 1
        self.state["reverse_total"] = int(self.state.get("reverse_total", 0) or 0) + 1
        if good:
            self.state["reverse_good"] = int(self.state.get("reverse_good", 0) or 0) + 1
        self.state["reverse"] = st
        self._life(
            f"你把自己准备的「{gift.get('name')}」送给了主人（{'心情不错' if good else '今天不太高兴'}）",
            "gift_out",
        )
        self.growth_record("reverse", quote=f"{gift.get('name')}｜{'心情不错' if good else '今天不太高兴'}", source="self")
        self._save_state()

    def _reverse_payload(self) -> dict:
        st = self.state.get("reverse") or {}
        items = [i for i in (st.get("items") or []) if isinstance(i, dict)]
        day = today_str()
        count = int(st.get("count", 0) or 0) if str(st.get("day") or "") == day else 0
        return {
            "today": count,
            "limit": 2,
            "items": items[-6:],
            "latest": items[-1] if items else None,
        }

    # ---------------- 闲时行为状态（网页端上报，聊天端按真实时长回答） ----------------

    ACTIVITY_LABELS = {"phone": "玩手机", "watch_tv": "看电视", "sit": "坐着发呆"}
    async def _web_activity(self, **kwargs) -> dict:
        """面板上报：她开始/停下某个持续行为。只有真的做了一段时间，聊天里才承认。"""
        try:
            try:
                body = await web_request.json(default={})
            except Exception:
                body = {}
            body = body if isinstance(body, dict) else {}
            act = str(body.get("act") or "")
            on = bool(body.get("on"))
            if act not in self.ACTIVITY_LABELS:
                return {"status": "error", "message": "不认识的行为", "data": {}}
            cur = self.state.get("activity") or {}
            if on:
                same = str(cur.get("name") or "") == act
                still = same and not float(cur.get("ended_ts", 0) or 0) and float(cur.get("since", 0) or 0)
                if not still:
                    # 新行为或上一段已结束：重新计时；同一段重复上报不重置时长
                    self.state["activity"] = {"name": act, "since": now_ts(), "ended_ts": 0.0}
                    self._save_state()
            elif str(cur.get("name") or "") == act and not float(cur.get("ended_ts", 0) or 0):
                cur["ended_ts"] = now_ts()
                self.state["activity"] = cur
                self._save_state()
            return {"ok": True, "data": self._activity_fact() or {}}
        except Exception as e:
            logger.warning("[鱼的窝] 行为上报异常: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    def _activity_fact(self) -> dict | None:
        """聊天端事实：后端引擎优先（手机/电视，离线也成立）；旧版前端上报作兜底。"""
        now = now_ts()
        amb = self._ambient_now(now)
        if amb.get("name"):
            mins = max(0, int(amb.get("mins", 0) or 0))
            st0 = self.state.get("ambient") or {}
            prev = st0.get("prev_idle") or {}
            # 刚拿起手机（<1分钟）且之前有场景：答之前的，不算"玩手机"
            if amb["name"] == "phone" and mins < 1 and prev.get("name"):
                plabel = self.ACTIVITY_LABELS.get(str(prev["name"]), str(prev["name"]))
                pmins = max(0, int((min(now, float(prev.get("until", 0) or 0)) - float(prev.get("since", 0) or 0)) // 60))
                return {"name": str(prev["name"]), "label": plabel, "mins": pmins,
                        "active": False, "just_stopped": False, "replying": True}
            if amb["name"] == "phone" and mins < 1:
                return {"name": "phone", "label": "玩手机", "mins": 0,
                        "active": True, "just_stopped": False, "replying": True}
            return {"name": amb["name"], "label": self.ACTIVITY_LABELS.get(str(amb["name"]), str(amb["name"])),
                    "mins": mins, "active": True, "just_stopped": False}
        st = self.state.get("ambient") or {}
        since = float(st.get("phone_since", 0) or 0)
        until = float(st.get("phone_until", 0) or 0)
        if since and until and 0 <= now - until <= 120:
            return {"name": "phone", "label": "玩手机", "mins": int((until - since) // 60),
                    "active": False, "just_stopped": True}
        iname = str(st.get("idle_name") or "")
        iuntil = float(st.get("idle_until", 0) or 0)
        isince = float(st.get("idle_since", 0) or 0)
        if iname and iuntil and 0 <= now - iuntil <= 120:
            return {"name": iname, "label": self.ACTIVITY_LABELS.get(iname, iname),
                    "mins": int((iuntil - isince) // 60), "active": False, "just_stopped": True}
        cur = self.state.get("activity") or {}
        name = str(cur.get("name") or "")
        if name not in self.ACTIVITY_LABELS:
            return None
        since = float(cur.get("since", 0) or 0)
        ended = float(cur.get("ended_ts", 0) or 0)
        now = now_ts()
        if ended and now - ended > 120:
            self.state["activity"] = {}
            return None
        mins = int(max(0.0, (ended or now) - since) // 60)
        return {"name": name, "label": self.ACTIVITY_LABELS[name],
                "mins": mins, "active": not ended, "just_stopped": bool(ended)}

    def _activity_fact_line(self, act: dict) -> str:
        mins = int(act.get("mins", 0) or 0)
        label = str(act.get("label") or "")
        if act.get("replying"):
            if str(act.get("name") or "") != "phone":
                if mins >= 1:
                    return f"你刚才{label} {mins} 分钟，现在拿起手机在回对方消息。被问在干嘛两个都要说：'刚{label}，现在在回你消息。'"
                return f"你刚{label}，现在正拿起手机回对方消息。被问在干嘛就说在回消息。"
            if mins < 1:
                return "你刚拿起手机在回对方消息。被问在干嘛就说在回消息，别把回消息说成玩手机。"
            return f"你拿起手机已经 {mins} 分钟了一直在回消息。被问在干嘛照实说：在跟他聊天。"
        if act.get("just_stopped"):
            if mins < 1:
                return f"你刚准备{label}，对方就来了，你把东西收了。被问起就说没干嘛。"
            return f"你刚才{label} {mins} 分钟，因为对方来了才放下。被问起要承认，可以说'刚'在{label}。"
        if mins < 1:
            return f"你刚开始{label}不到一分钟。被问在干嘛就说在发呆，别承认{label}。"
        if mins < 5:
            return f"你现在正在{label}，已经 {mins} 分钟了。被问在干嘛要照实说。"
        return f"你已经{label} {mins} 分钟了。被问在干嘛照实说，还可以抱怨一句眼睛酸。"

    # ---------------- 线索（signals）：抽取 / 存储 / 谓词 ----------------

    _SENT_SPLIT = re.compile(r"[。！？!?;；\n]+")

    def _extract_signals(self, text: str) -> list[dict]:
        """一条消息最多抽 3 个 tag；evidence 存命中关键词的那个分句原话。"""
        if not text:
            return []
        out: list[dict] = []
        parts = [p.strip() for p in self._SENT_SPLIT.split(text) if p.strip()]
        for tag, kws in SIGNAL_TAGS.items():
            for p in parts:
                hit = next((k for k in kws if k in p), None)
                if hit:
                    out.append({"tag": tag, "evidence": p[:60]})
                    break
            if len(out) >= 3:
                break
        return out

    def _record_signals(self, text: str, now: float) -> list[str]:
        """存进 state.signals：14 天内带原句，14~90 天只留结构化。返回本次新增 tag。"""
        if not bool(self._cfg("signals.enabled", True)):
            return []
        found = self._extract_signals(text)
        if not found:
            return []
        rows = list(self.state.get("signals") or [])
        tags = []
        for f in found:
            rows.append({"ts": now, "tag": f["tag"], "evidence": f["evidence"]})
            tags.append(f["tag"])
        # 修剪：14 天外的丢原句，90 天外整条丢
        cut_full, cut_raw = now - 14 * 86400, now - 90 * 86400
        trimmed = []
        for r in rows:
            ts = float(r.get("ts", 0) or 0)
            if ts < cut_raw:
                continue
            if ts < cut_full:
                r = {"ts": ts, "tag": str(r.get("tag") or "")}
            trimmed.append(r)
        self.state["signals"] = trimmed[-600:]
        return tags

    def _signal_stats(self) -> dict:
        """给 cond 引擎用的谓词：sig_<tag>_7d 计数 + 危险组合计数。"""
        now = now_ts()
        start7 = now - 7 * 86400
        counts = {f"sig_{t}_7d": 0 for t in SIGNAL_TAGS}
        danger = 0
        for r in (self.state.get("signals") or []):
            if not isinstance(r, dict):
                continue
            ts = float(r.get("ts", 0) or 0)
            if ts < start7:
                continue
            tag = str(r.get("tag") or "")
            key = f"sig_{tag}_7d"
            if key in counts:
                counts[key] += 1
            if tag in DANGER_TAGS:
                danger += 1
        counts["sig_danger_7d"] = danger
        return counts

    def _recent_signal_evidence(self, tag: str, days: int = 3) -> dict | None:
        """取最近 N 天该 tag 的一条原句（越近越优先）。"""
        start = now_ts() - days * 86400
        for r in reversed(self.state.get("signals") or []):
            if isinstance(r, dict) and str(r.get("tag") or "") == tag and float(r.get("ts", 0) or 0) >= start:
                ev = str(r.get("evidence") or "").strip()
                if ev:
                    return {"tag": tag, "evidence": ev, "ts": float(r.get("ts", 0) or 0)}
        return None

    # ---------------- 闲时行为引擎（后端真相源：手机/电视） ----------------

    def _tv_window(self) -> tuple[int, int]:
        """看电视时段：优先跟 B 站 watch_log 的真实小时分布（近 14 天命中最多的小时），
        取覆盖 ≥60% 观看记录的连续区间；没数据回落配置（默认 20-23）。"""
        try:
            start = int(self._cfg("activity.tv_start", 20) or 20)
            end = int(self._cfg("activity.tv_end", 23) or 23)
        except Exception:
            start, end = 20, 23
        try:
            base = str(StarTools.get_data_dir(BILI_PLUGIN_NAME))
        except Exception:
            base = ""
        if not base or not bool(self._cfg("activity.tv_follow_bili", True)):
            return start, end
        path = os.path.join(base, "watch_log.json")
        if not os.path.exists(path):
            return start, end
        try:
            with open(path, "r", encoding="utf-8") as f:
                log = json.load(f)
        except Exception:
            return start, end
        cutoff = time.time() - 14 * 86400
        hours: dict[int, int] = {}
        for x in log if isinstance(log, list) else []:
            if not isinstance(x, dict):
                continue
            ts = x.get("watched_at") or x.get("time") or ""
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except Exception:
                continue
            if dt.timestamp() < cutoff:
                continue
            hours[dt.hour] = hours.get(dt.hour, 0) + 1
        if not hours:
            return start, end
        total = sum(hours.values())
        # 找覆盖 ≥60% 记录的最短连续小时区间（跨午夜允许；覆盖同则取更短）
        best = (start, end)
        best_score = (-1.0, 99)
        for h0 in range(24):
            cov = 0
            for k in range(13):
                hh = (h0 + k) % 24
                cov += hours.get(hh, 0)
                if cov / total >= 0.6:
                    score = (cov / total, -(k + 1))
                    if score > best_score:
                        best_score = score
                        best = (h0, hh)
                    break
        return best

    def _in_tv_window(self, now: float) -> bool:
        s, e = self._tv_window()
        h = datetime.fromtimestamp(now).hour
        return (s <= h <= e) if s <= e else (h >= s or h <= e)

    def _ambient_msg_touch(self, now: float) -> None:
        """收到主人消息：拿起手机回（续 60 秒）；连续持有超 30 分钟强制落一次。
        拿起前先记下"之前她在干嘛"——回消息≠玩手机，被问要答前者。"""
        try:
            hold = int(self._cfg("activity.phone_hold_seconds", 60) or 60)
            cap = int(self._cfg("activity.phone_max_minutes", 30) or 30) * 60
        except Exception:
            hold, cap = 60, 1800
        amb = self.state.setdefault("ambient", {"phone_since": 0.0, "phone_until": 0.0})
        # 掷一下场景骰（推进账本），再记录拿起手机前的场景
        self._ambient_idle_roll(now)
        idle_name = str(amb.get("idle_name") or "")
        idle_until = float(amb.get("idle_until", 0) or 0)
        if idle_name and now < idle_until:
            amb["prev_idle"] = {"name": idle_name,
                                "since": float(amb.get("idle_since", 0) or 0),
                                "until": idle_until}
        else:
            amb["prev_idle"] = None
        since = float(amb.get("phone_since", 0) or 0)
        if since and now - since > cap:
            since = 0.0
        amb["phone_since"] = since or now
        amb["phone_until"] = now + hold
        self.state["ambient"] = amb

    def _ambient_idle_roll(self, now: float) -> None:
        """闲置场景骰子（后端）：没手机消息、不在电视时段时，她也可能自己选择玩手机/看电视/坐着发呆。
        权重即频率；深夜手机/电视加权；掷空=站着（前端微动作接管）。"""
        if not bool(self._cfg("activity.idle_enabled", True)):
            return
        amb = self.state.get("ambient") or {}
        if now < float(amb.get("idle_next_at", 0) or 0):
            return
        try:
            w_phone = float(self._cfg("activity.idle_phone_weight", 18) or 18)
            w_tv = float(self._cfg("activity.idle_tv_weight", 12) or 12)
            w_sit = float(self._cfg("activity.idle_sit_weight", 6) or 6)
            boost = float(self._cfg("activity.idle_night_boost", 2.2) or 2.2)
            scene_s = max(60.0, float(self._cfg("activity.idle_scene_minutes", 4) or 4) * 60)
            gap = max(10.0, float(self._cfg("activity.idle_gap_seconds", 45) or 45))
        except Exception:
            w_phone, w_tv, w_sit, boost = 18.0, 12.0, 6.0, 2.2
            scene_s, gap = 240.0, 45.0
        hour = datetime.fromtimestamp(now).hour
        night = hour >= 22 or hour < 6
        if night:
            w_phone *= boost
            w_tv *= boost
        none_w = float(self._cfg("activity.idle_none_weight", 30) or 30)
        total = w_phone + w_tv + w_sit + none_w
        r = random.uniform(0, total)
        if r < w_phone:
            name, dur = "phone", scene_s
        elif r < w_phone + w_tv:
            name, dur = "watch_tv", scene_s
        elif r < w_phone + w_tv + w_sit:
            name, dur = "sit", min(90.0, scene_s)
        else:
            name, dur = "", 60.0
        amb["idle_name"] = name
        amb["idle_since"] = now
        amb["idle_until"] = now + dur
        amb["idle_next_at"] = now + dur + gap
        self.state["ambient"] = amb

    def _ambient_now(self, now: float) -> dict:
        """此刻她在干嘛（后端算，前端只播）：手机消息会话 > 电视作息 > 闲置场景骰 > 空。"""
        if not bool(self._cfg("activity.enabled", True)):
            return {"name": "", "since": 0.0, "mins": 0, "engine": False}
        amb = self.state.get("ambient") or {}
        since = float(amb.get("phone_since", 0) or 0)
        until = float(amb.get("phone_until", 0) or 0)
        if since and now < until:
            return {"name": "phone", "since": since, "mins": int((now - since) // 60), "engine": True}
        if self._in_tv_window(now):
            s, e = self._tv_window()
            local = datetime.fromtimestamp(now)
            tv_since = local.replace(hour=s % 24, minute=0, second=0, microsecond=0).timestamp()
            if s > e and local.hour <= e:      # 跨午夜且现在是凌晨段：窗口从昨晚开始
                tv_since -= 86400
            return {"name": "watch_tv", "since": tv_since, "mins": max(0, int((now - tv_since) // 60)), "engine": True}
        self._ambient_idle_roll(now)
        amb = self.state.get("ambient") or {}
        iname = str(amb.get("idle_name") or "")
        iuntil = float(amb.get("idle_until", 0) or 0)
        if iname and now < iuntil:
            return {"name": iname, "since": float(amb.get("idle_since", 0) or 0),
                    "mins": int((now - float(amb.get("idle_since", 0) or 0)) // 60), "engine": True}
        return {"name": "", "since": 0.0, "mins": 0, "engine": True}

    async def _web_activity_get(self, **kwargs) -> dict:
        try:
            return {"ok": True, "data": self._ambient_now(now_ts())}
        except Exception as e:
            return {"status": "error", "message": str(e), "data": {}}

    async def _web_typing(self, **kwargs) -> dict:
        """她是不是正在回话——只有这时候才该冒三点气泡。"""
        try:
            ts = float(self.state.get("typing_ts", 0) or 0)
            alive = bool(ts) and (now_ts() - ts) < 120
            return {"ok": True, "data": {"typing": alive, "ts": ts}}
        except Exception as e:
            return {"status": "error", "message": str(e), "data": {}}

    # ---------------- 信箱 ----------------

    def _letter_rows(self) -> list[dict]:
        rows = []
        for i, x in enumerate([r for r in (self.state.get("letters") or []) if isinstance(r, dict)]):
            ts = float(x.get("ts", 0) or 0)
            dt = datetime.fromtimestamp(ts) if ts else None
            rows.append({
                "id": str(x.get("id") or f"L{i}"),
                "ts": ts,
                "date": dt.strftime("%Y-%m-%d") if dt else "",
                "clock": dt.strftime("%H:%M") if dt else "",
                "month": dt.strftime("%Y-%m") if dt else "",
                "kind": str(x.get("kind") or "regular"),
                "title": "加急信" if str(x.get("kind")) == "urgent" else "写给你的信",
                "body": str(x.get("body") or ""),
                "refs": [str(r)[:40] for r in (x.get("refs") or [])][:6],
                "read": bool(x.get("read")),
            })
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return rows

    async def _web_letters(self, **kwargs) -> dict:
        try:
            rows = self._letter_rows()
            return {"ok": True, "data": {
                "letters": rows,
                "unread": len([r for r in rows if not r["read"]]),
                "cost": 20,
                "balance": self._balance(),
                "week_count": int(self.state.get("letter_week_count", 0) or 0),
                "week_cap": 2,
            }}
        except Exception as e:
            return {"status": "error", "message": str(e), "data": {}}

    async def _web_letter_read(self, **kwargs) -> dict:
        try:
            try:
                body = await web_request.json(default={})
            except Exception:
                body = {}
            body = body if isinstance(body, dict) else {}
            lid = str(body.get("id") or "")
            rows = [r for r in (self.state.get("letters") or []) if isinstance(r, dict)]
            if body.get("all"):
                for r in rows:
                    r["read"] = True
            elif lid:
                for r in rows:
                    if str(r.get("id")) == lid:
                        r["read"] = True
            self.state["letters"] = rows
            self._save_state()
            return {"ok": True, "data": {"unread": len([r for r in rows if not r.get("read")])}}
        except Exception as e:
            return {"status": "error", "message": str(e), "data": {}}

    async def _web_letter_write(self, **kwargs) -> dict:
        """花余额要一封加急信：走同一套写信流程，只是不等每周那三天的间隔。"""
        try:
            cost = 20
            ok, why = self._letters_gate(True)
            if not ok:
                msg = {
                    "disabled": "写信关掉了",
                    "shut": "她现在不想写字",
                    "week_full": "这周已经写过两封了，让她缓缓",
                }.get(why, "现在写不了")
                return {"status": "error", "message": msg, "data": {}}
            if not self._balance_can(cost):
                return {"status": "error",
                        "message": f"钱不够，要 {cost}，你现在有 {self._balance()}", "data": {}}
            self._balance_add(-cost, "加急一封信")
            await self._letter_send("urgent")
            return {"ok": True, "data": {"balance": self._balance(), "cost": cost,
                                         "letters": self._letter_rows()[:20]}}
        except Exception as e:
            logger.warning("[鱼的窝] 加急写信失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    # ---------------- 三档脸色：投影自鱼的心（不新建数值） ----------------

    def _band(self) -> str:
        """normal / sulk / shut——直接读心的状态；strictness 只影响"多快算生气"。"""
        snap = self._heart_snapshot()
        mode = str(snap.get("mode") or "calm")
        blame = float(snap.get("blame", snap.get("blame_pressure", 0)) or 0)
        strict = "soft"          # 固定用 soft：只有真委屈才会赌气回避
        enter, exit_ = (70.0, 50.0) if strict == "soft" else (45.0, 35.0)
        if mode == "shut" or (strict == "normal" and blame >= 55):
            result = "shut"
        elif mode in ("wronged", "afraid") or blame >= enter:
            # 赌气：心的委屈/害怕态直接算；或 blame 越过进入线（soft 70 / normal 45）
            result = "sulk"
        elif strict == "normal" and blame >= exit_:
            result = "sulk"
        else:
            prev = str((self.state.get("band_state") or {}).get("name") or "normal")
            held = prev == "sulk" and blame >= exit_
            result = "sulk" if (blame >= enter or held) else "normal"
        if (self.state.get("band_state") or {}).get("name") != result:
            self.state["band_state"] = {"name": result, "since": now_ts()}
        return result

    BAND_REJECT_LINES = (
        "她没开门。",
        "门里面没声音。",
        "她把你递进来的东西从门缝推回来了。",
    )

    def _record_reject(self, what: str) -> None:
        """回避期拒收：不当没发生——记一笔，下封信素材。"""
        self._life(f"主人想给「{what}」，她没接。", "reject")
        beats = list(self.state.get("pending_letter_beats") or [])
        mark = f"你给「{what}」那天，我没接"
        if mark not in beats:
            beats.append(mark)
        self.state["pending_letter_beats"] = beats[-8:]
        self.growth_record("conflict", quote=f"回避期拒收：{what}", source="self")
        self._save_state()

    # ---------------- 台阶（已下线，只留一份空数据给前端） ----------------

    def _steps_view(self) -> dict:
        # 台阶功能下线：好感度只能靠聊天回暖，面板不再给捷径
        return {"available": False, "used": 0, "left": 0, "cooldown_min": 0, "kinds": []}

    def _diy_view(self) -> dict:
        return {"enabled": bool(self._cfg("diy_feed.enabled", True)),
                "used_today": str(self.state.get("diy_day") or "") == today_str(),
                "last": list(self.state.get("diy_log") or [])[-1:]}

    # ---------------- DIY 点单：食物你写，分数你也填（一天一次，不花钱） ----------------

    DIY_TASTES = ("辣", "酸", "苦", "甜", "咸", "烫", "凉", "冰", "腥", "腻", "软", "硬", "焦")

    async def _web_diy_feed(self, **kwargs) -> dict:
        try:
            if not bool(self._cfg("diy_feed.enabled", True)):
                return {"status": "error", "message": "DIY 点单已关掉", "data": {}}
            try:
                body = await web_request.json(default={})
            except Exception:
                body = {}
            body = body if isinstance(body, dict) else {}
            text = str(body.get("text") or "").strip()[:60]
            if not text:
                return {"status": "error", "message": "总得写一句这是什么吧", "data": {}}
            try:
                score = float(body.get("score", 0) or 0)
            except Exception:
                score = 0.0
            score = max(-10.0, min(20.0, score))
            day = today_str()
            if str(self.state.get("diy_day") or "") == day and not self._test_mode():
                return {"status": "error", "message": "今天已经做过一顿饭了。明天再来。", "data": {}}
            band = self._band()
            if band == "shut":
                self._record_reject(text)
                return {"status": "error", "message": random.choice(self.BAND_REJECT_LINES), "data": {}}
            tastes = [t for t in self.DIY_TASTES if t in text]
            self.state["diy_day"] = day
            logs = list(self.state.get("diy_log") or [])
            taste = dict(self.state.get("taste") or {})
            bonus = sum(float(taste.get(t, 0) or 0) for t in tastes)
            adj = score + max(-2.0, min(2.0, bonus * 0.5))
            logs.append({"ts": now_ts(), "raw": text, "tastes": tastes, "score": score,
                         "adj": round(adj, 1), "react": "", "icon": ""})
            for t in tastes:
                taste[t] = round(max(-5.0, min(5.0, float(taste.get(t, 0) or 0) + (0.5 if score > 0 else -0.5))), 2)
            self.state["taste"] = taste
            self.state["diy_log"] = logs[-60:]
            # 打分不再动好感度：随机加减一点工资（先这么放着）
            swing = round(random.uniform(-1.0, 1.0) * (2.0 + min(20.0, abs(score)) * 0.3), 1)
            self._wage_bonus_add(swing)
            logs[-1]["wage"] = swing
            self.state["diy_log"] = logs[-60:]
            self._life(f"你亲手做了「{text[:20]}」，工资 {swing:+.1f}", "diy")
            self._apply_radar({"joy": 0.2, "intimacy": 0.1} if score >= 0
                              else {"stress": 0.6, "joy": -0.3})
            if score <= -10 and band == "sulk":
                inst = self._heart_inst()
                try:
                    if inst is not None:
                        hs = inst.state
                        hs["blame_pressure"] = min(100.0, float(hs.get("blame_pressure", 0) or 0) + 25.0)
                        bl = hs.setdefault("blame_log", [])
                        bl.append({"ts": now_ts(), "kind": "diy", "text": text[:40]})
                        hs["blame_log"] = bl[-30:]
                        if hasattr(inst, "_save_state"):
                            inst._save_state()
                except Exception:
                    pass
            fed = list(self.state.get("fed_kinds") or [])
            self.state["fed_total"] = int(self.state.get("fed_total", 0) or 0) + 1
            self.growth_record("feed", quote=text[:40], source="diy")
            self._check_achievements()
            self._save_state()
            if score >= 15:
                line = random.choice([
                    f"唔……{'很' if tastes else ''}{tastes[0] if tastes else '有点东西'}。但是好吃。下次还做。",
                    "你居然会做这个。……第二碗的事，我再想想。",
                ])
            elif score >= 0:
                line = random.choice([
                    f"吃过了。{'、'.join(tastes[:2]) if tastes else '说不上来'}的味道。",
                    "嗯。味道很怪，但我吃完了。你自己尝过没有。",
                ])
            else:
                line = random.choice([
                    f"你确定这是给人吃的？{'、'.join(tastes[:2]) if tastes else '这个味道'}太重了。",
                    "我吃了。数字你说了算，态度我说了算。",
                ])
            if tastes and band == "sulk":
                line += "……你说是就是吧。"
            self._post_feed(text[:20], "food", score)
            # 好吃/难吃 -> 表情动画（前端按 react 播）
            if score >= 8:
                react = "delight"
            elif score > 0:
                react = "eat"
            elif score == 0:
                react = "neutral"
            elif score > -8:
                react = "disgust"
            else:
                react = "angry"
            return {"ok": True, "data": {"line": line, "score": score, "tastes": tastes, "react": react}}
        except Exception as e:
            logger.warning("[鱼的窝] DIY 点单异常: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    # ---------------- 她自己想的成就：AI 提条件，引擎判达成 ----------------

    NOTE_TEMPLATES = {
        "work_late": ("她记住了那些加班的晚上", "你说过{count}次加班。有一次太晚了，她第一次没催你回消息。"),
        "insomnia": ("凌晨的份数", "你{count}次睡不着。那些小时她都替你数着。"),
        "tired": ("他说累的时候", "你{count}次说累。她学会了自己闹，少吵你一点。"),
        "sick": ("身体不舒服名单", "你{count}次说不舒服。下次她先把水放到手边。"),
        "fight": ("那些不痛快", "你{count}次提到吵架。她庆幸这些事你还会跟她讲。"),
        "exam": ("考试周", "你{count}次说起考试。她帮不上，就陪着。"),
        "stay_up": ("熬夜对账", "你{count}次熬夜。她假装没数，其实记了。"),
        "no_meal": ("忘了吃饭的人", "你{count}次没吃饭。她把自己那份省下来过（并没有）。"),
    }

    def _manual_report_text(self) -> str:
        """第 31 天她自己写的那一页：全部由现有统计拼出来，不调模型，也不告诉她叫什么。"""
        s = self._stats()
        days = int(self.state.get("day_index", 1) or 1)
        bits = [
            f"认识第 {days} 天。",
            f"他一共说了 {int(s.get('chat_total', 0) or 0)} 句，今天 {int(s.get('chat_today', 0) or 0)} 句，"
            f"连着 {int(s.get('streak', 0) or 0)} 天。",
            f"喂过我 {int(self.state.get('fed_total', 0) or 0)} 次。",
            f"我们一起熬过 {int(self.state.get('late_nights', 0) or 0)} 个深夜。",
            f"我给他写过 {len([x for x in (self.state.get('letters') or []) if isinstance(x, dict)])} 封信。",
        ]
        quotes = [str(r.get("quote") or "") for r in (self.state.get("events") or [])
                  if isinstance(r, dict) and str(r.get("quote") or "").strip()]
        if quotes:
            bits.append(f"他最常说的一句是「{quotes[-1][:24]}」。")
        if float(self._favor()) < 10:
            bits.append("现在我还在生气。但这一页我写完了。")
        else:
            bits.append("这一页不是给谁看的。写下来，我就知道自己没白待着。")
        return "\n".join(bits)

    def _check_ai_notes(self) -> None:
        """达成判定永远归引擎；生成每周最多 1 个，条件必须能从现有统计算出来。"""
        now = now_ts()
        ai = self.state.setdefault("ai_achv", [])
        stats = self._stats()
        changed = False
        # 第 31 天：她自己多写一页（不占她那三个框，也不显示名字）
        if int(self.state.get("day_index", 1) or 1) >= 31 and not self.state.get("manual_report_ts"):
            self.state["manual_report_ts"] = now
            ai.append({
                "id": "manual", "tag": "manual", "title": "……",
                "why": self._manual_report_text(),
                "evidence": "", "cond": {}, "made_ts": now, "done_ts": now,
                "hidden": True,
            })
            self._life("她在本子上多写了一页，没给人看。", "ai_note")
            changed = True
        for e in ai:
            if not isinstance(e, dict) or e.get("done_ts"):
                continue
            cond = e.get("cond") or {}
            if cond and self._eval_cond(cond, stats):
                e["done_ts"] = now
                self._wish_gain(6.0, f"她自己的成就：{e.get('title')}")
                self._life(f"她自己想出来的成就达成了：「{e.get('title')}」。", "ai_note")
                changed = True
        target = 3
        if len(ai) >= 3 and all(isinstance(e, dict) and e.get("done_ts") for e in ai):
            target = min(6, len(ai) + 1)
        last_gen = float(self.state.get("ai_achv_last_gen", 0) or 0)
        if len(ai) < target and now - last_gen >= 7 * 86400 and bool(self._cfg("signals.enabled", True)):
            used = {str(e.get("tag")) for e in ai if isinstance(e, dict)}
            best = None
            for tag, (_title) in self.NOTE_TEMPLATES.items():
                if tag in used:
                    continue
                cnt = int(stats.get(f"sig_{tag}_7d", 0) or 0)
                if cnt >= 2 and (best is None or cnt > best[1]):
                    best = (tag, cnt)
            if best:
                tag, cnt = best
                title, why_tpl = self.NOTE_TEMPLATES[tag]
                ev = self._recent_signal_evidence(tag, days=14)
                ai.append({
                    "id": f"ai_{tag}", "tag": tag, "title": title,
                    "why": why_tpl.replace("{count}", str(cnt)),
                    "evidence": (ev or {}).get("evidence", ""),
                    "cond": {"stat": f"sig_{tag}_7d", "op": ">=", "value": min(5, cnt + 1)},
                    "made_ts": now, "done_ts": 0,
                })
                self.state["ai_achv"] = ai[-6:]
                self.state["ai_achv_last_gen"] = now
                changed = True
                logger.info("[鱼的窝] 她自己想了个成就: %s", title)
        if changed:
            self._save_state()

    async def _web_notes(self, **kwargs) -> dict:
        try:
            self._check_ai_notes()
            ai = list(self.state.get("ai_achv") or [])
            items = []
            for e in ai:
                if not isinstance(e, dict):
                    continue
                cond = e.get("cond") or {}
                done = bool(e.get("done_ts"))
                prog = 1.0 if done else self._condition_progress(cond, self._stats())
                items.append({
                    "id": str(e.get("id") or ""), "title": str(e.get("title") or ""),
                    "why": str(e.get("why") or ""), "evidence": str(e.get("evidence") or ""),
                    "done": done, "done_ts": float(e.get("done_ts", 0) or 0),
                    "progress": round(float(prog), 4),
                    "hidden": bool(e.get("hidden")),
                })
            return {"ok": True, "data": {"items": items, "slots": len(items)}}
        except Exception as e:
            logger.warning("[鱼的窝] 自成就读取失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    # ---------------- 手写信：三天一封、一周两封、加急占名额 ----------------

    def _letter_week_key(self) -> str:
        try:
            return time.strftime("%G-W%V")
        except Exception:
            return today_str()

    def _letters_gate(self, urgent: bool) -> tuple[bool, str]:
        """能不能写。加急信同样消耗本周名额——不然「一周最多两封」就是空话。"""
        if False:
            return False, "disabled"
        if str(self._heart_snapshot().get("mode") or "") == "shut":
            return False, "shut"
        wk = self._letter_week_key()
        if str(self.state.get("letter_week_key") or "") != wk:
            self.state["letter_week_key"] = wk
            self.state["letter_week_count"] = 0
        cap = 2
        if int(self.state.get("letter_week_count", 0) or 0) >= cap:
            return False, "week_full"
        try:
            interval = 3 * 86400.0
        except Exception:
            interval = 3 * 86400
        last = float(self.state.get("letter_last_ts", 0) or 0)
        if not urgent and now_ts() - last < interval:
            return False, "too_early"
        return True, "urgent" if urgent else "regular"

    def _letter_material(self) -> dict:
        rows = [r for r in (self.state.get("signals") or [])
                if isinstance(r, dict) and str(r.get("evidence") or "").strip()]
        return {
            "quotes": [str(r.get("evidence") or "")[:40] for r in rows[-8:]],
            "tags": [str(r.get("tag") or "") for r in rows[-8:]],
            "beats": [str(b) for b in (self.state.get("pending_letter_beats") or []) if str(b).strip()],
            "day_index": int(self.state.get("day_index", 1) or 1),
            "streak": self._streak(),
        }

    def _letter_system(self, kind: str, mat: dict) -> str:
        name = str(self._cfg("panel.character_name", "她") or "她")
        pname, pblock = self._persona_block()      # 跟主动说话用同一个来源：完整人设正文
        persona = pblock or pname or ""
        why = "他今天情绪很不好，这封是加急的，先接住人。" if kind == "urgent" else "到了该写信的日子，这封是从上封信之后攒下来的事。"
        quotes = "、".join(f"「{q}」" for q in (mat.get("quotes") or [])[-6:]) or "（最近没记下什么原话，就写你自己在意的小事）"
        beats = "、".join(f"「{b}」" for b in (mat.get("beats") or [])) or "无"
        who_line = (
            f"你的人设是：{persona}\n" if persona
            else "（人设按你平时说话的样子来，不要另外编性格）\n"
        )
        return (
            f"你是{name}。你在给你的主人写一封手写信。{why}\n"
            + who_line
            + "自称、口吻、对他的称呼都必须跟上面的人设完全一致——"
              "人设里自称「本王」就写本王，自称「老娘」就写老娘，不许统一改成「我」；"
              "人设怎么称呼对方就怎么称呼，不许改成「你/主人」以外的写法。\n"
            f"素材——他最近真实说过的话（至少引用一处，别编造他没说过的事）：{quotes}\n"
            f"之前名额满了、攒着没写进去的事：{beats}\n"
            f"你们认识第 {mat.get('day_index', 1)} 天，今天 {today_str()}。\n"
            "要求：像手写的，短而真，40~180 字；可以只讲一件小事，可以抱怨，可以嘴硬；"
            "不许提系统/插件/面板/AI；结尾单独一行落款「——{n}」。".replace("{n}", name)
        )

    def _letter_fallback(self, kind: str, mat: dict) -> str:
        name = str(self._cfg("panel.character_name", "她") or "她")
        ev = (mat.get("quotes") or [""])[-1] if mat.get("quotes") else ""
        beat = (mat.get("beats") or [""])[0]
        body = []
        if ev:
            body.append(f"听到你说「{ev}」。我没什么大本事，能做的就是把这句话记住。")
        elif beat:
            body.append(f"有件事攒了一阵了：「{beat}」。今天一起写给你。")
        else:
            body.append(f"没什么大事。就是到了该给你写信的日子。")
        body.append(f"认识第 {mat.get('day_index', 1)} 天了。记得吃饭，别熬。")
        return "\n".join(body) + f"\n——{name}"

    async def _letter_compose(self, kind: str, mat: dict) -> str:
        if not bool(self._cfg("post_feed.use_llm", True)):
            return self._letter_fallback(kind, mat)
        try:
            umo = self._post_feed_umo()
            provider = self.context.get_using_provider(umo) if umo else None
            if provider is None:
                raise RuntimeError("no provider")
            resp = await provider.text_chat(prompt="（写这封信）", system_prompt=self._letter_system(kind, mat))
            text = (getattr(resp, "completion_text", "") or "").strip()
            text = text.strip("\"'“”")
            if 20 <= len(text) <= 420:
                return text
        except Exception as e:
            logger.warning("[鱼的窝] 写信失败，用模板兜底: %s", e)
        return self._letter_fallback(kind, mat)

    async def _letter_send(self, kind: str) -> None:
        try:
            mat = self._letter_material()
            body = await self._letter_compose(kind, mat)
            now = now_ts()
            self.state["letter_last_ts"] = now
            wk = self._letter_week_key()
            if str(self.state.get("letter_week_key") or "") != wk:
                self.state["letter_week_key"] = wk
                self.state["letter_week_count"] = 0
            self.state["letter_week_count"] = int(self.state.get("letter_week_count", 0) or 0) + 1
            letters = list(self.state.get("letters") or [])
            letters.append({"ts": now, "kind": kind, "body": body, "refs": mat.get("quotes") or [],
                            "read": False, "id": f"L{int(now)}"})
            self.state["letters"] = letters[-60:]
            self.state["pending_letter_beats"] = []
            self._life(f"她写了一封信（{kind}）。", "letter")
            self.growth_record("care", quote=f"手写信（{'加急' if kind == 'urgent' else '例行'}）", source="self")
            self._save_state()
            umo = self._post_feed_umo()
            if umo:
                head = "……给你写了封信：\n" if kind == "urgent" else "给你写了封信：\n"
                await self.context.send_message(umo, MessageChain([Plain(head + body)]))
        except Exception as e:
            logger.warning("[鱼的窝] 寄信失败: %s", e)

    def _letters_check(self, now: float, urgent_tags: list[str] | None = None) -> None:
        """例行在跨天结算里查；加急由危险信号直接触发。名额满了的加急：口头接住 + 攒进下封。"""
        if not float(self.state.get("letter_last_ts", 0) or 0):
            # 第一次见到这封制度：静默起表，不从零秒直接寄信
            self.state["letter_last_ts"] = now
            self._save_state()
            return
        urgent = bool(urgent_tags)
        ok, why = self._letters_gate(urgent)
        if ok:
            try:
                asyncio.create_task(self._letter_send(why))
            except Exception as e:
                logger.warning("[鱼的窝] 起信失败: %s", e)
            return
        if urgent and why == "week_full":
            beats = list(self.state.get("pending_letter_beats") or [])
            for tag in urgent_tags or []:
                hit = self._recent_signal_evidence(tag, days=2)
                if hit and hit["evidence"] not in beats:
                    beats.append(hit["evidence"][:40])
            self.state["pending_letter_beats"] = beats[-8:]
            self._save_state()
            try:
                asyncio.create_task(self._letter_soft_reply())
            except Exception:
                pass

    async def _letter_soft_reply(self) -> None:
        """名额满了的加急：不当没看见，先口头接一句，事记到下封信。"""
        try:
            umo = self._post_feed_umo()
            if not umo:
                return
            line = random.choice([
                "……你还在吗。我在。这周的信名额用完了，但这件事我记下了，下封一起写给你。",
                "听到了。先回你一句：今天到此为止也行，明天再说。剩下的我攒着，写进下封信。",
            ])
            await self.context.send_message(umo, MessageChain([Plain(line)]))
        except Exception as e:
            logger.warning("[鱼的窝] 加急口头回信失败: %s", e)

    # ---------------- 「她送我的」：里程碑/状态回礼 ----------------

    def _her_state(self) -> dict:
        hg = self.state.get("her_gifts")
        if not isinstance(hg, dict):
            hg = {"given": {}, "claimed": {}, "missed": [], "pending": [], "waiting": [], "count": 0}
            self.state["her_gifts"] = hg
        hg.setdefault("given", {})
        hg.setdefault("claimed", {})
        hg.setdefault("missed", [])
        hg.setdefault("pending", [])
        hg.setdefault("waiting", [])
        hg.setdefault("sig_cd", {})
        hg.setdefault("count", 0)
        return hg

    def _check_her_gifts(self) -> list[str]:
        """里程碑条件满足就记一件，排进待领取队列。不占日常回礼额度、不吃冷却。"""
        if not (self.her_gifts or self.signal_gifts):
            return []
        stats = self._stats()
        hg = self._her_state()
        self._her_expire()
        given = hg["given"]
        newly: list[str] = []
        for g in self.her_gifts:
            gid = str(g.get("id") or "")
            if not gid:
                continue
            if bool(g.get("once", True)):
                if gid in given:
                    continue
            else:
                cd = float(g.get("cooldown_hours", 0) or 0) * 3600
                if cd and now_ts() - float(given.get(gid, 0) or 0) < cd:
                    continue
            conds = g.get("cond") or []
            if not conds or not self._eval_conditions(conds, stats):
                continue
            given[gid] = now_ts()
            hg["count"] = int(hg.get("count", 0) or 0) + 1
            newly.append(gid)
            name = str(g.get("name") or gid)
            lines = [str(x) for x in (g.get("lines") or []) if str(x).strip()]
            hg["waiting"].append({
                "id": gid,
                "ts": now_ts(),
                "line": random.choice(lines) if lines else "……给你。",
            })
            hg["waiting"] = hg["waiting"][-10:]
            self._life(f"她把「{name}」藏在了身后，想找个合适的时机给你。", "her_gift")
            self.growth_record("reverse", quote=f"{name}｜里程碑回礼", source="self")
        if newly:
            self._save_state()
            logger.info("[鱼的窝] 她回礼 %d 件: %s", len(newly), "、".join(newly))
        self._check_signal_gifts()
        self._her_release()
        return newly

    def _check_signal_gifts(self) -> None:
        """信号回礼：他这两天说过的话，她挑一件东西留着。
        与里程碑共用「一天一件」名额和待领队列；同一 tag 7 天冷却；回避期不准备。"""
        if not self.signal_gifts or not bool(self._cfg("signals.enabled", True)):
            return
        hg = self._her_state()
        if any(isinstance(p, dict) and not p.get("taken") for p in hg["pending"]) or hg["waiting"]:
            return
        if str(self._heart_snapshot().get("mode") or "") == "shut":
            return
        cd = hg["sig_cd"]
        now = now_ts()
        start48 = now - 2 * 86400
        cand: list[tuple[str, str]] = []
        seen: set[str] = set()
        for r in reversed(self.state.get("signals") or []):
            if not isinstance(r, dict):
                continue
            if float(r.get("ts", 0) or 0) < start48:
                break
            tag = str(r.get("tag") or "")
            ev = str(r.get("evidence") or "").strip()
            if not tag or not ev or tag in seen:
                continue
            if now - float(cd.get(tag, 0) or 0) < 7 * 86400:
                continue
            seen.add(tag)
            cand.append((tag, ev))
        cand.sort(key=lambda x: 0 if x[0] in DANGER_TAGS else 1)
        for tag, ev in cand:
            g = next((sg for sg in self.signal_gifts if tag in (sg.get("tags") or [])), None)
            if not g:
                continue
            line = str(g.get("line") or "……给你。").replace("{evidence}", ev[:30])
            hg["waiting"].append({
                "id": str(g.get("id")), "ts": now, "line": line,
                "tag": tag, "evidence": ev, "source": "signal",
            })
            hg["waiting"] = hg["waiting"][-10:]
            cd[tag] = now
            self._life(f"他说「{ev[:30]}」，她把「{g.get('name')}」留了下来，明天找机会给他。", "signal_gift")
            self.growth_record("reverse", quote=f"{g.get('name')}｜因为{SIGNAL_TAG_CN.get(tag, tag)}", source="self")
            self._save_state()
            logger.info("[鱼的窝] 信号回礼入队: %s(%s)", g.get("id"), tag)
            break

    def _her_expire(self) -> int:
        """递出去没领的，她过几天就收回去；等太久没递出去的直接作废。
        没有这一步，一件不领的礼物会把整套回礼永久卡住。"""
        hg = self._her_state()
        try:
            hours = max(1.0, float(self._cfg("her_gifts.offer_expire_hours", 72) or 72))
        except Exception:
            hours = 72.0
        try:
            wait_days = max(1.0, float(self._cfg("her_gifts.waiting_expire_days", 14) or 14))
        except Exception:
            wait_days = 14.0
        now = now_ts()
        keep: list[dict] = []
        back: list[dict] = []
        for p in (hg.get("pending") or []):
            if not isinstance(p, dict):
                continue
            if p.get("taken"):
                keep.append(p)
                continue
            if now - float(p.get("offer_ts") or p.get("ts") or now) >= hours * 3600.0:
                back.append(p)
            else:
                keep.append(p)
        changed = bool(back)
        if back:
            hg["pending"] = keep
            missed = list(hg.get("missed") or [])
            waiting = list(hg.get("waiting") or [])
            for item in back:
                gid = str(item.get("id") or "")
                g = self.her_gift_map.get(gid) or {}
                name = str(g.get("name") or gid or "她准备的东西")
                # 收回去，但不是丢掉：放回等递队列，之后按「一天一件」的节奏再递出来。
                # given 不动（否则一次性礼物会被立刻重新准备，不领就能无限刷），也不罚心愿。
                if not any(str(w.get("id")) == gid for w in waiting):
                    again = dict(item)
                    again["ts"] = now
                    again.pop("offer_ts", None)
                    again.pop("taken", None)
                    waiting.append(again)
                missed.append({"id": gid, "name": name, "ts": now})
                self._life(f"她把「{name}」先收回去了，说等你想收的时候再问她要。", "her_gift_back")
                self._pending("her_gift_back", f"她上次想给你的「{name}」你没接，她先收回去放着。")
            hg["missed"] = missed[-30:]
            hg["waiting"] = waiting[-10:]
        # 排队太久没递出去的，作废（不记仇，只是时机过了）
        waiting = [w for w in (hg.get("waiting") or []) if isinstance(w, dict)]
        fresh = [w for w in waiting if now - float(w.get("ts") or now) < wait_days * 86400.0]
        if len(fresh) != len(waiting):
            hg["waiting"] = fresh[-10:]
            changed = True
        if changed:
            self._save_state()
        return len(back)

    def _her_release(self) -> None:
        """递出手的节奏：手里没东西时，从等递队列里取一件。
        普通模式一天最多递一件（偶尔才珍贵）；创造/测试模式不限，方便试看。"""
        hg = self._her_state()
        self._her_expire()
        if any(isinstance(p, dict) and not p.get("taken") for p in hg["pending"]):
            return
        if self._band() == "shut":     # 躲起来时不递东西
            return
        waiting = hg.get("waiting") or []
        if not waiting:
            return
        fast = bool(self.creative) or self._test_mode()
        if not fast and str(hg.get("last_offer_day") or "") == today_str():
            return
        item = dict(waiting.pop(0))
        hg["waiting"] = waiting
        item["taken"] = False
        item["offer_ts"] = now_ts()
        hg["pending"].append(item)
        hg["pending"] = hg["pending"][-10:]
        hg["last_offer_day"] = today_str()
        self._save_state()

    def _her_summary(self) -> dict:
        hg = self._her_state()
        pending = [p for p in hg["pending"] if isinstance(p, dict) and not p.get("taken")]
        return {
            "pending": len(pending),
            "done": len(hg.get("claimed") or {}),
            "ready": len([g for g in (hg.get("given") or {}) if g not in (hg.get("claimed") or {})]),
            "total": len(self.her_gifts),
        }

    def _her_gift_payload(self) -> dict:
        stats = self._stats()
        hg = self._her_state()
        given = hg["given"]
        rows = []
        for g in self.her_gifts:
            gid = str(g.get("id") or "")
            conds = g.get("cond") or []
            is_claimed = gid in hg["claimed"]
            is_ready = (gid in given) and not is_claimed
            is_done = is_claimed
            prog = 1.0 if is_done else (self._condition_progress({"all": conds}, stats) if conds else 0.0)
            hidden = bool(g.get("hidden"))
            teased = (not hidden) or is_done or prog >= 0.5
            leafs = self._leaf_views({"all": conds}, stats) if (conds and teased) else []
            lines = [str(x) for x in (g.get("lines") or []) if str(x).strip()]
            rows.append({
                "id": gid,
                "name": str(g.get("name") or gid) if teased else "？？？",
                "icon": str(g.get("icon") or ""),
                "rarity": str(g.get("rarity") or "common"),
                "hint": str(g.get("hint") or "") if teased else "还不知道是什么。",
                "line": lines[0] if lines else "",
                "done": is_done,
                "claimed": is_claimed,
                "ready": is_ready,
                "ts": float(hg["claimed"].get(gid) or given.get(gid) or 0),
                "hidden": hidden,
                "teased": teased,
                "progress": round(float(prog), 4),
                "conditions": leafs,
            })
        pending = [p for p in hg["pending"] if isinstance(p, dict) and not p.get("taken")]
        head = pending[0] if pending else None
        if head:
            g = self.her_gift_map.get(str(head.get("id"))) or {}
            head = dict(head)
            head["name"] = str(g.get("name") or head.get("id") or "")
            head["icon"] = str(g.get("icon") or "")
            head["rarity"] = str(g.get("rarity") or "common")
        return {
            "items": rows,
            "done": sum(1 for r in rows if r.get("claimed")),
            "ready": sum(1 for r in rows if r.get("ready")),
            "total": len(rows),
            "pending": head,
            "missed_total": len(hg.get("missed") or []),
            "queue": max(0, len(pending) - 1),
            "character": {"name": str(self._cfg("panel.character_name", "她") or "她")},
        }

    async def _web_her_gifts(self, **kwargs) -> dict:
        try:
            self._check_her_gifts()
            return {"ok": True, "data": self._her_gift_payload()}
        except Exception as e:
            logger.warning("[鱼的窝] 她送我的读数据失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    async def _web_her_gift_take(self, **kwargs) -> dict:
        """领取：她把手里的东西递给你，心愿 +2，队列里下一件顶上。"""
        try:
            hg = self._her_state()
            head = next((p for p in hg["pending"] if isinstance(p, dict) and not p.get("taken")), None)
            if not head:
                return {"status": "error", "message": "她手里没有东西了", "data": {}}
            if self._band() == "shut":
                return {"status": "error", "message": random.choice(self.BAND_REJECT_LINES), "data": {}}
            okg, whyg = self._gift_gate()
            if not okg:
                return {"status": "error", "message": whyg, "data": {}}
            self._gift_mark()
            head["taken"] = True
            gid = str(head.get("id") or "")
            hg["claimed"][gid] = now_ts()
            hg["missed"] = [m for m in (hg.get("missed") or [])
                            if isinstance(m, dict) and str(m.get("id")) != gid][-30:]
            g = self.her_gift_map.get(gid) or {}
            data = {
                "id": gid,
                "name": str(g.get("name") or gid),
                "icon": str(g.get("icon") or ""),
                "rarity": str(g.get("rarity") or "common"),
                "line": str(head.get("line") or ""),
                "ts": float(head.get("ts") or 0),
            }
            hist = self.state.setdefault("history", [])
            hist.append({"ts": now_ts(), "kind": "her_gift", "gift": gid,
                         "name": data["name"], "line": data["line"]})
            self.state["history"] = hist[-60:]
            self._save_state()
            self._her_release()
            return {"ok": True, "data": data}
        except Exception as e:
            logger.warning("[鱼的窝] 领取回礼失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    # ---------------- 天气：当地实况 → 房间背景 ----------------
    _BG_SCENE = None
    _BG_NATIVE = False

    def _wx_get(self, key: str, default: Any = None) -> Any:
        return self._cfg(f"weather.{key}", default)

    @staticmethod
    def _http_json(url: str, timeout: float = 12.0) -> dict:
        req = urllib.request.Request(
            url, headers={"User-Agent": "astrbot-plugin-fish-home/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _wx_scene(self):
        """懒加载生成器；numpy/Pillow 不在也不影响聊天。"""
        if self._BG_SCENE is not None:
            return self._BG_SCENE or None
        try:
            import importlib.util

            path = os.path.join(PLUGIN_ROOT, "bg_scene.py")
            spec = importlib.util.spec_from_file_location("fish_home_bg_scene", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._BG_SCENE = mod
            return mod
        except Exception as e:
            logger.warning("[鱼的窝] 背景生成器加载失败（房间将用静态图）: %s", e)
            self._BG_SCENE = False
            return None

    def _wx_render(self, slot: str, kind: str, season: str, temp: float, phase: float, dst: str,
                   intensity: float = 0.6, lights: bool = True) -> bool:
        """画一张 1920×1080。实测 0.04 秒，丢线程里跑就行。默认开灯。"""
        scn = self._wx_scene()
        if not scn:
            return False
        try:
            from PIL import Image

            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not self._BG_NATIVE:
                scn.load_native(
                    os.path.join(PLUGIN_ROOT, "views", "reward", "assets", "floor-on.png")
                )
                self._BG_NATIVE = True
            arr = scn.build(
                slot, kind, intensity=intensity, season=season, temp=temp, phase=phase,
                lights=lights,
            )
            Image.fromarray(arr).resize(
                (scn.W * scn.S, scn.H * scn.S), Image.NEAREST
            ).save(dst)
            return True
        except Exception as e:
            logger.warning("[鱼的窝] 背景绘制失败: %s", e)
            return False

    async def _wx_refresh(self) -> None:
        """拉一次实况、必要时重画背景。任何一步失败都只保留上一张。"""
        if not bool(self._wx_get("enabled", True)):
            return
        city = str(self._wx_get("city", "杭州") or "杭州").strip() or "杭州"
        try:
            geo = self.state.get("wx_geo") or {}
            if str(geo.get("city") or "") != city or not geo.get("lat"):
                url = (
                    "https://geocoding-api.open-meteo.com/v1/search?name="
                    + urllib.parse.quote(city)
                    + "&count=1&language=zh"
                )
                g = await asyncio.to_thread(self._http_json, url)
                hits = (g or {}).get("results") or []
                if not hits:
                    raise RuntimeError(f"找不到城市：{city}")
                geo = {
                    "city": city,
                    "name": hits[0].get("name") or city,
                    "lat": hits[0].get("latitude"),
                    "lon": hits[0].get("longitude"),
                }
                self.state["wx_geo"] = geo
            url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={geo['lat']}&longitude={geo['lon']}"
                "&current=temperature_2m,weather_code,is_day,precipitation"
                "&daily=sunrise,sunset&timezone=auto"
            )
            data = await asyncio.to_thread(self._http_json, url)
            cur = data.get("current") or {}
            daily = data.get("daily") or {}
            if not cur:
                raise RuntimeError("天气接口没给 current")
            now_local = datetime.fromisoformat(str(cur.get("time")))
            sunrise = datetime.fromisoformat(str((daily.get("sunrise") or [""])[0]))
            sunset = datetime.fromisoformat(str((daily.get("sunset") or [""])[0]))
            kind = wmo_kind(int(cur.get("weather_code") or 0))
            temp = float(cur.get("temperature_2m") or 0)
            # 雨雪大小跟着实况降水量走
            precip = float(cur.get("precipitation") or 0)
            inten = 0.2
            for thr, val in ((0.2, 0.2), (1.0, 0.35), (2.5, 0.55), (6.0, 0.75), (1e9, 0.95)):
                if precip < thr:
                    inten = val
                    break
            if sunset - timedelta(minutes=45) <= now_local <= sunset:
                slot = "dusk"
            elif now_local < sunrise or now_local > sunset:
                slot = "night"
            else:
                slot = "day"
            season = season_of(now_local.month) if bool(self._wx_get("auto_season", True)) else "winter"
            key = f"{slot}_{kind}"
            # 只画开灯版，文件名就带「开灯」：屋里永远是亮着的
            dst = os.path.join(
                PLUGIN_ROOT, "views", "reward", "assets", "bg", "gen", key + "_开灯.png"
            )
            old = self.state.get("wx") or {}
            if old.get("key") != key or not os.path.exists(dst):
                await asyncio.to_thread(
                    self._wx_render, slot, kind, season, temp,
                    moon_phase_of(now_local), dst, inten, True,
                )
            self.state["wx"] = {
                "ts": now_ts(),
                "city": geo.get("name") or city,
                "temp": round(temp, 1),
                "code": int(cur.get("weather_code") or 0),
                "kind": kind,
                "kind_cn": WMO_CN.get(kind, kind),
                "slot": slot,
                "slot_cn": SLOT_CN.get(slot, slot),
                "season": season,
                "key": key,
                "precip": round(precip, 2),
                "intensity": inten,
                # 屋里永远开灯：指向刚画出来的开灯图
                "bg": ((f"./assets/bg/gen/{key}_开灯.png" if os.path.exists(dst) else None)
                       or str(old.get("bg") or "./assets/bg/now_开灯.webp")),
                "sunrise": sunrise.strftime("%H:%M"),
                "sunset": sunset.strftime("%H:%M"),
            }
            self._save_state()
            logger.info(
                "[鱼的窝] 天气：%s %s %s %.1f℃ → %s",
                geo.get("name") or city, SLOT_CN.get(slot, slot),
                WMO_CN.get(kind, kind), temp, key,
            )
        except Exception as e:
            logger.warning("[鱼的窝] 取天气失败，先沿用上一张背景: %s", e)

    def _favor_tier(self) -> str:
        """三档直接投影鱼心的「行为档」：

        鱼心按责怪压力分五档（calm / wronged / angry / afraid / shut），
        这里把它们压成三档：
            calm                    → normal（日常）
            wronged / angry / afraid → sulk  （赌气：pout 定格）
            shut                    → gone  （回避：人不见）
        只有没装鱼心时才退回按好感度判断。
        """
        snap = self._heart_snapshot()
        if str(snap.get("source") or "") == "heart":
            mode = str(snap.get("mode") or "calm")
            if mode == "shut":
                tier = "gone"
            elif mode in ("wronged", "angry", "afraid"):
                tier = "sulk"
            else:
                tier = "normal"
            self.state["favor_tier"] = tier
            self.state["favor_tier_mode"] = mode
            return tier
        fav = float(self._favor())
        prev = str(self.state.get("favor_tier") or "normal")
        if fav < 0:
            tier = "gone"
        elif fav < 10:
            tier = "sulk"
        elif fav < 15 and prev in ("sulk", "gone"):
            tier = "sulk"
        else:
            tier = "normal"
        self.state["favor_tier"] = tier
        self.state["favor_tier_mode"] = "local"
        return tier

    def _wx_heal(self) -> None:
        """老存档可能还指着已经删掉的无灯图：换回默认那张，并让它下次重画。"""
        wx = self.state.get("wx") or {}
        bg = str(wx.get("bg") or "")
        if not bg.startswith("./assets/"):
            return
        path = os.path.join(PLUGIN_DIR, "views", "reward", bg[2:])
        if os.path.exists(path):
            return
        logger.info("[鱼的窝] 背景图没了，先回到默认开灯图：%s", bg)
        wx["bg"] = "./assets/bg/now_开灯.webp"
        wx["key"] = ""              # 逼下一次天气刷新重画
        self.state["wx"] = wx

    def _state_payload(self) -> dict:
        self._roll_day()
        self._wx_heal()
        snap = self._heart_snapshot()
        self._update_peaks(snap)
        self._check_decay(now_ts())
        self._check_gifts()
        self._check_furniture()
        self._check_achievements()
        self._check_her_gifts()
        self._check_ai_notes()
        self._settle_favor()
        self._online_bump()
        self._free_food_tick()
        if not self._test_mode():
            self._reverse_tick()
        stats = self._stats()
        tier = self._favor_tier()
        meters = self.state.get("meters") or {}
        levels = self.state.get("meter_levels") or {}
        peaks = self.state.get("meter_peak") or {}
        lv = level_info(int(levels.get("intimacy", 0) or 0), float(meters.get("intimacy", 0) or 0) / METER_MAX)
        gifts = [self._gift_payload(g, stats) for g in self.catalog]
        send = self.state.get("send") or {}
        sent_today = int(send.get("count", 0) or 0) if str(send.get("day") or "") == today_str() else 0
        cooldown = 20
        last_send = float(send.get("last_ts", 0) or 0) if str(send.get("day") or "") == today_str() else 0.0
        cooldown_left = max(0, int(cooldown - (now_ts() - last_send))) if last_send else 0
        sendable = [g for g in gifts if g["can_send"]]
        pending = [p for p in (self.state.get("pending") or []) if isinstance(p, dict)]
        payload = {
            "version": VERSION,
            "now": now_ts(),
            "enabled": bool(self._cfg("enabled", True)),
            "creative": bool(self.creative),
            "title": str(self._cfg("panel.title", "鱼的窝") or "鱼的窝"),
            "character": {
                "name": str(self._cfg("panel.character_name", "她") or "她"),
            },
            "level": lv,
            "meters": {
                key: {
                    "key": key,
                    "label": METER_LABELS[key],
                    "value": round(clamp(float(meters.get(key, 0) or 0)), 1),
                    "level": int(levels.get(key, 0) or 0),
                    "peak": round(clamp(float(peaks.get(key, 0) or 0)), 1),
                    "max": METER_MAX,
                    "feeds_heart": METER_FEED[key][0],
                    "step": FEED_STEP,
                }
                for key in METER_KEYS
            },
            "feed": {
                "step": FEED_STEP,
                "total": int(self.state.get("fed_total", 0) or 0),
                "backlog": {k: round(float(v or 0), 1) for k, v in (self.state.get("heart_backlog") or {}).items()},
            },
            "intimacy": {
                "current": round(clamp(float(meters.get("intimacy", 0) or 0)), 1),
                "peak": round(float(self.state.get("intimacy_peak", 0) or 0), 1),
                "level": int(levels.get("intimacy", 0) or 0),
                "source": "local",
                "source_label": f"攒满 {fmt_num(METER_MAX)} → 她的心 +{fmt_num(FEED_STEP)}",
                "mood": str(snap.get("mood") or "平常"),
                "mode": str(snap.get("mode") or "calm"),
                "mode_cn": MODE_CN.get(str(snap.get("mode") or "calm"), ""),
                "expectation": round(float(snap.get("expectation", 0) or 0), 1),
                "attachment": round(clamp(float(meters.get("attachment", 0) or 0)), 1),
                "joy": round(clamp(float(meters.get("joy", 0) or 0)), 1),
                "soothe": round(clamp(float(meters.get("soothe", 0) or 0)), 1),
                "stress": round(float(snap.get("stress", 0) or 0), 1),
            },
            "moods": self._mood_payload(snap),
            "radar": self._radar_payload(snap),
            "chat": {
                "total": int((self.state.get("chat") or {}).get("total", 0) or 0),
                "today": self._chat_today(),
                "streak": self._streak(),
                "days": self._chat_days(),
            },
            "gifts": self._food_payload(),
            "gifts_all": gifts,
            "favor": {
                "value": round(self._favor(), 1),
                "cap": 100,
                "today": round(float(self.state.get("favor_earned_today", 0) or 0), 1),
                "tier": tier,
                "mode": str(self.state.get("favor_tier_mode") or ""),
                "log": list((self.state.get("favor_log") or [])[-6:]),
            },
            "wage": self._wage_progress(),
            "balance": {
                "value": self._balance(),
                "today": int(self.state.get("wage_today", 0) or 0),
                "net": round(float(self.state.get("wage_net", 0) or 0), 1),
                "mood": str(self.state.get("wage_day_mood") or ""),
                "log": list((self.state.get("balance_log") or [])[-6:]),
            },
            "reverse": self._reverse_payload(),
            "her_gifts": self._her_summary(),
            "activity": self._activity_fact() or {},
            "ambient": self._ambient_now(now_ts()),
            "letters": {
                "recent": list(self.state.get("letters") or [])[-6:],
                "week_count": int(self.state.get("letter_week_count", 0) or 0),
                "week_cap": 2,
                "beats": list(self.state.get("pending_letter_beats") or []),
                "unread": len([x for x in (self.state.get("letters") or [])
                               if isinstance(x, dict) and not x.get("read")]),
            },
            "away": dict(self.state.get("away") or {}),
            "last_seen": float((self.state.get("chat") or {}).get("last_ts", 0) or 0),
            "skin": self._skin(),
            "band": {"name": self._band()},
            "steps": self._steps_view(),
            "diy": self._diy_view(),
            "wx": dict(self.state.get("wx") or {}),
            "notes": {"count": len([e for e in (self.state.get("ai_achv") or []) if isinstance(e, dict)])},
            "pending": pending,
            "history": list((self.state.get("history") or [])[-20:]),
            "last_reaction": dict(self.state.get("last_reaction") or {}),
            "limits": {
                "daily_limit": 5,
                "sent_today": sent_today,
                "cooldown_left": cooldown_left,
                "sendable": len(sendable),
            },
            "clues": self._clues(gifts),
            "furniture": self._furniture_payload(stats),
            "owner_configured": bool(self._owner_umo()),
            "thoughts": [
                {"text": str(t.get("text") or ""), "kind": str(t.get("kind") or "")}
                for t in (snap.get("thoughts") or [])
            ][:3],
        }
        # 好感度接管左边的横条（负数时前端会自动变灰）
        try:
            payload["meters"]["intimacy"]["value"] = round(self._favor(), 1)
            payload["meters"]["intimacy"]["max"] = 100
            payload["meters"]["intimacy"]["level"] = 0
        except Exception:
            pass
        payload["achv"] = self._ach_summary_payload()
        payload["growth"] = self._growth_payload()
        self._save_state()
        return payload

    def _clues(self, gifts: list[dict]) -> list[str]:
        out = []
        for g in gifts:
            if g.get("hidden") and not g.get("revealed") and g.get("hint"):
                out.append(f"{g['hint']}")
            elif g.get("hidden") and g.get("revealed") and not g.get("unlocked"):
                out.append(f"那件东西的轮廓更清楚了：{g.get('desc') or ''}")
            elif (not g.get("hidden")) and (not g.get("unlocked")) and g.get("progress", 0) >= 0.75:
                out.append(f"「{g['name']}」已经很近了：{g.get('source') or ''}")
        return out[:3]

    def _chat_days(self) -> list[dict]:
        days = (self.state.get("chat") or {}).get("days") or {}
        out = []
        for i in range(6, -1, -1):
            d = datetime.now() - timedelta(days=i)
            key = d.strftime("%Y-%m-%d")
            out.append({
                "date": key,
                "label": "今天" if i == 0 else ("昨天" if i == 1 else d.strftime("%m-%d")),
                "count": int(days.get(key, 0) or 0),
            })
        return out

    # ---------------- 生命周期 ----------------

    def _ensure_init(self) -> None:
        if bool(self.state.get("initialized")):
            return
        snap = self._heart_snapshot()
        for key in METER_KEYS:
            self.state.setdefault("meters", {}).setdefault(key, 0.0)
            self.state.setdefault("meter_levels", {}).setdefault(key, 0)
            self.state.setdefault("meter_peak", {}).setdefault(key, 0.0)
        self.state.setdefault("heart_backlog", {})
        self.state["expectation_peak"] = max(
            float(self.state.get("expectation_peak", 0) or 0), float(snap.get("expectation", 0) or 0)
        )
        self.state["created_at"] = now_ts()
        self.state["daily_wish"] = self._pick_daily_wish(today_str())
        if not str(self.state.get("day_started") or ""):
            self.state["day_started"] = today_str()
            self.state.setdefault("day_index", 1)
        if bool(self._cfg("starter_gift", True)) and not self.state.get("starter_done"):
            gift = self.gift_map.get("candy")
            if gift:
                self._grant_gift(gift, "欢迎礼", repeat=True)
            self.state["starter_done"] = True
        self.state["initialized"] = True
        self._check_gifts()
        self._save_state()
        logger.info(
            "[鱼的窝] 初始化完成：亲密最高=%.1f，礼物=%d",
            self.state["intimacy_peak"], len(self.state.get("gifts") or {}),
        )

    async def initialize(self) -> None:
        self._ensure_init()
        self._register_web_apis()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="fish-home-loop")
        # 她会饿：45 秒看一次，到饭点发消息
        if getattr(self, "_hunger_task", None) is None or self._hunger_task.done():
            self._hunger_task = asyncio.create_task(self._hunger_loop(), name="fish-home-hunger")
        # 先把实况拿到手，别等第一个十秒循环——否则第一次打开面板是晴天屋里
        try:
            asyncio.create_task(self._wx_refresh(), name="fish-home-wx")
        except Exception:
            pass
        self._save_state()

    async def terminate(self) -> None:
        for t in (self._task, getattr(self, "_hunger_task", None)):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._save_state()
        logger.info("[鱼的窝] 已停止")

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(600)
                if not bool(self._cfg("enabled", True)):
                    continue
                self._roll_day()
                self._update_peaks(self._heart_snapshot())
                self._check_decay(now_ts())
                self._check_gifts()
                try:
                    await self._persona_sync()
                except Exception:
                    pass
                try:
                    await self._wx_refresh()
                except Exception as e:
                    logger.warning("[鱼的窝] 天气刷新异常: %s", e)
                try:
                    await self._offline_fetch()
                except Exception as e:
                    logger.debug("[鱼的窝] 离线补看异常: %s", e)
                self._save_state()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[鱼的窝] 后台循环异常: %s", e)
                await asyncio.sleep(30)

    def _register_web_apis(self) -> None:
        routes = [
            ("/panel/state", self._web_state, ["GET"], "鱼的窝：状态"),
            ("/panel/send", self._web_send, ["POST"], "鱼的窝：送礼物"),
            ("/panel/read", self._web_read, ["POST"], "鱼的窝：清空提醒"),
            ("/panel/tap", self._web_tap, ["POST"], "鱼的窝：点小窝里的家具"),
            ("/panel/achv", self._web_achv, ["GET"], "鱼的窝：成就板"),
            ("/panel/achv_pop", self._web_achv_pop, ["POST"], "鱼的窝：成就弹窗回执"),
            ("/panel/achv_test", self._web_achv_test, ["POST"], "鱼的窝：测试解锁一个成就"),
            ("/panel/growth_card", self._web_growth_card, ["GET"], "鱼的窝：今日卡"),
            ("/panel/growth_settle", self._web_growth_settle, ["POST"], "鱼的窝：结算今天"),
            ("/panel/her_gifts", self._web_her_gifts, ["GET"], "鱼的窝：她送我的"),
            ("/panel/her_gift_take", self._web_her_gift_take, ["POST"], "鱼的窝：领取回礼"),
            ("/panel/activity", self._web_activity, ["POST"], "鱼的窝：闲时行为上报"),
            ("/panel/activity", self._web_activity_get, ["GET"], "鱼的窝：此刻她在干嘛"),
            ("/panel/diy_feed", self._web_diy_feed, ["POST"], "鱼的窝：DIY 点单"),
            ("/panel/typing", self._web_typing, ["GET"], "鱼的窝：她是否正在回话"),
            ("/panel/letters", self._web_letters, ["GET"], "鱼的窝：信箱"),
            ("/panel/letter_read", self._web_letter_read, ["POST"], "鱼的窝：读信"),
            ("/panel/letter_write", self._web_letter_write, ["POST"], "鱼的窝：加急写信"),
            ("/panel/notes", self._web_notes, ["GET"], "鱼的窝：她自己想的成就"),
            ("/panel/furn_lib", self._web_furn_lib, ["GET"], "鱼的窝：家具工坊：我的家具"),
            ("/panel/furn_generate", self._web_furn_generate, ["POST"], "鱼的窝：家具工坊：生成施工图"),
            ("/panel/furn_save", self._web_furn_save, ["POST"], "鱼的窝：家具工坊：保存家具"),
            ("/panel/furn_layout", self._web_furn_layout, ["POST"], "鱼的窝：家具工坊：摆放/收回"),
            ("/panel/furn_delete", self._web_furn_delete, ["POST"], "鱼的窝：家具工坊：删除家具"),
        ]
        prefixes = ["/astrbot_plugin_fish_home", "/fish_home"]
        try:
            for prefix in prefixes:
                for suffix, handler, methods, desc in routes:
                    self.context.register_web_api(prefix + suffix, handler, methods, desc)
            logger.info("[鱼的窝] Web 页面接口已注册（%d 条）", len(prefixes) * len(routes))
        except Exception as e:
            logger.warning("[鱼的窝] 注册 Web API 失败（页面将不可用）: %s", e)


    # ---------------- 成就（星露谷式成就板 + 解锁动画数据源） ----------------

    def _skin(self) -> dict:
        """美术皮肤清单：换图只改 assets/skin/skin.json，代码不用动。读不到就用内置默认。"""
        try:
            with open(SKIN_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and raw:
                return raw
        except Exception as e:
            logger.debug("[鱼的窝] 读皮肤表失败，用默认: %s", e)
        return {
            "char": {"dir": "./assets/char/act", "freeze_dir": "./assets/char/act/freeze",
                     "idle": "idle.gif", "walk": "walk.gif", "acts": {}},
            "window": {"dir": "./assets/bg/window", "clear": "clear.webp", "rain": "rain.webp",
                       "snow": "snow.webp", "night": "night.webp"},
            "room": {"dir": "./assets/bg", "default": "now_开灯.webp"},
            "furniture": {"dir": "./assets/furniture"},
            "diy": {"dir": "./assets/skin/diy"},
        }

    def _load_achievements(self) -> list[dict]:
        """读 assets/achievements.json；读不到就返回空，面板显示「还没有成就表」。"""
        try:
            with open(ACHV_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            cats = [c for c in (raw.get("categories") or []) if isinstance(c, dict) and c.get("items")]
            for c in cats:
                c["items"] = [i for i in (c.get("items") or []) if isinstance(i, dict) and i.get("id")]
            return cats
        except Exception as e:
            logger.warning("[鱼的窝] 读成就表失败: %s", e)
            return []

    def _spent_add(self, amount: float) -> None:
        """记「今天为她花了多少好感度」，跨天自动清零。"""
        day = today_str()
        if str(self.state.get("spent_day") or "") != day:
            self.state["spent_day"] = day
            self.state["spent_today"] = 0
        self.state["spent_today"] = int(self.state.get("spent_today", 0) or 0) + int(amount)

    def _ach_done(self) -> dict:
        a = self.state.get("achievements")
        return a if isinstance(a, dict) else {}

    def _check_achievements(self) -> list[str]:
        """条件满足就记成就，并排进主界面弹窗队列。"""
        if not self.ach_items:
            return []
        stats = self._stats()
        done = self.state.setdefault("achievements", {})
        newly: list[str] = []
        for it in self.ach_items:
            aid = str(it.get("id"))
            if aid in done:
                continue
            conds = it.get("cond") or []
            if not conds or not self._eval_conditions(conds, stats):
                continue
            done[aid] = now_ts()
            newly.append(aid)
            self._ach_enqueue(it)
        if newly:
            self._save_state()
            logger.info("[鱼的窝] 成就解锁 %d 个: %s", len(newly), "、".join(newly))
        return newly

    def _ach_summary_payload(self) -> dict:
        done = self._ach_done()
        total = len(self.ach_items)
        rows = [p for p in (self.state.get("ach_pop") or []) if isinstance(p, dict)]
        pop = [p for p in rows if not p.get("popped")]
        board = [p for p in rows if not p.get("board_seen")]
        return {
            "done": len([k for k in done if k in self.ach_map]),
            "total": total,
            "pop": pop[-4:],
            "board_pending": len(board),
            "latest": max(done.values()) if done else 0.0,
        }

    def _ach_payload(self) -> dict:
        stats = self._stats()
        done = self._ach_done()
        cats_out = []
        for cat in self.ach_cats:
            items = []
            for it in cat.get("items") or []:
                aid = str(it.get("id"))
                conds = it.get("cond") or []
                is_done = aid in done
                leafs = self._leaf_views({"all": conds}, stats) if conds else []
                prog = 1.0 if is_done else (self._condition_progress({"all": conds}, stats) if conds else 0.0)
                hidden = bool(it.get("hidden"))
                # 隐藏款：完成度过半才露一点线索
                teased = (not hidden) or is_done or prog >= 0.5
                items.append({
                    "id": aid,
                    "name": str(it.get("name") or aid) if teased else "？？？",
                    "desc": str(it.get("desc") or "") if teased else "还不知道是什么。",
                    "icon": str(it.get("icon") or "🏆") if teased else "？",
                    "icon_kind": str(it.get("icon_kind") or "emoji"),
                    "done": is_done,
                    "ts": float(done.get(aid) or 0),
                    "hidden": hidden,
                    "teased": teased,
                    "progress": round(float(prog), 4),
                    "conditions": leafs if teased else [],
                })
            cats_out.append({
                "key": str(cat.get("key") or ""),
                "title": str(cat.get("title") or ""),
                "done": sum(1 for i in items if i["done"]),
                "total": len(items),
                "items": items,
            })
        total = sum(c["total"] for c in cats_out)
        dones = sum(c["done"] for c in cats_out)
        return {
            "title": str(self._cfg("panel.title", "鱼的窝") or "鱼的窝"),
            "character": {"name": str(self._cfg("panel.character_name", "她") or "她")},
            "summary": {"done": dones, "total": total, "pct": round(dones / total, 4) if total else 0.0},
            "counters": {
                "favor": round(self._favor(), 1),
                "favor_peak": round(float(self.state.get("favor_peak", 0) or 0), 1),
                "level": int((self.state.get("meter_levels") or {}).get("intimacy", 0) or 0),
                "chat_total": int((self.state.get("chat") or {}).get("total", 0) or 0),
                "streak": self._streak(),
                "furniture": len([
                    o for o in (self.state.get("furniture") or {})
                    if str((self.furniture_map.get(str(o)) or {}).get("kind") or "furniture") == "furniture"
                ]),
                "fed_total": int(self.state.get("fed_total", 0) or 0),
            },
            "categories": cats_out,
            "pop": [p for p in (self.state.get("ach_pop") or []) if isinstance(p, dict)],
        }

    def _ach_enqueue(self, it: dict) -> None:
        """成就达成的统一落库：队列、历史、提醒、生活日志、心愿。"""
        aid = str(it.get("id"))
        name = str(it.get("name") or aid)
        pop = list(self.state.get("ach_pop") or [])
        pop.append({
            "id": aid, "name": name, "desc": str(it.get("desc") or ""),
            "icon": str(it.get("icon") or "🏆"), "icon_kind": str(it.get("icon_kind") or "emoji"),
            "cat": str(self.ach_cat_of.get(aid) or ""), "ts": now_ts(),
            "popped": False, "board_seen": False,
        })
        self.state["ach_pop"] = pop[-12:]
        hist = self.state.setdefault("history", [])
        hist.append({"ts": now_ts(), "kind": "achievement", "gift": aid, "name": name, "source": "成就解锁"})
        self.state["history"] = hist[-200:]
        self._pending("achievement", f"成就解锁：「{name}」。")
        self._life(f"主人达成了成就「{name}」", "achievement")
        self._wish_gain(2, f"成就：{name}")

    async def _web_achv_test(self, **kwargs) -> dict:
        """测试解锁：强制点亮成就，走正常队列与动画。
        可指定 id（body.id 或 ?id=）；不指定则挑完成度最高的那个还没解锁的。"""
        try:
            stats = self._stats()
            done = self.state.setdefault("achievements", {})
            want = ""
            try:
                body = await web_request.json(default={})
            except Exception:
                body = {}
            body = body if isinstance(body, dict) else {}
            want = str(body.get("id") or kwargs.get("id") or "").strip()
            if not want:
                try:
                    want = str(web_request.query.get("id") or "").strip()
                except Exception:
                    want = ""
            if want and want not in self.ach_map:
                return {"status": "error", "message": f"没有这个成就：{want}", "data": {}}
            if want and want in done:
                return {"ok": True, "data": {
                    "forced": False, "replay": True, "id": want,
                    "name": str((self.ach_map.get(want) or {}).get("name") or want),
                }}
            todo = [it for it in self.ach_items if str(it.get("id")) not in done]
            if want:
                pick = self.ach_map.get(want) or {}
            elif todo:
                pick = max(
                    todo,
                    key=lambda it: self._condition_progress({"all": it.get("cond") or []}, stats),
                )
            else:
                pick = {}
            if pick:
                aid = str(pick.get("id"))
                done[aid] = now_ts()
                self._ach_enqueue(pick)
                self._check_furniture()
                self._save_state()
                return {"ok": True, "data": {
                    "forced": True, "replay": False, "id": aid,
                    "name": str(pick.get("name") or aid),
                }}
            rows = sorted(done.items(), key=lambda kv: float(kv[1] or 0), reverse=True)
            aid = str(rows[0][0]) if rows else ""
            return {"ok": True, "data": {
                "forced": False, "replay": True, "id": aid,
                "name": str((self.ach_map.get(aid) or {}).get("name") or aid),
            }}
        except Exception as e:
            logger.warning("[鱼的窝] 测试解锁失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    async def _web_achv(self, **kwargs) -> dict:
        try:
            self._check_achievements()
            return {"ok": True, "data": self._ach_payload()}
        except Exception as e:
            logger.warning("[鱼的窝] 成就板读数据失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    async def _web_achv_pop(self, **kwargs) -> dict:
        """主界面已经弹过了，把队列标记为已消费。"""
        try:
            try:
                body = await web_request.json(default={})
            except Exception:
                body = {}
            body = body if isinstance(body, dict) else {}
            ids = {str(x) for x in (body.get("ids") or []) if str(x)}
            channel = str(body.get("channel") or "banner")
            rows = list(self.state.get("ach_pop") or [])
            for r in rows:
                if not isinstance(r, dict):
                    continue
                if ids and r.get("id") not in ids:
                    continue
                if channel == "board":
                    r["board_seen"] = True
                else:
                    r["popped"] = True
            self.state["ach_pop"] = [r for r in rows if isinstance(r, dict)][-12:]
            self._save_state()
            return {"ok": True, "data": self._ach_summary_payload()}
        except Exception as e:
            logger.warning("[鱼的窝] 成就弹窗回执失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    # ---------------- Web API ----------------

    async def _web_state(self, **kwargs) -> dict:
        try:
            if not int(self.state.get("panel_seen", 0) or 0):
                self.state["panel_seen"] = 1
                self._save_state()
                self._check_achievements()
            return {"ok": True, "data": self._state_payload()}
        except Exception as e:
            logger.warning("[鱼的窝] 页面读状态失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    def _gift_gate(self) -> tuple[bool, str]:
        """送礼节流：一周最多 2 次、两次之间至少 3 天。"""
        if not bool(self._cfg("gift.throttle_enabled", True)):
            return True, ""
        wk = self._letter_week_key()
        if str(self.state.get("gift_week_key") or "") != wk:
            self.state["gift_week_key"] = wk
            self.state["gift_week_count"] = 0
        cap = int(self._cfg("gift.week_cap", 2) or 2)
        if int(self.state.get("gift_week_count", 0) or 0) >= cap:
            return False, "这周收的够多了，放着吧。"
        try:
            gap = float(self._cfg("gift.interval_days", 3) or 3) * 86400
        except Exception:
            gap = 3 * 86400
        if now_ts() - float(self.state.get("gift_last_ts", 0) or 0) < gap:
            return False, "离上次送礼还没到三天呢。"
        return True, ""

    def _gift_mark(self) -> None:
        self.state["gift_last_ts"] = now_ts()
        self.state["gift_week_count"] = int(self.state.get("gift_week_count", 0) or 0) + 1

    async def _web_send(self, **kwargs) -> dict:
        try:
            body = await web_request.json(default={})
            body = body if isinstance(body, dict) else {}
            gift_id = str(body.get("gift") or kwargs.get("gift") or "").strip()
            # 「送她礼物」已下线：只保留喂食
            if gift_id and gift_id not in self._food_map():
                return {"status": "error", "message": "这个已经不做啦，直接跟我说话就行。", "data": {}}
            if gift_id in self._food_map():
                result = self._feed_food(gift_id)
            else:
                result = self._send_gift(gift_id)
            if not result.get("ok"):
                return {"status": "error", "message": result.get("error") or "送礼失败", "data": {}}
            payload = self._state_payload()
            payload["result"] = result
            return {"ok": True, "data": payload}
        except Exception as e:
            logger.warning("[鱼的窝] 页面送礼失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    async def _web_read(self, **kwargs) -> dict:
        try:
            self.state["pending"] = []
            self._save_state()
            return {"ok": True, "data": self._state_payload()}
        except Exception as e:
            logger.warning("[鱼的窝] 清空提醒失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    async def _web_tap(self, **kwargs) -> dict:
        """点一下小窝里的家具：给一句她的话，每天前几次还能顺手涨一点心愿。"""
        try:
            body = await web_request.json(default={})
            body = body if isinstance(body, dict) else {}
            oid = str(body.get("object") or kwargs.get("object") or "").strip()
            obj = self.furniture_map.get(oid)
            owned = self.state.get("furniture") or {}
            if not obj or oid not in owned:
                return {"status": "error", "message": "这个还没解锁", "data": {}}
            lines = [str(x) for x in (obj.get("lines") or []) if str(x).strip()]
            line = random.choice(lines) if lines else "……"
            tap = self.state.setdefault("furniture_tap", {"day": "", "count": 0})
            if str(tap.get("day") or "") != today_str():
                tap["day"] = today_str()
                tap["count"] = 0
            gain = 0.0
            if int(tap.get("count", 0) or 0) < 3:
                tap["count"] = int(tap.get("count", 0) or 0) + 1
                gain = self._wish_gain(1, f"陪她在{obj.get('name') or oid}边待了一会儿")
            self.state["tap_total"] = int(self.state.get("tap_total", 0) or 0) + 1
            self.growth_record("care" if oid in ("lamp", "water", "quiet") else "chat",
                               quote=f"{obj.get('name') or oid}", source="tap")
            if oid == "pet":
                self.state["pet_total"] = int(self.state.get("pet_total", 0) or 0) + 1
            self._check_achievements()
            self._save_state()
            return {
                "ok": True,
                "data": {
                    "line": line,
                    "gain": round(float(gain), 1),
                    "wish": round(float(self.state.get("wish", 0) or 0), 1),
                },
            }
        except Exception as e:
            logger.warning("[鱼的窝] 点家具失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    # ---------------- 家具工坊：AI 出施工图，代码渲染像素家具 ----------------

    def _furn_lib_load(self) -> list[dict]:
        try:
            with open(self.furn_lib_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return [x for x in (raw.get("items") or []) if isinstance(x, dict) and x.get("id")]
        except Exception:
            return []

    def _furn_lib_save(self) -> None:
        try:
            with open(self.furn_lib_path, "w", encoding="utf-8") as f:
                json.dump({"items": self.furn_lib}, f, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.warning("[鱼的窝] 写家具库失败: %s", e)

    def _furn_img_path(self, fid: str) -> str:
        return os.path.join(self.furn_img_dir, f"{fid}.png")

    def _furn_icon_b64(self, fid: str) -> str:
        try:
            with open(self._furn_img_path(fid), "rb") as f:
                return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
        except Exception:
            return ""

    def _furn_gen_left(self) -> int:
        limit = int(self._cfg("furn_gen.daily_limit", 0) or 0)
        if self._test_mode() or limit <= 0:   # 0/负数 = 不限次（默认）
            return 999
        g = self.state.get("furn_gen") or {}
        if str(g.get("day") or "") != today_str():
            return limit
        return max(0, limit - int(g.get("count", 0) or 0))

    def _furn_gen_count(self) -> None:
        g = self.state.setdefault("furn_gen", {"day": "", "count": 0})
        if str(g.get("day") or "") != today_str():
            g["day"] = today_str()
            g["count"] = 0
        g["count"] = int(g.get("count", 0) or 0) + 1

    def _furn_lib_view(self) -> dict:
        """家具工坊的全量数据：自定义件 + 已解锁的成就件（带生效坐标）+ 名额。"""
        payload = self._furniture_payload()
        old = [
            {
                "id": o["id"], "name": o["name"], "icon": o["icon"], "emoji": o["emoji"],
                "x": o["x"], "y": o["y"], "s": o.get("s") or 1, "z": o.get("z"), "native": o.get("native") or [],
            }
            for o in payload if o.get("unlocked") and not o.get("custom")
        ]
        items = []
        for it in self.furn_lib:
            items.append({
                "id": str(it.get("id")),
                "name": str(it.get("name") or "家具"),
                "prompt": str(it.get("prompt") or ""),
                "icon": self._furn_icon_b64(str(it.get("id"))),
                "native": [int(v) for v in (it.get("native") or [24, 24])][:2],
                "x": it.get("x"),
                "y": it.get("y"),
                "s": float(it.get("s") or 1),
                "z": int(it["z"]) if isinstance(it.get("z"), (int, float)) else None,
                "mode": str(it.get("mode") or "spec"),
            })
        limit = int(self._cfg("furn_gen.daily_limit", 0) or 0)
        return {
            "items": items,
            "old": old,
            "gen": {
                "left": self._furn_gen_left(),
                "limit": limit,
                "unlimited": self._test_mode() or limit <= 0,
                "img": self._furn_img_enabled(),
            },
            "caps": {
                "max_items": max(0, int(self._cfg("furn_gen.max_items", 0) or 0)),
                "max_placed": int(self._cfg("furn_gen.max_placed", 20) or 20),
            },
        }

    def _furn_z_pool(self) -> list[int]:
        """已知的所有层号（自定义件 + 成就件摆放覆盖），给下一次落位发号。"""
        zs: list[int] = []
        for it in self.furn_lib:
            z = it.get("z")
            if isinstance(z, (int, float)):
                zs.append(int(z))
        for ov in (self.state.get("furniture_layout") or {}).values():
            if isinstance(ov, dict):
                z = ov.get("z")
                if isinstance(z, (int, float)):
                    zs.append(int(z))
        return zs

    def _furn_next_z(self, to_back: bool = False) -> int:
        """摆放叠放规则：松手在哪层最高（max+1）；右键压回 = 最底（min-1）。没动过的件走深度序，见前端 stk()。"""
        zs = self._furn_z_pool()
        if not zs:
            return 1
        return min(zs) - 1 if to_back else max(zs) + 1

    def _furn_placed_count(self) -> int:
        layout = self.state.get("furniture_layout") or {}
        return len([x for x in self.furn_lib if x.get("x") is not None]) + len(layout)

    async def _furn_compose(self, prompt: str) -> str:
        umo = self._post_feed_umo()
        provider = self.context.get_using_provider(umo) if umo else None
        if provider is None:
            raise RuntimeError("现在没有可用的模型，画不了。")
        resp = await provider.text_chat(
            prompt=f"用户想画的东西：{prompt}",
            system_prompt=FURN_DESIGN_SYSTEM,
        )
        return (getattr(resp, "completion_text", "") or "").strip()

    # ---------------- 家具工坊：精绘（可选生图后端，OpenAI 风格 /v1/images/generations） ----------------

    def _furn_img_enabled(self) -> bool:
        """设置里填了 api_key 才走精绘；留空 = 默认 LLM 施工图。"""
        return bool(str(self._cfg("furn_gen.image.api_key", "") or "").strip())

    @staticmethod
    def _http_json_post(url: str, payload: dict, headers: dict | None = None,
                        timeout: float = 90.0) -> dict:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": "astrbot-plugin-fish-home/" + VERSION,
                     **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                j = json.loads(e.read().decode("utf-8", "replace"))
                detail = str(j.get("message") or j.get("error") or "")[:120]
            except Exception:
                pass
            raise ValueError(f"HTTP {e.code}" + (f"：{detail}" if detail else "")) from e

    @staticmethod
    def _http_get_bytes(url: str, timeout: float = 60.0, cap: int = FURN_IMG_RAW_CAP) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "astrbot-plugin-fish-home/" + VERSION})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(cap + 1)
        if len(data) > cap:
            raise ValueError("生成的图太大")
        return data

    async def _furn_img_generate(self, prompt: str) -> tuple[bytes, list[int]]:
        """一句话 → 生图接口 → 本地抠图量化成像素家具。失败抛 ValueError。

        兼容两种口型：OpenAI/硅基流动（/v1/images/generations、image_size、images[]）
        和火山方舟/豆包（/api/v3/images/generations、size、data[]、默认带水印→主动关掉）。"""
        key = str(self._cfg("furn_gen.image.api_key", "") or "").strip()
        base = str(self._cfg("furn_gen.image.base_url", "") or "").strip().rstrip("/") \
            or "https://api.siliconflow.cn"
        model = str(self._cfg("furn_gen.image.model", "") or "Qwen/Qwen-Image").strip()
        low = base.lower()
        ark = "volces.com" in low or "/v3" in low
        if "/images/generations" in low:
            ep = base
        else:
            ep = base + ("/images/generations" if ark else "/v1/images/generations")
        payload: dict[str, Any] = {"model": model, "prompt": furn_img_prompt(prompt), "n": 1}
        if ark:
            payload["size"] = "2048x2048"      # 方舟 Seedream 建议尺寸
            payload["watermark"] = False       # 豆包默认打水印，会烙进图里，必须关
        else:
            payload["image_size"] = "1024x1024"
        data = await asyncio.to_thread(
            self._http_json_post, ep, payload,
            {"Authorization": "Bearer " + key}, 120.0,
        )
        imgs = None
        if isinstance(data, dict):
            imgs = data.get("images") or data.get("data")   # 硅基流动回 images[]，方舟回 data[]
        first = imgs[0] if isinstance(imgs, list) and imgs and isinstance(imgs[0], dict) else {}
        raw: bytes | None = None
        b64 = first.get("b64_json")
        if isinstance(b64, str) and b64:
            try:
                raw = base64.b64decode(b64)
            except Exception as e:
                raise ValueError("生图回传的 base64 是坏的") from e
        url = first.get("url")
        if raw is None and isinstance(url, str) and url.startswith("http"):
            raw = await asyncio.to_thread(self._http_get_bytes, url)
        if not raw:
            raise ValueError("生图接口没回图")
        return await asyncio.to_thread(furn_prep_image, raw)

    async def _web_furn_lib(self, **kwargs) -> dict:
        try:
            return {"ok": True, "data": self._furn_lib_view()}
        except Exception as e:
            logger.warning("[鱼的窝] 读家具库失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    async def _web_furn_generate(self, **kwargs) -> dict:
        """一句话 → 施工图 → 渲染预览。只给预览不入库，计一次当天名额。"""
        try:
            if not bool(self._cfg("furn_gen.enabled", True)):
                return {"status": "error", "message": "家具工坊已关掉", "data": {}}
            try:
                body = await web_request.json(default={})
            except Exception:
                body = {}
            body = body if isinstance(body, dict) else {}
            prompt = str(body.get("prompt") or "").strip()[:80]
            if not prompt:
                return {"status": "error", "message": "总得说一句想要什么吧", "data": {}}
            if self._furn_gen_left() <= 0:
                return {"status": "error", "message": "今天的手已经用完了，明天再画。", "data": {}}
            spec = None
            png = None
            native = [0, 0]
            gen_name = ""
            note = ""
            if self._furn_img_enabled():           # 设置里填了生图 key → 精绘优先
                try:
                    png, native = await self._furn_img_generate(prompt)
                    gen_name = prompt[:8]
                except Exception as e:
                    logger.warning("[鱼的窝] 精绘失败，退回施工图: %s", e)
                    note = "精绘没走通（" + str(e)[:40] + "），已改用施工图"
            if png is None:                         # 默认/退回：她的模型出施工图，代码渲染
                spec_text = await self._furn_compose(prompt)
                spec = furn_validate_spec(furn_parse_spec(spec_text), prompt)
                png, native = furn_render_png(spec)
                gen_name = spec["name"]
            self._furn_gen_count()
            self._save_state()
            self._dbg("生成家具「%s」 %dx%d %s", gen_name, native[0], native[1],
                      "精绘" if spec is None else "%d 条指令" % len(spec["ops"]))
            data = {
                "prompt": prompt,
                "mode": "img" if spec is None else "spec",
                "name": gen_name,
                "native": native,
                "preview": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
            }
            if spec is not None:
                data["spec"] = spec
            if note:
                data["note"] = note
            return {"ok": True, "data": data}
        except ValueError as e:
            return {"status": "error", "message": f"这次没画成（{e}），点「换一张」再试一次。", "data": {}}
        except Exception as e:
            logger.warning("[鱼的窝] 生成家具失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    async def _web_furn_save(self, **kwargs) -> dict:
        """预览确认后入库。施工图模式：图是唯一真源，重渲染存图。
        精绘模式：没有施工图，直接存回传的成品图（再走一遍校验管线防坏图）。"""
        try:
            if not bool(self._cfg("furn_gen.enabled", True)):
                return {"status": "error", "message": "家具工坊已关掉", "data": {}}
            try:
                body = await web_request.json(default={})
            except Exception:
                body = {}
            body = body if isinstance(body, dict) else {}
            prompt = str(body.get("prompt") or "").strip()[:80]
            cap = int(self._cfg("furn_gen.max_items", 0) or 0)
            if cap > 0 and len(self.furn_lib) >= cap:
                return {"status": "error", "message": f"家具库满了（上限 {cap} 件），先删几件。", "data": {}}
            if str(body.get("mode") or "").lower() == "img":
                raw = str(body.get("preview") or "")
                if raw.startswith("data:") and "," in raw:
                    raw = raw.split(",", 1)[1]
                try:
                    png, native = furn_prep_image(base64.b64decode(raw))
                except Exception as e:
                    return {"status": "error", "message": f"这张精绘图存不了（{str(e)[:60]}），重新生成一张吧。", "data": {}}
                spec = None
                name = str(body.get("name") or "").strip()[:12] or prompt[:8] or "精绘家具"
            else:
                spec_raw = body.get("spec")
                if not isinstance(spec_raw, dict):
                    return {"status": "error", "message": "没有施工图可保存", "data": {}}
                spec = furn_validate_spec(spec_raw, prompt)
                png, native = furn_render_png(spec)
                name = spec["name"]
            fid = f"f{int(now_ts() * 1000):x}{random.randint(100, 999)}"
            os.makedirs(self.furn_img_dir, exist_ok=True)
            with open(self._furn_img_path(fid), "wb") as f:
                f.write(png)
            self.furn_lib.append({
                "id": fid, "name": name, "prompt": prompt, "spec": spec,
                "mode": "img" if spec is None else "spec",
                "native": native, "x": None, "y": None, "s": 1.0, "ts": now_ts(),
            })
            self._furn_lib_save()
            self._post_feed(name, "furniture", 0.0, fid=fid)
            self._dbg("保存家具 %s（%s）", fid, name)
            return {"ok": True, "data": self._furn_lib_view()}
        except ValueError as e:
            return {"status": "error", "message": f"这件保存不了（{e}）。", "data": {}}
        except Exception as e:
            logger.warning("[鱼的窝] 保存家具失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    async def _web_furn_layout(self, **kwargs) -> dict:
        """摆 / 挪 / 收。c_ 前缀 = 自定义件；成就件必须已解锁。"""
        try:
            try:
                body = await web_request.json(default={})
            except Exception:
                body = {}
            body = body if isinstance(body, dict) else {}
            fid = str(body.get("id") or "").strip()
            if not fid:
                return {"status": "error", "message": "缺家具 id", "data": {}}
            remove = bool(body.get("remove"))
            back = bool(body.get("back"))
            cap = int(self._cfg("furn_gen.max_placed", 20) or 20)
            try:
                x = round(max(2.0, min(98.0, float(body.get("x", 50) or 50))), 1)
                y = round(max(1.0, min(94.0, float(body.get("y", 8) or 8))), 1)
            except Exception:
                return {"status": "error", "message": "坐标不对", "data": {}}
            try:
                s = round(max(0.4, min(3.0, float(body.get("s", 1) or 1))), 2)
            except Exception:
                s = 1.0

            if fid.startswith("c_"):
                cid = fid[2:]
                item = next((z for z in self.furn_lib if str(z.get("id")) == cid), None)
                if item is None:
                    return {"status": "error", "message": "没这件家具", "data": {}}
                was_placed = item.get("x") is not None
                if not remove and not was_placed and self._furn_placed_count() + 1 > cap:
                    return {"status": "error", "message": f"摆不下了（同屏上限 {cap} 件），先收回一点。", "data": {}}
                if remove:
                    item["x"] = None
                    item["y"] = None
                else:
                    item["x"] = x
                    item["y"] = y
                    item["s"] = s
                    item["z"] = self._furn_next_z(back and not remove)
                self._furn_lib_save()
                if not remove and not was_placed:
                    self._post_feed(str(item.get("name") or "家具"), "furnplace", 0.0, fid=cid)
            else:
                self._check_furniture()
                obj = self.furniture_map.get(fid)
                if obj is None:
                    return {"status": "error", "message": "没这件家具", "data": {}}
                if fid not in (self.state.get("furniture") or {}):
                    return {"status": "error", "message": "这个还没解锁，先去把条件达成。", "data": {}}
                layout = self.state.setdefault("furniture_layout", {})
                was_placed = fid in layout
                if not remove and not was_placed and self._furn_placed_count() + 1 > cap:
                    return {"status": "error", "message": f"摆不下了（同屏上限 {cap} 件），先收回一点。", "data": {}}
                if remove:
                    layout.pop(fid, None)
                else:
                    layout[fid] = {"x": x, "y": y, "s": s, "z": self._furn_next_z(back and not remove)}
                self._save_state()
                if not remove and not was_placed:
                    self._post_feed(str(obj.get("name") or "家具"), "furnplace", 0.0, fid=fid)
            return {"ok": True, "data": {"furniture": self._furniture_payload()}}
        except Exception as e:
            logger.warning("[鱼的窝] 摆放家具失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    async def _web_furn_delete(self, **kwargs) -> dict:
        try:
            try:
                body = await web_request.json(default={})
            except Exception:
                body = {}
            body = body if isinstance(body, dict) else {}
            fid = str(body.get("id") or "")
            cid = fid[2:] if fid.startswith("c_") else fid
            before = len(self.furn_lib)
            self.furn_lib = [z for z in self.furn_lib if str(z.get("id")) != cid]
            if len(self.furn_lib) == before:
                return {"status": "error", "message": "没这件家具", "data": {}}
            self._furn_lib_save()
            try:
                os.remove(self._furn_img_path(cid))
            except Exception:
                pass
            # 她那边：这件的记忆行直接抹掉（新旧两袋都扫），不吭声
            for bag in ("feed_memory", "furn_memory"):
                fm = self.state.get(bag) or []
                kept = [r for r in fm
                        if not (isinstance(r, dict) and str(r.get("fid") or "") == cid
                                and str(r.get("kind") or "") in ("furniture", "furnplace"))]
                if len(kept) != len(fm):
                    self.state[bag] = kept
                    self._save_state()
            self._dbg("删除家具 %s", cid)
            return {"ok": True, "data": self._furn_lib_view()}
        except Exception as e:
            logger.warning("[鱼的窝] 删除家具失败: %s", e)
            return {"status": "error", "message": str(e), "data": {}}

    # ---------------- 聊天 / 提示 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        try:
            if not bool(self._cfg("enabled", True)):
                return
            if not self._is_owner(event):
                return
            text = (getattr(event, "message_str", "") or "").strip()
            if not text or self._is_command(text):
                return
            # 「等你说完」用的计数器：连发时她只答最后一句
            try:
                _now = now_ts()
                try:
                    _gap = 5.0          # 4 秒 + 1 秒容差
                except Exception:
                    _gap = 6.0
                _prev = float(getattr(self, "_chat_ts", 0) or 0)
                self._chat_seq = int(getattr(self, "_chat_seq", 0) or 0) + 1
                self._burst_n = (int(getattr(self, "_burst_n", 0) or 0) + 1
                                 if (_prev and _now - _prev <= _gap) else 1)
                self._chat_ts = _now
                # 他离开了多久才回来：按时间差决定她怎么接
                _last = float(((self.state.get("chat") or {}).get("last_ts")) or 0)
                if _last and _now - _last >= 3600:
                    _hours = round((_now - _last) / 3600.0, 1)
                    self.state["away"] = {"hours": _hours, "ts": _now, "seen_ts": _now}
                    if _hours >= 8:
                        self._pending("back", f"你回来了。她等了 {int(_hours)} 个小时，没催过。")
            except Exception:
                pass
            self._roll_day()
            sig_now = now_ts()
            new_tags = self._record_signals(text, sig_now)
            self._ambient_msg_touch(sig_now)
            if new_tags:
                self._save_state()
                self._check_her_gifts()
                urgent = [t for t in new_tags if t in DANGER_TAGS]
                if urgent:
                    self._letters_check(sig_now, urgent)
            now = now_ts()
            today = today_str()
            chat = self.state.setdefault("chat", {"total": 0, "days": {}, "last_ts": 0.0})
            days = chat.setdefault("days", {})
            days[today] = int(days.get(today, 0) or 0) + 1
            chat["total"] = int(chat.get("total", 0) or 0) + 1
            chat["last_ts"] = now
            if str(self.state.get("first_day") or "") != today:
                self.state["first_day"] = today
                self._wish_gain(2, "今天第一次找她")
            self._wish_gain_chat()
            self._check_decay(now)
            self._check_timely_reply(now)
            cur_act = self.state.get("activity") or {}
            if str(cur_act.get("name") or "") and not float(cur_act.get("ended_ts", 0) or 0):
                cur_act["ended_ts"] = now
                self.state["activity"] = cur_act
            self._check_daily_wish_text(text)
            self._favor_from_chat(text)
            self._check_thoughts()
            self._update_peaks(self._heart_snapshot())
            self._check_gifts()
            self.growth_record("chat", quote=text[:60], source="msg")
            if any(w in text for w in GOODNIGHT_WORDS):
                card = self._growth_settle("晚安")
                if card:
                    self._pending("card", f"她把今天存成了一张卡：{card.get('headline')}")
            if hour_now() >= 22 or hour_now() < 3:
                if str(self.state.get("late_day") or "") != today:
                    self.state["late_day"] = today
                    self.state["late_nights"] = int(self.state.get("late_nights", 0) or 0) + 1
            self._check_achievements()
            self._save_state()
        except Exception as e:
            logger.warning("[鱼的窝] 聊天计数异常: %s", e)

    @filter.on_waiting_llm_request()
    async def on_waiting_llm(self, event: AstrMessageEvent) -> None:
        """等你说完：你连着发好几条，她只在你说完之后答一次。

        默认关着——它会拦掉一部分请求，万一判断出错就是"她不回我了"。
        开之前先确认延迟能接受：设置里 chat.wait_done = true。"""
        try:
            if False:
                return
            if not self._is_owner(event):
                return
            try:
                wait = 4.0
                maxn = 8
            except Exception:
                wait, maxn = 5.0, 8
            # 连续拦太多次就先放行一次，免得把对话彻底堵死
            if int(getattr(self, "_stops", 0) or 0) >= 6:
                self._stops = 0
                return
            my_seq = int(getattr(self, "_chat_seq", 0) or 0)
            if my_seq <= 0:
                return
            if int(getattr(self, "_burst_n", 1) or 1) > maxn:
                return                       # 说太多了，先答一句
            started = now_ts()
            # 她头上先冒三点：这段时间她在听/在想
            self.state["typing_ts"] = started
            await asyncio.sleep(wait)
            newer = int(getattr(self, "_chat_seq", 0) or 0) > my_seq
            # 必须是"我等的这段时间里"真的又来了新消息，才算你在接着说
            fresh = float(getattr(self, "_chat_ts", 0) or 0) > started
            if newer and fresh:
                self.state["typing_ts"] = 0
                self._stops = int(getattr(self, "_stops", 0) or 0) + 1
                try:
                    event.stop_event()
                except Exception:
                    pass
        except Exception as e:
            logger.warning("[鱼的窝] 等你说完失败: %s", e)

    @filter.on_llm_request(priority=0)
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        try:
            self._remember_umo(event)
            # 记一笔「她正在想怎么回」——前端只在这段时间冒三点
            try:
                if bool(self._cfg("enabled", True)) and self._is_owner(event):
                    self.state["typing_ts"] = now_ts()
            except Exception:
                pass
            if not bool(self._cfg("enabled", True)):
                return
            if not bool(self._cfg("notify.inject_hint", True)):
                return
            if not self._is_owner(event):
                return
            self._roll_day()
            name = str(self._cfg("panel.character_name", "她") or "她")
            pending = [p for p in (self.state.get("pending") or []) if isinstance(p, dict) and not p.get("notified")]
            dw = self.state.get("daily_wish") or {}
            wdef = self._daily_wish_def() or {}
            daily_hint = (
                bool(wdef)
                and not dw.get("done")
                and not dw.get("hinted")
                and hour_now() >= 18
            )
            # 喂过/画过什么：两袋近账各出最后 3 条（她真的记得，不只是最近一小时）
            _rel = self._rel_time
            act = self._activity_fact()
            band_now = self._band()

            meters = self.state.get("meters") or {}
            levels = self.state.get("meter_levels") or {}
            owned = [o for o in self._furniture_payload() if o.get("unlocked")]
            lines = ["", f"## {name}这边刚发生的事（内部事实，别照抄，别提面板/系统/插件）"]
            life_rows = [
                r for r in (self.state.get("life_log") or [])
                if isinstance(r, dict) and str(r.get("text") or "").strip()
            ][-4:]
            if life_rows:
                for row in life_rows:
                    lines.append(f"- {_rel(row.get('ts'))}：{row.get('text')}")
            lines.extend(self._feed_memory_lines())
            if act:
                lines.append(f"- 你正在做的事：{self._activity_fact_line(act)}")
            for p in pending[:2]:
                # 面板里是第三人称（她），塞进你自己的上下文时改成第二人称
                text = str(p.get("text") or "").replace("她", "你")
                lines.append(f"- {text}")
            if daily_hint:
                lines.append(f"- 你今天想要：{wdef.get('text')}（{wdef.get('hint')}）")
            if owned:
                lines.append(f"- 你房间里现在有：{'、'.join(o['name'] for o in owned)}")
            lines.append(
                f"- 你现在的进度：亲密度 {fmt_num(float(meters.get('intimacy', 0) or 0))}"
                f"/{int(levels.get('intimacy', 0) or 0)}层，喜悦 {fmt_num(float(meters.get('joy', 0) or 0))}"
                f"/{int(levels.get('joy', 0) or 0)}层"
            )
            lines.append(f"用{name}自己的语气，最多自然带一句，别复述数字、别念清单。")
            band = self._band()
            if band == "shut":
                lines.append("- 你现在在躲着对方（回避期）。这轮对话你只回很短的话（一个字到半句），不回礼物不接吃的；"
                             "对方如果真诚来哄（敲门/纸条/热水/软话），允许你松一点点，但一次哄不好。别解释规则，别提到面板。")
            elif band == "sulk":
                lines.append("- 你在赌气，但没不理人：话可以短、可以阴阳一下，最后还是会回应。")
            amb_act = self._ambient_now(now_ts())
            if amb_act.get("name"):
                lines.append(f"- 被问\"在干嘛\"时照实说：你正在{self.ACTIVITY_LABELS.get(str(amb_act['name']), str(amb_act['name']))}，"
                             f"已经 {int(amb_act.get('mins', 0) or 0)} 分钟。")
            # 状态摘要：让她“知道”面板上的事（用她自己的口吻说，不报数字）
            try:
                _fav = self._favor()
                _bal = float(self.state.get("balance", 0) or 0)
                _mode = str(self.state.get("favor_tier_mode") or "")
                _mode_cn = {"wronged": "有点委屈", "angry": "在生气", "afraid": "有点怕你",
                            "shut": "躲起来不想说话"}.get(_mode, "")
                _tier = _mode_cn or ("正常" if _fav >= 10 else ("已经很小很小了" if _fav >= 0 else "躲起来了"))
                _bits = [f"你对他的好感大概是{_tier}", f"他手上的余额约 {int(_bal)} 块"]
                _diy = list(self.state.get("diy_log") or [])
                if _diy:
                    _l = _diy[-1]
                    _bits.append(f"他上次亲手给你做的是「{str(_l.get('raw') or '')[:20]}」")
                _unread = len([x for x in (self.state.get("letters") or []) if isinstance(x, dict) and not x.get("read")])
                if _unread:
                    _bits.append(f"你有 {_unread} 封信还没被读过")
                _away = self.state.get("away") or {}
                _ah = float(_away.get("hours", 0) or 0)
                if _ah >= 1:
                    if _ah >= 72:
                        _bits.append(f"他有 {int(_ah / 24)} 天没来了，你等得有点久")
                    elif _ah >= 24:
                        _bits.append(f"他隔了一天多才回来")
                    elif _ah >= 8:
                        _bits.append(f"他走了 {int(_ah)} 个小时才回来")
                    else:
                        _bits.append(f"他刚离开了 {int(_ah)} 个小时")
                lines.append("- 你现在知道的（自然地说，别报数字）： " + " · ".join(_bits))
                # 离线那会儿你说的话：她上线后补看到了，可以自然接上
                _off = [x for x in (self.state.get("offline") or []) if isinstance(x, dict)]
                if _off:
                    _txt = "；".join(f"「{str(x.get('text') or '')[:28]}」" for x in _off[-6:])
                    lines.append(f"- 你不在线的时候他说过：{_txt}（当时没收到回复，可以顺着接一句）")
                    self.state["offline"] = []
            except Exception:
                pass
            req.system_prompt = (getattr(req, "system_prompt", "") or "").rstrip() + "\n" + "\n".join(lines)
            for p in pending:
                p["notified"] = True
            if daily_hint:
                dw["hinted"] = True
            self._save_state()
        except Exception as e:
            logger.warning("[鱼的窝] 注入提示异常: %s", e)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response=None) -> None:
        """她说完了：三点气泡收掉。"""
        try:
            if not self._is_owner(event):
                return
            self.state["typing_ts"] = 0
            self.state["typing_done_ts"] = now_ts()
            self._stops = 0        # 回出来了就别再拦
        except Exception:
            pass

    # ---------------- 命令 ----------------

    def _status_text(self) -> str:
        self._roll_day()
        self._update_peaks(self._heart_snapshot())
        self._check_gifts()
        self._save_state()
        stats = self._stats()
        meters = self.state.get("meters") or {}
        levels = self.state.get("meter_levels") or {}
        dw = self.state.get("daily_wish") or {}
        wdef = self._daily_wish_def() or {}
        gifts = [self._gift_payload(g, stats) for g in self.catalog]
        sendable = [g for g in gifts if g["can_send"]]
        slot = lambda k: (  # noqa: E731
            f"{METER_LABELS[k]} {fmt_num(float(meters.get(k, 0) or 0))}/100 · {int(levels.get(k, 0) or 0)}层"
        )
        lines = [
            "【鱼的窝】",
            f"她今天想要：{wdef.get('text') or '没什么特别想要的'}"
            + ("（已完成）" if dw.get("done") else f"（{wdef.get('hint') or ''}）"),
            slot("intimacy") + f"（历史最高 {fmt_num(stats['intimacy_peak'])}）",
            "｜".join(slot(k) for k in ("joy", "attachment", "soothe")),
            f"攒满一轮 → 她的心 +{fmt_num(FEED_STEP)}｜已攒满 {int(self.state.get('fed_total', 0) or 0)} 次",
            f"今日聊天 {stats['chat_today']}｜总计 {stats['chat_total']}｜连续 {stats['streak']} 天",
        ]
        if sendable:
            stock = "、".join(f"{g['emoji']} {g['name']}×{g['count']}" for g in sendable[:4])
            lines.append(f"可送：{stock}")
        else:
            lines.append("可送：暂时没有，去陪她聊聊天吧")
        nxt = next((g for g in gifts if not g["unlocked"] and not g["hidden"]), None)
        if nxt:
            lines.append(f"下一份：{nxt['name']}（{nxt['source']}，{int(nxt['progress'] * 100)}%）")
        lines.append("/奖励 送 糖果　/奖励 记录　/奖励 面板")
        return "\n".join(lines)

    def _send_text(self, result: dict) -> str:
        """送礼回执：本地槽涨了多少、攒满了没有、反哺了鱼的心多少。"""
        meters = result.get("meters") or {}
        levels = self.state.get("meter_levels") or {}
        cur = meters.get("intimacy") or {}
        text = (
            f"{result.get('line') or '……谢谢'}\n"
            f"亲密 +{fmt_num(float(cur.get('delta', 0) or 0))} → "
            f"{fmt_num(float(cur.get('after', 0) or 0))}/100 · {int(levels.get('intimacy', 0) or 0)}层"
        )
        for key in ("joy", "attachment", "soothe"):
            row = meters.get(key) or {}
            delta = float(row.get("delta", 0) or 0)
            if abs(delta) > 1e-9:
                text += f"｜{METER_LABELS[key]} +{fmt_num(delta)}"
        fed = result.get("fed") or []
        if fed:
            bits = []
            for key in fed:
                if key == "soothe":
                    bits.append(f"安抚攒满 → 她的压力 -{fmt_num(FEED_STEP)}")
                else:
                    field = HEART_FIELD_CN.get(METER_FEED[key][0], METER_FEED[key][0])
                    bits.append(f"{METER_LABELS[key]}攒满 → 她的心 · {field} +{fmt_num(FEED_STEP)}")
            text += "\n" + "；".join(bits)
        return text

    def _history_text(self) -> str:
        rows = [h for h in (self.state.get("history") or []) if isinstance(h, dict)][-5:]
        if not rows:
            return "还没有记录。先从 /奖励 送 糖果 开始吧。"
        out = ["【最近的记录】"]
        for h in reversed(rows):
            when = datetime.fromtimestamp(float(h.get("ts", 0) or 0)).strftime("%m-%d %H:%M")
            if h.get("kind") == "send":
                tail = "｜攒满反哺" if h.get("fed") else ""
                out.append(f"{when} 送她「{h.get('name')}」，亲密 +{fmt_num(float(h.get('delta', 0) or 0))} → {fmt_num(float(h.get('after', 0) or 0))}/100{tail}")
            elif h.get("kind") == "unlock":
                out.append(f"{when} 解锁「{h.get('name')}」（{h.get('source') or ''}）")
        return "\n".join(out)

    @filter.command("创造", alias={"creative"})
    async def creative_cmd(self, event: AstrMessageEvent):
        if not self._is_owner(event) and not self._is_admin(event):
            yield event.plain_result("这个指令是给主人的。")
            return
        raw = (getattr(event, "message_str", "") or "").strip()
        parts = raw.split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""

        def status_text() -> str:
            state = "开" if self.creative else "关"
            return (
                f"【创造模式】现在：{state}\n"
                f"开着的时候：礼物全解锁、库存无限、不吃每日次数和冷却，成就满足就解锁。\n"
                f"存档始终是同一个 state.json，关掉之后进度照样在。\n"
                f"/创造 开　/创造 关"
            )

        if arg in ("开", "on", "1", "打开"):
            self._switch_mode(True)
            yield event.plain_result(status_text())
            return
        if arg in ("关", "off", "0", "关闭"):
            self._switch_mode(False)
            yield event.plain_result(status_text())
            return
        if arg in ("切", "toggle", "换"):
            self._switch_mode(not self.creative)
            yield event.plain_result(status_text())
            return
        yield event.plain_result(status_text())


    @filter.command("成长", alias={"今日", "卡片"})
    async def growth_cmd(self, event: AstrMessageEvent):
        """今日卡 / 当日好感度 / 手动结算 / 导出。"""
        if not self._is_owner(event) and not self._is_admin(event):
            yield event.plain_result("这个是给主人看的。")
            return
        raw = (getattr(event, "message_str", "") or "").strip()
        parts = raw.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        head, _, rest = arg.partition(" ")
        head = head.strip()
        rest = rest.strip()
        if not self._g_on():
            yield event.plain_result("成长模块在配置里被关掉了（growth.enabled）。")
            return
        if not arg or head in ("状态", "status"):
            g = self._growth_payload()
            st = g.get("stats") or {}
            yield event.plain_result(
                f"第 {g['day']} 天（{g['date']}）\n"
                f"今天好感度 {g['favor']} / 上限 {fmt_num(g['cap'])}（涨 {fmt_num(g['earned'])}、花 {fmt_num(g['spent'])}）\n"
                f"今天：聊 {st.get('chat', 0)} 句、喂 {st.get('feed', 0)} 次、送礼 {st.get('gift', 0)} 件、"
                f"她回赠 {st.get('reverse', 0)} 次、被敷衍 {st.get('conflict', 0)} 次\n"
                f"已存 {g['cards_total']} 张卡。{g['next']}"
            )
            return
        if head in ("结算", "晚安", "settle"):
            card = self._growth_settle("命令")
            yield event.plain_result(self._card_brief(card) if card else "今天已经结算过了。")
            return
        if head in ("卡", "卡片", "card"):
            want = rest or "今天"
            cards = [c for c in (self.state.get("daily_cards") or []) if isinstance(c, dict)]
            if want in ("今天", "today"):
                day = today_str()
                card = next((c for c in reversed(cards) if str(c.get("date")) == day), None)
                if card is None:
                    card = self._growth_build_card(day)
            elif want in ("昨天", "yesterday"):
                day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                card = next((c for c in reversed(cards) if str(c.get("date")) == day), None)
            elif want.isdigit():
                card = next((c for c in cards if str(c.get("day")) == want), None)
            else:
                card = cards[-1] if cards else None
            if card is None:
                yield event.plain_result("还没有存下任何卡。说完晚安就会有一张。")
                return
            yield event.plain_result(self._card_brief(card))
            return
        if head in ("导出", "export"):
            path = os.path.join(self.data_dir, f"成长导出-{today_str()}.json")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({
                        "exported_at": now_ts(),
                        "day_index": self.state.get("day_index"),
                        "daily_cards": self.state.get("daily_cards"),
                        "event_days": self.state.get("event_days"),
                        "knowledge": self.state.get("knowledge"),
                        "memories": self.state.get("memories"),
                    }, f, ensure_ascii=False, indent=2)
                yield event.plain_result(f"导出好了：{path}")
            except Exception as e:
                yield event.plain_result(f"导出失败：{e}")
            return
        yield event.plain_result(
            "/成长            今天怎么样\n"
            "/成长 卡 昨天      翻某一天的卡（也能给天数：/成长 卡 3）\n"
            "/成长 结算        现在就结算今天\n"
            "/成长 导出        把卡片和记录导成 JSON"
        )


    @filter.command("奖励", alias={"礼物"})
    async def reward_cmd(self, event: AstrMessageEvent):
        if not self._is_owner(event) and not self._is_admin(event):
            yield event.plain_result("这个面板是给主人的。")
            return
        raw = (getattr(event, "message_str", "") or "").strip()
        parts = raw.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if not arg or arg.lower() in ("状态", "status", "s"):
            yield event.plain_result(self._status_text())
            return
        head, _, rest = arg.partition(" ")
        head = head.strip()
        rest = rest.strip()
        if head in ("送", "送礼", "give", "send"):
            target = rest or ""
            if not target:
                yield event.plain_result("要送哪件？发 /奖励 看看仓库，比如：/奖励 送 糖果")
                return
            result = self._send_gift(target)
            if not result.get("ok"):
                yield event.plain_result(f"没送成：{result.get('error')}")
                return
            text = self._send_text(result)
            if result.get("newly_unlocked_names"):
                text += "\n她还翻出了：" + "、".join(result["newly_unlocked_names"])
            if result.get("daily_wish_done"):
                text += "\n今天的心愿也完成了。"
            gif_path = self._reaction_gif(str(result.get("gift") or ""))
            if gif_path:
                try:
                    yield event.chain_result([Plain(text), Image.fromFileSystem(gif_path)])
                    return
                except Exception as e:
                    logger.warning("[鱼的窝] 发送 GIF 失败，退回文字: %s", e)
            yield event.plain_result(text)
            return
        if head in ("记录", "历史", "history"):
            yield event.plain_result(self._history_text())
            return
        if head in ("面板", "panel", "页面"):
            yield event.plain_result(
                "打开 AstrBot 后台 → 插件 → 鱼的窝 → 点「页面」，就是她的小窝。\n"
                "里面有像素房间、今日心愿、礼物仓库和收礼动画。"
            )
            return
        if head in ("帮助", "help", "?", "？"):
            yield event.plain_result(
                "/奖励　看状态\n/奖励 送 <礼物>　送她一份礼物\n/奖励 记录　最近记录\n/奖励 面板　怎么打开面板"
            )
            return
        # 直接 /奖励 糖果 也当成送礼
        result = self._send_gift(arg)
        if result.get("ok"):
            text = self._send_text(result)
            gif_path = self._reaction_gif(str(result.get("gift") or ""))
            if gif_path:
                try:
                    yield event.chain_result([Plain(text), Image.fromFileSystem(gif_path)])
                    return
                except Exception:
                    pass
            yield event.plain_result(text)
            return
        yield event.plain_result(
            "用法：/奖励 看状态｜/奖励 送 糖果｜/奖励 记录｜/奖励 面板"
        )










































