# 鱼的窝 · astrbot_plugin_fish_home

一个 AstrBot 插件：给你的机器人搭一间**能住人的像素小窝**。她有自己的心情、好感度、工资和晚饭；你在聊天里说的话她都会记账，房间里摆了什么东西她也知道。

当前版本 **v0.2.1**。详细玩法与安装见 [README_发布说明.txt](README_发布说明.txt)，规则数字的集中速查见 [docs/玩法速查.md](docs/玩法速查.md)。

## 它是什么

- **养成**：好感度按每句话实时结算（认真说话涨、凶她掉），赌气和「屋里没有人」都是真的。
- **生活**：每天有工资、会做饭吃饭、吃多了同一种会腻；晚上可能给你写信（信箱，一周最多两封，可加急）。
- **DIY**：一句话描述 + 你打的分（−10~+20），她按自己的口味表决定惊喜还是嫌弃。
- **家具工坊**：一句话 → AI 出像素家具 → 拖进房间随便摆。
  - 两条生成路线：LLM 施工图（默认，零新增 key）／精绘模式（接一个生图 API，兼容 OpenAI-style 与火山方舟 Ark，豆包 Seedream 可直接用；本地自动抠图/降采样/限色/描边）。
  - 想画什么就画什么：家具、动物、食物、一张脸——主体完全跟着你的描述走。
  - 叠放：松手的永远在最上层，右键压到最底，位置、大小、层序全部存档。
  - 她知道屋里有什么：画成、首摆会按人设主动提一嘴，删除则静默遗忘。

## 聊天里的命令

```
/奖励          看仓库、送东西（别名 /礼物；/奖励 送 草莓糖）
/成长          今日摘要；/成长 卡 昨天；/成长 结算；/成长 导出
/创造          创造 / 测试模式开关（别名 /creative）
```

## 安装

1. 需要前置插件 **astrbot_plugin_fish_heart**（她的心情、九维、责难数据源；没装的话面板能开但状态恒为默认）。

   <!-- TODO: 这里原来是个空占位链接 https://github.com/ ，请补上鱼的心仓库地址 -->
2. 把本仓库克隆/解压到 AstrBot 的 `data/plugins/astrbot_plugin_fish_home/`。
3. 重启 AstrBot，后台插件列表 → 鱼的窝 → 点「页面」打开小窝。

升级直接整目录覆盖，存档在 `data/plugin_data/astrbot_plugin_fish_home/`，不会丢。

## 配置

设置项都在插件配置里（`_conf_schema.json` 有说明），关键几处：

- `主人会话`：好感度、离线补看、信件只对这一个会话生效。
  填的是统一会话串，形如 `平台名:FriendMessage:你的QQ号`，三段都要写全 —— 少一段会导致既计不到聊天、也发不出主动消息。
- `家具工坊 · 精绘模式`：生图 api_key / base_url / model。留空 = 用默认 LLM 施工图路线；填了才启用精绘。Ark 的 AgentPlan 套餐 key 记得把 base_url 填成 `.../api/plan/v3`。
- `furn_gen.daily_limit / max_items`：默认 0 = 不限次；`max_placed` 默认 20（同屏摆放上限）。

密钥只存在你自己的 AstrBot 配置文件里，仓库和插件目录都不含任何 key。

## 数据在哪

```
data/plugin_data/astrbot_plugin_fish_home/state.json             主存档
data/plugin_data/astrbot_plugin_fish_home/custom_furniture.json  家具工坊的库（施工图，唯一真源）
data/plugin_data/astrbot_plugin_fish_home/furniture_imgs/        渲染出来的像素 PNG（可重生成）
```

想从头开始玩，删掉 `state.json` 即可；`samples/` 里有一份「正常玩法」示例存档。
