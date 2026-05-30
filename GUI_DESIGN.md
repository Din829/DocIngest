# GUI 后端设计

DocIngest 要做一个本机单用户的工具式 GUI（不是对话 agent）：上传文件 →
看成本预检 → 处理 → 看产物。本文只定后端怎么接，前端控件细节另说。

参考 [MinerU](参考项目/MinerU) 的 Gradio 工具形态，但按 DocIngest 自己的特征做减法。

## 一句话结论

**GUI 直接 `import docingest.api` 调核心，不要 HTTP 层。** DocIngest 后端
几乎不用改——`inspect/ingest/refine` + `on_progress` 进度回调 + safety 成本
预检都现成。唯一要补的是「每次处理用独立输出目录」，避免多次运行互相覆盖。

## 为什么不照抄 MinerU 的服务层

MinerU 的 GUI 是 `GUI → 本地 mineru-api(HTTP) → 核心`，还带 FastAPI 双端点、
任务队列、router 多 GPU 负载均衡。那套是为**重型 GPU 模型推理**设计的——模型要
常驻进程、预加载、隔离崩溃，所以宁可多一层 HTTP。

DocIngest 的核心是 IO + 调外部 LLM API，**没有常驻 GPU 模型**。多一层 HTTP 是
纯开销。所以：

| MinerU 有 | DocIngest | 理由 |
|---|---|---|
| GUI→本地 HTTP API→核心 | GUI 直接 import `api.py` | 核心轻量，不需要进程隔离 |
| FastAPI 双端点 / 任务队列 / 并发 cap | 不做 | 本机单用户，一次一个作业 |
| router 多 GPU 编排 | 不做 | 集群设施，单机用不到 |
| 核心独立于界面层 | **照搬**（已是这样） | `api.py` 就是核心，CLI/GUI 共用 |
| UUID 作业目录隔离 | **照搬**（要补） | 见下 |
| 流式进度（队列+yield） | **照搬**（已有回调） | `on_progress` 现成 |

> 判断依据：MinerU `gradio_app.py` 用 httpx 调本地起的 mineru-api；DocIngest
> `api.py` 的 `ingest()` 是可直接调的同步函数，带 `on_progress` 回调。

## 后端接口（GUI 调这几个，都现成）

| 调用 | 来源 | 用途 | 进度 |
|---|---|---|---|
| `inspect(paths)` | api.py | 成本/页数预检，返回 `list[dict]`（含 `est_cost_usd` / `pages` / `recommendation`） | 快，无需进度 |
| `ingest(paths, output=, on_progress=, acknowledge_large=)` | api.py | 处理，返回 `IngestResult` | `on_progress` 发 `file_done` 事件 |
| `refine(files, skill=)` | api.py | 可选：产可读版 | 后续再接 |

进度事件形状（pipeline.py:2613 已发）：
`{kind, status, file, current, total, chunks, elapsed_ms, error, error_type}` ——
GUI 把 `current/total` 接到状态框即可；`error` + `error_type`（超时 / 解析失败
等分类）用于失败文件的提示。

## 处理流程（两步，DocIngest 特有）

MinerU 上传完直接转；DocIngest 调外部 LLM 按页计费，**必须先报成本再处理**：

```
1. 选文件 → [检查] → inspect() → 显示预检表（文件/页数/成本/建议）
2. 看到成本 → [开始] → ingest(acknowledge_large=用户已确认)
              → on_progress 实时刷状态 → 完成显产物
```

这一步不能省——成本预检 + strict 安全门是 DocIngest 的核心价值，GUI 必须保留。

## 前端设计（参考 MinerU 的形态，不抄它的复杂度）

只定设计/信息架构；具体 UI 组件、控件库、文案语言留白，落地时再选。

### 交付形态：本地工具，零部署

技术栈和 MinerU 一致：纯 Python + Gradio（界面在浏览器渲染）。但「浏览器」
不等于「要部署服务器」——服务起在用户本机（`localhost`），数据不出本机、不联网、
不配域名。交付时用 PyInstaller 打包成 exe：员工双击 → 自动起本地服务 + 弹浏览器，
体验像桌面软件，且**连 Python 都不用装**。这正好解决「员工不碰命令行」的诉求。

### 系统二进制依赖：稳定灵活地一起带上

DocIngest 除 Python 包外，还调用两个系统级二进制（pip 装不了、要随 exe 一起带）：

| 二进制 | 用途（已读源码确认） | 缺了的后果（已实测） |
|---|---|---|
| **LibreOffice (soffice)** | 把 PPT / Excel 渲染成图给 Vision 看（截图）+ 老 `.xls`→`.xlsx`。**PDF/Word 走 docling 原生，不碰它** | 降级、不崩（`find_binary` 返回 None，调用方 skip） |
| **ffmpeg / ffprobe** | 音视频处理（ffmpeg）+ 取时长（ffprobe） | ffprobe 缺：只丢时长，inspect 不崩（已实测） |

