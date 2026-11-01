# 大肥鱼表情包 · astrbot_plugin_nekowhale_stickers

让 Bot 说话自带表情包，还能把一句话贴进表情里画一张新的。

插件**只含代码**，不含任何表情文件：清单、标注、GIF 全部在运行时按需从
[kiriyuki.com](https://kiriyuki.com) 拉取，用到哪张下哪张。

> 素材署名：表情作者 B站 **可以叫我云大人**（<https://space.bilibili.com/103739>），
> kiriyuki.com 制作。形象基于 ZipZipPipe 与 上善无形。
> 完整许可见 [NOTICE](NOTICE) 与 [LICENSE-ASSETS](LICENSE-ASSETS)。

## 安装

**方式一（推荐）**：AstrBot 面板 → 插件 → **「通过 Git 克隆安装」**，填

```text
https://gitee.com/in1t1a1/astrbot_plugin_nekowhale_stickers
```

> 注意别用「从链接安装」：那个入口是给 GitHub 归档地址用的，会用 httpx 直接下载 zip；
> Gitee 对这类请求有反爬（返回的是 47KB 的 JS 挑战页），会报「插件压缩包格式错误」。
> 顶部那个「Git 克隆」入口走 `git` 命令，Gitee 完全支持。

**方式二**：下载仓库 zip（`https://gitee.com/in1t1a1/astrbot_plugin_nekowhale_stickers/repository/archive/main.zip`）
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
/表情自检        看清单、缓存、向量、贴字模板是否就绪
/表情下载 6      预热 6 张（首装后自动做）
/表情下载 全部   把整套下到本地（约 388MB，只在你想离线用时需要）
/表情图 想 今天不想上班
/表情署名        看作者与许可
```

装完第一条回复会慢一点（后台在下清单和建向量），之后就正常了。

## 两个能力

### 1. 自动配图

每条回复之后，按语境挑一张大肥鱼补在后面。

- 讲正事、答技术问题时挑中性不抢戏的图；对方在说沉重的事就不发
- 正文先发出去，表情随后补一条，不拖慢回复
- 模型自己发过图就自动让位，不会一条回复两张图
- 有最小间隔、防重复窗口、情绪排除、会话黑名单

选图有两种方式：

| selector | 做什么 | 花多少 |
| --- | --- | --- |
| `semantic`（默认） | 向量召回 10 张 → 对话模型挑一张或说不合适 | 每条回复几百 token |
| `keyword` | 本地关键词打分，随机取 | 零 token |
| `auto` | 向量没建好时退关键词 | 视情况 |

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

纯 Python 渲染（Pillow + numpy + fontTools），**不需要 Node**，
Windows / Linux / macOS 行为一致。字体按 unicode-range 只下命中的那几个分片。

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

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled_packs` | `["大肥鱼"]` | 想用双人包就加 `"大肥鱼和GPT娘"` |
| `data_dir` | 空 | 留空自动选；老版本已下到 `/tmp/.astrbot/nekowhale-stickers` 的会继续用老目录，不重下 |
| `prefetch_count` | 6 | 首装预热几张 |
| `meme_enabled` | true | 贴字表情总开关 |
| `meme_watermark` | false | 贴字表情左下角加「用户生成」 |
| `max_bytes` | 3000000 | 超过这个体积的图不进候选池 |
| `min_sim` | 0.4 | 相似度低于这个值直接不发 |

完整 30 项见插件面板。

## 依赖

`httpx`（AstrBot 自带）。贴字表情额外需要 `Pillow` 与 `numpy`（AstrBot 自带）、
`fontTools`（缺了会退化成系统字体，画出来仍然能看）。

## 许可

- 插件代码：Apache-2.0，上游 [dsh-nekowhale-stickers](https://github.com/Loping151/dsh-nekowhale-stickers)
- 标注数据：CC BY-NC 4.0
- 表情 GIF：版权归作者，允许保留署名的非商业分享

本插件不镜像、不打包、不再分发任何表情素材。
