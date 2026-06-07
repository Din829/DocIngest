# docling-parse Windows 内存爆炸（std::bad_alloc）—— 现状与长期监控

> **状态（2026-06-06）**：根因在 IBM 的 docling-parse C++ 库（Windows 专属 bug，
> 官方已知、在修但未修好）。DocIngest 侧已落地一套**临时工程绕过（分批兜底，
> 已实测有效）**，问题不再阻塞使用。**但根子在上游、没根治**——必须**长期监控
> docling-parse 的官方修复**，修好后删掉我们的绕过、回到原生路径（见 §4 待办 4）。
>
> 本文档因此**从"临时交接"转为"长期监控档"**，不要删。给后续维护者 / Agent 用：
> 先读 §0 工作铁律 → §1 现状一图 → §4 待办（监控 + 收尾）。

---

## 0. 你必须先知道的前提（用户的工作铁律）

接手前，按这些规矩干活（用户的硬性要求）：

1. **用中文、大白话交流**。结论先行，别铺垫，别堆术语。报告越长越要说人话。
2. **测试原则（最重要）**：
   - **方案先实测验证，不臆断**。重要/有风险的方案，落地前用真实文件实跑、拿到证据再执行。该烧 token 就烧。
   - **测试结果不盲信，防假绿灯**。"PASS" 是事实，"功能对" 是结论，中间差一层验证。警惕：弱断言（测了个永远不会失败的场景）/ mock 掩盖真实 / 单次孤证。关键结论至少独立对照一次。
   - **事实 ≠ 结论**。工具返回 None/空/失败时，先区分"事实"和"结论"，用低成本验证核对根因再推进。
3. **反过度防御 + fail loud**：只在系统边界做防御；内部信任契约。`fail loud` 比 `fail safe` 更有价值——宁可明确报错，不要静默吃掉错误（这正是本问题"假绿灯"的教训）。
4. **最小侵入**：改动尽量不破坏现有逻辑，优先向后兼容。改完回看 diff，问自己"这是用户要的吗"。
5. **三阶段工作流**：收到任务从【分析问题】开始（搜代码、找根因、给 1~3 方案并推荐），用户选定后【细化方案】列变更，再【执行方案】。不要一上来就改代码。
6. **本机 Windows + PowerShell**。中文路径有坑（见下）。可用 Bash 工具跑 POSIX。

---

## 1. 一句话问题

DocIngest 用 docling 解析**多页 PDF**时，docling 底层的 `docling-parse`（C++ backend）
在 Windows 上间歇性 `std::bad_alloc`（内存分配失败），**导致部分页被静默丢弃**。
不是机器内存不够（**实测 RSS 才 2.7GB 就崩，机器有 31GB 空闲**），是 docling-parse
某版本的**回归 bug**。

> **两种症状是同一个 bug（2026-06-06 实测定性）**：页数少时报 `std::bad_alloc`（C++ 层局部崩）；
> 页数多时（实测 100 页）升级成 Python `MemoryError`（进程内存被 docling-parse 累积占满，
> 连 4MB 都分配不出）。**铁证**：同样 100 页、关 vision/ocr、同机器，**换 PyPdfium2 backend
> 单次跑 RSS 峰值才 2.3GB、0 报错全成**——证明内存爆 100% 是 docling-parse 不释放内存，
> 不是模型/页图累积、更不是机器内存不够。**含义：IBM 修好 docling-parse 内存管理后，
> bad_alloc 和 MemoryError 会一起消失，回单次处理自然好——MemoryError 不用单独处理。**
> （分批兜底对两种症状都有效，因为根治手段都是"重建 converter 释放内存"。出问题再细查。）

本问题有**两层**，**两层都已处理**（绕过，非根治）：
- **第一层：假绿灯（已修 ✅）** —— OOM 丢页后 docling 报 `PARTIAL_SUCCESS`，但 DocIngest
  以前没检查，当成功处理 → 产出"悄悄少了几十页"的知识库。**已修复**（status 检测 + fail loud）。
- **第二层：内存爆本身（已绕过 ✅，非根治 ⚠️）** —— bug 仍在 docling-parse 里，无法在我们这层
  根治。落地了**分批兜底**：整篇崩了就自动按小批重解析、每批重建 converter 释放 C++ 内存
  （实测 75 页 48 崩 → 0，产物完整）。**官方修好后应删掉它回原生路径——这就是要长期监控的原因。**

