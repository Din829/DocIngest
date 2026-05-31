# 图文预览方案（前端预览层图文混排）

GUI 预览要做到**图文混排**（文字旁边显示对应的图），而不是只看纯文字。本文是落地施工图。
核心结论（已照源码 + Docling 官方文档核实）：**Docling 原生支持带图导出，不用自己裁图**；
DocIngest 现在出空占位只因导出时没传 `image_mode`，改一个参数即可带图。

> 配套文档：[GUI_DESIGN.md](GUI_DESIGN.md)（前端整体设计，技术栈 pywebview）。

---

## 一、问题与真相（已照源码 + Docling 官方文档核实）

DocIngest 现在的 `sources/*.md` 里图片是 `<!-- image -->` 空占位（无路径），直接渲染看不到图。
但**根因不是"做不到"，而是导出模式没开**——核实如下：

- **图其实早就提取了**：`docling_parser.py:209` 已 `generate_picture_images = True`，
  Docling 内部已按 bbox 把每张图切好（`PictureItem` 可拿 PIL 图）。
- **只差一个参数**：`docling_parser.py:292` 的 `export_to_markdown(...)` **没传 `image_mode`**，
  默认走 `ImageRefMode.PLACEHOLDER` → 所以输出 `<!-- image -->` 空占位。
- **Docling 原生支持带图导出**（本地 Docling 实测 API 可用）：`ImageRefMode` 三模式——
  `PLACEHOLDER`（现状）/ `EMBEDDED`（base64 内嵌）/ `REFERENCED`（导出 PNG + md 写 `![](路径)`）。
  对**所有 Docling 支持的格式（PDF/PPT/Word/HTML）统一生效**，无需自己按 bbox 裁图。

> 一句话：**不用重造 MinerU 的裁图轮子。Docling 的 `REFERENCED`/`EMBEDDED` 模式
> 直接给出带真实路径/内嵌的 md——这才是泛用、稳的做法。**

仍要处理的两件事：
- **readable（refine 产物）** 会被 AI 重排：实测 default 挡位**删占位、甚至编造假路径**
  （写出 `assets/garbage_area.jpg`）。所以 readable 路径要让 AI **保留真实图片语法别删**（见三）。
- **主产物要不要带图**：带图会改变给 RAG 的 md（多出图片链接，可能干扰检索）。
  **策略：主产物保持纯净（PLACEHOLDER 现状不变），只在 GUI 预览时单独跑一份带图版给人看。**

---

## 二、方案：Docling 原生带图，分两条预览路径

核心：**用 Docling 的 `ImageRefMode` 拿到带真实图片的 markdown，不自己裁图。** 两条预览来源：

### 路径 A：预览「原始产物」（不经 AI，最准）
处理时**额外**用 `REFERENCED`/`EMBEDDED` 模式跑一次导出（或对已解析的 doc 重新 export），
得到带 `![](真实路径)` 的 md + Docling 自动存好的图。前端直接渲染。
- 图的位置由 Docling 按原文档版面写定，**精确、所有格式统一**。
- 路径/文件名全是 Docling 写的真路径，**不存在编造问题**。

### 路径 B：预览「整形版 readable」（经 AI 重排，文字更顺）
readable 是 AI 重排的，要让图跟着走。两种实现，按稳定性选：
- **B1（推荐，已验稳）**：refine 输入里图用**编号占位 `<!-- IMG:n -->`**，程序存「编号→真实图」账本，
  AI 只搬运编号不碰路径，回填时按编号换成真图。AI 没法编错（文件名在账本里），回填精确。
- **B2（备选，未验）**：让 refine 保留 Docling 写好的 `![](真实路径)` 别删——路径已是真的，AI 只要不删。
- **❌ 勿用：现成 faithful 挡位（图是空占位 `![図]()`）+ 顺序硬配——实测会错位**（见下）。

**为什么不能用空占位顺序配、要带编号（难场景实测坐实）**：用现成 faithful 跑难样本（0253331，含 9 张手机截图类配图），实测发现：
- ✅ **好的**：AI 自动把图整理成「`![図]()` + 下方 `>` 转写 + 说明」的结构（正合「图下面有转写」的理想），
  且**自动筛图**——Docling 导出 26 张，readable 只留 6 个有内容的，装饰图被 AI 丢弃。
- ❌ **致命问题**：readable 的 `![図]()` 是**空占位、无身份**。Docling 26 张 vs readable 6 个占位，
  按出现顺序硬配 → **第 n 个占位对不上第 n 张图**（中间 20 张被 AI 丢了但仍占 Docling 序列），插错图。
- **结论**：空占位靠顺序匹配不可靠。**只有带编号 `<!-- IMG:n -->`（B1）程序才能精确回填**。

