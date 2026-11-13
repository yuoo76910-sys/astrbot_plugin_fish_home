# -*- coding: utf-8 -*-
"""
「家里」· 落地窗 v4

三件事：
1. 窗外是一整片固定的风景（远山 + 林线 + 草地 + 小屋 + 树 + 篱笆 + 鸟），
   任何天气都画同一套坐标、同一套颜色，只叠「大气雾 + 粒子 + 光」，
   所以白天各种天气的景色是完全对齐的。
2. 玻璃去掉格子，只留质感：微蓝罩色、上左亮边、下右暗边、两道斜向反光。
3. 天气拆成「时段 × 天气」：day / dusk / night × clear / cloudy / overcast /
   rain / thunder / snow / fog，雨雪带强度；夜里月亮按日期算盈亏。

原生 480x270，4x 最近邻 = 1920x1080
"""
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = None
LIVE = None

W, H = 480, 270
S = 4
FLOOR_Y = 108        # 星露谷式室内：墙 108、地 162

# 窗户：小一点，放在墙面左侧
WX0, WY0, WX1, WY1 = 34, 12, 164, 92
FT = 5
IX0, IY0, IX1, IY1 = WX0 + FT, WY0 + FT, WX1 - FT, WY1 - FT
MX = (IX0 + IX1) // 2
MW = 6
BASE_H = 8            # 踢脚线高度

# 窗外风景仍旧按以前设计好的 243x129 画，再整体缩进窗洞。
DX0, DY0, DX1, DY1 = 119, 13, 361, 141
DW, DH = DX1 - DX0 + 1, DY1 - DY0 + 1

LINE = (52, 34, 22)
FRAME_D = (104, 68, 42)
FRAME = (146, 98, 60)
FRAME_L = (188, 138, 90)
FRAME_XL = (214, 168, 116)
WALL = (245, 232, 208)
WOOD_L = (186, 136, 88)
WOOD_D = (120, 78, 46)

BAYER = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], float) / 16.0

# 风景基色（所有天气共用，保证一致）
C_FAR = (150, 162, 190)
C_FARSNOW = (222, 230, 242)
C_MID = (116, 138, 150)
C_FOREST = (80, 116, 88)
C_FOREST_D = (64, 96, 74)
C_MEADOW = (126, 168, 96)
C_MEADOW_D = (106, 146, 84)
C_MEADOW_L = (146, 186, 110)
C_TRUNK = (96, 72, 50)
C_CANOPY = (72, 126, 80)
C_CANOPY_L = (94, 150, 92)
C_CANOPY_D = (56, 104, 68)
C_HWALL = (238, 226, 198)
C_HWALL_D = (214, 198, 170)
C_ROOF = (168, 84, 64)
C_ROOF_D = (134, 64, 50)
C_DOOR = (108, 74, 48)
C_HWIN = (255, 224, 150)
C_FENCE = (152, 118, 78)
C_PATH = (178, 154, 116)

HORIZON = 116
PLUM_BLOOM_TEMP = 5.0        # 冬天低于这个温度梅花才开

SKY = {
    "day":   ((126, 188, 232), (218, 238, 250)),
    "dusk":  ((222, 124, 104), (255, 214, 166)),
    "night": ((12, 18, 38), (46, 58, 92)),
}
BANDS = 8


def mix(a, b, t):
    return tuple(int(round(np.clip(np.array(a, float) * (1 - t) + np.array(b, float) * t, 0, 255)[i])) for i in range(3))


def px(a, x, y, col, alpha=1.0):
    x, y = int(x), int(y)
    if 0 <= x < W and 0 <= y < H:
        if alpha >= 1:
            a[y, x] = col
        else:
            a[y, x] = a[y, x] * (1 - alpha) + np.array(col, float) * alpha


def rect(a, x0, y0, x1, y1, col, alpha=1.0):
    x0, x1 = max(0, int(x0)), min(W - 1, int(x1))
    y0, y1 = max(0, int(y0)), min(H - 1, int(y1))
    if x1 < x0 or y1 < y0:
        return
    if alpha >= 1:
        a[y0:y1 + 1, x0:x1 + 1] = col
    else:
        a[y0:y1 + 1, x0:x1 + 1] = a[y0:y1 + 1, x0:x1 + 1] * (1 - alpha) + np.array(col, float) * alpha


def hline(a, x0, x1, y, col, alpha=1.0):
    rect(a, x0, y, x1, y, col, alpha)


def vline(a, x, y0, y1, col, alpha=1.0):
    rect(a, x, y0, x, y1, col, alpha)


