<p align="center">
  <img src="logo.png" width="160" alt="大肥鱼表情包">
</p>

# 大肥鱼表情包 · astrbot_plugin_nekowhale_stickers

让 Bot 说话自带表情包，还能把一句话贴进表情里画一张新的。

插件**只含代码**（约 120 KB），**不含任何表情文件**：清单、标注、GIF 全部在运行时
按需从 [kiriyuki.com](https://kiriyuki.com) 拉取，用到哪张下哪张。

> 素材署名：表情作者 B站 **可以叫我云大人**（<https://space.bilibili.com/103739>），
> kiriyuki.com 制作。形象基于 ZipZipPipe 与 上善无形。
> 完整许可见 [NOTICE](NOTICE) 与 [LICENSE-ASSETS](LICENSE-ASSETS)。

---

## 安装

**方式一（推荐）**：AstrBot 面板 → 插件 → **「通过 Git 克隆安装」**，填

```text
https://github.com/thermallemon/astrbot_plugin_nekowhale_stickers
```

> 「从链接安装」入口走 httpx 直接下 zip，要填归档地址（见方式二），别填仓库主页。

**方式二**：下载仓库 zip（`https://github.com/thermallemon/astrbot_plugin_nekowhale_stickers/archive/refs/heads/main.zip`）
→ 面板「上传安装」→ 选 zip。

要求：AstrBot **>= 4.11.0**。**不需要 Node**，贴字表情是纯 Python 渲染。

可选依赖（想让贴字表情和官方效果逐像素一致时装上）：

```bash
pip install fonttools brotli
```

不装也能用 —— 字体分片需要 fontTools 解码 woff2，缺了会自动退回系统字体
（Windows 用楷体/雅黑，Linux 用 Noto CJK），排版一致，只是字形略不同。

## 装好之后

```text
/表情自检          看清单、缓存、向量、贴字模板是否就绪
/表情下载 6        手动预热 6 张（不占启动时间）
/表情下载 全部     把整套下到本地（约 388 MB，只在想离线用时需要）
/表情图 想 今天不想上班
/表情署名          看作者与许可
```

首条回复如果向量库还没建好，会自动先用关键词选图（`selector=自动` 时），不会卡住。

## 两个能力

### 1. 自动配图

每条回复之后，按语境挑一张大肥鱼补在后面。

- 正文先发出去，表情随后补一条，不拖慢回复
- 模型自己发过图就自动让位，不会一条回复两张图
- 有最小间隔、防重复窗口、情绪排除、会话黑名单
- 双人表情（大肥鱼和GPT娘）按 `dual_weight` 降权，默认 0.85

选图三种方式（面板里是中文下拉）：

| 选项 | 做什么 | 花多少 |
| --- | --- | --- |
| `自动`（默认） | 向量库就绪就用向量；没就绪先用关键词，不卡首条回复 | 视情况 |
| `向量选图` | 向量召回 10 张 → 对话模型挑一张 | 每条回复几百 token |
| `关键词选图` | 本地关键词打分，随机取 | 零 token |

### 2. 贴字表情

把一句话贴进表情里，生成一张新 GIF 发出去。

```text
/表情图 想 今天不想上班
/表情图 举牌 今天也要开心
/表情图 葱卷 一句话可以很长很长
/表情图 上联 恭喜发财
/表情图 抱            ← 用发送者的 QQ 头像
```

模板：`想`(think) / `举牌`(sign) / `葱卷`(scroll) / `上联`(couplet_top) /
`下联`(couplet_bottom) / `抱`(hug)。

`抱` 的头像只从 QQ 头像 CDN（`qlogo.cn`）或素材站域名（`site`）取；
模型传其他 http(s) 地址会被明确拒绝，本地文件路径照旧可用。

纯 Python 渲染（Pillow + numpy + fontTools），**不需要 Node**，Windows / Linux / macOS 行为一致。
字体按 unicode-range 只下命中的那几个分片，缓存后离线也能继续用。

## 四个 LLM 工具

等价于上游 MCP 的四个工具，但不用装 Node：

| 工具 | 作用 |
| --- | --- |
| `nekowhale_sticker_search` | 按意思或情绪搜表情，返回编号 + 情绪 + 标签 + 用法 |
| `nekowhale_sticker_send` | 按名字或编号把表情发到当前会话 |
| `nekowhale_sticker_info` | 查一张表情的完整标注 |
| `nekowhale_meme_render` | 贴字表情，直接发图 |

模型调用 `nekowhale_sticker_send` / `nekowhale_meme_render` 之后，
插件会记住「这一轮已经发过图」，自动配图不会再补一张。

## 配置要点

面板里直接点选的四项：

| 键 | 可选项 | 说明 |
| --- | --- | --- |
| `selector` 选图方式 | `自动` / `向量选图` / `关键词选图` | 默认自动：有向量用向量，没有先用关键词 |
| `mode` 发送时机 | `正文后补一张` / `图文一起发` | 默认正文后补一张：正文先发，表情补一条 |
| `embedding_provider_id` 向量模型 | `auto` / 你机器上的真实 ID | 默认 auto：自动用第一个可用的向量模型 |
| `rerank_provider_id` 精选模型 | 你已配置的对话模型 | 下拉列表由 AstrBot 动态填入 |

其余常用项：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled_packs` | `["大肥鱼"]` | 想用双人包就加 `"大肥鱼和GPT娘"` |
| `dual_weight` | 0.85 | 双人表情权重：1.0 = 不降权，0.5 = 明显少，0.05 = 基本不出现 |
| `max_size_mb` | 3.0 | 超过这个体积（MB）的图不进候选池 |
| `data_dir` | 空 | 留空用插件数据目录；老版本下到 `/tmp/.astrbot/nekowhale-stickers` 的素材首次加载自动迁移，失败则继续用老目录 |
| `meme_enabled` | true | 贴字表情总开关 |
| `min_sim` | 0.4 | 相似度低于这个值的候选会被标注出来（仍然继续往下走） |
| `download_concurrency` | 3 | 同时下载几个；网络差可以调成 1 |

完整 28 项见插件面板。

## 兼容性

| 项目 | 支持情况 |
| --- | --- |
| Python | 3.10 及以上（跟随 AstrBot） |
| Windows 10/11 | ✅ 实测通过（含中文用户名、中文路径、输出文件被占用的情况） |
| Linux | ✅ 生产环境长期运行 |
| macOS | ✅ 未实测，代码无平台分支，字体回退到 Songti/PingFang |
| Node.js | ❌ 不需要 |
| 额外服务 | ❌ 不需要（素材走官方 CDN，国内直连即可） |

Windows 上专门处理过的坑：

- **文件占用**：Windows 下 `os.replace` 只要目标文件被别人打开着就会失败，
  所以所有写盘都走「临时文件 → 退避重试改名」，贴字表情实在换不动就换个文件名发出去，
  不会让这条回复失败。
- **字体**：优先用官方 woff2 分片（需要 fontTools + brotli），缺了就退回系统楷体/雅黑。
- **日志编码**：日志文案保持 cp936 可编码，重定向到文件不会炸。

## 依赖

全部走标准库 `urllib`，没有额外 HTTP 依赖。贴字表情需要：

- `Pillow`、`numpy` —— AstrBot 自带
- `fontTools`（可选）—— 解码 woff2 字体分片，缺了退回系统字体

## 许可

- 插件代码：Apache-2.0，上游 [dsh-nekowhale-stickers](https://github.com/Loping151/dsh-nekowhale-stickers)
- 标注数据：CC BY-NC 4.0
- 表情 GIF：版权归作者，允许保留署名的非商业分享
- 插件封面 `logo.png`：作者官方立绘（<https://kiriyuki.com/meme/kit/nekowhale/id-photo-blue.png>），
  仅用于标识本插件，版权归作者，不作商业用途

本插件不镜像、不打包、不再分发任何表情素材。

## 更新日志

### 2.3.3

- 图片缓存损坏自愈：缓存文件是 0 字节（下载中断留下）时，以前直接返回空内容，现在视为未命中并重新下载
- 缓存命中不再绕过大小上限：超过 `max_bytes` 的旧缓存重新下载，避免把超大文件整段读进内存
- Windows / Linux 双平台 11 项回归测试通过

### 2.3.2

- **修「有时候不配图」**：`最小间隔` 默认值从 `1` 改成 `0`（每条回复都配）。
  口径没变，还是「回复条数」：填 1 = 发一张隔一条再发，填 2 = 隔两条。
  旧版默认 1，等于同一会话每两条回复必跳过一条，体感就像随机丢图
- 语义选图这一层没给出结果时不再直接放弃，改为退回关键词再挑一次，
  embedding 服务抖一下也不会让这条回复空着
- 跳过时补全日志：日志级别调到 DEBUG 能看到「这条为什么没配图」，最小间隔会打印还剩几条

### 2.3.1

- 贴字表情长文本不再被切字：`fit_font` 留 8% 边距

### 2.3.0

- 选图方式默认改为「自动」：向量没好时不再等，直接用关键词
- 加插件封面 `logo.png`
- Windows 兼容性加固：`os.replace` 占用重试、GIF 显式指定格式、写盘移出事件循环
- README 补齐兼容性说明与更新日志

### 2.2.1

- 去掉「讲正事挑中性图 / 只对沉重话题不配图」的特殊规则，改为正常配图
- 去掉启动预热（`prefetch_on_start` / `prefetch_count`）
- 去掉贴字表情的水印（`meme_watermark`）
- 体积上限改用 MB（`max_size_mb`）
- 双人表情降权改为可调系数（`dual_weight`）
- 选图方式、发送时机改为中文下拉

### 2.2.0

- 重写为运行时按需下载，插件体积从 388 MB 降到 ~50 KB
- 纯 Python 复刻贴字表情渲染（不再需要 Node）
- 把上游 MCP 的四个工具做成插件自带的 LLM 工具