**B1 编号保留已实测**：2 样本共 4 次真跑，`<!-- IMG:n -->` **100% 保留、零编造、位置合理**。
对比 default 挡位让 AI 自由发挥必编假路径——差别在「搬运 vs 生成」。

---

## 三、灵活适配所有文件类型（Docling 统一处理）

**泛用的关键**：`ImageRefMode.REFERENCED` 对所有 Docling 支持的格式（PDF/PPT/Word/HTML）
**统一生效**，Docling 内部按各格式的版面把图切好、写对路径——不用为每种格式写不同的裁图/匹配逻辑。

- **PDF/PPT/Word/HTML**：走 Docling `REFERENCED`，每张 picture 自动存图 + md 写 `![](路径)`。
  位置精确（Docling 按版面定），所有格式同一套，无需 bbox 自己算。
- **xlsx**：DocIngest 用 openpyxl 自渲染（不走 Docling），图标记是 `<!-- image: 文件名.png -->`，
  文件名精确——预览层把它换成 `assets/文件名` 即可。这条单独处理（xlsx 不经 Docling export）。
- **装饰图/Vision 已充分描述**：Docling 不会为无图的地方写 `![]()`，天然不会裂图。

> 注：原方案靠 `element_boxes` 的 bbox 自己裁图——现在**不需要**了，Docling 原生更准更省。
> bbox 数据仍在 `index.json`（RAG 引用/高亮用），与本预览方案无关。

---

## 四、图的筛选：只贴「被 Vision 读过」的图（零硬编码，几乎不误伤）

**问题**（深度测试实测暴露）：Docling REFERENCED 把**所有图形元素都当图导出**——
一个 PPT 65 张、一个 PDF 26 张，含大量小图标/装饰/logo。全贴进预览会刷屏、很吵。

**为什么不能按尺寸/类型硬编码筛**：尺寸和「重不重要」没有可靠相关性——小图可能是关键
印章/QR/小图表，大图可能是整页背景。一刀切**必然误伤**。

**正解：复用系统已有的 Vision triage 判断，只贴「位于 `<!-- vision-enriched -->` 段内」的图。**
- DocIngest 自己的逻辑已经认定（`pipeline.py:1833`）：vision-enriched 段里的图 = Vision 已读懂、
  有视觉价值；纯文字页的装饰图被 8 层 triage 跳过、**没有** vision-enriched 标记。
- 所以「该不该贴」**不自己发明判断**——直接用 triage 的判断：
  - 有 vision-enriched 的段 → 贴这段的图（系统已确认有视觉内容，不冤）。
  - 没 vision-enriched 的段（装饰图所在）→ 不贴。那 65/26 张里的装饰图自动滤掉。
- **零硬编码**：不设任何尺寸/类型阈值，避免误伤小印章/QR/小图表。

**为什么几乎不误伤**：误伤 = 丢了有价值的图。而「有价值」由 triage 的 8 层规则判过
（glyph 乱码 / 图片元素 / 复杂表格 / 脚本异常…），且 triage **故意偏严**（宁可多送 Vision 不漏），
覆盖面宽，漏判率极低。我们复用它的判断，不重新发明，所以误伤概率 = triage 自身漏判率（系统级，已很低）。

**诚实的残留**：① vision-enriched 是**段级**信号——一段里多张图时分不清主次，但「全贴」也都在
有视觉内容的段里，不算误伤。② 若 triage 漏判某页（8 层都没触发但其实有重要图），那页不贴——
这是 triage 本身的漏判率，非本方案新引入。

**待验（落地时真测）**：Docling REFERENCED 导出的 `![](路径)` 在 md 里的位置，能否和
DocIngest 后加的 `<!-- vision-enriched -->` 段对齐（一个是 Docling 导出、一个是 DocIngest 后处理，
要确认在同一份 md 里、位置可对应）。

---

## 五、落地：改动清单（集中可控，比原方案更省）

### 1. 后端：带图导出独立方法 ✅ 已落地
- **已实现** `DoclingParser.export_with_images(file_path, output_md_path, image_mode="referenced")`
  —— 独立方法，**主 `parse()` 一行不动**（实测主产物仍出空占位、图链接 0、现有 api 单测全过）。
  - 复用现有 `_get_converter()`（已开 `generate_picture_images`），re-parse + `save_as_markdown(image_mode=...)`。
  - `referenced`（PNG + `![](路径)`）/ `embedded`（base64 内嵌）两种，默认 referenced。
  - **不调 Vision**（docling_parser 本就只解析），所以带图导出快且免费。
  - 失败绝不 raise，返回 False（预览是 best-effort）。
  - 实测：PDF/PPTX/DOCX 三格式 referenced+embedded 全成功，坏文件返回 False 不崩。