> **走到分批兜底的来龙去脉（2026-06-06 实测，三条路否了两条）**：
> - **方案 A 换 PyPdfium2 → 否决**：治 OOM 但**毁表格**，密集数据表被拍平成散落数字（§3-A）。
> - **方案 B 降级 docling-parse 4.7.3 → 堵死**：docling 2.96.1 顶层硬 import
>   `DoclingThreadedPdfParser`，4.7.3 没这个类，整个 docling 起不来（§3-B）。
> - **方案 C 分批兜底 → 采用 ✅**：不换 backend、不降版本、保住表格精度，只在检测到丢页时
>   自动分批救回（§3-C，已落地代码 + 实测）。

---

## 2. 已经做完并验证的（带铁证，别重复测）

### 2a. 功能：全局 `parsing.max_pages`（只解析前 N 页）—— 已完成 ✅

用户要的功能：AI 可显式指定"只解析前 N 页"（对有页数的文件：PDF/PPT/DOCX）。

**实现（已落地，3 处）：**
- `src/docingest/parsers/docling_parser.py`：`parse()` 里读 `parsing.max_pages`，
  传给 docling 的 `convert(..., page_range=(1, N))`。docling 的 page_range 是 **1-based、
  闭区间**，`(1, N)` = 前 N 页（实测 docling 2.96.1 无 off-by-one、无丢尾页 bug）。
- `src/docingest/inspect.py`：成本预检联动 —— cap pages 到 N，保留原始页数到 `total_pages`，
  加 `pages_capped` 标记。成本按 N 算。
- `config/default.yaml`：加全局 `parsing.max_pages: null`（默认全解析，向后兼容），
  并删掉了一个从未被读的死配置 `parsing.pdf.max_pages`。

**AI 怎么用（零改，走现成机制）：**
`ingest(paths, config_overrides={"parsing.max_pages": 30})`

**实测铁证（真实 519 页 WEO PDF）：**
- inspect 无 cap：pages=519，成本 $0.1038
- inspect max_pages=10：pages=**10**、total_pages=**519**、成本=**$0.002**（降 50 倍）✅
- 真解析 max_pages=3：产物只含 3 页（2 个 pagebreak 标记）✅

### 2b. 假绿灯修复 —— 已完成 ✅

**根因（源码级确认）：**
- docling 的 `docling/pipeline/standard_pdf_pipeline.py` —— 多线程 stage pipeline
  **catch 了每页异常（含 bad_alloc），只 log "Stage preprocess failed for run %d, pages %s"，不上抛**。
- docling 其实诚实返回了 `ConversionStatus.PARTIAL_SUCCESS`，**是 DocIngest 以前没检查 status**。

> **2026-06-06 校正了根因链条的细节**（结论不变，定位更准——供后续 debug 别找错地方）：
> 本机 docling 2.96.1 的真实机制是**三步**，不是"catch 后留空页槽"：
> 1. stage 内吞异常（`standard_pdf_pipeline.py:440-451`）：`except Exception` 只
>    `_log.error("Stage preprocess failed...")`，把该批 item 标 `is_failed=True`，不上抛。
> 2. base_pipeline **过滤掉失败页**（`base_pipeline.py:317`：`pages = [p for p in pages if p.size is not None]`）
>    ——失败页是被**删掉**的，不是留空槽。所以旧文档说"page 槽还在、len(doc.pages) 看着对"
>    **不准**（实测页数会变少）。但**修复方案不受影响**：修复查 `status` 不查页数，文档原本
>    也强调了"只能靠 status"，结论安全。
> 3. `_determine_status`（`base_pipeline.py:345-354`）扫到失败 backend → 标 `PARTIAL_SUCCESS`。
>
> 那句一字不差的错误信息 `"Stage preprocess failed for run N, pages [X]"` 真实存在于
> `standard_pdf_pipeline.py:441`，和 issue #227 对得上。

**修复（已落地，只动 `docling_parser.py` 的 parse() convert 段）：**
- convert 后检查 `result.status`。非 `SUCCESS` → **重试整篇一次**（OOM 间歇，可能救回）
  → 仍非 SUCCESS → 返回 `ParseResult(success=False, error_type="parse_incomplete", markdown="")`，
  **不产出残缺知识库**（对齐 fail-loud + 用户判断"企业级场景报警就不该产出"）。
- override_stream 路径在重试时会 `seek(0)` 重读（已处理）。