**捆绑定位机制（已实测成立，是这套方案的根基）**：DocIngest 的 `find_binary`
查找链第 1～2 级认 config（`binaries.soffice.path`）和环境变量
（`SOFFICE_PATH` / `FFMPEG_PATH` / `FFPROBE_PATH`）。所以：

> **启动时把「随 exe 带的二进制」的路径注入对应环境变量，`find_binary` 零改代码就命中** ——
> 不依赖系统 PATH、不写死路径、跨平台、缺了还能往下降级。这是「稳定灵活」的关键。

实测铁证（`tests/bundle_deps_probe.py`，本机真跑）：
- `SOFFICE_PATH` 指向自带二进制 → `find_binary` 命中；指向坏路径 → 返回 None 不静默回退。✅
- LibreOffice headless 真转换 PPT→PDF 成功（出 39954 字节 PDF）。✅
- ffprobe 指向不存在 → inspect 正常返回、`duration_sec` 干净缺失、不崩。✅

各二进制怎么带：
- **ffmpeg**：用 `imageio-ffmpeg`（pip 包，wheel 自带跨平台 ffmpeg，~60MB），启动时
  `os.environ["FFMPEG_PATH"] = imageio_ffmpeg.get_ffmpeg_exe()`。
- **ffprobe**：⚠ `imageio-ffmpeg` **不带 ffprobe**（已实测坐实），需单独带一个 ffprobe
  二进制，或评估「只丢时长可接受」就不带。
- **LibreOffice**：无 pip 包，用便携形态随包带（Windows Portable / Linux AppImage /
  Mac `.app`），启动时把其 `soffice` 路径注入 `SOFFICE_PATH`。~300MB，是体积大头。

**仍待验（不在此假装通过）**：真 PyInstaller 打包后，LibreOffice 在 `_MEIPASS`
临时目录能否被定位 + headless 正常跑。本机模拟不了，必须真打包后实测。

### 三层解耦：前端壳可换，逻辑不动

为「以后可能换成原生窗口 exe（PyQt / pywebview，不走浏览器）」留好路，代码分三层：

```
① 界面层  (gui_app.py：Gradio；以后可换 PyQt / pywebview)  ← 想换就换这层
      只做：收集控件值、显示结果。不写业务逻辑。
② 适配层  (gui_logic.py：把界面输入翻译成 api 调用，把进度/结果整理成易显示的数据)
③ 核心层  (docingest.api 的 inspect / ingest / refine)       ← 永远不动
```

**解耦铁律：适配层的函数签名里不出现任何 Gradio 类型。**
- 进度用普通回调 `on_progress(事件dict)`，不用 Gradio 的 streaming 对象。
- 返回用 dict / dataclass，不用 `gr.update()`。
- 适配层对「前端是什么」完全无知 —— 这才是真解耦。

这样 Gradio 版和未来的 PyQt 版**调同一个 `gui_logic.py`**，换前端 = 只重写界面层，
业务逻辑零改动。

> 反例（会绑死，禁止）：把 `api.ingest()` 直接写进 Gradio 按钮回调、进度直接
> `yield gr.update(...)`。逻辑和 Gradio 焊死，换前端就得重写。

> 这是设计文档开头「核心与界面解耦」原则的延伸：不只核心可换前端，前端本身也分
> 壳（可换）和逻辑（复用）两层。代价几乎为零——逻辑本来就要写，只是放对文件。

### 抄 MinerU 的（成熟、对路）

- **左右分栏：左输入、右输出。** 文档工具的标准形态，视觉上输入产出分离。
- **进阶参数收进可折叠区。** 主区只放每次都碰的，少数人调的折叠起来。
  （MinerU 是按后端切换联动显隐，因为它多后端、参数互斥；DocIngest 没有这种
  互斥，用「用户自己展开折叠」即可，更简单。）
- **长任务用「流式逐步刷新」显示进度，而非一次性返回。** 把后端 `on_progress`
  的事件接到一个滚动状态区，处理一个文件刷一行。这是 MinerU 前端的真精华。
- **输出区分「下载/打开产物」与「预览」两块。**

### 按 DocIngest 功能改的（关键，别照抄错）

- **进度是「文件级」不是「页级」。** `on_progress` 给的是
  `current/total`（第几个文件 / 共几个）+ 文件名 + 该文件 chunk 数（pipeline.py:2613），
  没有页级粒度。所以进度只能显示「3/10：report.pdf」，**别照抄 MinerU 的「第 N 页」**。