def disc(a, cx, cy, r, col, alpha=1.0):
    for y in range(int(cy - r), int(cy + r) + 1):
        for x in range(int(cx - r), int(cx + r) + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r + r * 0.7:
                px(a, x, y, col, alpha)


def banded(a, x0, y0, x1, y1, top, bot, n=BANDS):
    colors = [mix(top, bot, i / (n - 1)) for i in range(n)]
    h = y1 - y0 + 1
    for i, col in enumerate(colors):
        ya = y0 + h * i // n
        yb = y0 + h * (i + 1) // n - 1
        rect(a, x0, ya, x1, yb, col)
        if i < n - 1:
            nxt = colors[i + 1]
            for x in range(x0, x1 + 1):
                if BAYER[yb % 4, x % 4] < 0.5:
                    px(a, x, yb + 1, nxt)


def haze(a, col, k):
    if k <= 0:
        return
    reg = a[IY0:IY1 + 1, IX0:IX1 + 1]
    a[IY0:IY1 + 1, IX0:IX1 + 1] = reg * (1 - k) + np.array(col, float) * k


# ---------------------------------------------------------------- 固定风景
def mountains(a, rng):
    peaks = [(IX0 + 22, 60), (IX0 + 80, 72), (IX0 + 132, 56), (IX0 + 200, 68)]
    for i in range(IX1 - IX0 + 1):
        x = IX0 + i
        h = 0
        for (pxx, ph) in peaks:
            d = abs(x - pxx)
            h = max(h, ph - d * 0.52)
        if h > 0:
            top = int(HORIZON - h)
            vline(a, x, max(IY0, top), HORIZON, C_FAR)
            if h > 40:
                cap = min(9, 4 + int((h - 40) * 0.22))
                vline(a, x, max(IY0, top), max(IY0, top) + cap, C_FARSNOW)
    # 中景山脊
    for i in range(IX1 - IX0 + 1):
        x = IX0 + i
        h = 16 + int(7 * np.sin(i / 26.0)) + int(4 * np.sin(i / 9.0 + 1.4))
        vline(a, x, HORIZON - h, HORIZON, C_MID)
    hline(a, IX0, IX1, HORIZON, C_MID)


def forest(a, rng):
    """林线：一排小树，压在山脊上"""
    x = IX0 + 2
    while x < IX1 - 2:
        h = int(rng.integers(7, 13))
        w = int(rng.integers(3, 5))
        base = HORIZON + 1
        rect(a, x - w, base - h, x + w, base, C_FOREST)
        rect(a, x - w + 1, base - h - 2, x + w - 1, base - h, C_FOREST)
        rect(a, x - 1, base - h, x + 1, base - h + 2, C_FOREST_D)
        x += int(rng.integers(6, 12))


def meadow(a):
    # 草地边缘起伏
    for i in range(IX1 - IX0 + 1):
        x = IX0 + i
        y = HORIZON + int(2 * np.sin(i / 13.0)) + int(1 * np.sin(i / 5.0))
        vline(a, x, y, IY1, C_MEADOW)
    rect(a, IX0, IY1 - 9, IX1, IY1, C_MEADOW_D)
    # 几丛草 / 小石头
    rng = np.random.default_rng(41)
    for _ in range(26):
        x = int(rng.integers(IX0 + 4, IX1 - 4))
        y = int(rng.integers(HORIZON + 12, IY1 - 2))
        rect(a, x, y, x + 1, y + 1, C_MEADOW_L)
    for (rx, ry) in ((IX0 + 62, IY1 - 8), (IX0 + 148, IY1 - 5), (IX1 - 40, IY1 - 12)):
        rect(a, rx, ry, rx + 3, ry + 1, (150, 150, 150))
        rect(a, rx + 1, ry, rx + 2, ry, (176, 176, 176))


def tree(a, x, base, h, snow=False):
    rect(a, x - 1, base - h + 6, x + 1, base, C_TRUNK)
    # 三团树冠
    rect(a, x - 7, base - h + 2, x + 7, base - h + 10, C_CANOPY)
    rect(a, x - 9, base - h + 8, x + 9, base - h + 14, C_CANOPY)
    rect(a, x - 6, base - h - 5, x + 6, base - h + 4, C_CANOPY)
    rect(a, x - 4, base - h - 8, x + 4, base - h - 1, C_CANOPY)
    # 亮面
    rect(a, x - 6, base - h - 4, x - 1, base - h + 2, C_CANOPY_L)
    rect(a, x - 8, base - h + 9, x - 2, base - h + 12, C_CANOPY_L)
    # 暗面
    rect(a, x + 2, base - h + 3, x + 8, base - h + 12, C_CANOPY_D)
    if snow:
        rect(a, x - 6, base - h - 8, x + 4, base - h - 7, (246, 250, 254))
        rect(a, x - 9, base - h + 8, x + 9, base - h + 9, (246, 250, 254))
        rect(a, x - 7, base - h + 2, x + 7, base - h + 3, (246, 250, 254))


def house(a, lit, snow=False):
    """（已废弃，保留空实现防止旧调用）"""
    return None


def line(a, p0, p1, col, thick=1):
    x0, y0 = p0
    x1, y1 = p1
    n = max(abs(x1 - x0), abs(y1 - y0))
    for i in range(n + 1):
        x = x0 + (x1 - x0) * i // max(1, n)
        y = y0 + (y1 - y0) * i // max(1, n)
        rect(a, x, y, x + thick - 1, y + thick - 1, col)


BLOSSOM_PINK = (240, 172, 190)
BLOSSOM_PALE = (252, 216, 226)
BLOSSOM_CORE = (250, 236, 196)
BRANCH = (74, 54, 44)
BRANCH_L = (104, 78, 62)


def blossom(a, x, y, petal=BLOSSOM_PINK, pale=BLOSSOM_PALE):
    """5 瓣梅花，7x7 左右"""
    # 花瓣（2x2 一块）
    for (dx, dy) in ((0, -3), (3, -1), (2, 3), (-2, 3), (-3, -1)):
        rect(a, x + dx - 1, y + dy, x + dx, y + dy + 1, petal)
    # 内圈亮色
    for (dx, dy) in ((0, -1), (1, 0), (0, 1), (-1, 0)):
        px(a, x + dx, y + dy, pale)
    rect(a, x - 1, y - 1, x, y, pale)
    # 花心
    rect(a, x, y, x, y, (232, 128, 120))


def bud(a, x, y):
    rect(a, x, y, x + 1, y + 1, (196, 116, 140))
    px(a, x, y, (232, 156, 176))


def leafclump(a, x, y, col, col_d, r=3):
    for (dx, dy) in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1)):
        px(a, x + dx, y + dy, col)
    px(a, x + 1, y - 1, col)
    px(a, x - 1, y + 1, col_d)