**实测铁证：**
- 20 页（不 OOM）：success=True，markdown 107k ✅（没误伤正常路径）
- 75 页（触发 OOM）：以前 success=True+产物齐全（假绿灯）→ 现在 **success=False、
  error_type=parse_incomplete、markdown=0**（重试日志可见）✅

### 2c. 根因深挖 + 外部课题调研 —— 已完成 ✅

**这是已知的、广泛报告的 docling 核心问题**（别重新调研，下面是结论）：

- 相关 GitHub issue：docling #3345 / #773 / #1654 / #2779 / #2209 / #2829；
  **docling-parse #227（最对口，错误信息一字不差）**。
- **issue #227 的关键线索**：错误就是 `"Stage preprocess failed for run 1, pages [X]: std::bad_alloc"`
  （和本机日志完全一致）；间歇性、不同页号、初始页正常后级联失败；
  **`docling-parse 4.7.3` 能正常处理同样文档，`5.3.3`+ 出问题** → 是 **docling-parse 版本回归**。
- 社区两类现象要区分：
  - #3345 那种是"内存累积到 32GB 才崩"（累积型，~345 页）。
  - **本机这种是 RSS 才 2.7GB 就崩**（间歇分配失败，非累积）—— 更像 #227 的版本回归。
  - 注意：本机 `docling-parse` 版本是 **6.2.0**（比坏版本 5.3.3 还新，仍带回归）。

**本机环境关键事实（验过）：**
- docling **2.96.1**，docling-parse **6.2.0**，Python **3.13**。
- **docling 装在系统 Python313**：`C:\Users\q9951\AppData\Local\Programs\Python\Python313\python.exe`
  （注意：不是 anaconda！anaconda 各环境都没 docling。跑测试用这个解释器）。
- **DocIngest 不指定 backend 时，docling 2.96.1 默认用的是 `DoclingParseDocumentBackend`（v2），不是 v4**
  （旧文档 §3 表格里"默认 docling-parse v4"措辞不准，已校正）。底层仍是 docling-parse 6.2.0 这个有 bug 的库。
- **6.2.0 是 PyPI 当前最新版**（2026-06-06 确认）；issue #227 到 2026-04 官方仍未修——
  **没有"又新又好"的逃生版**。
- docling-parse 4.7.3 **有 py3.13 wheel** 但**装上和 docling 2.96.1 不兼容**（§3 方案 B 实测，降级 4.7.3 这条路已堵死）。

---

## 3. 已验证的两个根治方向（都能治 OOM，差别在代价）

### 方案 A：换 PDF backend 为 PyPdfium2 —— 治 OOM ✅ 但毁表格 ❌【2026-06-06 实测否决】

PyPdfium2 不走 docling-parse 那个有 bug 的 C++ 模块。

**OOM 实测铁证（同一 75 页）：**
| Backend | status | bad_alloc | pages | md字符 |
|---|---|---|---|---|
| 默认（docling-parse v2） | PARTIAL_SUCCESS | **42 页失败** | 75 | 139k |
| **PyPdfium2** | **SUCCESS** | **0** | 75 | **198k** |

稳定性 ✅ 完全根治、速度 ✅ 基本一样——**但精度这关没过**。

**精度对照（2026-06-06 测透，旧文档卡住的就是这个）：**
- 样本选对：WEO 449-456 页（统计附录密集数据表，单表 17×16 发电成本矩阵，数字占比 29%），
  不是旧文档误用的"贡献者名单"。
- 对照公平：页范围落在默认 backend **不 OOM** 的小区间（两版实测都 status=success）。
- 三方比对：pymupdf 抽 ground truth 当基准，肉眼 + 量化双证。

| 指标 | 默认 docling-parse | PyPdfium2 |
|---|---|---|
| 同一发电成本表 | `Nuclear \| 5000 \| 4700 \| 4500 \| ... \| 110 \|`（**数字精确落位**）✅ | `Capital costs`/`2024`/`5000`/`30` **各自孤立成行**，行列关联全丢 ❌ |
| **裸数字碎片行（全文）** | **3** | **589** |
| 下游能否答"美国2035核电LCOE" | 能 | 不能 |