- **预览的是「产物 Markdown」，不是「输入文件」。** MinerU 上传 PDF 渲染单页预览
  （gradio_pdf）——DocIngest 输入啥都有（音视频 / ZIP / URL），无法统一预览输入；
  它的产物是干净的 `sources/*.md`，预览**产物 Markdown** 才有意义。
- **产物是「一个知识库目录」，不是「一个转换后文件」。** 完成后磁盘上是
  `sources/*.md` + `chunks.jsonl` + `index.json` + `knowledge_search.SKILL.md`
  （SKILL.md:15-17）。输出区给的是「打开这个目录 / 预览其中的 Markdown」，
  而非 MinerU 那种「下载单个结果 ZIP」。这是 agentic-search 定位的体现——
  产物是给下游检索的知识库，不是给人下载的成品。
- **比 MinerU 多一步「成本预检」。** 见上节两步流程：先出预检表（文件/页/成本/
  建议），用户确认再处理。MinerU 上传即转，没有这块。

### 不抄的（MinerU 因重模型/分布式/历史包袱才需要）

- **并发限制器**：本机单用户一次一个作业，无并发可限。
- **多框架版本兼容分支**：新起步直接锁一个版本，别背 MinerU 多年维护的兼容包袱。
- **输入文件预览组件**：理由见上（输入形态太杂，预览产物即可）。

### 信息架构草案（组件/语言留白）

```
左栏（输入）                      右栏（输出）
- 文档选择（多文件）              - 进度状态区（流式滚动；接 on_progress）
- 输出目录                          「3/10：report.pdf …」
- 〔检查〕→ 预检表                - 预检表（检查后：文件/页/成本/建议）
- 〔折叠〕进阶选项                - 产物概览（完成后：N 文件 / M chunks）
    · 只产 Markdown 不切块         - 打开产物目录
    · 切块策略                     - 产物 Markdown 预览
    · 安全模式
- 〔开始处理〕（预检确认后启用）
```

## 唯一要补的后端改动：作业目录隔离

**现状**：`ingest()` 不传 `output` 时——单输入自动按文件名分目录
（`./knowledge/<stem>/`），多输入才落到 `./knowledge`（api.py:296-301）。所以
单文件已天然隔离；真正会撞的是**多输入**或**同名/重复处理**时写进同一目录。

**做法**：GUI 每次处理显式传一个独立目录给 `ingest(output=...)`，不依赖默认
推导——这样无论单/多输入都不会撞，行为可预期。

放哪做要权衡（细化时定）：
- **放 GUI 层**（推荐）：GUI 自己用 `tempfile` 或 `output/<时间戳或uuid>/` 生成
  目录传进去。后端零改动，符合最小侵入——隔离是「前端怎么用」的事，不是核心职责。
- 放 api 层：给 `ingest` 加「自动分配隔离目录」选项。会改到核心契约，非必要不做。

> 灵活性判断：输出目录本就是 `ingest(output=)` 的参数（会变、调用方决定），
> 由调用方（GUI）传，不硬编码、不进核心——符合「会变的值走参数」。

## 沙箱：用现有的，不加重的

DocIngest 实际场景是给 AIPowerPoint 做咨询 PPT 的资料预处理，**文档来源可控**
（员工传的需求书/合同），不是公开上传。所以：

| 防护 | 现状 | 结论 |
|---|---|---|
| 成本沙箱（per_file/per_run 预算、strict 拦截） | ✅ 已有（safety.py） | 够用，GUI 依赖它 |
| ZIP 炸弹（限深度/大小/数量） | ✅ 已有（config zip 段） | 够用 |
| 进程隔离 / 内存上限 / 子进程超时 | ❌ 无 | **不加** —— 为不会发生的场景（不可信上传）加防御违反反过度防御 |
| 上传文件大小上限 | ❌ 无 | **可加**（GUI 边界验证，配置项默认值）—— 这是系统边界（用户输入），该有 |

MinerU 面向公开上传才需要进程隔离；DocIngest 单机可信场景不需要。现有的 safety
机制本质就是「成本沙箱」，已经覆盖。

## 不做（避免过度设计）

- HTTP 服务层、任务队列、router —— 单机单用户不需要。
- 进程级资源沙箱 —— 文档可信，非必要。
- graph 子命令上 GUI —— opt-in 进阶场景，第一版只做 inspect→ingest 主链。
- 把几十个 config 项全搬上界面 —— GUI 只露高频参数，其余交给 config 文件。

## 升级路径（万一以后要服务化）

因为核心（`api.py`）和界面解耦，将来若真要多用户/HTTP：在 `api.py` 外包一层
FastAPI（参考 MinerU `fast_api.py` 的 UUID 目录 + 队列），核心不动。本设计不
预先实现，但不堵死这条路。