# 分枝主干（从右下角探进来）—— 按「设计窗洞」DX/DY 定位
PLUM_SPINE = [(DX1 - 2, DY1 - 2), (DX1 - 10, 128), (DX1 - 17, 117), (DX1 - 27, 107),
              (DX1 - 36, 97), (DX1 - 47, 87), (DX1 - 57, 79), (DX1 - 66, 74)]

PLUM_TWIGS = [
    ((DX1 - 17, 117), (DX1 - 12, 108)),
    ((DX1 - 27, 107), (DX1 - 24, 96)),
    ((DX1 - 36, 97), (DX1 - 33, 86)),
    ((DX1 - 47, 87), (DX1 - 44, 76)),
    ((DX1 - 57, 79), (DX1 - 55, 68)),
    ((DX1 - 66, 74), (DX1 - 68, 64)),
    ((DX1 - 27, 107), (DX1 - 36, 112)),
    ((DX1 - 47, 87), (DX1 - 56, 92)),
]

PLUM_TIPS = [(DX1 - 12, 106), (DX1 - 24, 94), (DX1 - 33, 84), (DX1 - 44, 74),
             (DX1 - 55, 66), (DX1 - 68, 62), (DX1 - 37, 112), (DX1 - 57, 92),
             (DX1 - 17, 114), (DX1 - 47, 86), (DX1 - 60, 76)]


def plum_branch(a, state):
    """窗外探进来的一枝梅花。state: bloom / bud / leaf / autumn"""
    for i in range(len(PLUM_SPINE) - 1):
        line(a, PLUM_SPINE[i], PLUM_SPINE[i + 1], BRANCH, 3)
    for i in range(len(PLUM_SPINE) - 1):
        x0, y0 = PLUM_SPINE[i]
        x1, y1 = PLUM_SPINE[i + 1]
        n = max(abs(x1 - x0), abs(y1 - y0))
        for k in range(0, n, 2):
            x = x0 + (x1 - x0) * k // max(1, n)
            y = y0 + (y1 - y0) * k // max(1, n)
            px(a, x, y, BRANCH_L)
    for (p0, p1) in PLUM_TWIGS:
        line(a, p0, p1, BRANCH, 1)

    if state == "bloom":
        for i, (tx, ty) in enumerate(PLUM_TIPS):
            blossom(a, tx, ty, BLOSSOM_PINK if i % 3 else BLOSSOM_PALE)
        for i, (tx, ty) in enumerate(PLUM_TIPS[:5]):
            blossom(a, tx + 4, ty - 5, BLOSSOM_PALE if i % 2 else BLOSSOM_PINK)
        for (tx, ty) in PLUM_TIPS[:4]:
            bud(a, tx - 4, ty + 4)
    elif state == "bud":
        for (tx, ty) in PLUM_TIPS:
            bud(a, tx, ty)
        for (tx, ty) in PLUM_TIPS[:5]:
            bud(a, tx + 3, ty - 4)
    elif state == "leaf":
        for i, (tx, ty) in enumerate(PLUM_TIPS):
            leafclump(a, tx, ty, (104, 160, 96) if i % 2 else (126, 182, 110), (80, 132, 82))
        for i, (tx, ty) in enumerate(PLUM_TIPS[:5]):
            leafclump(a, tx + 4, ty - 5, (126, 182, 110), (86, 140, 88), 2)
    else:  # autumn
        for i, (tx, ty) in enumerate(PLUM_TIPS):
            leafclump(a, tx, ty, (214, 150, 72) if i % 2 else (232, 176, 92), (176, 112, 56))
        for i, (tx, ty) in enumerate(PLUM_TIPS[:4]):
            leafclump(a, tx + 4, ty - 5, (200, 132, 68), (168, 104, 52), 2)