> **结论：PyPdfium2 把数据表彻底拍平成文本碎片，毁了 DocIngest 最核心的表格能力（97.9% 那个卖点）。
> 对喂 RAG/Agentic Search 的前处理引擎，毁表 = 静默降质，比偶尔 OOM 失败更糟（OOM 现在会 fail loud）。
> 方案 A 否决。**
>
> 注：旧文档 3 轮没测出来，根因都是样本选错（叙述文字/人名单当数据表）+ 只数 TableItem 个数
> （两版都报 8 个表的"假持平"——其实 PyPdfium2 只建了表头骨架，数据全漏到表外）。
> 复现脚本：仓库根 `_table_precision_probe.py`，产物 `_table_probe_out/`。

### 方案 B：降级 docling-parse 到 4.7.3 —— 【2026-06-06 实测：和 docling 2.96.1 不兼容，堵死】

原计划：退到 4.7.3（issue #227 实证它不复现 OOM），保住默认 backend 的表格能力。

**实测结果：装上 4.7.3，docling 整个 import 就崩**——
```
ImportError: cannot import name 'DoclingThreadedPdfParser' from 'docling_parse.pdf_parser'
```
- 根因（源码级确认）：docling 2.96.1 的 `backend/docling_parse_backend.py` **在模块顶层**
  硬 import `DoclingThreadedPdfParser`（多线程类）+ `from docling_parse.pdf_parsers import DecodePageConfig`。
  4.7.3 的 `pdf_parser` **只有 `DoclingPdfParser`，没有那个多线程类**（它是 5.x 才引入的），
  连 `pdf_parsers`（复数）模块都没有。
- 后果：不是 PDF 不能解析，是 `from docling.document_converter import ...` 这一句就 ImportError，
  **整个 docling 废掉**（音视频/Office 全用不了）。这是 import 期硬依赖，运行时绕不过。
- 旧文档标的"可能有别的不兼容（未实测）"——**实测命中，这条路堵死。**

**→ 换版本这条路整体放弃**（A 否决 + B 堵死）。曾考虑探 5.0.0~5.3.2 窗口（带那个类
又可能没 OOM bug），但**方案 C 分批兜底更稳**（不赌版本、不动依赖、保表格精度），故采用 C，
不再探版本窗口。

### 方案 C：分批兜底（采用 ✅，已落地代码 + 实测）

不换 backend、不降版本。**检测到整篇丢页时，自动按小批重解析、每批重建 converter 释放
C++ 内存。** 这是仅有的"治 OOM + 保精度 + 不改库"方案。

**为什么有效（实测机制）**：
- 整篇一次跑 75 页 → 崩 48 页；**每批新建 converter + `del` + `gc.collect()` → 崩 0**。
- 关键：是"**重建 converter**"释放了 C++ 累积内存。docling **内部** `page_batch_size`
  **无效**（实测设 1/4/20 都崩 51 页）——因为它复用同一个 converter，C++ 后端不释放。
- 子进程分批也行（进程退出释放内存），但**同进程重建 converter 已足够**（两轮各 8 批全 0 崩），
  落地更简单、无跨进程开销，故选同进程。

**实测铁证**：
| 方式 | 崩页 | 结果 |
|---|---|---|
| 整篇 75 页 | 48 | partial（残缺） |
| 同进程分批=10 ×2轮 | 0 / 0 | 完整 |
| 端到端 parse() A/B | A=success pages=75 fallback_used=True；B(关开关)=fail loud | 对照成立 |

**批大小**：实测 8/10/12 都 0 崩、15 崩 9。默认取 **10**（离临界 15 留余量 + 少建 converter）。
**精度**：无跨批表的区间分批=整篇（差 1 字符）；跨批表区表格数/维度一致（WEO 表单页完整不被切）。
**速度代价**：完整 75 页分批 ~135-175s vs 整篇残缺 ~20s（慢 ~7-9x，但**只在真崩才触发**）。

**已落地代码**（4 处，全可拆，官方修好后整体删）见 §6。

---

## 4. 待办（监控 + 收尾）

### ~~待办 1：方案 A 表格质量测透~~ —— ✅ 2026-06-06 已完成（结论：毁表格，否决）
见 §3 方案 A。589 vs 3 裸数字碎片，PyPdfium2 不能用。**这条不用再做。**

### 待办 2：其它格式大文件会不会爆 —— ✅ 源码级确定（不用再跑大文件验）
2026-06-06 直接从 docling 2.96.1 的 `format_to_options` 映射确认（实证，非推断）：
- **PDF / image**：走 `StandardPdfPipeline` + **`DoclingParseDocumentBackend`** → 碰 docling-parse → **会爆**。
- **PPT / Word / HTML / MD / CSV / VTT 等**：走 `SimplePipeline` + 各自专用 backend
  （MsWord / MsPowerpoint / HTML / Markdown…）→ **不碰 docling-parse，不爆这个 bug**。