- **待接**（有消费方时再做）：① 前端预览调它的入口；② 配置项 `output.preview.image_mode`；
  ③ xlsx 路径（openpyxl 渲染不经 Docling）单独把 `<!-- image: 文件名 -->` 转 `![](assets/文件名)`。

### 2. 后端：readable 路径要让 AI 保留图（新增 SKILL，路径 B 用）
- 仅当要预览「整形版 readable + 图」时需要。新文件 `skills/refine_with_images.SKILL.md`。
- 内核 = `refine_faithful` + 保图指令。两种（见二）：
  - **B1**：图用 `<!-- IMG:n -->` 编号占位 + 程序回填（实测稳）。提示词要点：
    > `<!-- IMG:数字 -->` 是图片占位符。**禁止删除/改写/复制**，番号原样、放对位置，**禁止自己写路径**。
  - **B2**：直接保留 Docling 写的 `![](真实路径)` 别删（落地需实测保留率）。
- 走现有 `--skill` 机制，`refine.py` 无需大改。

### 3. 前端：预览适配层（gui_logic，落地 pywebview 时写）
- 路径 A：直接拿带图 md（Docling REFERENCED/EMBEDDED）→ markdown 渲染。
- 路径 B：回填（B1 编号→真图 / B2 已是真路径）→ markdown 渲染。
- 图引用统一处理成可显示：REFERENCED 的相对路径 → 读文件转 base64 内嵌（前端跨不到磁盘）；
  或直接用 Docling 的 EMBEDDED 模式省掉这步。
- **图筛选**（见四）：只保留位于 `<!-- vision-enriched -->` 段内的图，其余 `![]()` 转回占位/删除——零硬编码，复用 triage 判断。
- markdown 渲染：`markdown` 库 + tables 扩展（实测可用）。渲染前清掉残留管线标记。
- 这层**不出现界面框架类型**，纯 `输入文本 → 输出 HTML`（符合 GUI_DESIGN.md 解耦铁律）。

---

## 六、诚实的说明

- **能做到 MinerU 式的单图精确嵌入**（不是整页对照）——因为 Docling `REFERENCED` 模式内部
  已按版面把单图切好、写对位置，PDF/PPT/Word 统一支持。（早期误判过"只能整页对照"，已纠正。）
- **主产物 md 不带图**（保持纯净给 RAG）——带图只在预览时单独生成，不改 sources/ 现状。
- **base64 内嵌让 HTML 体积膨胀**（图多时几 MB）——预览场景可接受，不落盘。
- **REFERENCED 会在磁盘多存图片文件**——预览用临时目录，用完可清；或用 EMBEDDED 不落盘。
- **落地待验**：① REFERENCED 导出的图与文位置在 DocIngest 各类样本上的实际效果；
  ② 路径 B2（AI 保留真实 `![]()`）的保留率（B1 已验，B2 未验）。

---

## 七、实测/核实证据（本机真跑 + 官方文档，留档）

- **Docling 原生能力**：官方 `export_figures` 示例 + 本地 Docling 实测——`generate_picture_images`、
  `images_scale`、`ImageRefMode.{PLACEHOLDER,EMBEDDED,REFERENCED}`、`PictureItem` 均可用。
- **根因核实**：`docling_parser.py:209` 已开 `generate_picture_images`，但 `:292` 的
  `export_to_markdown` 未传 `image_mode` → 默认 PLACEHOLDER → 出 `<!-- image -->`。改参数即可带图。
- **编号占位稳定性（路径 B1）**：2 样本 × 共 4 次真跑 AI，`<!-- IMG:n -->` 100% 保留、零编造、位置合理。
- **完整链路终态**：带编号 readable → 回填真图 → markdown 渲染，图文混排 + 表格渲染正确
  （样本 `_final.html` / `_final_shot.png` 留档于项目根，可删）。
- **base64 内嵌渲染**：真实图（含 logo/表格/QR）在浏览器正常显示。
- **REFERENCED 三格式实测**（真跑 Docling，非 mock）：PDF/PPTX/DOCX 占位全部转为真实 `![](路径)` +
  磁盘存图，数量零丢失（26/65/6）；EMBEDDED 同样三格式可用。**同时暴露「图过多」**（PPT 65 张含
  大量装饰），佐证第四节「按 vision-enriched 筛选」的必要——筛选信号 `pipeline.py:1833` 已确认存在。
- **新方法位置实测**：`export_with_images`（embedded）渲染 DOCX，真渲染截图确认**单图精确嵌在
  对应段落**（非整页快照），位置准。
- **难场景实测（路径 B）**：readable + faithful 跑 0253331——AI 自动成「图+转写+说明」结构且自动筛图
  （26→6），但 `![図]()` 空占位 vs 26 张图按序硬配**会错位**。坐实「路径 B 必须用 B1 带编号」。
