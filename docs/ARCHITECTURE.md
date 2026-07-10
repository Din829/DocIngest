# DocIngest 架构与扩展指南

**入口型导航**：讲清楚项目怎么跑 + 我要改/加去哪看。细节不在本文复述——指向源码、`config/default.yaml`、README。源码注释密度很高（尤其 `pipeline.py`），"为什么这么做"基本都在注释里，本文只给地图和契约。

用户文档见 [README.md](../README.md)。

---

## 1. 项目定位与设计原则

### 1.1 定位

通用文档前处理引擎。任意输入（PDF / Office / HTML / 图像 / 音视频 / ZIP / URL）→ Markdown + 分片 + 索引，两个下游共用同一份输出：

- **RAG**：`chunks.jsonl` 做向量检索
- **Agentic Search**：`sources/*.md` 做 grep / glob

核心思想：**Markdown 作为唯一中间格式**。不自己搞检索，不管 embedding。核心引擎不含 UI 逻辑——可选桌面 GUI（`docingest.gui`，pywebview）是独立工具壳，三层解耦直调 `api.py`，核心零感知（见 [GUI/GUI_DESIGN.md](GUI/GUI_DESIGN.md)）。

### 1.2 核心设计原则

| 原则 | 具体表现 |
|---|---|
| **程序优先，AI 兜底** | 规则能解决绝不喂 LLM（Excel 去噪 / 合并单元格 / 语言检测 / 片段合并）；AI 只处理每页不确定内容，且有 10 层 triage 筛掉纯文本页 |
| **配置驱动** | 所有阈值、策略、模型、DPI、回退路径都在 YAML；自动支持 `DOCINGEST__*` 环境变量覆盖 |
| **可插拔** | Parser / Chunker / Model provider / Hook / PostProcessor 都可替换或添加 |
| **错误隔离** | 单文件失败不影响全局；Vision 挂保留 Docling 文本；Chunk 挂 fallback 到 recursive；Hook 挂降为 warning |
| **反幻觉 Vision** | 只允许 `[?]`（部分可读）和 `[unreadable]`（不可读）两种标记，便于 quality_report 机器扫描 |
| **增量缓存** | 内容哈希 + 相关 config 子集哈希；改不影响输出的配置（`output.dir` 等）不触发重跑 |

### 1.3 不做什么

| 不做 | 理由 |
|---|---|
| Embedding / 向量索引（主流程） | 下游 RAG 职责。**例外**：可选 `docingest.graph` 子模块做 GraphRAG，opt-in，主 ingest 不嵌向量 |
| 语义搜索（主流程） | 前处理工具不做检索。**例外**：`docingest.graph` 暴露 `query()`，严格隔离在子包内 |
| Late Chunking / 多粒度索引 | 依赖 embedding，下游职责 |
| Web 爬虫 / 实时监控 | 只处理本地文件 + 明确 URL；是批处理工具 |

**可选子模块的边界规则**（`graph` / `azure` / `postprocess` 共用同一套，重要）：

- 主 `pipeline.py` 一行不动；`docingest.api` / `providers` / `__init__` 不导出子模块任何东西
- 用户必须**显式** `import docingest.graph`（或 `.postprocess`）才触发其可选依赖加载
- 产物落各自子目录（`graph/` / `extracted/`），删掉不影响主知识库
- CLI / MCP 条件注册：依赖没装则子命令 / 工具压根不出现在帮助里

GraphRAG 详见 §9；二次加工层（postprocess）详见 §10。

---

## 2. Pipeline 全景

### 2.1 骨架

`run_pipeline`（`pipeline.py`）：文件发现（递归目录 / ZIP 展开 / URL 解析）→ 按增量缓存分区（跳过未变文件）→ 逐个 `process_single_file`（Phase 1-3）→ 聚合 `index.json` / `chunks.jsonl` / `knowledge_map.yaml` / `quality_report.json`。