- **Excel**：`SimplePipeline` + `MsExcelDocumentBackend`，且 DocIngest 默认还用 openpyxl 绕开 → 无关此 bug。
- **音视频**：`AsrPipeline` + `NoOpBackend` → 无关。
> 结论：**只有 PDF（和 image）会爆**。image 是单张、无"多页 OOM"场景，风险可忽略。
> 用户最初问的"其它格式会不会爆"——答案是不会。

### 待办 3【当前主线】：探 docling-parse 5.0.0~5.3.2 窗口
方案 A 否决、方案 B 退 4.7.3 堵死后，这是仅剩的活路（见 §3 方案 B 末尾推导）。
- **第一步（轻量、不污染环境）**：逐个版本只下 wheel 解压看 `pdf_parser.py` 有没有
  `DoclingThreadedPdfParser`（`pip download docling-parse==X --no-deps` → 解压 grep）。
  筛出**带这个类的最老版本**（= 兼容 docling 2.96.1 的下限）。
- **第二步**：对那个下限版（及它附近 1-2 个）做 OOM 实测（75 页 WEO）+ 表格精度对照
  （449-456 页，对比当前 6.2.0 基线，用 §3 那套裸数字碎片指标）。三件事都要过：
  ① import 不崩（兼容）② status=SUCCESS（治 OOM）③ 表格精度不比 6.2.0 差。
### ~~待办 3：根治方案落地~~ —— ✅ 已落地方案 C 分批兜底（见 §3-C / §6）
换版本路（A/B）放弃，采用分批兜底，代码已落地 + 实测。**这条完成。**

### 待办 4【⭐ 当前主线：长期监控官方修复】
分批兜底是**临时绕过，不是根治**——根子在 docling-parse 上游。官方修好 Windows 后，
**删掉我们的绕过、回原生路径**才是终点。这条要**长期挂着、定期看**。
追踪进展（2026-06-06 查 issue #227 全部 69 条评论确认的事实）：

- **根因官方说法**：与 docling-parse C++ 层的 input/output 队列内存管理有关（维护者
  PeterStaar-IBM 原话）；社区 @ed197676 进一步定位到 **Windows + formula 模块**触发，
  Mac/Linux 不犯——所以维护者在 macOS 上一度复现不了。
- **已合的修复**：`docling-parse#274`（feat: release unused python memory，**已 merged**，
  加了 `release_native_memory_every_n_pages` 内存修剪开关）→ 拉进 `docling#3377`。
- **⚠️ 但截至 2026-06-06 仍没修好 Windows**：社区 @simonschoe 在 **2.96.1（=本机版本）**
  上确认 `std::bad_alloc` on Windows **仍然复现**。即本机这版是"官方自以为修了、Windows 上
  没修好"的版本。**别误以为升到 2.96.1 就好了——本机实测正在崩。**
- **怎么监控（定期做）**：
  1. 隔一段时间看 issue #227 / PR #274 / `docling#3377` 有没有"Windows 专门修复"的后续 release。
  2. docling 或 docling-parse 出**新版**时，用 §5 的 75 页 WEO 脚本跑一遍——
     **整篇 `status=SUCCESS + 0 bad_alloc` 就是修好了**。
- **修好后怎么收尾（删绕过，回原生路径）**：
  1. 升级 docling / docling-parse 到修复版。
  2. 删掉分批兜过代码（§6 列的 4 处）：`config` 的 `oom_batch_fallback` 段、`incremental.py`
     白名单那行、`docling_parser.py` 的 `_parse_pdf_batched` + 假绿灯检测点的分叉 + `_page_range`
     参数（假绿灯 status 检测本身**保留**，它是独立的好东西）。
  3. 重新跑 75 页 WEO 端到端，确认原生路径 `status=SUCCESS`，绕过删干净无回归。
- **判断**：分批兜底已让问题不阻塞使用，所以这条**不紧急**；但它是临时贴片，**越早删越干净**，
  别让它在代码里沉淀成永久债。

---

## 5. 测试用的素材 & 命令（现成的，直接用）

- **测试 PDF**：`test_docs/WorldEnergyOutlook2025.pdf`（519 页，14MB，IEA 官方 CC BY 4.0，
  数据表格密集，是这个问题的理想复现素材）。
