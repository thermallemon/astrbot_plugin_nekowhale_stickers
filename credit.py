"""署名与许可文案 —— 全部集中在这一个文件里，改这里就够了。

上游来源（事实依据）：
  · 代码框架：https://github.com/Loping151/dsh-nekowhale-stickers（Apache-2.0）
  · 素材站点：https://kiriyuki.com
  · 表情作者：B站 可以叫我云大人（https://space.bilibili.com/103739）
  · 形象来源：大肥鱼 基于 ZipZipPipe 与 上善无形；GPT娘 基于 ZipZipPipe
"""

AUTHOR = "可以叫我云大人"
AUTHOR_BILI = "https://space.bilibili.com/103739"
SITE = "https://kiriyuki.com"
UPSTREAM = "https://github.com/Loping151/dsh-nekowhale-stickers"

# 一句话署名：出图、发图、/表情自检 结尾都能挂，尽量短
CREDIT_BRIEF = f"表情：{AUTHOR}（{SITE}）"

# /表情署名 的完整版：作者、站点、形象来源、许可、本插件自身
CREDIT_FULL = f"""🐋 大肥鱼表情包 · 署名与许可

表情作者：B站 {AUTHOR}
　{AUTHOR_BILI}
　{SITE}（制作/发布，欢迎约原稿同款表情包）

形象来源：
　大肥鱼：基于 ZipZipPipe 与 上善无形
　GPT娘：基于 ZipZipPipe
　网页桌宠：{SITE}

许可：
　· 表情 GIF、立绘与站点图片 —— 版权归作者所有，
　　允许「保留作者链接与网址」的非商业二次分发，
　　禁止付费提供或其他商业用途。
　· 知识库数据（meta/）—— CC BY-NC 4.0。
　· 上游插件代码 —— Apache-2.0，二次修改须保留 NOTICE
　　（{UPSTREAM}）。

本插件：astrbot_plugin_nekowhale_stickers
　插件本身只含代码，不含任何表情文件；
　表情与标注在运行时按需从 {SITE} 取，
　不做本地镜像、不做二次分发。
　本插件以 Apache-2.0 发布，保留上述全部署名。"""

# /表情自检 里挂在末尾的一行许可提示
LICENSE_BRIEF = (
    f"表情版权归 {AUTHOR} 所有，非商业使用请保留 {SITE} 与作者链接；"
    "插件代码 Apache-2.0，素材数据 CC BY-NC 4.0。"
)

# 首次加载时打印到日志，方便排查「这张图哪来的」
LOAD_BANNER = (
    f"[nekowhale] 表情素材来自 {SITE}（作者 {AUTHOR} / {AUTHOR_BILI}）；"
    f"上游 {UPSTREAM}（Apache-2.0）；素材仅运行时拉取，不随插件分发。"
)