### 2.2 Phase 明细（`process_single_file` 调用链，加新 Phase 的主战场）

定位某 Phase 源码：在 `pipeline.py` grep 注释标记 `--- Phase <n.x>`。（例外：1.4.5 语言检测、3.1 Lineage attach 没有独立 `--- Phase` 注释——它们是穿插在相邻 Phase 里的小步骤，直接 grep 下方"入口"列的函数名即可。）

| Phase | 做什么 | 入口 |
|---|---|---|
| **0.5 Legacy convert** | `.xls/.doc/.ppt` → OOXML（LibreOffice），失败降级，产物缓存 | `_maybe_convert_legacy_office()` |
| **1.0 pre_parse hook** | 改写原文件（DOCX OMML → LaTeX） | `run_pre_parse_hooks()` |
| **1 Parse** | Docling / Media / Text → markdown + pages。受动态超时 + 大 PDF 主动分批保护 | `parser.parse()` |
| **1.1 Garbled fallback** | 检测 `glyph<` 乱码 → pymupdf 重抽 | `_detect_garbled` + `_pymupdf_fallback` |
| **1.2 Excel denoise** | xlsx/xls/csv 行内去重 + 空格剥离 | `_clean_excel_markdown` |
| **1.2.5 通用表格去噪** | 非 Excel 格式的合并单元格去重 | `_denoise_markdown_table_rows` |
| **1.3 页图生成** | xlsx/docx/pptx → LibreOffice → PDF → 截图；顺带抽 sheet→page 映射 / docx 逐页文本 | `_ensure_office_page_images`（格式→config 走 `_OFFICE_PAGE_IMAGE_FORMATS` 表） |
| **1.4 post_parse hook** | 注入结构化数据给 Vision（PPTX chart 直读） | `run_post_parse_hooks("post_parse")` |
| **1.4.5 语言检测** | CJK 字符分布 → `metadata["language"]` | `_detect_language` |
| **1.5 Vision 增强** | 逐页 Vision（10 层 triage + 并发缓存 + ground truth 切片）；格式分流 supplement/full | `_enrich_with_vision` |
| **1.6 pre_write hook** | 写盘前处理（exiftool、aliases/tags 派生、PII sanitize） | `run_post_parse_hooks("pre_write")` |
| **1.7 Vision dedup** | full 模式按 `output.vision_keep` 选保留哪半 | `_apply_vision_keep` |
| **2 Write** | sources/*.md + frontmatter + assets/ | `write_markdown` |
| **3 Chunk** | 按策略切分 + 保护块 + 片段合并 + 路径注入 | `chunker.chunk()` + `_postprocess_chunks` + `inject_paths` |
| **3.1 Locator attach** | 纯增量补统一来源坐标（page / slide / sheet / time）；不改文本/顺序/ID | `_attach_chunk_locators` |
| **3.2 Lineage attach** | 给每个 chunk 挂 `metadata.lineage` | `_build_chunk_lineage` |
| **4.5 Explicit sync** | 仅 `sync_root` 显式启用；成功完整运行后清理已删除输入拥有的产物并原子更新清单 | `incremental.py::finalize_sync` |

### 2.3 数据流

所有 Phase 共享同一个**可变** `parse_result`（`markdown` / `metadata` / `pages`），就地 mutate。后面 Phase 看得见前面的产出。**顺序敏感**：改 Phase 顺序前看清谁读谁写什么字段。Phase 2 和 3 消费**同一份内存中的 markdown**，绝不二次读盘——保证 `sources/*.md` 和 `chunks.jsonl` 永远同步。

---

## 3. 代码导航

### 3.1 目录结构

完整目录树看代码本身（最快、不会过时）。顶层布局：`config/default.yaml`（配置单一真相源）、`skills/`（refine prompt）、`postprocess_templates/`（抽取模板）、`src/docingest/`（parsers / chunkers / hooks / enrichment / models / output / utils + 可选子包 graph / azure / postprocess / integrations / gui）、`tests/`。

### 3.2 代码导航速查（"我要看 X 代码，去哪？"）

| 想看什么 | 去哪儿 |
|---|---|
| 整体流程骨架 | `pipeline.py` `run_pipeline` + `process_single_file` |
| 旧版 Office → OOXML 转换 (Phase 0.5) | `pipeline.py::_maybe_convert_legacy_office` |
| hook 注册机制 + 现有 hook 参考 | `hooks/__init__.py` / `hooks/docx_omml.py`(pre_parse) / `pptx_chart.py`(post_parse) / `file_metadata.py`(pre_write) |
| Parser 接口 + 路由 | `parsers/base.py` / `parsers/__init__.py` `_DoclingWithFallback` |
| Chunker 接口（含保护块规则）+ 工厂 | `chunkers/base.py` / `chunkers/__init__.py` |
| 配置加载 + 环境变量 | `config.py` |
| 增量缓存 + config_hash 白名单 + 显式目录同步 | `incremental.py`（白名单常量 `_RELEVANT_CONFIG_PATHS`；同步清单 `.cache/sync-manifest.json`） |
| AI provider + fallback 链 + AI 结果缓存 | `models/provider.py` / `models/audio_provider.py` / `models/cache.py` |
| Vision 主逻辑 + 10 层 triage | `parsers/vision.py` + `pipeline.py::_enrich_with_vision` / `_should_skip_vision` |
| 全分辨率抠图（图里小字，docx/xlsx/pptx）| 抽取 `docling_parser.py::_extract_docling_pictures`（docx/pptx）/ openpyxl（xlsx）→ 读图 `pipeline.py::_enrich_embedded_images`（专用 `_EMBEDDED_IMAGE_PROMPT`）|
| 解析超时（按页数动态） | `pipeline.py::_resolve_parse_timeout` + `_probe_page_count` |
| 大文件主动分批 / OOM 兜底 | `docling_parser.py::_parse_pdf_batched` |
| xlsx openpyxl 渲染 | `docling_parser.py::_parse_xlsx_via_openpyxl` |
| Chunk metadata 黑名单 | `pipeline.py::_CHUNK_METADATA_BLACKLIST` |
| Chunk 统一来源定位 | `pipeline.py::_attach_chunk_locators` |
| Chunk lineage 构建 | `pipeline.py::_build_chunk_lineage` |
| Refine（独立命令） | `refine.py` + `skills/*.SKILL.md` |
| 知识图生成 / 质量报告 | `output/knowledge_map.py` / `output/quality_report.py` |
| CLI / MCP server / Public API | `cli.py` / `mcp_server.py` / `api.py` + `providers.py` + `__init__.py` |
| GraphRAG（可选层） | `graph/api.py`（facade）/ `graph/backends/lightrag_backend.py` / `graph/enricher.py`（详见 §9） |
| 二次加工层（可选） | `postprocess/base.py`（Runner + ABC）/ `postprocess/processors/extract.py`（详见 §10） |
| Azure 插件（可选） | `azure/di_parser.py`（云解析）/ `azure/search_export.py`（导出向量库） |

### 3.3 Public API 契约

`docingest.__init__.py` 导出的名字是**稳定 public API**。承诺：公开名（`ingest` / `inspect` / `refine` / `list_knowledge` / `get_summary` / `IngestResult` / `build_config` / Provider 类）的签名+返回语义在同一 minor 版本内不破坏；新增参数一律 keyword-only；`outputs` 白名单字符串只加不改名；Provider 新字段带默认值。

非公开路径（`docingest.pipeline` / `parsers` / `chunkers` / `hooks` / `output` / `models`）可随意重构。

**三条接入面**：Python library（`docingest.ingest(...)`，最稳，首选）/ CLI（`docingest run`，直走 `run_pipeline` 不经 facade）/ MCP（`@mcp.tool` 薄壳全转调 `api.py`）。

**facade 分工**：`api.py` 只做配置合并 + 调 `run_pipeline`/`inspect_files`/`refine_files` + 按 `outputs` 白名单读回产物；`providers.py` 是 dataclass 壳，`.to_model_config()` 塑成 config dict。加新 public API：函数加到 `api.py`（首参外全 `*`）→ `__init__.py` 的 `__all__` 导出 → README 加示例。

---

## 4. 四个稳定扩展点

公开 API，升级尽量保持兼容。

### 4.1 Hooks（最常用、最轻量）

按文件扩展名派发 + 三个时机点 + 永不 raise。适合**可选增强**，不适合关键路径。

| 类型 | 时机 | 返回值 | 典型用途 |
|---|---|---|---|
| **pre_parse** | Docling 之前 | `BytesIO` 或 `None` | 替换文件内容再喂 Docling（DOCX OMML → LaTeX） |
| **post_parse** | 解析后、Vision 前 | 无 | 注入结构化数据给 Vision（PPTX chart 直读 / xlsx 连接线） |
| **pre_write** | Vision 后、写盘前 | 无 | 加元数据 / 改 markdown（file_metadata、derive_aliases/tags、sanitize） |

**契约**（细节 + 最小例子见 [hooks/\_\_init\_\_.py](../src/docingest/hooks/__init__.py) 的模块 docstring 和 `_register_default_hooks`）：

1. **永不 raise**（除 `HookNoOp`）——普通异常被 `logger.warning` 吞掉。无事可做时 `raise HookNoOp`，runner 静默跳过且**不记入 lineage**
2. 从 config 读自己的开关（`config["hooks"][<name>]` 或 `parsing.<format>.<feature>`）
3. 按扩展名派发（`_register_post("pre_write", ["docx"], hook)` 或 `["*"]`）
4. pre_parse 返回非 None 会**替代**原文件喂 Docling，第一个非 None 赢
5. 别 mutate 原文件

**调试技巧**：hook 被吞异常是最常见坑——临时把 `run_*_hooks` 里 `except Exception` 改 `raise`，或 `logger.warning` 升 `logger.exception`。

### 4.2 Parsers（加全新解析引擎）

**何时写**：Docling 搞不定的格式（CAD / DWG / 专有二进制）。**何时不写**：能被 Docling 解析只需预处理 → 用 pre_parse hook；能当文本读 → `TextParser` 兜底。

`BaseParser` 契约（见 [parsers/base.py](../src/docingest/parsers/base.py)）：`parse()` **绝不 raise**，失败返回 `ParseResult(success=False, error=...)`；`metadata` 至少填 `format` 和 `title`。路由写在 `parsers/__init__.py` 的 `_DoclingWithFallback`（MediaParser → Docling → TextParser），加新 parser 在此插一层。`media_parser.py` 是好参考。

### 4.3 Chunkers（加新切分策略）

**何时写**：现有策略（recursive / heading / slide / sheet / timestamp / whole）都不合适（语义切分 / AST 切分等）。

`BaseChunker` 契约（见 [chunkers/base.py](../src/docingest/chunkers/base.py)）：实现 `chunk(markdown, metadata)`；继承自动获得 CJK 感知 `estimate_tokens`、保护块检测（table/code/list/quote）、`max_tokens` 等配置。加新 chunker：新文件继承 → `chunkers/__init__.py` `create_chunker()` 加 elif → 让 `auto` 选中（config `chunking.auto.format_strategies.<ext>` + `AutoChunker._select_strategy()`）。

### 4.4 PostProcessors（加二次加工功能）

**何时写**：要对**已产出的** `sources/*.md` / `chunks.jsonl` 做"读→切片→并行调LLM→合并→写出"类的二次加工（抽取 / 翻译 / 分类 / QA 生成）。

`PostProcessor` 契约（见 [postprocess/base.py](../src/docingest/postprocess/base.py)）：实现三钩子 `prepare` / `process_piece` / `merge`，Runner 提供通用循环（读单元 → 切片 → 并行 → 合并 → 错误隔离）。可选覆盖 `split()` 自定义切片（默认字符切；`split_on_headings` 是保护块感知版，refine 也复用它）。现有实现：`processors/extract.py`（模板驱动强类型抽取）。详见 §10。

---

## 5. 关键机制（指针）

实现细节在源码注释里，本节只给索引。

- **Vision 10 层 triage**：纯文本页跳过省 30-60% 成本；10 层检测捕捉乱码/CMap 失败/脚本不一致/Latin 替换密码。见 `pipeline.py::_should_skip_vision`（含每层注释）+ `parsing.vision.triage` config 段。
- **格式分流 supplement/full**：xlsx 只补视觉不重抄表（openpyxl 已渲染干净）；PDF/PPT 整页转写。见 `_enrich_with_vision` + `parsing.<format>.vision.supplement_only`。
- **Excel openpyxl 渲染**：每 sheet 独立标题、合并单元格锚点化、空列剪除。见 `docling_parser.py::_parse_xlsx_via_openpyxl`。
- **ground truth 切片**：Vision input 按 sheet / docx PDF 文本层切，省 input token。见 `pipeline.py::_xlsx_per_page_ground_truth` / `_docx_per_page_ground_truth`。
- **Chunking 策略 + 保护块**：auto 按格式选策略；表格/代码/列表块超限时按行/项边界切（表头每片重复）。见 `chunkers/*.py` + `chunking.protection.*` config。
- **Chunk locator**：`metadata.locator` 统一承载 PDF 页范围、PPT 页号、Excel sheet、音视频秒数；旧字段原样保留。PDF 仅在 pagebreak 总数严格等于 `pages - 1` 时生成，否则 warning + 全文件不写，禁止猜测。缓存回放也走同一函数，所以不升 `CACHE_CONTRACT_VERSION`、不重烧解析/Vision。见 `pipeline.py::_attach_chunk_locators`。
- **增量缓存**：`cache_key = 内容哈希`，`config_hash` 只算白名单子集（改不影响输出的配置不触发重跑）。`CACHE_CONTRACT_VERSION` 隔离不兼容的产物逻辑；改 parser/chunker/output 语义时必须同步升版。白名单与版本都在 `incremental.py`。
- **显式目录同步**：普通 ingest 永不删除旧 source；CLI `--sync` / API、MCP `sync=True` 才把一个知识库绑定到一个本地目录。清理必须等完整运行成功，且只能删除清单登记的 `sources/`、`assets/` 和对应 cache meta；失败、中断、Safety abort 均保留旧清单。空目录同步表示明确清空。实现见 `incremental.py::load_sync_baseline/finalize_sync`。
- **Chunk lineage**：每 chunk 挂 `source_markdown` + `original_input` + `transformations` 数组（实际起作用的变换才记）。见 `pipeline.py::_build_chunk_lineage`。
- **视频双路径**：默认 `native_video`（整段一次调用，Gemini 原生）；不支持时降级抽帧 + per-page Vision。见 `media_parser.py` + `parsing.audio.native_video` config。
- **动态超时 / OOM 分批**：超时按页数缩放（`_resolve_parse_timeout`）；PDF 超阈值主动分批控内存 + 解析失败被动分批兜底（`docling_parser.py::_parse_pdf_batched` + `parsing.pdf.oom_batch_fallback` config）。起因是 docling-parse 的 Windows OOM bug——**上游已修（7.4.0+，2026-07 本机升级验证）**，机制留作长期防线，历史见 [docling_parse_OOM_Windows_长期监控.md](docling_parse_OOM_Windows_长期监控.md)。
- **派生 metadata**：aliases / tags / 语义 type，零额外 LLM。见 `hooks/derive_*.py` + `output/tags_enrichment.py`。
- **其它**：ZIP 防炸弹（`utils/zip_expander.py`）/ URL 走 yt-dlp（`utils/url_resolver.py`）/ magika 内容识别（`utils/format_detector.py`）/ 加密检测（`utils/encryption.py`）。

---

## 6. 加新 Phase / 改 pipeline.py

在 `process_single_file` 里按 `--- Phase X.Y` 注释找位置插入。三件必做：

```python
# Phase X.Y: <名字>（为什么 + 什么时候触发）
result_field = do_something(parse_result, config)
parse_result.transformations.append({"step": "...", ...})  # 记 lineage
```

同步检查：① 读/写的 `parse_result` 字段有没有和上下游冲突（顺序敏感）；② 新配置项加到 `config/default.yaml` + 必要时进 `incremental.py` 白名单；③ 如果旧产物不再可安全复用，升级 `CACHE_CONTRACT_VERSION`；④ 错误隔离（非关键增强失败降级，系统边界的无效配置直接报错）。

---

## 7. 配置层

**四层优先级**（高→低）：CLI args > `DOCINGEST__*` 环境变量 > 项目 `docingest.yaml` > `config/default.yaml`。加载逻辑见 `config.py`，所有项的语义注释在 `config/default.yaml`（单一真相源）。

**处理档位（`--mode`）**：日常不必手调单个旋钮——`fast`/`balanced`/`best` 三档把成本/质量旋钮（engine / triage / 并发 / 抠图 / batched）打包成场景预设，且按文件格式分流（实现见 `api.py::_MODE_PRESETS` / `_resolve_mode`，落点在 `build_config` 的 `config_overrides` 之下，故显式 override 仍能覆盖 mode）。档×格式映射与依据见 [PROCESSING_MODES.md](PROCESSING_MODES.md)。

⚠️ **缓存陷阱**（最常见坑）：改了 config 但没重跑？因为只有 `incremental.py::_RELEVANT_CONFIG_PATHS` 白名单里的配置变更才触发重跑——改不影响输出的配置（`output.dir` 等）故意不触发。要强制全重建用 `--force`。

---

## 8. 常见坑点

| 现象 | 原因 / 解法 |
|---|---|
| 改了 config 没生效 | 缓存未失效（§7）；或改的不在白名单；`--force` 强制 |
| hook 不起作用 | 被吞异常（§4.1 调试技巧）；或 config 开关没开 |
| Vision 没跑 | triage 判为纯文本页跳过（正常省钱）；或页图生成失败（LibreOffice/poppler 缺）|
| 大 PDF 卡住/OOM | 已有动态超时 + 主动分批；调 `parsing.pdf.oom_batch_fallback` / `parsing.dynamic_timeout` |
| chunk 丢字段 | chunk metadata 黑名单（`_CHUNK_METADATA_BLACKLIST`，文件级诊断字段不进 chunk，是设计）|
| 多文件并发无加速 | 解析按设计串行（避 docling OOM）；并发收益在 Vision I/O overlap（`performance.file_concurrency`）|

---

## 9. GraphRAG 子模块（`docingest.graph`，可选）

在已建知识库上抽实体/关系建图，跑 global/local/hybrid 查询（LightRAG 后端）。**严格 opt-in**：`docingest run` 不碰，必须显式 `import docingest.graph`。

- **边界**：见 §1.3。产物落 `{output.dir}/graph/`，缓存独立于主 `.cache/`。
- **入口**：`graph/api.py` 的 `build` / `query` / `status` / `enrich_chunks`，每个都是薄 facade。
- **build 模式**：`vector_only`（省，跳社区检测，支持 naive/local 查询）vs `full`（贵，加社区摘要，支持 global/hybrid/mix）。
- **实体反哺**：`graph/enricher.py` 把图实体回灌进 `chunks_enriched.jsonl`（纯文件回放，无 LLM），让传统向量 RAG 也吃到图的精度。
- **加新 backend**：实现 `graph/backends/base.py` 的 `GraphBackend` ABC。为什么选 LightRAG、`Communities=0` 行为、三层缓存等——见各文件 docstring + README 的 GraphRAG 章。

用法（CLI / 库 / config）见 [README.md](../README.md) 的 GraphRAG 章。

---

## 10. 二次加工层（`docingest.postprocess`，可选）

对**已产出的** `sources/*.md` / `chunks.jsonl` 做第二遍 LLM 加工，不碰原文档。**opt-in**，同 §1.3 边界规则。第一个（目前唯一）处理器：**模板驱动强类型抽取**（`docingest extract`）——按 YAML 声明的字段表，用 litellm structured output 把每篇文档填成强类型记录，写 `extracted/<template>.jsonl`。

- **共享骨架**：`postprocess/base.py` 的 `Runner` + `PostProcessor` ABC，把"读产物 → 超上下文则切片 → 并行调 LLM → 合并 → 错误隔离"做一次，所有处理器复用。加新处理器只写三钩子（§4.4）。
- **去重**：`refine` 的保护块切片 + 并行执行已复用本层的 `split_on_headings` / `run_pieces_parallel`（消除重复造轮子）。`graph` / `export` 模式不同，故意不并入。
- **强类型机制**：`models/provider.py::text_completion_structured`（litellm `response_format=PydanticModel`），不引 langchain。
- **模板**：内置在 `postprocess_templates/`，用户自带模板放项目目录优先。schema 编译见 `postprocess/schema_builder.py`。

用法见 [README.md](../README.md) 的 extract 章 + `docingest extract --help`。

---

## 附：技术债 / 已知偏差（一行版）

| 项 | 状态 |
|---|---|
| 文件并发解析串行化 | 全局解析锁仍在（历史原因：避 docling-parse Windows OOM，上游 2026-07 已修）；解锁并行提速待评估，当前收益靠 Vision I/O overlap |
| Parser 路由写死在 `_DoclingWithFallback` | 未中心化为注册表；加 parser 改一处即可，暂不抽象 |
| 多栏混排版面阅读顺序错位 | 上游 Docling reading-order 决定（三栏作者+双栏正文等复杂版面块顺序会乱，内容不丢）；DocIngest 层无法修，RAG 按关键词检索基本不受影响 |
| 源 PDF 文本层 CMap 损坏（字形→错码位，如阿拉伯字形出希腊字母） | 源文件自身损坏（"垃圾进"）；triage 的脚本一致性检测仅覆盖 ja/zh/en/ko 且跳过 <50 字符短页，故部分漏网；非 DocIngest 层能修 |

## 附：设计依据（参考）

关键设计判断的外部依据：chunking 选 recursive≈512t（[Vecta 2026.02 benchmark](https://www.runvecta.com/blog/we-benchmarked-7-chunking-strategies-most-advice-was-wrong)；语义切分不划算见 [Vectara NAACL 2025](https://aclanthology.org/2025.findings-naacl.114/)）；解析选 Docling（表格 97.9%，[对比](https://llms.reducto.ai/document-parser-comparison)）；Agentic Search 可行性（[关键词搜索达 RAG 90%+，Amazon Science](https://www.amazon.science/publications/keyword-search-is-all-you-need-achieving-rag-level-performance-without-vector-databases-using-agentic-tool-use)）；方眼紙 Excel→MD（[手法比较](https://zenn.dev/ougotti/articles/houganshi-excel-to-markdown)）。

## 附：维护约定

改命令、`--strategy` 值、refine 默认值后，必须同步 `.claude/skills/docingest/SKILL.md` + 两份 `AGENTS.md`（`tests/unit/test_command_catalog.py` 会把文档钉死在代码上，不同步则测试红）。改 parser/chunker/output 产物语义时，同时判断是否升级 `CACHE_CONTRACT_VERSION`。