- **跑测试的 Python**：`C:\Users\q9951\AppData\Local\Programs\Python\Python313\python.exe`
  （这个才有 docling；anaconda 没有）。
- **快速复现 OOM**（关 vision/ocr 省钱，几十秒）：
  ```python
  import sys; sys.path.insert(0, r"<DocIngest>/src")
  from docling.document_converter import DocumentConverter, PdfFormatOption
  from docling.datamodel.pipeline_options import PdfPipelineOptions
  from docling.datamodel.base_models import InputFormat, ConversionStatus
  o = PdfPipelineOptions(); o.do_ocr=False; o.generate_page_images=False
  conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=o)})
  res = conv.convert(r"<...>/WorldEnergyOutlook2025.pdf", page_range=(1,75))
  print(res.status)  # PARTIAL_SUCCESS = 复现了（默认 backend）
  ```
- **Windows 测试坑（踩过）**：
  - 用 `python -u` 无缓冲 + 重定向到文件读，**别用 `| grep` 管道**（缓冲到进程结束才出，看着像卡住）。
  - docling 日志噪声多，过滤 `RapidOCR|[INFO]|onnx|download_file|main.py|base.py:22`。
  - 跑完记得 `taskkill` 残留 python 进程（会占内存影响下次测试）。
  - 中文路径：`cd` 进中文目录会乱码，用绝对路径。

---

## 6. 改动文件清单（已落地的绕过代码 —— 官方修好后按 §4 待办 4 整体删）

**第一层假绿灯修复 + max_pages（独立的好东西，修好 OOM 后也保留）：**
- `src/docingest/parsers/docling_parser.py` —— max_pages 的 page_range + 假绿灯 status 检查/重试
- `src/docingest/inspect.py` —— max_pages 成本预检联动
- `config/default.yaml` —— 全局 parsing.max_pages、删死配置 pdf.max_pages

**第二层分批兜底（方案 C，临时贴片，4 处，全可拆）：**
- `config/default.yaml` —— `parsing.pdf.oom_batch_fallback` 段（默认 enabled，注释标了删除时机）
- `src/docingest/parsers/docling_parser.py`：
  - `parse()` 加内部参数 `_page_range`（默认 None → 行为不变）
  - 假绿灯检测点加一个分叉：整篇崩 → 调 `_parse_pdf_batched`
  - 新增 `_parse_pdf_batched()`（循环重建 converter 分批 + 拼接，整块可删）
- `src/docingest/incremental.py` —— 白名单加 `parsing.pdf.oom_batch_fallback`（否则改开关缓存不刷新）

> 删除时：主解析路径一行没动过，删掉上面分批 4 处 + config 段即复原。`_page_range=None`
> 时 `parse()` 和改之前完全等价（已验）。

**已知边界（实现时标注，未顺手扩展——反过度防御）**：
- 慢 ~7-9x，但只在真崩才触发。
- **关 vision 时分批会误触发 LibreOffice 全量转换（~226s/批）**——真实场景 vision 开（默认）
  不会，但 vision-off 的库用户（如 Mplat）走分批会很慢。只在"vision关+PDF崩+走分批"三重边界发生。
- 跨页表格分批可能切断（WEO 表单页完整不受影响，社区已知局限）。
- **vision 结果 overflow → 边界页 title_path 错标**：分批拼接让 section 数和 vision 页索引
  差 1，触发 `pipeline.py:1668` 的 overflow 分支（warning 文本是为 xlsx 写的，PDF 分批也命中）。
  实测 100 页 IEA：**仅 1/183 chunk 受影响（0.5%）**——第 98 页内容被追加到上一段，继承了上一段
  的 title_path（实标 `Figure 2.8`、实际是 `Figure 2.9 / 2.2.3`）。**内容不丢（Figure 2.9、
  2.2.3 节都在、vision 转写经页图核对准确），只是这一段 title_path 标签错**，且带 `(overflow)`
  标记可被下游过滤。单次处理不会有此 overflow——又一条"回单次后自然消失"的临时代价。

---

## 7. 一句话给接手 / 监控的人

OOM 已**绕过、不阻塞使用**：第一层假绿灯修好（不再静默丢页），第二层分批兜底自动救回大 PDF。
但**根子在 docling-parse 上游、没根治**——你的活是**长期盯官方修复**（§4 待办 4）：出新版就用
75 页 WEO 跑一遍，整篇 SUCCESS 就是修好了，那时删掉我们的分批贴片回原生路径。**严守测试原则，别造假绿灯。**