def wildflowers(a, rng, season, snow):
    """草地上的小花"""
    if snow:
        return
    if season == "spring":
        cols = [(250, 216, 226), (252, 236, 176), (232, 176, 200), (246, 246, 240)]
        n = 26
    elif season == "summer":
        cols = [(252, 236, 176), (246, 246, 240), (232, 176, 200)]
        n = 18
    elif season == "autumn":
        cols = [(232, 176, 96), (226, 140, 120)]
        n = 8
    else:  # winter
        cols = [(236, 236, 240)]
        n = 3
    for _ in range(n):
        x = int(rng.integers(IX0 + 6, IX1 - 6))
        y = int(rng.integers(HORIZON + 14, IY1 - 4))
        c = cols[int(rng.integers(0, len(cols)))]
        px(a, x, y, c)
        px(a, x + 1, y, c)
        px(a, x, y + 1, c, 0.6)
        px(a, x + 1, y + 1, (96, 140, 88))
        px(a, x, y + 2, (88, 130, 80))
        if rng.random() < 0.5:
            px(a, x + 4, y + 2, cols[int(rng.integers(0, len(cols)))])
            px(a, x + 5, y + 2, cols[int(rng.integers(0, len(cols)))])


def fence(a, x0, x1, y, snow=False):
    rect(a, x0, y, x1, y + 1, C_FENCE)
    rect(a, x0, y + 5, x1, y + 6, C_FENCE)
    for x in range(x0, x1 + 1, 9):
        rect(a, x, y - 3, x + 1, y + 9, C_FENCE)
        if snow:
            rect(a, x, y - 3, x + 1, y - 2, (248, 251, 255))


def landscape(a, rng, time, snow, season="winter", temp=4.0):
    mountains(a, rng)
    forest(a, rng)
    meadow(a)
    if snow:
        for i in range(IX1 - IX0 + 1):
            x = IX0 + i
            y = HORIZON + int(2 * np.sin(i / 13.0)) + int(1 * np.sin(i / 5.0))
            vline(a, x, y, IY1, (232, 238, 246))
    wildflowers(a, rng, season, snow)
    tree(a, IX0 + 34, IY1 - 1, 34, snow)
    tree(a, IX0 + 84, IY1 - 3, 24, snow)
    fence(a, IX0 + 4, IX0 + 56, IY1 - 12, snow)
    # 梅花枝：冬天够冷才开
    if season == "winter":
        state = "bloom" if temp <= PLUM_BLOOM_TEMP else "bud"
    elif season == "autumn":
        state = "autumn"
    else:
        state = "leaf"
    plum_branch(a, state)
    return state


# ---------------------------------------------------------------- 天气
def sky_for(time, weather):
    day = SKY[time]
    if time == "night":
        return {
            "clear": ((12, 18, 38), (46, 58, 92)),
            "cloudy": ((22, 28, 44), (44, 54, 74)),
            "overcast": ((30, 34, 44), (50, 56, 68)),
            "rain": ((24, 28, 38), (40, 46, 58)),
            "thunder": ((18, 22, 30), (34, 40, 52)),
            "snow": ((44, 54, 74), (86, 98, 120)),
            "fog": ((40, 46, 58), (74, 82, 96)),
        }[weather]
    return {
        "clear": day,
        "cloudy": (mix(day[0], (150, 160, 175), .22), mix(day[1], (188, 196, 208), .28)),
        "overcast": (mix(day[0], (118, 126, 138), .55), mix(day[1], (166, 172, 182), .62)),
        "rain": (mix(day[0], (84, 94, 110), .68), mix(day[1], (128, 138, 152), .70)),
        "thunder": (mix(day[0], (58, 66, 82), .76), mix(day[1], (100, 110, 126), .74)),
        "snow": (mix(day[0], (172, 188, 208), .58), mix(day[1], (224, 232, 242), .62)),
        "fog": (mix(day[0], (192, 200, 208), .62), mix(day[1], (224, 230, 236), .66)),
    }[weather]


