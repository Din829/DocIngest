# 前端 API 契约 + 知识库管理设计

GUI（pywebview + 手写前端）落地的后端契约。GUI_DESIGN.md 讲界面/交付/打包，
本文讲**后端给前端暴露什么、知识库怎么隔离与管理**。

写本文前已核源码：`api.py` 现导出 `ingest / inspect / refine / build_config /
IngestResult`；`knowledge/` 下的真实目录形态与混乱现状（见第三节）已实测。

## 一、架构：三层落到 pywebview

```
JS 界面（手写 HTML/CSS）
   │  window.pywebview.api.<method>(...)   ← JS 调 Python（返回 promise）
   │  window.evaluate_js("...")            ← Python 推进度回 JS
② gui_api.py（js_api 桥类）+ gui_logic.py（适配层）
   │  普通 Python：把界面输入翻译成 api 调用，整理结果/进度成易显示数据
③ docingest.api（ingest/inspect/refine）+ 知识库管理新函数（第四节）
```

> **纠正 GUI_DESIGN 的「GUI 直接 import api.py」措辞**：pywebview 下 JS 界面**不能
> 直接 import Python**，必须经 `js_api` 桥（JS 调 `window.pywebview.api.x()`，
> Python 用 `window.evaluate_js()` 推回）。所以②的桥类是**必须层**，不是可选。
> 三层解耦的「适配层不碰界面框架类型」仍成立——桥类只收发 dict/原生类型。

## 二、前端 API 契约（js_api 桥暴露给 JS 的方法）

按界面屏分组。每个方法都对应一个真实后端能力，不臆造。

### A. 造库（01–04 屏 + 09 弹窗）

| JS 调用 | 桥内部 | 返回 / 进度 |
|---|---|---|
| `inspect(paths)` | `api.inspect(paths)` | 预检表：每文件 `{name, format, size_mb, pages, est_cost_usd, recommendation}`（inspect 实测字段） |
| `start_ingest(paths, name, opts)` | 分配隔离目录（第三节）→ `api.ingest(output=<dir>, on_progress=推送, ...)` | 完成返回库概要；进度见下 |
| 进度推送 | `ingest` 的 `on_progress(event)` 回调 → `window.evaluate_js` 推 JS | 两种事件：`kind="file_done"` 文件级 `{kind,status,file,current,total,chunks,elapsed_ms,error,error_type}`；`kind="file_progress"` 文件内 `{kind,phase,file,current,total,sub_current,sub_total}`（`phase="vision"` 带页号、`phase="parse"` 为忙碌态）。03 屏可做"文件 3/10 + 该文件 Vision 页 5/11"两级进度条 |
| `confirm_ingest(...)` | 09 成本确认「确认」→ 同上但 `acknowledge_large=True` | — |

> 长任务（ingest 跑几分钟）必须异步：桥方法在后台线程跑 ingest，`on_progress`
> 里 `window.evaluate_js` 推进度，不阻塞 UI 线程。

### B. 用库 / 管理（04 屏 + 库列表 —— GUI_DESIGN 没有，新增）

| JS 调用 | 后端 | 返回 |
|---|---|---|
| `list_knowledge()` | 扫 `knowledge/` 的**正式库**（第三节判定） | `[{name, dir, files, chunks, created_at}]` |
| `get_summary(dir)` | 读 `index.json` + `quality_report.json` | 完了屏/库详情数据（文件数、chunk 数、各文件 title/pages/language） |
| `preview_markdown(dir, file)` | 读 `sources/<file>.md` | 完了屏右侧预览内容 |
| `open_folder(dir)` | 系统打开目录 | — |
| `start_refine(dir, files, skill)` | `api.refine(files, skill=, output=)` | 整形结果（10 弹窗选 skill：refine_default/faithful/html）。`output` 默认推导到 sources 的父目录，桥层可显式传该库目录 |

### C. 环境 / 配置（05–07 设置屏）

| JS 调用 | 后端 | 备注 |
|---|---|---|
| `doctor()` | 现有 doctor 逻辑 | 环境检查屏：API key / 外部工具状态 |
| `get_settings()` / `save_settings(d)` | **新增**（第四节） | ⚠ 现在只有 `load_config`（读默认），**没有保存用户配置的机制** |

## 三、知识库：位置 / 隔离 / 形态

### 现状问题（实测）
`knowledge/` 下混乱：真库（pwc_docusign / jp_final_baseline）+ 测试垃圾
（`2/`、`_notorch_scan/`、`csv_strict_test/`）+ 撒在根的 `assets/`（某次没传
`output`、默认推导漏建子目录的产物碎片）。**「有 index.json」不足以判定正式库**
——`2/`、`_notorch_*` 也有 index.json。

### 形态（稳定，可依赖）
每库固定结构，前端靠它读概要：
```
<库>/
  sources/*.md              干净 Markdown（预览 / Agentic Search 源）
  chunks.jsonl              切块（RAG 源）
  index.json                文件索引（version/processed_at/files[]/stats）
  knowledge_map.yaml        机读知识图
  knowledge_search.SKILL.md 检索指南
  assets/                   页图
  quality_report.json       质量报告
```