def clouds(a, rng, kind, time, sky_bot, shift=0):
    """kind: clear / cloudy / overcast；shift = 云层整体横向位移（像素）"""
    if kind == "clear":
        if time == "day":
            c = [(IX0 + 26, IY0 + 42, 26), (IX1 - 62, IY0 + 56, 22)]
        elif time == "dusk":
            c = [(IX0 + 40, IY0 + 44, 30), (IX1 - 40, IY0 + 34, 24)]
        else:
            c = []
    elif kind == "cloudy":
        c = [(IX0 + 30, IY0 + 26, 34), (IX0 + 96, IY0 + 18, 40),
             (IX0 + 168, IY0 + 30, 32), (IX0 + 60, IY0 + 52, 30),
             (IX1 - 26, IY0 + 42, 26)]
    else:  # overcast
        c = [(IX0 + 26, IY0 + 20, 52), (IX0 + 90, IY0 + 12, 60),
             (IX0 + 160, IY0 + 22, 50), (IX0 + 56, IY0 + 44, 46),
             (IX0 + 130, IY0 + 40, 54), (IX1 - 30, IY0 + 50, 40)]
    light = mix(sky_bot, (255, 255, 255), 0.62 if kind != "overcast" else 0.30)
    dark = mix(sky_bot, (0, 0, 0), 0.16)
    span = IX1 - IX0 + 1

    def crect(x0, y0, x1, y1, col):
        x0 = max(int(x0), IX0)
        x1 = min(int(x1), IX1)
        y0 = max(int(y0), IY0)
        y1 = min(int(y1), IY1)
        if x1 >= x0 and y1 >= y0:
            rect(a, x0, y0, x1, y1, col)

    for (cx, cy, cw) in c:
        cx = IX0 + int((cx - IX0 + shift) % span)
        # 贴边时两侧都画一份，出画面不硬切
        for cx2 in (cx, cx - span, cx + span):
            if cx2 + cw // 2 + 4 < IX0 or cx2 - cw // 2 - 4 > IX1:
                continue
            crect(cx2 - cw // 2, cy, cx2 + cw // 2, cy + 7, light)
            crect(cx2 - cw // 2 + 6, cy - 6, cx2 + cw // 2 - 8, cy + 2, light)
            crect(cx2 - cw // 2 - 4, cy + 3, cx2 + cw // 2 + 4, cy + 8, light)
            crect(cx2 - cw // 2, cy + 8, cx2 + cw // 2, cy + 9, dark)
            crect(cx2 - cw // 2 - 4, cy + 8, cx2 + cw // 2 + 4, cy + 9, dark)


def sun_moon(a, time, weather, phase, rng, moon=True):
    if weather in ("overcast", "rain", "thunder", "fog", "snow"):
        return
    if time == "night":
        if not moon:
            return
        cx, cy, r = IX1 - 46, IY0 + 21, 10
        disc(a, cx, cy, r + 3, mix(SKY["night"][1], (255, 255, 240), 0.30))
        draw_phase_moon(a, cx, cy, r, phase)
    else:
        cx, cy = (IX0 + 96, IY0 + 24) if time == "day" else (IX0 + 54, IY1 - 24)
        r = 10 if time == "day" else 12
        if time == "dusk":
            disc(a, cx, cy, r + 4, mix((255, 208, 140), SKY["dusk"][1], 0.55))
        disc(a, cx, cy, r + 2, (255, 246, 196))
        disc(a, cx, cy, r, (255, 250, 224))
        disc(a, cx, cy, r - 3, (255, 254, 242))


def draw_phase_moon(a, cx, cy, r, phase):
    """phase: 0=新月 0.25=上弦 0.5=满月 0.75=下弦。只画亮面，暗面留给夜空"""
    ang = 2 * np.pi * phase
    c = np.cos(ang)
    for y in range(-r, r + 1):
        w = np.sqrt(max(0.0, r * r - y * y))
        for x in range(-r, r + 1):
            if x * x + y * y > r * r:
                continue
            xt = w * c
            lit = (x > xt) if phase <= 0.5 else (x < -xt)
            if lit:
                px(a, cx + x, cy + y, (250, 248, 232))
    # 月面细节（太小就不点了，免得像噪点）
    if r >= 7:
        for (dx, dy) in ((-3, -3), (2, 1), (-1, 4), (4, -3)):
            if phase <= 0.5:
                px(a, cx + dx, cy + dy, (232, 228, 204))
            else:
                px(a, cx + dx, cy + dy, (228, 224, 200))


def rain(a, rng, intensity=0.6, thunder=False):
    n = int(60 + 260 * intensity)
    for _ in range(n):
        x = int(rng.integers(IX0, IX1))
        y = int(rng.integers(IY0, IY1))
        ln = int(rng.integers(2, 5 + int(3 * intensity)))
        for k in range(ln):
            px(a, x, y + k, (200, 216, 234), 0.5)
    if thunder:
        pts = [(IX0 + 150, IY0 + 12), (IX0 + 146, IY0 + 22), (IX0 + 152, IY0 + 24),
               (IX0 + 144, IY0 + 36), (IX0 + 150, IY0 + 38), (IX0 + 140, IY0 + 52)]
        for i in range(len(pts) - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            steps = max(abs(x1 - x0), abs(y1 - y0))
            for s in range(steps + 1):
                px(a, x0 + (x1 - x0) * s // max(1, steps), y0 + (y1 - y0) * s // max(1, steps), (255, 250, 210))


def snow(a, rng, intensity=0.6):
    n = int(80 + 220 * intensity)
    for _ in range(n):
        x = int(rng.integers(IX0, IX1))
        y = int(rng.integers(IY0, IY1))
        px(a, x, y, (255, 255, 255), 0.95)
        if rng.random() < 0.45:
            px(a, x + 1, y, (255, 255, 255), 0.85)
            px(a, x, y + 1, (255, 255, 255), 0.85)
            px(a, x + 1, y + 1, (238, 245, 252), 0.75)


def fog(a, rng, time):
    for i in range(5):
        y = IY0 + 34 + i * 18
        rect(a, IX0, y, IX1, y + 9, mix(SKY[time][1], (255, 255, 255), 0.35), 0.16)


# ---------------------------------------------------------------- 玻璃
def glass(a, time):
    """只要质感：罩色 + 亮暗边 + 两道斜反光，不要格子"""
    rect(a, IX0, IY0, IX1, IY1, (176, 220, 242), 0.05)
    # 上左亮边 / 下右暗边
    hline(a, IX0, IX1, IY0, (238, 250, 255), 0.45)
    vline(a, IX0, IY0, IY1, (238, 250, 255), 0.45)
    hline(a, IX0, IX1, IY1, (128, 168, 196), 0.32)
    vline(a, IX1, IY0, IY1, (128, 168, 196), 0.32)
    # 斜向反光（两条，硬边）
    for band, (off, hgt, al) in enumerate(((26, 7, 0.055), (52, 3, 0.035))):
        for d in range(hgt):
            for x in range(IX0, IX1 + 1):
                y = IY0 + off + d + int((x - IX0) * 0.42)
                if IY0 <= y <= IY1:
                    px(a, x, y, (255, 255, 255), al)
    # 角落小亮点
    for i in range(5):
        px(a, IX0 + 12 + i, IY1 - 10 - i, (255, 255, 255), 0.35)
        px(a, IX1 - 16 - i, IY0 + 14 + i, (255, 255, 255), 0.25)


# ---------------------------------------------------------------- 窗框 / 墙 / 地板
def frame_outer(a):
    rect(a, WX0, WY0, WX1, WY1, LINE)
    for (x0, y0, x1, y1) in ((WX0, WY0, WX1, WY0 + FT - 1),
                             (WX0, WY1 - FT + 1, WX1, WY1),
                             (WX0, WY0, WX0 + FT - 1, WY1),
                             (WX1 - FT + 1, WY0, WX1, WY1)):
        rect(a, x0 + 1, y0 + 1, x1 - 1, y1 - 1, FRAME)
        hline(a, x0 + 1, x1 - 1, y0 + 1, FRAME_XL)
        vline(a, x0 + 1, y0 + 1, y1 - 1, FRAME_XL)
        hline(a, x0 + 1, x1 - 1, y0 + 2, FRAME_L)
        vline(a, x0 + 2, y0 + 1, y1 - 1, FRAME_L)
        hline(a, x0 + 1, x1 - 1, y1 - 1, FRAME_D)
        vline(a, x1 - 1, y0 + 1, y1 - 1, FRAME_D)


def mullion(a):
    """中挺（单独抽出来，动画里盖粒子用）"""
    rect(a, MX - MW // 2, IY0, MX + MW // 2 - 1, IY1, LINE)
    rect(a, MX - MW // 2 + 1, IY0, MX + MW // 2 - 2, IY1, FRAME)
    vline(a, MX - MW // 2 + 1, IY0, IY1, FRAME_XL)
    vline(a, MX - MW // 2 + 2, IY0, IY1, FRAME_L)
    vline(a, MX + MW // 2 - 2, IY0, IY1, FRAME_D)




def frame_inner(a):
    mullion(a)
    # 底槛
    rect(a, WX0 - 4, WY1 - 4, WX1 + 4, WY1, LINE)
    rect(a, WX0 - 3, WY1 - 3, WX1 + 3, WY1 - 1, FRAME_L)
    hline(a, WX0 - 3, WX1 + 3, WY1 - 3, FRAME_XL)
    # 窗台在墙上的一点点投影，让窗户看起来是嵌在墙里的
    for i, al in enumerate((0.18, 0.10, 0.05)):
        hline(a, WX0 - 4, WX1 + 4, WY1 + 1 + i, (60, 40, 28), al)


def room(a, rng):
    a[:FLOOR_Y] = WALL
    a[:FLOOR_Y] += rng.integers(-1, 2, size=(FLOOR_Y, W, 1))
    # 墙纸：细竖条 + 越往上越暗
    for x in range(0, W, 26):
        a[:FLOOR_Y, x] *= 0.965
        a[:FLOOR_Y, x + 1] *= 0.985
    for y in range(FLOOR_Y):
        a[y] *= 1 - 0.10 * (1 - y / float(FLOOR_Y))
    for y in range(6):
        a[y] *= 1 - 0.03 * (6 - y)
    # 踢脚线整条拉通
    a[FLOOR_Y - BASE_H:FLOOR_Y - BASE_H + 2, :] = np.array(WOOD_L, float)
    a[FLOOR_Y - BASE_H + 2:FLOOR_Y - 1, :] = np.array(WOOD_D, float)
    a[FLOOR_Y - 1, :] = np.array(LINE, float)
    for d in range(4):
        a[FLOOR_Y - BASE_H - 1 - d, :] *= 1 - 0.07 * (1 - d / 4)
    # 墙脚一圈环境阴影，墙才像立在前面
    for i in range(14):
        y = FLOOR_Y - BASE_H - 1 - i
        if y >= 0:
            a[y] *= 1 - 0.09 * (1 - i / 14.0)
    a[:FLOOR_Y] = np.clip(a[:FLOOR_Y], 0, 255)


def floor(a, native=None, rng_seed=12):
    """星露谷式木地板：横向长板，工字错缝（奇数行挪半块），越靠前越高"""
    h = H - FLOOR_Y
    yy, xx = np.mgrid[FLOOR_Y:H, 0:W]

    # 行：越靠前越高（轻微透视）
    edges = [FLOOR_Y]
    step = 9.0
    while edges[-1] < H - 1:
        nxt = int(round(edges[-1] + step))
        edges.append(min(max(nxt, edges[-1] + 1), H - 1))
        step *= 1.055
    edges = np.array(edges, dtype=int)
    row = np.clip(np.searchsorted(edges, yy, side="right") - 1, 0, len(edges) - 2)

    # 工字拼：每块 120 长，奇数行整体挪 60
    spacing = 120.0
    off = (row % 2) * (spacing / 2.0)
    plank = np.floor((xx - off) / spacing).astype(np.int32)
    cols = np.array([(170, 124, 86), (167, 121, 83), (173, 127, 89)], float)
    a[FLOOR_Y:] = cols[(row + plank) % 3]

    seam = np.array((136, 98, 70), float)
    for y in edges[1:-1]:
        a[int(y)] = seam
    for y in range(FLOOR_Y, H):
        r = int(np.clip(np.searchsorted(edges, y, side="right") - 1, 0, len(edges) - 2))
        if y in edges:
            continue
        x0 = int((r % 2) * 60) % 120
        for x in range(x0, W, 120):
            a[y, x] = seam

    rng = np.random.default_rng(rng_seed)
    for _ in range(80):
        x = int(rng.integers(0, W - 8))
        y = int(rng.integers(FLOOR_Y + 4, H - 1))
        ln = int(rng.integers(3, 9))
        a[y, x:x + ln] *= 0.95

    # 近处稍亮
    for i, y in enumerate(range(FLOOR_Y, H)):
        a[y] *= 0.97 + 0.05 * (i / float(h))

    # 墙脚投影
    for i in range(12):
        a[FLOOR_Y + i] *= 1 - 0.25 * (1 - i / 12.0)


def _ambient(time, weather, lights):
    if lights:
        base = {
            "day":   (np.array((1.04, 1.00, 0.94)), np.array((8, 6, 2)), (255, 236, 190), 0.30),
            "dusk":  (np.array((1.06, 0.98, 0.90)), np.array((14, 6, 0)), (255, 200, 140), 0.42),
            "night": (np.array((0.62, 0.56, 0.48)), np.array((26, 18, 10)), (255, 200, 140), 0.55),
        }[time]
    else:
        base = {
            "day":   (np.array((1.00, 1.00, 0.99)), np.array((4, 4, 3)), (255, 246, 220), 0.22),
            "dusk":  (np.array((1.03, 0.96, 0.90)), np.array((8, 3, 0)), (255, 190, 136), 0.34),
            "night": (np.array((0.30, 0.31, 0.38)), np.array((4, 3, 7)), (140, 166, 214), 0.18),
        }[time]
    tint, add, pool, amp = base
    k = {"clear": 1.0, "cloudy": 0.94, "overcast": 0.84, "rain": 0.74,
         "thunder": 0.70, "snow": 0.95, "fog": 0.88}[weather]
    if weather in ("rain", "thunder"):
        add = add + np.array((0, 0, 4))
    if weather == "snow":
        add = add + np.array((4, 5, 7))
    if weather == "fog":
        add = add + np.array((4, 4, 4))
    return tint * k, add, pool, amp




def light(a, time, weather, lights=False):
    tint, add, pool, amp = _ambient(time, weather, lights)
    a *= tint[None, None, :]
    a += add[None, None, :]

    xs = np.arange(W)
    for y in range(FLOOR_Y, H):
        t = (y - FLOOR_Y) / float(H - FLOOR_Y)
        half = 120 + t * 28
        cxs = (WX0 + WX1) / 2 + t * 4
        d = np.abs(xs - cxs) / half
        m = np.clip(1 - d ** 2, 0, 1) * (1 - t * 0.6) * amp
        a[y] += m[:, None] * np.array(pool, float) * 0.30

    # 开灯：天花板方向来一团暖光，把屋子照亮
    if lights:
        xs2 = np.arange(W)[None, :]
        ys2 = np.arange(H)[:, None]
        dx = (xs2 - W * 0.5) / 235.0
        dy = (ys2 - 26.0) / 215.0
        m = np.clip(1 - (dx * dx + dy * dy), 0, 1) ** 2
        a += m[..., None] * np.array((52, 34, 14), float)
        a *= 1.06


# ---------------------------------------------------------------- 组装
NAT = None


def load_native(floor_path=None):
    global NAT
    p = Path(floor_path) if floor_path else Path('floor-on.png')
    NAT = np.array(Image.open(p).convert('RGB').resize((W, H), Image.NEAREST), dtype=np.float64)


WEATHER_KIND = {
    "clear": "clear", "cloudy": "cloudy", "overcast": "overcast",
    "rain": "overcast", "thunder": "overcast", "snow": "overcast", "fog": "overcast",
}
HAZE = {"clear": 0.0, "cloudy": 0.10, "overcast": 0.24, "rain": 0.30,
        "thunder": 0.32, "snow": 0.28, "fog": 0.52}


def render_outside(time, weather, intensity=0.6, phase=0.5, season="winter",
                   temp=4.0, cloud_shift=0):
    """按设计尺寸 DX0..DY1 画窗外，返回那一块（原尺寸，还没缩）"""
    global IX0, IY0, IX1, IY1, MX
    saved = (IX0, IY0, IX1, IY1, MX)
    IX0, IY0, IX1, IY1 = DX0, DY0, DX1, DY1
    MX = (IX0 + IX1) // 2
    try:
        buf = np.zeros((H, W, 3), dtype=np.float64)
        rng = np.random.default_rng(abs(hash((time, weather, season))) % (2 ** 31))

        top, bot = sky_for(time, weather)
        banded(buf, IX0, IY0, IX1, IY1, top, bot)

        sun_moon(buf, time, weather, phase, rng, moon=False)
        clouds(buf, rng, WEATHER_KIND[weather] if weather != "thunder" else "overcast",
               time, bot, shift=cloud_shift)
        landscape(buf, rng, time, snow=(weather == "snow"), season=season, temp=temp)

        hz = mix(bot, (255, 255, 255), 0.18) if time != "night" else mix(bot, (110, 130, 170), 0.30)
        haze(buf, hz, HAZE[weather])

        if weather in ("rain", "thunder"):
            rain(buf, rng, intensity, thunder=(weather == "thunder"))
        elif weather == "snow":
            snow(buf, rng, intensity)
        elif weather == "fog":
            fog(buf, rng, time)

        crop = buf[DY0:DY1 + 1, DX0:DX1 + 1].copy()
    finally:
        IX0, IY0, IX1, IY1, MX = saved
    return crop


def build(time, weather, intensity=0.6, phase=0.5, season="winter", temp=4.0,
          cloud_shift=0, dim=1.0, lights=False):
    rng = np.random.default_rng(abs(hash((time, weather, season))) % (2 ** 31))
    a = np.zeros((H, W, 3), dtype=np.float64)
    room(a, rng)
    frame_outer(a)

    # 窗外：画好一整张，再缩进小窗洞
    crop = render_outside(time, weather, intensity, phase, season, temp, cloud_shift)
    gw, gh = IX1 - IX0 + 1, IY1 - IY0 + 1
    small = Image.fromarray(np.clip(crop, 0, 255).astype(np.uint8)).resize((gw, gh), Image.NEAREST)
    a[IY0:IY1 + 1, IX0:IX1 + 1] = np.asarray(small, dtype=np.float64)

    glass(a, time)
    frame_inner(a)
    floor(a, NAT)

    # 窗外的光不跟着室内灯变：先把窗洞存下来，室内照亮之后再按“没开灯”的夜色贴回去
    win = a[IY0:IY1 + 1, IX0:IX1 + 1].copy()
    light(a, time, weather, lights)
    if lights:
        tint, add, _, _ = _ambient(time, weather, False)
        a[IY0:IY1 + 1, IX0:IX1 + 1] = np.clip(win * tint + add, 0, 255)

    # 月亮也跟着缩到窗洞里，位置按比例放，光晕不贴框
    sx, sy = gw / DW, gh / DH
    mx = int(IX0 + gw * 0.78)
    my = int(IY0 + gh * 0.30)
    emissive(a, time, weather, phase, mx, my, max(5, int(round(9 * sy))))

    if dim != 1.0:
        a *= dim
    return np.clip(a, 0, 255).astype(np.uint8)


def emissive(a, time, weather, phase, cx, cy, r):
    """夜里月亮是自发光，压暗之后再补回来；阴雨雪雾天月亮被云挡住，不画。
    光晕只在玻璃范围内画，不然会糊到窗框上（穿帮）。"""
    if time != "night" or weather in ("overcast", "rain", "thunder", "snow", "fog"):
        return
    for rr, al in ((r + 4, 0.10), (r + 2, 0.06)):
        for y in range(cy - rr, cy + rr + 1):
            if y < IY0 or y > IY1:
                continue
            for x in range(cx - rr, cx + rr + 1):
                if x < IX0 or x > IX1:
                    continue
                if (x - cx) ** 2 + (y - cy) ** 2 <= rr * rr:
                    px(a, x, y, (255, 250, 224), al)
    draw_phase_moon(a, cx, cy, r, phase)


def save(arr, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(path.with_name(path.stem + "_480x270.png"))
    Image.fromarray(arr).resize((W * S, H * S), Image.NEAREST).save(path.with_name(path.stem + "_1920x1080.png"))


def main():
    load_native()
    out = ROOT / "png_落地窗v4"
    jobs = [("day", "clear"), ("day", "cloudy"), ("day", "overcast"), ("day", "rain"),
            ("day", "snow"), ("day", "fog"), ("dusk", "clear"), ("night", "clear")]
    for t, w in jobs:
        save(build(t, w), out / f"{t}_{w}")
        print("ok", t, w)


if __name__ == "__main__":
    main()