### 隔离与命名约定（要落地）
0. **输出根：按消费者分**——`output` 是 `ingest` 的参数，由调用方决定，这是灵活点：
   - **CLI / SDK agent**：**不固定**，调用方（开发者 / agent）自己传 `output`，不传
     则走 api 默认推导（`./knowledge/...`，相对 cwd）。保持现状，灵活自定义。
   - **GUI**：**固定**到一个绝对、永远可写、用户找得到的根 ——
     `~/Documents/DocIngest/knowledge/<库名>/`（跨平台用户文档区）。原因：打包成 exe
     后 cwd 不可预期，`./knowledge` 默认会飘到不可写 / 找不到的地方（已是
     `knowledge/assets/` 撒根碎片的同类根因）。GUI 适配层启动时算出这个绝对根、每次
     **显式传** `output`。
   - 关键：这只是 **GUI 适配层的选择**（一个调用方固定了输出位置），**api.py 的默认
     值不动**——CLI / agent 仍享有自定义。会变的值（输出位置）走参数、由调用方决定。
1. **每库独立目录，GUI 显式传绝对 `output`** —— 不靠默认推导（默认推导正是撒根 /
   撞目录、且 exe 下 cwd 飘的根因）。
2. **目录名 = 用户起的友好名**（slug 化），不是 uuid 乱码 —— 用户要在库列表认得出。
3. **每库写 `meta.json`**（新增小产物）：`{display_name, source_files, created_at}`。
   index.json 有 `processed_at` 但没「用户起的名」；库列表展示要靠它。
4. **正式库 vs 临时/测试**：`_` 前缀目录视为非正式，`list_knowledge` 排除；
   判定分现状 / 将来：**现状**（meta.json 尚未实现，见第四节）只能粗判——非 `_`
   前缀 + 有 index.json + 不是裸 `assets/`（撒根碎片，无 index.json）；**将来**
   加了 meta.json 后，以「有 meta.json」为正式库的准确标识。

## 四、要补的后端改动

| 改动 | 在哪 | 性质 / 理由 |
|---|---|---|
| `list_knowledge()` / `get_summary(dir)` | **api.py 新增** | 库管理是「数据层」能力，CLI / 未来 web 检索 agent 也都用得上，不放 GUI 专属 |
| 库 `meta.json`（display_name/source/created_at） | `ingest` 完成时写 | 让库列表信息够；最小新增产物 |
| `get_settings()` / `save_settings()` + 用户配置持久化 | **config 层新增** | 设置屏要存模型 / 成本上限；现在只有读默认。存到 `~/.docingest/config.yaml`（用户级，跨项目） |
| `gui_api.py` 桥类 + 进度 `evaluate_js` 推送 | GUI 层（前端落地时配套） | js_api 桥；适配层只收发 dict |

## 五、云部署（Azure / AWS / GCP）——能力已就位，配置驱动

DocIngest 上云无阻塞性硬伤（源码实证；尚未真在云上跑过，真部署时按下方验证）。

**已就位（源码证实）**：
- **三大云 provider 都是一等公民**：`AzureOpenAIProvider` / `BedrockProvider` /
  `VertexAIProvider`（顶层包导出，`from docingest import AzureOpenAIProvider` 即可，
  实测可用）。Azure 认标准环境变量 `AZURE_API_KEY` /
  `AZURE_API_BASE`(endpoint) / `AZURE_API_VERSION`，model 用 `azure/<deployment>`
  格式（provider.py）。
- **纯环境变量驱动，零文件**：`DOCINGEST__<段>__<键>` 前缀可覆盖任意配置
  （`load_env_overrides`，config.py），容器靠注入环境变量即可全配；key 从
  KeyVault / 容器 secret 注入到对应 env，不写死、不进代码。
- **无绑死本地的假设**：核心是 IO + 调云端 LLM，无常驻本地模型；系统二进制
  （LibreOffice/ffmpeg）在 Linux 云上 `apt install` 即可，不装则降级。

**部署时必须配对（不改代码，配环境变量 / 挂卷即可）**：
- **缓存目录**：默认 `.docingest_cache`（相对 cwd，cache.py）。云容器无状态、cwd 可能
  不可写 → 指到持久卷：`DOCINGEST__cache__dir=/data/cache`（或挂载点）。功能不受影响
  （缓存丢了重算），但不配会每次重启失缓存 / 可能写不进。
- **输出目录**：同 `./knowledge` 相对问题 → CLI/API 传绝对路径，或
  `DOCINGEST__output__dir=/data/knowledge` 指到挂载的持久存储 / 对象存储挂载点。

**真部署时验证**：Azure 三件套注入 → 跑一次 ingest 出产物；缓存/输出落在持久卷。

## 六、边界 / 不做

- **检索 / 问答 API（query）不在此**：检索（含 graph query）归未来独立 web agent，
  本契约只到「造库 + 管理库 + 预览」。见 GUI_DESIGN「检索归未来 web agent」。
- **不加 HTTP / 多用户**：本机单用户，js_api 桥足够；服务化是未来 web agent 的事。
- **库管理 API 保持「数据层」纯净**：list/summary/meta 只读写产物，不含界面逻辑。

## 七、待验（不假装）
- pywebview 后台线程跑 ingest + `evaluate_js` 推进度，UI 不卡 —— 真接前端时验。
- `list_knowledge` 对现有混乱 `knowledge/`（含 `_` 前缀、撒根 assets）能否正确
  只挑出正式库 —— 实现后用现有目录实测。
