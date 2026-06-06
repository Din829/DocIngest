# DocIngest 架构与扩展指南

一份文档讲清楚：项目怎么跑 + 我要改/加该怎么做。给接手开发者和维护 Agent 用。

用户文档见 [README.md](../README.md)。

---

## 1. 项目定位与设计原则

### 1.1 定位

通用文档前处理引擎。任意输入（PDF / Office / HTML / 图像 / 音视频 / ZIP / URL）→ Markdown + 分片 + 索引，两个下游共用同一份输出：

- **RAG**：`chunks.jsonl` 做向量检索
- **Agentic Search**：`sources/*.md` 做 grep / glob

核心思想：**Markdown 作为唯一中间格式**。不自己搞检索，不管 embedding。核心引擎本身不含 UI 逻辑——可选的桌面 GUI（`docingest.gui`，pywebview）是独立的工具壳，通过三层解耦直调 `api.py`，核心零感知（见 [GUI_DESIGN.md](GUI_DESIGN.md)）。

### 1.2 核心设计原则

| 原则 | 具体表现 |
|---|---|
| **程序优先，AI 兜底** | 规则能解决绝不喂 LLM（Excel 去噪 / 合并单元格 / 语言检测 / 片段合并）；AI 只处理每页不确定内容，且有 8 层 triage 筛掉纯文本页 |
| **配置驱动** | 所有阈值、策略、模型、DPI、回退路径都在 YAML；改了自动支持 `DOCINGEST__*` 环境变量覆盖 |
| **可插拔** | Parser / Chunker / Model provider / Hook 都可替换或添加 |
| **错误隔离** | 单文件失败不影响全局；Vision 挂保留 Docling 文本；Chunk 挂 fallback 到 recursive；Hook 挂降为 warning |
| **反幻觉 Vision** | 只允许 `[?]`（部分可读）和 `[unreadable]`（不可读）两种标记，禁止 `[illegible]` / `???` 等变体，便于 quality_report 机器扫描 |
| **增量缓存** | 内容哈希 + 相关 config 子集哈希，改不影响输出的配置（`output.dir`、`performance.parallel_files`）不触发重跑 |

### 1.3 不做什么

| 不做 | 理由 |
|---|---|
| Embedding / 向量索引（主流程） | 下游 RAG 职责。**例外**：可选 `docingest.graph` 子模块嵌实体 / 关系做 GraphRAG，但它是 opt-in 的下游能力，主 ingest 流程仍不嵌任何向量 |
| 语义搜索（主流程） | 前处理工具，不做检索。**例外**：`docingest.graph` 暴露 `query()` 给已构建的图做检索 —— 这是 GraphRAG 的核心价值，不暴露等于白做。但严格隔离在子包内 |
| Late Chunking / 多粒度索引 | 依赖 embedding 模型，下游职责 |
| Web 爬虫 | 只处理本地文件 + 明确的 URL |
| 实时监控 | 批处理工具 |

**`docingest.graph` 子包的边界规则**（重要）：

- 主 `pipeline.py` 一行不能动；`docingest.api` / `docingest.providers` / `docingest.__init__` 不导出 graph 任何东西
- 用户必须**显式** `import docingest.graph` 才会触发 lightrag-hku / openai 等可选依赖加载
- graph 产物落 `{output.dir}/graph/` 子目录，删掉不影响主知识库
- graph 增量缓存 `{output.dir}/.graph_cache/` 与主流程 `.cache/` 完全分离，互不污染
- CLI / MCP 工具条件注册：lightrag 没装时 `graph` 子命令 / `build_graph` 工具压根不出现在帮助里

详细架构见 §10。

---

## 2. Pipeline 全景

### 2.1 三层结构

```
run_pipeline (src/docingest/pipeline.py)
  ├─ discover_files        文件发现（递归目录 / ZIP 展开 / URL 解析）
  ├─ 按 incremental cache 分区
  │   ├─ cached 文件       直接复用 meta.json + 老 chunks.jsonl
  │   └─ to_process 文件   → 逐个调 process_single_file
  ├─ IndexBuilder          聚合 index.json
  ├─ write_chunks          合并 reused + new → chunks.jsonl
  ├─ generate_knowledge_map  Phase 4
  └─ generate_quality_report  [?] / [unreadable] 扫描
```

### 2.2 Phase 明细

`process_single_file` 的完整调用链（按执行顺序）。**加新 Phase / 改流程的主战场**。定位某个 Phase 的源码位置：在 `pipeline.py` 里 grep 注释标记 `--- Phase <n.x>`。

| Phase | 做什么 | 实际入口 |
|---|---|---|
| **0.5 Legacy format convert** | 旧版二进制 Office `.xls`/`.doc`/`.ppt` → 对应 OOXML `.xlsx`/`.docx`/`.pptx`（LibreOffice），失败降级；产物落 `.cache/_legacy_convert/<sha256>.<目标后缀>`，后续 Phase 全部对转换后的现代格式操作。映射表 `_LEGACY_OFFICE_CONVERSIONS` 驱动，每格式一个开关 `parsing.<xls|doc|ppt>.auto_convert_to_*` | `_maybe_convert_legacy_office()` |
| **1.0 pre_parse hook** | 改写原文件内容（如 DOCX OMML → LaTeX） | `run_pre_parse_hooks()` |
| **1 Parse** | Docling / Media / Text 解析为 markdown + pages | `parser.parse()` |
| **1.1 Garbled fallback** | 检测 `glyph<` 乱码 → pymupdf 重抽文本 | `_detect_garbled` + `_pymupdf_fallback` |
| **1.2 Excel denoise** | xlsx/xls/csv 行内去重 + 空格剥离 | `_clean_excel_markdown` |
| **1.2.5 通用表格去噪** | 非 Excel 格式的合并单元格去重 | `_denoise_markdown_table_rows` |
| **1.3 页图生成** | xlsx/docx/pptx → LibreOffice → PDF → 截图 | `_ensure_{excel,docx,pptx}_page_images` |
| **1.4 post_parse hook** | 注入结构化数据给 Vision（如 PPTX chart 直读） | `run_post_parse_hooks(phase="post_parse")` |
| **1.4.5 语言检测** | CJK 字符分布 → 填 `metadata["language"]` | `_detect_language` |
| **1.5 Vision 增强** | 逐页调 Vision（8 层 triage + 并发缓存）；按格式分流 supplement/full——xlsx 只补视觉不重抄、PDF/PPT 整页转写（§5.1） | `_enrich_with_vision` |
| **1.6 pre_write hook** | 写盘前最后处理（exiftool、PII sanitize） | `run_post_parse_hooks(phase="pre_write")` |
| **1.7 Vision dedup** | **旧后处理、默认关**。重复主力已由 Phase 1.5 的 supplement 在源头消除；本步按长度比删一份不可靠，仅留作纯文本 PDF 可选（§5.4） | `_dedup_vision` |
| **2 Write** | sources/*.md + YAML frontmatter + assets/ | `write_markdown` |
| **3 Chunk** | 按策略切分 + 保护块 + 片段合并 + 路径注入 | `chunker.chunk()` + `_postprocess_chunks` + `inject_paths` |
| **3.1 Lineage attach** | 给每个 chunk 挂 `metadata.lineage`（source + original_input + transformations 数组） | `_build_chunk_lineage` |

`parse_result.transformations` 是一条贯穿 Phase 1-3 的"变换日志"——Phase 1 / 1.0 / 1.4 / 1.5 / 1.6 每次实际起作用时都 append 一条（参见 §5.10）。Phase 3.1 把这条日志加上 chunker 自己的条目，挂到每个 chunk 的 `metadata.lineage.transformations`。

### 2.3 数据流：`parse_result` 共享对象

所有 Phase 共享同一个 `parse_result`（`markdown` / `metadata` / `pages`）——每个 Phase 就地 mutate。这意味着：

- 后面 Phase **能看见**前面 Phase 的产出
- hook 里 `parse_result.metadata["foo"] = ...` 之后所有下游都能读
- hook 里改了 markdown，后续所有 Phase 看到的都是改后的
- **顺序敏感**：改 Phase 顺序前看清谁读谁写什么字段

Phase 2 和 Phase 3 消费**同一份内存中的 markdown**，绝不从磁盘二次读。保证 `sources/*.md` 和 `chunks.jsonl` 永远同步。

---

## 3. 代码地图

### 3.1 目录结构

```
DocIngest/
├─ config/default.yaml               默认配置（完整注释）
├─ skills/                           Refine prompt 模板
│  ├─ refine_default.SKILL.md        允许改写润色
│  └─ refine_faithful.SKILL.md       逐字保留，只去重和重排版
├─ src/docingest/
│  ├─ __init__.py                    Public API 导出（ingest / inspect / refine / Provider 类）
│  ├─ api.py                         Facade 主体（ingest / inspect / refine + IngestResult + build_config）
│  ├─ providers.py                   Provider 类（GeminiProvider / OpenAIProvider / DashScopeProvider / ...）
│  ├─ cli.py                         typer CLI：run / inspect / refine / doctor（直接走 pipeline，不经 facade）
│                                    + 条件挂载 graph 子命令组（lightrag 装了才出现）
│  ├─ mcp_server.py                  MCP server（薄壳转调 api.py）：6 个核心工具
│                                    + 3 个 graph 工具（build_graph / query_graph / graph_status，条件注册）
│  ├─ config.py                      YAML 加载 + 环境变量 + 模型 defaults 注入
│  ├─ pipeline.py                    主编排（run_pipeline + process_single_file + Phase 实现）
│  ├─ safety.py                      Phase 0 预算体检（off / warn / strict）
│  ├─ inspect.py                     docingest inspect 预检
│  ├─ doctor.py                      docingest doctor 环境体检
│  ├─ refine.py                      docingest refine 独立命令
│  ├─ incremental.py                 增量缓存（cache_key / config_hash / meta.json）
│  ├─ parsers/
│  │  ├─ base.py                     BaseParser + ParseResult + PageData + PAGEBREAK_MARKER
│  │  ├─ __init__.py                 create_parser + _DoclingWithFallback 路由
│  │  ├─ docling_parser.py
│  │  ├─ media_parser.py             音视频：subtitle-first + ASR fallback；视频默认走
│  │  │                              native_video（整段一次调用，Gemini 原生），不支持则降级抽帧画面理解
│  │  ├─ text_parser.py              兜底 + 多编码尝试
│  │  └─ vision.py                   per-page Vision + resolve_vision_config + prompt
│  ├─ chunkers/
│  │  ├─ base.py                     BaseChunker + Chunk + 保护块检测 + CJK token 估算
│  │  ├─ __init__.py                 AutoChunker + create_chunker 工厂
│  │  ├─ recursive.py / heading.py / slide.py / sheet.py / timestamp.py
│  │  └─ table_splitter.py           row-level 表格切分（被 recursive 调用）
│  ├─ hooks/
│  │  ├─ __init__.py                 注册机制 + 默认 hook 挂载
│  │  ├─ docx_omml.py                pre_parse：OMML → LaTeX
│  │  ├─ pptx_chart.py               post_parse：chart 直读
│  │  ├─ file_metadata.py            pre_write：Docling origin 提升 + exiftool
│  │  ├─ sanitize.py                 pre_write：PII 掩码（默认关）
│  │  ├─ strip_repeating.py          pre_write：跨页重复页眉脚去重（保留首份，默认关）
│  │  └─ _docx_math/                 OMML → LaTeX 转换器（port from MarkItDown）
│  ├─ enrichment/
│  │  └─ path_injector.py            chunk 路径注入
│  ├─ models/
│  │  ├─ provider.py                 Vision / text 完成（litellm + 多 provider）
│  │  ├─ audio_provider.py           ASR 抽象（DashScope + litellm）
│  │  ├─ cache.py                    AI 结果 diskcache 缓存
│  │  └─ token_tracker.py            全局 token 用量累计
│  ├─ utils/
│  │  ├─ binary_finder.py            跨平台定位 soffice / exiftool / ffmpeg / yt-dlp
│  │  ├─ zip_expander.py             ZIP 展开 + CJK 文件名 + bomb 保护
│  │  ├─ url_resolver.py             yt-dlp 统一下载
│  │  ├─ format_detector.py          magika ML 格式识别
│  │  └─ script_detector.py          Unicode 脚本分类（triage 语言一致性用）
│  ├─ output/
│  │  ├─ markdown_writer.py          sources/*.md + frontmatter
│  │  ├─ index_builder.py            index.json
│  │  ├─ chunks_writer.py            chunks.jsonl + chunk_id 生成
│  │  ├─ knowledge_map.py            Phase 4 + AI summary
│  │  ├─ keyword_extractor.py        Sudachi / regex 双后端
│  │  ├─ quality_report.py           Vision marker 扫描
│  │  ├─ run_log.py                  log.md append-only 运行时间线
│  │  └─ visualizer.py               element_boxes + 页图 → 带框 PNG（docingest visualize，QA/调试）
│  ├─ integrations/                  可选下游框架适配（独立 import，不进主 pipeline）
│  │  ├─ __init__.py                 包标记（不 import 任何子模块，避免拉可选依赖）
│  │  └─ langchain.py                chunks.jsonl → LangChain Document（DocIngestLoader，opt-in）
│  ├─ graph/                         可选 GraphRAG 子模块（独立 import，不进主 pipeline）
│  │  ├─ __init__.py                 public API: build / query / status / enrich_chunks / EmbeddingProvider / GraphBackend
│  │  ├─ api.py                      facade：build / query / status / enrich_chunks + Result dataclass + 配置合并
│  │  ├─ providers.py                EmbeddingProvider 基类 + OpenAI / Gemini / SentenceTransformer
│  │  ├─ enricher.py                 chunks.jsonl + graph 实体 → chunks_enriched.jsonl（纯文件回放，无 LLM）
│  │  ├─ cache.py                    chunk 级抽取缓存（chunk_id → content_hash + llm_config_hash）
│  │  ├─ cli.py                      typer 子命令组：graph build / query / enrich / status（被主 cli.py 条件挂载）
│  │  ├─ adapters/
│  │  │  ├─ chunks_loader.py         chunks.jsonl → LoadedChunk（filter + path_injection 协调）
│  │  │  └─ llm_adapter.py           sync text_completion → async LightRAG llm_model_func
│  │  └─ backends/
│  │     ├─ base.py                  GraphBackend ABC + BuildOutcome / QueryOutcome / StatusOutcome
│  │     └─ lightrag_backend.py      LightRAG 实现 + force scrub + auto language resolver
│  └─ gui/                           可选桌面 GUI（pywebview + 手写前端，独立工具壳，三层解耦直调 api.py）
│     ├─ gui_app.py                  ① 界面层：pywebview 窗口 + 加载手写前端 + 拖拽（唯一认识 pywebview 的文件）
│     ├─ gui_api.py                  js_api 桥：JS↔Python，长任务后台线程 + evaluate_js 推进度
│     ├─ gui_logic.py                ② 适配层：界面输入 → api.py 调用，结果整理成易显示 dict（不含框架类型）
│     └─ web/                        手写前端：index.html + app.js + styles/*.css（9 屏，每页带返回）
└─ tests/
```

### 3.2 代码导航速查

| 想看什么 | 去哪儿 |
|---|---|
| 整体流程骨架 | `pipeline.py` `run_pipeline` + `process_single_file` |
| 旧版 Office (.xls/.doc/.ppt) → OOXML 自动转换 (Phase 0.5) | `pipeline.py::_maybe_convert_legacy_office` |
| hook 注册机制 | `hooks/__init__.py` |
| 现有 hook 参考 | `hooks/docx_omml.py` (pre_parse) / `hooks/pptx_chart.py` (post_parse) / `hooks/file_metadata.py` (pre_write) |
| Parser 接口 | `parsers/base.py` |
| Parser 路由 | `parsers/__init__.py` `_DoclingWithFallback` |
| Chunker 接口 + 保护块规则 | `chunkers/base.py` |
| Chunker 工厂 + auto 策略 | `chunkers/__init__.py` |
| 配置加载 + 环境变量 | `config.py` |
| 增量缓存 + config_hash 白名单 | `incremental.py` |
| AI provider + fallback 链 | `models/provider.py` / `models/audio_provider.py` |
| AI 结果缓存 | `models/cache.py` |
| Vision 主逻辑 | `parsers/vision.py` + `pipeline.py::_enrich_with_vision` |
| Vision 8 层 triage | `pipeline.py::_should_skip_vision` |
| Refine（独立命令） | `refine.py` + `skills/*.SKILL.md` |
| 知识图生成 | `output/knowledge_map.py` |
| 质量报告 | `output/quality_report.py` |
| CLI | `cli.py` |
| MCP server | `mcp_server.py` |
| Public Python API (facade) | `api.py` + `providers.py` + `__init__.py` 导出 |
| GraphRAG facade（可选层）| `graph/api.py` + `graph/__init__.py` 导出 |
| GraphRAG backend ABC | `graph/backends/base.py` |
| GraphRAG LightRAG 实现 | `graph/backends/lightrag_backend.py`（force scrub + language auto + LightRAG ainsert/aquery） |
| chunks.jsonl → LightRAG 适配 | `graph/adapters/chunks_loader.py`（path_injection 协调写在这） |
| 同步 text_completion → async LightRAG | `graph/adapters/llm_adapter.py` |
| GraphRAG 增量缓存 | `graph/cache.py`（独立于主 pipeline `incremental.py`） |
| GraphRAG CLI 子命令 | `graph/cli.py`（被主 `cli.py` 条件挂载） |
| Embedding provider 基类 | `graph/providers.py` |
| 实体反哺 chunks（chunks_enriched.jsonl） | `graph/enricher.py` |

### 3.3 Public API 契约

`docingest.__init__.py` 导出的名字是**稳定 public API**，供外部项目作为库接入。承诺：

- 公开名字（`ingest` / `inspect` / `refine` / `IngestResult` / `build_config` / Provider 类）的**函数签名 + 返回值语义**在同一 minor 版本内不破坏。
- 新增参数一律 keyword-only（`ingest()` 的签名已强制 `*` 分隔），未来加参数永远安全。
- `outputs` 白名单里的字符串枚举只加不改名。
- Provider 类新增字段会带默认值，老调用不 break。

非公开路径（`docingest.pipeline` / `docingest.parsers` / `docingest.chunkers` / `docingest.hooks` / `docingest.output` / `docingest.models`）**可以随意重构**——内部改动不触发版本破坏。

**三条接入面的分工**：

| 接入面 | 用户 | 入口 | 说明 |
|---|---|---|---|
| **Python library** | 工程代码（其他 Python 项目依赖 docingest） | `docingest.ingest(...)` | 首选路径，最薄最稳。支持 Provider 注入、`outputs=` 白名单、自带读回产物 |
| **CLI** | 终端用户、脚本 | `docingest run ...` | 直接走 `run_pipeline`，不经 facade。对用户永远稳定但不走 facade |
| **MCP server** | AI Agent | `python -m docingest.mcp_server` | `@mcp.tool` 薄壳，全部转调 `docingest.api`，协议层在 `mcp_server.py` |

**关键实现点 — facade 和 pipeline 的分工**：

- `api.py` **不做**业务逻辑，只做三件事：① 合并配置（`build_config` 复用现有 `load_config` 四层合并）；② 调 `run_pipeline` / `inspect_files` / `refine_files`；③ 按 `outputs` 白名单读回产物打包到 `IngestResult`。
- `providers.py` **不做**LLM 调用，只是 dataclass 壳子。`.to_model_config()` 把 Provider 对象塑成 `config["models"]["vision"]["primary"]` 一类的 dict 结构，交给现有的 `models/provider.py` 处理。
- `models/provider.py::_set_api_key` 加了"明文 api_key → 写入对应 env var"的路径，但原有 `api_key_env` 逻辑保留。**向后兼容**：改 YAML / 写 .env 的老用户不受影响。

**加新 public API 的规矩**：

1. 新函数加到 `api.py`；签名首参数外全部 `*`(keyword-only)
2. 在 `__init__.py` 的 `__all__` 里导出
3. 在 README 的 "Python Library" 章节加示例
4. 补充一条本表 §3.3 下方 "三条接入面的分工"

不这样做就不是 public API，也就没有版本承诺。

---

## 4. 三个稳定扩展点

这三个是"公开 API"——未来升级会尽量保持兼容。

### 4.1 Hooks（最常用、最轻量）

**设计思想**：按文件扩展名派发 + 三个时机点 + 永不 raise。适合**可选增强**，不适合**关键路径**。

#### 三种 hook 类型

| 类型 | 时机 | 返回值 | 典型用途 | 现有例子 |
|---|---|---|---|---|
| **pre_parse** | Docling 看到文件**之前** | `BytesIO` 或 `None` | 替换文件内容后喂给 Docling | DOCX OMML → LaTeX (`hooks/docx_omml.py`) |
| **post_parse** | 解析完成、Vision **之前** | 无 | 注入结构化数据给 Vision 做 ground truth | PPTX chart 直读 (`hooks/pptx_chart.py`) |
| **pre_write** | Vision 之后、写盘**之前** | 无 | 加元数据 / 改 markdown | file_metadata、sanitize |

#### 最小例子：在 markdown 末尾加文件大小

```python
# src/docingest/hooks/filesize_footer.py
from __future__ import annotations
from pathlib import Path
from typing import Any
from ..parsers.base import ParseResult

def filesize_footer_hook(
    file_path: Path,
    parse_result: ParseResult,
    config: dict[str, Any],
) -> None:
    if not config.get("hooks", {}).get("filesize_footer", {}).get("enabled", False):
        return
    size_kb = file_path.stat().st_size / 1024
    parse_result.markdown += f"\n\n---\n*File size: {size_kb:.1f} KB*\n"
```

在 `hooks/__init__.py::_register_default_hooks()` 加一行：

```python
try:
    from .filesize_footer import filesize_footer_hook
    _register_post("pre_write", ["*"], filesize_footer_hook)
except ImportError as e:
    logger.debug(f"filesize_footer hook not available: {e}")
```

扩展名 `"*"` 表示所有文件。

#### Hook 必须遵守的契约

1. **永不 raise（除了 `HookNoOp`）**：hook 抛普通异常只会被 `logger.warning` 吞掉（生产环境 log level 下可能完全看不到）。要么自己 try/except 并返回 None，要么只在调试时临时 raise。唯一例外：**检测到自己无事可做时**（config 开关关着 / 前置条件缺失 / 扫描完没匹配）**应该 `raise HookNoOp`**——runner 会静默跳过并**不记入 `lineage.transformations`**，保持血缘是"实际起作用的变换"的正面记录。`HookNoOp` 定义在 `hooks/__init__.py`
2. **从 config 读自己的开关**：惯例路径 `config["hooks"][<name>][<key>]` 或 `parsing.<format>.<feature>`
3. **按扩展名派发**：`_register_post("pre_write", ["docx", "doc"], my_hook)` 或 `["*"]` 匹配全部
4. **pre_parse 的 BytesIO 约定**：返回非 None 会**替代**原文件内容喂给 Docling。多个 pre_parse hook 都返回 BytesIO 时，**第一个非 None 赢**。runner 返回 `(stream, hook_name)`——hook_name 供 pipeline 写 lineage 用
5. **别 mutate `file_path` 指向的文件**：原文件永远只读

#### 调试技巧

hook 被吞异常是最常见的坑。调试时把 `run_pre_parse_hooks` / `run_post_parse_hooks`（两者都在 [hooks/__init__.py](src/docingest/hooks/__init__.py)）里的 `except Exception` 临时改成 `raise`，或把 `logger.warning` 升级为 `logger.exception`（会打印 traceback）。另一条路：hook 自己 `raise HookNoOp`（§4.1），runner 会**静默跳过**（不报 warning、不记 lineage），适合"没做事"场景，和"失败"区分开。

### 4.2 Parsers（加全新解析引擎）

**什么时候写 Parser**：Docling 根本搞不定的格式（CAD / DWG / 专有二进制）。

**什么时候不写**：能被 Docling 解析 + 只需要预处理 → 用 pre_parse hook。能被当文本读 → 交给 `TextParser` 兜底。

#### BaseParser 契约

```python
class BaseParser(ABC):
    def __init__(self, config: dict[str, Any]) -> None: ...

    @abstractmethod
    def parse(
        self,
        file_path: Path,
        *,
        override_stream: BytesIO | None = None,
    ) -> ParseResult: ...

    @abstractmethod
    def supported_extensions(self) -> set[str]: ...
```

关键约定：
- `parse()` **绝不 raise**。失败返回 `ParseResult(success=False, error="...")`
- `override_stream` 是 pre_parse hook 的产物；不支持流输入的 Parser 忽略它就好
- `ParseResult.metadata` 至少填 `format` 和 `title`

#### 加新 Parser 的路由

当前 parser 路由写死在 [parsers/\_\_init\_\_.py](src/docingest/parsers/__init__.py) 的 `_DoclingWithFallback` 类（优先级：MediaParser → Docling → TextParser）。

改 `_DoclingWithFallback.parse()` 加一层路由：

```python
# 放在 MediaParser 之后、Docling 之前：
if file_path.suffix.lower() in {".cad", ".dwg"}:
    return self._cad_parser.parse(file_path)
```

`Media parser` ([parsers/media_parser.py](src/docingest/parsers/media_parser.py)) 是个好参考：`accepts()` 方法 / subtitle-first 策略 / ASR fallback / 长音频自动分段 + 并发 / 视频两条可切换路径（默认 `native_video` 整段一次调用；不支持时降级抽帧 + per-page Vision，见 §5.11）。

### 4.3 Chunkers（加新切分策略）

**什么时候写**：现有策略（recursive / heading / slide / sheet / timestamp / whole）都不合适。例如语义切分、AST 代码切分、固定字节切分。

#### BaseChunker 契约

```python
class BaseChunker(ABC):
    def __init__(self, config: dict[str, Any]) -> None:
        # 自动读 chunking.max_tokens / min_tokens / overlap_tokens
        # 自动读 chunking.protection.*
        ...

    @abstractmethod
    def chunk(self, markdown: str, metadata: dict[str, Any]) -> list[Chunk]: ...

    @staticmethod
    def estimate_tokens(text: str) -> int:
        # CJK-aware: CJK≈1.5 tok，ASCII≈0.25 tok
        ...
```

继承 `BaseChunker` **自动获得**：CJK 感知的 `estimate_tokens` / 保护块检测（table / code / list / quote）/ `max_tokens` 等配置。

#### 加新 Chunker 的路由

1. 新文件 `chunkers/my_strategy.py` 继承 `BaseChunker` 实现 `chunk()`
2. 在 [chunkers/\_\_init\_\_.py](src/docingest/chunkers/__init__.py) 的 `create_chunker()` 加 elif
3. 让 `auto` 策略选中你的 chunker：config 加 `chunking.auto.format_strategies.<ext>: "my_strategy"`，并在 `AutoChunker._select_strategy()`（`chunkers/__init__.py:173`）里加 dispatch

---

## 5. 关键机制详解

### 5.1 Vision 增强与 8 层 triage

**架构哲学**：代码不做"哪页需要 Vision"的业务判断。prompt 做决定。

默认配置：

```yaml
parsing:
  vision:
    enabled: true
    max_pages: 50           # 全局 cap，null = 无限制
    image_dpi: 180
    triage:
      enabled: true         # 预筛选：纯文本页跳过 Vision，省 30-60% API
      min_text_length: 50
      max_replacement_ratio: 0.05
      table_line_threshold: 10
      max_mixed_script_fragments: 3
      language_script_check:
        enabled: true
        expected_scripts:
          ja: [Latin, Han, Hiragana, Katakana]
          zh: [Latin, Han, Hiragana, Katakana]
          en: [Latin]
          ko: [Latin, Hangul, Han]
```

#### 8 层检测（全通过才跳过 Vision）

`pipeline.py::_should_skip_vision`：

1. 无 `<!-- image -->` 标记
2. 无 PPTX chart 等 hook 注入的结构化数据
3. 文本长度 ≥ `min_text_length`
4. 无 `glyph<` / `glyph&lt;` CID 失败标记
5. U+FFFD 占比 ≤ `max_replacement_ratio`
6. Markdown 表格行数 < `table_line_threshold`
7. 无混合脚本碎片（CJK 之间夹短 ASCII，OCR 误识特征）
8. 脚本与声明语言一致（catches CMap 失败产出合法但错误 Unicode——比如日语 PDF 出现大量孟加拉语字符）

**设计动机**：false negative（多送 Vision）代价很小；false positive（该送没送）丢信息代价大。所以 8 层门槛故意偏严。

#### Vision prompt 反幻觉

`parsers/vision.py::_PAGE_PROMPT` 强制只用两种不确定标记：

- `[?]` — 部分可读（`¥1,234,5[?]`）
- `[unreadable]` — 完全不可读（可选冒号 + 位置描述）

禁止 `[illegible]` / `???` / `(low confidence)` / 省略号等变体。下游 `output/quality_report.py` 机器扫描这两个 marker 输出质量评分。

#### Supplement 模式（按格式去重，xlsx 默认开）

`parsing.vision.supplement_only` 决定 Vision 是"整页转写"还是"只补充"，**按格式分流**（`resolve_supplement_only(config, doc_format)`：`parsing.<format>.vision.supplement_only` > 全局）：

- **全局默认 `false`（full，整页转写）** —— PDF / PPT 必须这样：Docling 的 layout 模型把方眼紙表格拍成碎片（判不成 table），只有 Vision 整页转写能还原结构（实测合同首页关键字段召回 full 8/8 vs supplement 4/8，supplement 在碎片页会漏正文）。
- **xlsx 覆盖为 `true`（`parsing.xlsx.vision.supplement_only`）** —— Vision 拿 openpyxl 渲染的表格当 ground truth，**只补视觉内容（图 / 图表 / 印章 / 手写），绝不重抄表格行**。因为 xlsx 走 openpyxl（程序直读 cell，输出规整），表格正文已在 openpyxl 那份里、supplement 碰不到它 → 不会漏；Vision 不重抄 → 从源头消除 xlsx 的 Docling↔Vision 重复（实测 moonmile 重抄 30%→2.5%、foox 65%→0、松竹梅 11%→5.5%）。

prompt 用 `_PAGE_PROMPT_SUPPLEMENT`（per-page）/ `_BATCHED_PROMPT_SUPPLEMENT`（batched），都含"ground truth 空 / 缺大部分就整页转写"的逃生——对 xlsx 可靠（openpyxl 输出规整、不会像 Docling 碎片那样骗过逃生）。

**实现要点（坑）**：openpyxl 抽的表格文本（ground truth）必须喂给 Vision，但 xlsx 经 LibreOffice 渲染出来的 page 其 `page_data.text` 是**空的**，所以 `_enrich_with_vision` 的 **batched 和 per-page 两条路径都要把 `parse_result.markdown`（此时是 vision 前的 openpyxl 渲染）当 ground_truth 传给 Vision**。漏喂会让逃生误触发 → Vision 整页转写 → 重抄（per-page 路径曾因只喂 batched、漏喂 per-page 导致 moonmile 重抄不降，已修）。

#### 并发 + 缓存

- `ThreadPoolExecutor` 并发调用，worker 数读 `performance.parallel_files`（该配置被 Vision 页内并发独占使用；文件级并发**刻意不实装**，见 §9.1）
- 每次调用按**图片内容哈希 + prompt 内容哈希 + structured_data hash +（supplement 时）ground_truth hash** 做 cache key，同页同内容重跑零成本；prompt 文本或 ground truth 一变就自动失效，避免改 prompt 后吃到旧缓存（`_prompt_hash` + `pt_tag`）

#### Batched 多图调用（xlsx 长 sheet 专用）

xlsx 一个 sheet 可能被 LibreOffice 渲染成多张 PDF page（长方眼紙 / 跨页表），per-page 独立调用看不到跨页延续。当满足以下条件时，`_enrich_with_vision` 改走一次合并调用，把整批 page 图喂给 Vision：

- `parsing.vision.batched_call.enabled` = true（默认开）
- 文件格式 ∈ {xlsx, xls}
- `len(vision_tasks) / visible_sheet_count >= min_pages_per_sheet`（默认 1.5）
- `len(vision_tasks) <= max_images_per_batch`（默认 20，超了 fallback 单页）

不满足任一条件 → 完全走原 ThreadPoolExecutor 单页路径（PDF / PPTX / DOCX / 单页 xlsx / DB-spec 类多 sheet 1:1 xlsx 全部不触发）。合并结果以 `<!-- vision-enriched batched, pages=X-Y -->` 标记追加到最后一个 pagebreak section 末尾；任意失败（empty response / API 异常）自动 fallback 单页并 warn。Cache namespace 与单页 disjoint（`batched_vision|...` vs `page_vision|...`），不会互相污染。

实测见 commit `0248b6e` 的提交信息（foox USDM 4 sheet → 18 page：per-page ×18 跑 204s 截断 9/11，batched ×1 跑 69s 0 截断，命中率 18% → 90%+）。

### 5.2 Excel 路径 — 用 openpyxl 而非 Docling 渲染

**默认开启 `parsing.xlsx.use_openpyxl_renderer: true`**。`DoclingParser.parse` 入口最先判断 `suffix in {".xlsx", ".xls"}` + 配置开关 + openpyxl 可用，命中 → 调 `_parse_xlsx_via_openpyxl`；否则走原 Docling 路径。

**为什么不让 Docling 处理 xlsx**：Docling 的 Excel backend 把所有 sheet 渲染成一坨大表后插 `<!-- pagebreak -->`，但**标题段和 body 经常错位一位**——`## レスポンス_API仕様` 标题段下挂的内容是 `凡例）データ型` sheet 的，真正的 レスポンス 内容塞到了上一段 `## リクエスト_API仕様` 标题下。下游 chunker 按段切，整段错位的内容就背着错误的 `title_path` 进 chunks.jsonl，RAG / Agentic Search 按 sheet 名过滤直接漏。

`_parse_xlsx_via_openpyxl` 的设计：

- **每 sheet 一段**：`## <sheet名>\n\n<markdown 表>` + 段间 `<!-- pagebreak -->`，归属 100% 正确
- **合并单元格 anchor-only**：`merged_cells.ranges` 里只有左上角 cell 保留值，其它位置留空。彻底回避 Docling "每 cell 复制 N 份" 导致 2.7 MB 输出的问题
- **列剪枝**：扫一遍所有 cell，统计哪些列实际有值；输出时只 keep 这些列。方眼紙レイアウト (`max_column = 130` 但实际只 8 列有内容) 直接收缩成正常宽度
- **行剪枝**：列剪枝后全空的行整行丢
- **Cell 净化**：`\n / \r → 空格`，`| → \|`，markdown 表格语义不会被原 cell 内容打破
- **隐藏 sheet 跳过**：`ws.sheet_state != "visible"` 的 sheet 不输出，和人在 Excel 里看到的一致
- **metadata 与 Docling 路径完全一致**：`format / title / pages(=sheet 数) / has_tables / docling_origin{mimetype, binary_hash}` 都填上，下游 hook (`file_metadata` 提升 mimetype/binary_hash) 和 frontmatter writer 无需特殊处理。binary_hash 用 SHA-256 截 64bit（稳定但不要求和 Docling 算法一致——它是血缘标识不是 cache key）
- **嵌入图复用**：调既有的 `_extract_xlsx_images` 把 `xl/media/*` 抽出来，写进 `metadata.xlsx_embedded_images`
- **图片 anchor 链**：直接解 OOXML（`xl/workbook.xml` → `xl/worksheets/sheetN.xml` → `xl/drawings/drawingY.xml` + 各级 `_rels`），拿到每张嵌入图的 (sheet, row, media basename) 三元组。**不用 openpyxl `ws._images`** —— 它在 load 时静默丢弃 EMF/WMF 并会把 `path` 重写成不可靠的值，实测下不可信。只采 `<xdr:pic>` 锚点，跳 `<xdr:sp>` / `<xdr:cxnSp>`（形状 / 连接线没有 media，加进来会变 ghost marker）。每张图在 renderer 里对应行后插一行 `<!-- image: {filename} -->`，Vision triage 既有逻辑（看到 `<!-- image -->` 强制送 Vision）自动接住。EMF/WMF 也参与 marker —— 抽出文件仍在 `assets/`，下游 RAG / chunk 能搜到文件名
- **pages 字段空 list**：让 pipeline 后续的 `_ensure_excel_page_images`（LibreOffice → PDF → 截图）按原逻辑填上，Vision 增强一路不变

**逃生**：openpyxl 没装 → 返回 `None`，调用方 fall through 到 Docling；workbook 打不开（损坏文件）→ 返回 `ParseResult(success=False)`，调用方同样 fall through。**永远不会让 xlsx 解析挂掉**。

**对其它格式零影响**：PDF / DOCX / PPTX / HTML / MD / CSV / 图片 / 音视频 / ZIP 全部走原有 Docling 或专门 parser 路径，一行不动。

### 5.2.1 Excel 去噪策略

Excel 常见两种：
- **数据表**：行列规整 → 几乎不动
- **方眼纸式规格书**：大量合并单元格 + 空行 → 同一套规则自动识别并大幅清理（实测 126K 字符 → 1.9K 字符，零信息损失）

三遍清理（`pipeline.py::_clean_excel_markdown`）：

1. **行内去重**（run-length collapse）：`| foo | foo | foo |` → `| foo |`，安全门槛 ≥50% 去除率（防止合法重复值被误吞）
2. **空单元格剥离**：>50% 空的行 strip 掉空位；全空行整行删
3. **行间去重**：单元格数 ≤1 的连续同值行合并

### 5.3 LibreOffice 页图生成

Docling 的 DOCX / XLSX / PPTX 后端**不生成页图**（或质量差）。为让 Vision 能看到嵌入图表 / 截图 / 布局：

```
document.ext → LibreOffice --headless --convert-to pdf → pdf2image → 页截图
```

默认页数 cap：xlsx=10 / docx=20 / pptx=30。超了 → 只处理前 N 页 + warning。单图超过 `max_image_pixels` (默认 4M) 自动降采样。

LibreOffice 没装 → 所有 office 格式降级为纯文本模式，静默跳过（不报错）。二进制发现走 `utils/binary_finder.py`。

### 5.4 去重：源头 supplement（主力）+ 后处理 output.dedup（旧、默认关）

**消除 Docling↔Vision 重复的主力是源头 supplement（见 §5.1）**——xlsx 让 Vision 只补视觉、不重抄表格，重复在生成时就不产生（无损：openpyxl 那份正文始终保留）。下面这套 `output.dedup`（写盘后按长度比删一份）是更早的尝试，因长度比根本判不了"内容是否覆盖"、不可靠，**默认关**，保留作纯文本 PDF 语料的可选项。

Vision 的 prompt 里**送了 Docling 文本作为参考**，**理想情况下** Vision 输出是 Docling 的超集 —— 但这个前提**只对单页对齐的格式（纯文本 PDF / PPTX）可靠**。xlsx / docx / 混合输入里，Docling 看到的"段"和 Vision 看到的"页图"**经常对不上**：

- **xlsx**：Docling 把多 sheet 渲染成一坨大表，sheet 标题位置和 body 经常错位；LibreOffice 渲染又只截前 N 张页图，**真正的 docling 内容 ≠ vision 看到的内容**
- **docx**：流式文本无页概念，LibreOffice 渲染的页和 Docling 段也对不上（已走 Mode B append，不走段对齐）
- **半 vision 半 docling 场景**：xlsx 配额 10 张但 6 sheet 各占 N 页，必然有 sheet 一页页图都没；triage 也会逐页跳过纯文字页

去重逻辑（`pipeline.py::_dedup_vision`）按 pagebreak 段内长度比例 + 绝对字符门判断，**但长度根本无法判断"内容是否覆盖"**——长度相近可能是巧合，错位的 docling 内容会被无关 vision 描述替换。

**因此默认 `output.dedup.enabled = false`**：
- 段里同时有 Docling 和 Vision 时 → 都保留（用户/RAG 各取所需）
- 段里只有 Docling（没 vision-enriched 标记）→ 函数空转，无影响
- 段里只有 Vision（极少见）→ 保留原样

**何时手动开启**（`output.dedup.enabled: true`）：
- 你的语料**主要是纯文本 PDF**（Vision 是 Docling 超集这个前提在你身上成立）
- 你确认 markdown 体积压缩比"内容不丢"更重要
- 你测过你的下游 RAG / Agentic Search 在 dedup 后召回率没下降

开启后两个门同时管：`vision_ratio_threshold`（默认 0.7）+ `vision_min_chars`（默认 200，绝对字符底线，挡住小 Vision 误吃大 Docling 的场景）。

关闭代价：`sources/*.md` 可能比开启时大 30-100%（取决于 vision 覆盖比例）；chunks.jsonl 因 chunker 各自切分，**重复内容会自然分到相邻 chunk**，反而提升 RAG 召回。

### 5.5 ZIP / URL / magika 边缘输入

#### ZIP 展开（`utils/zip_expander.py`）

- 内容探测（`zipfile.is_zipfile`），不靠扩展名——重命名的 zip 也能识别
- 排除 Office OOXML（.docx / .pptx / .xlsx 结构上是 zip 但有专用 parser）
- **CJK 文件名恢复**：Windows zip 工具常编码为 CP932，Python zipfile 默认 CP437 解码出乱码。回退编码链 `utf-8 → cp932 → shift_jis → euc-jp`（CP437 故意排除，它是我们要逃离的编码）
- Bomb 保护：总大小 / 文件数 / 嵌套深度 / 单条目压缩比 1000x 四层限制
- 持久化展开到 `{output.dir}/.cache/_zip_extract/`，二次运行复用
- Zip slip 防护：`resolve().relative_to(extract_root)` 检查

#### URL 解析（`utils/url_resolver.py`）

通过 yt-dlp 支持 1000+ 视频平台：

- 一条命令同时下载音频（mp3）、所有语言字幕、info.json 元数据
- 直链媒体 URL（`.mp3` 等）跳过 yt-dlp，直接 HTTP GET
- 自动探测可用 JS runtime（node / deno / bun）
- 缓存到 `{output.dir}/.cache/_media/<url_hash>/`

#### 内容检测 magika

扩展名失真时用 Google magika ML 模型（约 25MB）做内容识别。典型场景：ZIP 解压出的 `README` / `Dockerfile`（无扩展名）、重命名后的 `data.tmp`。默认不覆盖 `.pdf` / `.docx` 等强扩展名（`correct_strong_extensions: false`）。

### 5.6 Chunking 与保护块

#### Auto 策略选择

```
chunking.strategy = "auto"（默认）
    ↓
检查 metadata["format"]
    ├─ pptx      → slide
    ├─ xlsx/csv  → sheet
    ├─ 音视频    → timestamp
    ├─ 图像      → whole
    └─ 其它      → 结构评分：
        ├─ score ≥ prefer_heading_threshold (默认 2) → heading
        └─ score < threshold → recursive
```

结构评分三项（每项得 1 分）：H1-H3 数量 ≥ `min_headings` / 标题层级不跳跃 / 段间内容多数在 100-2000 token。

#### 策略一览

| 策略 | 适用 | 行为 |
|---|---|---|
| `auto` | 默认 | 按格式 + 结构自动选 |
| `heading` | 结构化文档 | 按 H1-H3 切分，小段合并，大段递归再切 |
| `recursive` | 非结构化 | 段落 → 句 → 字符 递归，`overlap_tokens` overlap |
| `slide` | PPTX | pagebreak / HR / "Slide N" 标题检测 |
| `sheet` | XLSX / CSV | pagebreak 或 `##` 检测分 sheet，每表头重复在子 chunk 顶部 |
| `timestamp` | 音视频 | `[MM:SS]` 检测，每 chunk 带 `start_seconds` / `end_seconds` |
| `whole` | 图像 / 极短 | 整文件一个 chunk |

#### 保护规则（跨所有策略自动应用）

```yaml
chunking:
  protection:
    tables: true        # 不拆 Markdown 表格
    code_blocks: true   # 不拆 ``` 块
    lists: true         # 列表尽量整体
    quotes: true        # > 引用块尽量整体
    allowed_overflow:
      table: 2.0        # 可超 max_tokens 2x
      code_block: 3.0
      list: 1.5
      quote: 1.5
      default: 1.5
    on_overflow:
      table: "row_split"   # 超了按行分，每块复制表头（2026 业界做法）
      code_block: "bypass" # 超了保留不拆（拆会破坏语法）
      list: "bypass"
      default: "bypass"
```

**表格行切分**（`chunkers/table_splitter.py`）：按数据行切分，每个子 chunk 重复表头，让每片独立可读。解决 Docling 对合并单元格展开产生的超宽表格问题。

#### 后处理（`pipeline.py::_postprocess_chunks`）

1. Image 噪声清理：含 `<!-- vision-enriched -->` 的 chunk 删掉 `<!-- image -->` 占位符
2. 片段合并：token < `min_tokens` 的 chunk 向前/向后合并到同 section 邻居（双向两遍）
3. 重新编号 + 内容标记（`has_table` / `has_image_ref`）

### 5.7 Knowledge Map 两阶段

用于 Agent / 下游检索系统的导航元数据。

**Stage 1（零 AI 成本）** `output/knowledge_map.py::build_stage1`：
- 每文件的 sections / sheets / keywords
- 全库 `keyword_index`（keyword → 含该词的文件列表）
- 按文档频率过滤（> 70% 文件出现的词当 stop word 扔掉，跨语言通用）
- 关键词抽取：**SudachiPy**（可选，日语形态素）或 **regex 回退**

**Stage 2（可选，一次 AI 调用）**：
- 只读结构摘要，不读 chunk 内容（~3K token）
- 输出 `summary` + `search_guide`（3-5 条搜索策略建议）
- 三层保护：L1 token 预算走 `models.defaults` / L2 `finish_reason == "length"` 自动重试 / L3 schema 不完整条目丢弃

**产物**：`knowledge_map.yaml`（机读）+ `knowledge_search.SKILL.md`（Agent 读，按语言自动选 ja / zh / en 搜索协议模板）。

### 5.8 AI 配置与 fallback

#### 每任务独立 primary + fallback

```yaml
models:
  defaults:
    max_response_tokens: 32768
    retry_on_truncation: true        # 应用层：finish_reason=="length" 时重试
    retry_max_tokens: 65536
    max_retries: 2                    # 网络层：litellm 内置重试次数

  vision:
    max_response_tokens: 32768
    primary: { provider: "google", model: "...", api_key_env: "GEMINI_API_KEY" }
    fallback: { provider: "openai", model: "...", api_key_env: "OPENAI_API_KEY" }

  chunking_assist: { ... }
  audio_transcription: { ... }
```

Primary 失败自动切 fallback。所有 provider 走 litellm（DashScope 原生 SDK，因为 `qwen3-asr-flash` 的 base64 multimodal litellm 不原生支持）。

#### 双层重试：网络层 vs. 应用层

DocIngest 里有**两个独立**的重试机制，容易混淆：

| 层 | 触发条件 | 配置 | 实现位置 |
|---|---|---|---|
| **网络层** | 瞬时错误：rate limit / 5xx / TCP 重置 | `models.defaults.max_retries`（默认 2） | `litellm.completion(..., num_retries=...)`，litellm 内部指数退避 |
| **应用层截断** | `finish_reason == "length"`（LLM 被 token 预算切断） | `retry_on_truncation` + `retry_max_tokens` | `provider.py::text_completion` 自己递归一次，加大 `max_tokens` |

两者**正交**：网络层先失败→ litellm 自动重试几次 → 真正返回后，如果响应被截断 → 应用层再重试一次放大 budget。二者可以**同时触发**（网络问题得到响应 + 响应不完整），不会互相干扰。

`provider.py::resolve_max_retries` 和 `resolve_max_tokens` 同构：task 自己的 `max_retries` > `_defaults.max_retries` > hardcoded 2。所以 `parsing.<format>.vision.max_retries` 也能按格式 override（虽然 `resolve_vision_config` 当前的 shallow merge 只列了 model/max_response_tokens/image_dpi 三个 key，未来按需扩展即可）。

#### 每格式 Vision 覆盖

```yaml
parsing:
  pdf:
    vision:
      image_dpi: 220        # 高密度 PDF 提高 DPI
  pptx:
    vision:
      max_response_tokens: 8192   # PPT 内容稀，省 token
```

`parsers/vision.py::resolve_vision_config` 做 shallow merge——未设字段 fall through 到全局 `models.vision`。

#### 全局 token defaults 注入

`config.py::_inject_model_defaults` 在加载时把 `models.defaults` 注入到每个 task 的 `_defaults` 字段。`provider.py::resolve_max_tokens` 优先级：

1. 显式 max_tokens 参数
2. task 自己的 max_response_tokens
3. task._defaults.max_response_tokens
4. hardcoded 32768（只在配置彻底缺失时触发）

### 5.9 增量缓存

#### cache_key 算法

```
cache_key = md5(head_8192 + tail_8192 + size + filename)
```

**不含目录路径**——文件移动仍命中。**含文件名**——同内容不同名的文件不会互相覆盖。后果：重命名文件触发一次重跑（可接受的正确性代价）。

#### config_hash 白名单

**只有影响输出的配置才进 `_RELEVANT_CONFIG_PATHS`**。改 `output.dir` / `performance.parallel_files` 不触发重跑；改 `chunking.max_tokens` / `parsing.vision.image_dpi` 触发。

当前白名单见 `incremental.py::_RELEVANT_CONFIG_PATHS`。

#### 复用行为

cache hit 的文件：
- 跳过所有 Phase 1-3
- 从老 `chunks.jsonl` 按 `chunk_ids` 拉回 chunk records
- index entry 从 meta 直接加进 IndexBuilder
- 产物（sources/*.md / assets/）保持原样

`chunks.jsonl` 最终 = 复用的 records + 新 records，每次全量重写。

#### 失效触发

任一条件：
- `cache_key` 变了（文件内容或名字变了）
- `config_hash` 变了
- `sources/<file>.md` 或 assets 文件被外部删了
- meta 里记录的 `chunk_ids` 在当前 `chunks.jsonl` 里找不全

`--force` 命令行参数跳过整套 cache 检查。

### 5.10 Chunk Lineage（`metadata.lineage`）

每个 chunk 的 `metadata.lineage` 子字段是**显式血缘**——告诉下游消费者"这个 chunk 是从哪来的、被哪些变换塑造过"。

#### 格式

```json
{
  "lineage": {
    "source_markdown": "sources/contract.md",
    "original_input": {
      "filename": "contract.pdf",
      "mimetype": "application/pdf",
      "binary_hash": 1049930...,
      "last_modified": "2026-04-12T15:43:55"
    },
    "transformations": [
      {"step": "hook",   "name": "docx_omml_preprocess_hook", "phase": "pre_parse"},
      {"step": "parser", "name": "_DoclingWithFallback", "format": "pdf"},
      {"step": "hook",   "name": "pptx_chart_hook", "phase": "post_parse"},
      {"step": "vision", "model": "gemini-3-flash-preview", "pages_enriched": [1,2,5]},
      {"step": "hook",   "name": "file_metadata_hook", "phase": "pre_write"},
      {"step": "chunker","name": "HeadingChunker", "max_tokens": 1024}
    ]
  },
  ...  // 顶层平级字段 source / original_file / format / language / title_path
       // / chunk_index / tokens / ... 全部保留不变，向后兼容
}
```

#### 两部分语义

**Sources** — chunk 内容来自哪里（两跳血缘）
- `source_markdown` — chunk 直接切自哪个 `sources/*.md`
- `original_input` — 那份 md 又来自哪个原输入（PDF / DOCX / URL / ...）

**Transformations** — 有序数组，记录**实际起作用**的变换。按 step 类型分：
- `parser` — 用的解析器 + 识别出的格式
- `hook` — pre_parse / post_parse / pre_write 三个时机点上**实际触发**的 hook（`HookNoOp` 的不记）
- `vision` — 只有实际做了 Vision enrichment 才记（triage 全跳过的文件不记），带 `pages_enriched` 列表
- `chunker` — 最终用的 chunker 类 + 关键配置

#### 下游怎么用

- **RAG citation**：用户问"这条来自哪一页"时，结合 `source_markdown` + `original_input.filename` + `index.json` 的 `element_boxes`（按页面的 bbox 坐标）定位到原 PDF 某页某 block
- **质量归因**：某些 chunk 被用户反馈"不准"时，查 `transformations` 看是否被 Vision 改写过 / 哪个 chunker 切的，能缩小排查范围
- **可重现**：换 chunker 或 Vision 模型重跑时，diff 两次的 `transformations` 就知道差异来自哪个步骤

#### 实现要点

- 生成入口：`pipeline.py::_build_chunk_lineage`，在 Phase 3.1（`inject_paths` 之后）为每个 chunk 独立生成一份（copy-on-attach，chunks 之间互不污染）
- 填充点：Phase 1 / 1.0 / 1.4 / 1.5 / 1.6 在**成功**时往 `parse_result.transformations` append。失败的变换**不记**——这是一条**正面变换轨迹**，不是调试日志
- 顶层平级字段**一个都不删**：现有 RAG 代码读 `metadata.source` / `metadata.original_file` 继续工作。lineage 只是**归集 + 扩展**

### 5.11 视频两条路径：抽帧 vs 原生（native_video）

视频有**两条可切换**的处理路径，都在 `media_parser.py`，由 `parsing.audio.native_video.enabled` 选择：

| | **native_video（默认）** | **抽帧路径（降级 fallback）** |
|---|---|---|
| 怎么做 | 整段视频一次性丢给视频模型，模型同时看帧+听音轨，**一次调用**返回时间轴对齐的「转录+画面」Markdown | ffmpeg 抽音轨→ASR 转录；ffmpeg 每 N 秒抽一帧→当 PageData→pipeline Phase 1.5 逐帧 Vision；时间戳拼接对齐 |
| 调用次数 | **1 次** | N 帧 = N 次 Vision + ASR |
| provider | **仅 Gemini**（`google` provider）；其它降级到右列 | 任意（ASR + 任意 Vision） |
| 入口 | `_parse_via_native_video` → `provider.py::describe_video` | `_parse_from_asr` + `_attach_video_frames` |

**为什么并存不二选一**：原生路径成本/质量/对齐全面更优（实测 100s 录屏：1 调用 vs 10、~11K token vs ~50K、`[unreadable]` 大幅减少，因为模型看真实视频流而非缩小的单帧），但它**只有 Gemini 支持**，且 multi-provider 是项目根基——删掉抽帧 = 砍掉非 Gemini 用户的视频能力。所以加新路径、配置切换，不替换。

**融合点（关键）**：`_parse_via_native_video` 返回的 `ParseResult.pages=[]`。pipeline Phase 1.5 `_enrich_with_vision` 开头 `if not parse_result.pages: return` → **自动跳过逐帧 Vision**。产出已是带 `[MM:SS]` 的成品 Markdown，后续 Phase 2/3（写 md、`timestamp` chunker 切片）和普通文档完全一样——**主 pipeline.py 一行不动**。

**优先级**：字幕 > native_video > 抽帧。有字幕仍用字幕（免费、最准）；native 只在「视频 + 无字幕 + enabled」时走。

**传输分流**（`describe_video` 内，走 google-genai SDK）：`< files_api_threshold_mb`（默认 20）走 base64 内联；`>=` 走 Files API（上传→**轮询 ACTIVE**→引用→用完删）。轮询是真实外部异步状态，必须等（大文件上传后是 PROCESSING，不等就引用失败）。

**三条降级**（都 log warn、不 raise、回退抽帧）：① 非 Gemini provider（`NativeVideoUnsupported`）；② google-genai SDK 没装；③ API 调用失败 / 返回空。

**成本防爆**：native 成本按时长走（Gemini ~300 tok/s），由现有 Phase 0 safety 体检的 `safety.per_file.max_duration_sec`（默认 30 分钟）兜住——长视频触发 warn/strict 等 `--yes`，和大 PDF 一个待遇。没在 native 这里加单独的 cap（事后截断 = 信息丢失伪装成防御）。

**已知特性**：「说」（音轨转录）稳定可靠且能音画互校（实测把 ASR 听错的 "Max" 校正成画面里的 "MUX"）；「画面」（视觉 OCR）在小字/水印区有非确定性抖动（`[?]`/`[unreadable]` 标记数会跨次浮动）——这是模型诚实标注边界，不是幻觉，不影响转录完整性。

模型配置：`models.video_understanding` 若设则用，否则继承 `models.vision`（再继承 `models.defaults`）——和 vision/chunking_assist 同一套 one-model-to-rule 继承。

---

## 6. 加新 Phase / 改 pipeline.py

当上面的扩展点都不适合——比如要加"表格 AI 修正"Phase，位置在 Excel denoise 之后、Vision 之前——就得动 `pipeline.py` 了。

### 6.1 改 `process_single_file`

在合适的 Phase 编号位置插入代码，参考现有 Phase 的三件事：

```python
# Phase X.Y: <名字>
# <为什么 + 什么时候触发>
if get_nested(config, "my_feature.enabled", False):   # 1. config 开关
    try:
        my_feature_process(parse_result, config)       # 2. 核心逻辑
    except Exception as e:                             # 3. 不阻断后续
        _pipeline_logger.warning(f"my_feature failed: {e}")
```

### 6.2 必须同步修改

1. **`incremental.py` `_RELEVANT_CONFIG_PATHS`**：加 `my_feature.*` 的路径（否则 cache 无效）
2. **本文档 §2.2 Phase 表格**：加一行
3. **README.md Pipeline at a Glance**：简化图里视情况加一行

### 6.3 注意事项

- Phase 执行顺序即代码顺序，没有事件总线——改顺序要看清上下游谁读谁写 `parse_result`
- 需要并发/异步，参考 `pipeline.py::_enrich_with_vision` 的 ThreadPoolExecutor pattern
- 需要持久化中间结果，用 `AICache`（`models/cache.py`）——基于 content hash，天然幂等

---

## 7. 配置层

### 7.1 四层合并

优先级（高到低）：
1. CLI 参数（`--strategy`、`--force` 等）
2. 环境变量 `DOCINGEST__*`
3. 项目 `docingest.yaml`（工作目录下）
4. `config/default.yaml`（bundled 默认）

### 7.2 加新 YAML 配置项

直接加到 `config/default.yaml`：

```yaml
my_feature:
  enabled: false
  threshold: 0.5
```

读取：

```python
from ..config import get_nested
value = get_nested(config, "my_feature.threshold", 0.5)
```

### 7.3 环境变量自动支持

任何 YAML 路径都自动支持 `DOCINGEST__<path>=<value>` 覆盖：

```bash
export DOCINGEST__my_feature__enabled=true
export DOCINGEST__my_feature__threshold=0.8
```

双下划线 `__` 是层级分隔符。值会尝试推断类型（bool/int/float/null/str）。

### 7.4 配置改动和 incremental cache（**重要坑点**）

**这是最容易踩的坑**：如果新配置会**影响输出内容**（markdown / chunks），必须在 [incremental.py](src/docingest/incremental.py) 的 `_RELEVANT_CONFIG_PATHS` 里加上它的路径——**否则用户改了配置不会触发重跑，会得到过时但看似正常的输出**。

不影响输出的配置（如 `output.dir` / `performance.parallel_files`）**不要**加，避免无谓的 cache 失效。

判断标准：这个配置改了，同一份文件第二次跑是否应该产生不同的 `sources/*.md` 或 `chunks.jsonl`？是 → 加进去；否 → 不加。

---

## 8. 常见坑点

### 8.1 hook 异常被静默吞掉
`run_pre_parse_hooks` / `run_post_parse_hooks` 对 hook 异常是 `logger.warning` 级别。生产环境默认 log level 很可能完全看不到。调试把对应 warning 升级为 `logger.exception`。

### 8.2 `parse_result` 是共享可变对象
所有 Phase 共享同一个 `parse_result`。A hook 改 markdown 后，B hook 看到的就是改过的。顺序敏感。

### 8.3 语言检测只在一处跑
`_detect_language` 在 Phase 1.4.5 执行一次。如果你的 hook 在这之前读 `metadata["language"]`，可能是 None。

### 8.4 Vision triage 依赖前一步语言检测
triage 的 `language_script_check` 用 `parse_result.metadata["language"]` 做白名单判定。如果上游检测错（pymupdf fallback 后内容变了），triage 可能误判。

### 8.5 config 变了但 cache 没刷新
见 §7.4。**这是最难 debug 的问题**——"我改了配置怎么输出没变？"——90% 是新配置没进 `_RELEVANT_CONFIG_PATHS`。

### 8.6 Parser / Chunker 路由是硬编码
加新 Parser 得改 `parsers/__init__.py` 源码，不是注册中心模式。未来可能变，当前状态如此。

---

## 9. 技术债 / 已知偏差

这一节诚实记录当前实现和设计初衷不一致的地方，供后续修复参考。

### 9.1 文件级并行刻意不实装

`performance.parallel_files` 配置**实际只被** Vision 页内并发 + ASR 分段并发使用。`run_pipeline` 对 `to_process` 列表是**串行**处理。

这不是"来不及做"，是**评估后主动不做**。理由：

1. **嵌套并发的 rate-limit 乘数效应**：`parallel_files` 已经在 Vision 层内用（典型 = 4），若文件级再用一层同等并发，同时在飞的 Vision API 调用会是 `files × pages = 16+`，即便 Gemini 付费 tier 3 的 1000 RPM 也会在大 batch 时打爆
2. **共享可变状态需要成批加锁**：`existing_names` set（文件名去重）、`IndexBuilder`、`pipeline_result.*` 累加、`new_chunks.extend` 全部非线程安全，每个点都要加锁
3. **依赖线程安全性未保证**：pymupdf 官方明确声明非 thread-safe；Docling 内部调用 pymupdf，多线程共享 parser 实例行为未定义
4. **当前量级扛得住**：典型 batch 10-50 文件，串行 3-25 分钟可接受；+ 增量缓存（二次跑秒级）已经覆盖 90% 重复运行场景

**触发重估的信号**：用户真正报告"单次 500+ 文件 batch 太慢"、或遇到需要在线低延迟返回的集成场景时，再重新评估。届时建议同时设计**全局 LLM semaphore** 控制跨文件的 API 并发总量，否则必然打爆 rate limit。

### 9.2 Refine 不重试截断

`knowledge_map.py::enrich_with_ai` 实现了 `retry_on_truncation`，`refine.py` 没有——截断时只 warning + 加 marker。两个路径的策略应该统一（抽到 `text_completion` 内部）。

### 9.3 Parser / Chunker 路由未注册中心化

加新 Parser / Chunker 需要改 `parsers/__init__.py` / `chunkers/__init__.py` 的 `create_*()` 工厂和 `_DoclingWithFallback` / `AutoChunker` 的 if-elif。不是插件化注册模式。

### 9.4 DOCX 无页图时的 Vision 注入降级

DOCX 经 LibreOffice 渲染后，Docling 的 text 结构和 PDF 页 index **不对应**（DOCX 文本是流式的，没有原生页概念）。此时 Vision 结果不能按页对齐注入，退化为在文档末尾 append 所有 Vision 结果（Mode B，`pipeline.py::_enrich_with_vision` 注释详述）。

### 9.5 `_has_unexpected_scripts` 回退偏严

`doc_language` 为 `unknown` 或不在 `expected_scripts` 配置里时，triage 用所有配置语言的脚本并集做白名单。处理俄语 / 阿拉伯语 / 希伯来语文档（不在默认 ja/zh/en/ko 里）会被误判为 unexpected scripts，每页白送 Vision。多语言用户需要手动扩展 `expected_scripts`。

---

## 附：参考资料

- Chunking：[Vecta 2026.02 benchmark](https://www.runvecta.com/blog/we-benchmarked-7-chunking-strategies-most-advice-was-wrong) — recursive 512t 在 7 种策略中最优
- Chunking：[Vectara NAACL 2025](https://aclanthology.org/2025.findings-naacl.114/) — 语义切分成本不划算
- 解析：[Docling vs LlamaParse vs Unstructured](https://llms.reducto.ai/document-parser-comparison) — Docling 表格 97.9%
- 检索：[Amazon Science](https://www.amazon.science/publications/keyword-search-is-all-you-need-achieving-rag-level-performance-without-vector-databases-using-agentic-tool-use) — 关键词搜索达 RAG 90%+
- Excel：[方眼紙 Excel → MD 全手法比较](https://zenn.dev/ougotti/articles/houganshi-excel-to-markdown)
- 表格切分：Chonkie TableChunker / Ragie 表格切分（2026 业界做法）

---

---

## 10. GraphRAG 子模块（`docingest.graph`，可选）

### 10.1 位置和边界

`docingest.graph` 是装在主项目里的**可选下游能力**，不是主 pipeline 的一部分。和主流程的关系：

```
chunks.jsonl ─────┐
sources/*.md ─────┼──► docingest.graph.build()  ──►  knowledge/graph/
                  │      （用 LightRAG）              ├── graph_chunk_entity_relation.graphml
                  │                                  ├── vdb_entities.json
                  │                                  ├── vdb_relationships.json
                  │                                  ├── kv_store_*.json
                  │                                  └── docingest_graph.json (manifest)
                  │
                  └────────► （RAG / Agentic Search 仍照常用 chunks.jsonl + sources/*.md）
```

主 pipeline 完全不知道 graph 存在；graph 不重新解析任何原文档，只读 chunks.jsonl。

### 10.2 模块布局

```
src/docingest/graph/
├── __init__.py                        public API: build / query / status / providers / GraphBackend
├── api.py                             facade（build / query / status + dataclasses + 配置合并）
├── providers.py                       EmbeddingProvider 基类 + OpenAI / Gemini / SentenceTransformer
├── cache.py                           chunk-level 抽取缓存（chunk_id → content_hash + llm_config_hash）
├── cli.py                             typer 子命令组：graph build / query / status
├── adapters/
│   ├── chunks_loader.py               chunks.jsonl → LoadedChunk（filter + title_path enrichment）
│   └── llm_adapter.py                 同步 text_completion → LightRAG 期望的 async llm_model_func
└── backends/
    ├── base.py                        GraphBackend ABC + BuildOutcome / QueryOutcome / StatusOutcome
    └── lightrag_backend.py            LightRAG 实现（initialize_storages / ainsert / aquery / finalize）
```

### 10.3 入口三层（和主项目对称）

| 接入面 | 入口 | 何时用 |
|---|---|---|
| Python 库 | `docingest.graph.build / query / status` | 嵌入别的 Python 项目 |
| CLI | `docingest graph build / query / status` | 终端用户 + 脚本（条件注册：lightrag 没装则不出现） |
| MCP | `build_graph` / `query_graph` / `graph_status` 工具 | AI Agent（条件注册） |

### 10.4 三层缓存合奏

GraphRAG 索引贵，缓存设计是关键：

| 层 | 存什么 | 谁负责 | 失效条件 |
|---|---|---|---|
| 主 pipeline 增量 | chunk 文件级 | `incremental.py` | 原文档 / 主 config 变 |
| **graph extraction 缓存** | chunk_id → (content_hash, llm_config_hash) | `graph/cache.py` | chunk 内容 / graph LLM config 变 |
| LightRAG LLM cache | (prompt + model) → response | LightRAG 自己 | prompt 字面量变 |

三层独立、组合生效。一个 chunk 没变 → graph 跳过 ainsert；一个 prompt 重复 → LightRAG 跳过 LLM 调用。

### 10.5 两种 build mode

`graph.mode` 配置决定：

- **`vector_only`**：build 阶段仍跑 LightRAG 的实体 / 关系抽取（LightRAG 没有"只抽实体不抽关系"的开关），但 query 阶段**只允许** `naive` / `local` 两个模式。`global` / `hybrid` / `mix` 会被 `_validate_query_mode` 拒绝。适合"我只想要实体级别的检索强化"
- **`full`**（默认）：query 阶段全模式开放（`naive` / `local` / `global` / `hybrid` / `mix`）。适合需要多跳推理、跨文档对比、主题归纳的场景

**关于社区摘要的现状（重要）**：
LightRAG ≥ 1.4 默认**不再**在 `ainsert` 阶段自动跑 Leiden 社区检测 + 社区摘要——build 完只有 entities + relations + 各自的向量。实测产物里 `kv_store_community_reports.json` 不存在，`Communities = 0` 是预期。

这意味着：

- `vector_only` 和 `full` 的 build 实际开销几乎一样（差别只在 query 时允不允许走 global 路径）
- `global` 模式不会读社区摘要（因为没有），LightRAG 内部退化成"按问题向量找相关 relations 描述 + chunk"——实测对常规问题质量已经够用
- 真要用上社区摘要的优势（"按主题层级归纳"），需要等 LightRAG 加回这个步骤、或者自己在 lightrag_backend 里调 LightRAG 的 `cluster_communities`（如果暴露的话）补一步

### 10.6 三个独立可配置项

- **LLM**（`graph.llm`）：实体 / 关系抽取（以及未来若启用社区摘要时）使用的模型。完全独立于 `models.vision` / `models.chunking_assist`，可以用更小更便宜的模型
- **Embedding**（`graph.embedding`）：向量模型。维度一旦构建后不能换（LightRAG 把 dim 烧进索引），换了要 `--force` 全量重建
- **Backend**（`graph.backend`）：当前只有 `lightrag`，未来可加 `ms_graphrag` / `custom`，通过 `GraphBackend` ABC 即可插

### 10.7 为什么 build 阶段选 LightRAG（v1）

- **原生增量**：`ainsert` 多次调用合并入图（Microsoft GraphRAG 不支持，每次重建）
- **成本**：典型场景下 token 消耗约为 Microsoft GraphRAG 的 1/100
- **零部署**：默认 NetworkX + NanoVectorDB，纯文件存储，符合 DocIngest "文件即数据库"哲学
- **Apache 2.0 许可**：商用友好

未来想接 Microsoft GraphRAG / Neo4j / Memgraph 后端，把 `GraphBackend` 实现一下即可，facade / CLI / MCP 这层零改动。

### 10.8 加新 backend 的步骤

1. 新文件 `backends/<name>_backend.py` 继承 `GraphBackend`，实现 build / query / status
2. 在 `api._create_backend()` 加一行 elif 分发
3. 在 `default.yaml` 加 backend 特有的配置子段（如 `graph.<name>.*`），不要污染 `graph.lightrag.*`
4. 测试 + 更新本表

### 10.9 chunk 实体反哺（`enricher.py` + `chunks_enriched.jsonl`）

**做什么**：把 graph build 抽出来的实体名 + 描述写回到一份**新的** `chunks_enriched.jsonl`，让传统向量 RAG 也能吃到 graph 的红利。**原 `chunks.jsonl` 永不修改**——这是硬约束，单测 `test_chunks_jsonl_never_modified` 用 MD5 守卫。

**为什么有用**：

- **同义词召回**：用户问 "修繕費"，原 chunk 文本里写的是 "原状回復費用"——纯向量检索的 top-K 可能擦边漏掉。注入实体描述后，"原状回復費用 — 退去時の修復に必要な費用" 出现在 chunk 头部，embedding 空间靠近 "修繕費" 的语义。
- **锚点信号**：1000 token 长 chunk 里只有 30 token 真正讲 "敷金"，整体 embedding 被周围话题稀释。`[关键实体: 敷金 — ...]` 头部一行给 embedding 模型一个明确的"这个 chunk 是关于敷金"的信号。
- **元数据过滤**：metadata.entities 字段让向量库可以做 `WHERE entities CONTAINS '敷金'` 这种硬过滤。

**数据流**（纯文件回放，零 LLM 调用）：

```
graph/vdb_entities.json          (entity_name + content + source_id)
graph/kv_store_text_chunks.json  (LightRAG chunk id → 我们的 chunk_id)
                │
                ▼ 反向索引
        { docingest_chunk_id: [_EntityHit(name, desc, exclusive), ...] }
                │
                ▼ top-N 选择 (exclusive 优先 + 短名字优先)
                │
                ▼ 流式重写（atomic .tmp + os.replace）
chunks.jsonl  ──►  chunks_enriched.jsonl
   只读              text 头插入 + metadata.entities 字段
```

**top-N 选择规则**（`enricher._select_top_entities`）：

1. `exclusive=True`（实体的 source_id 只指向当前 chunk）的优先——这是该 chunk 独有的概念，最具定义性
2. 名字短的优先——长名字（如 "賃貸借契約のご解約について20250602"）通常是文档级 boilerplate，信息密度低
3. 字母序兜底——保证幂等

**两通道独立开关**：`inject_into_text` 给纯向量 RAG 受益（embedding 时编进去），`inject_into_metadata` 给混合检索 / metadata filter 受益。

**触发方式三选一**：

- 配置持久化：`graph.enrich_chunks.enabled: true` 让每次 build 自动 enrich
- CLI 一次性：`docingest graph build ./kb/ --enrich-chunks`
- 独立命令（已 build 过想补 enrich）：`docingest graph enrich ./kb/`

**幂等保证**：注入前先剥离上一次的实体行（按 `text_template` 的字面前缀检测），同输入永远产生同输出（除了 `enriched_from` 时间戳）。

**失败容忍**：enricher 不抛异常。graph 缺失 / chunks 缺失 / 写盘失败都记 `EnrichResult.errors`，调用方决定怎么处理（CLI 打 warning，build 流程把错误并进 BuildResult.errors，不让 build 整体被标记为失败）。

**和主流程缓存的关系**：完全无关。`incremental.py` 守护的是 chunks.jsonl 这个输入；enricher 写的是 chunks_enriched.jsonl 这个新输出。改 enrich 配置不会触发主 pipeline 重跑。

---

## 附：文档维护

- 架构 / Phase / 扩展点变更 → 本文档
- 用户指南（装、跑、MCP 配置）→ [README.md](../README.md)
- 集成指南（嵌入到别的系统）→ [INTEGRATION.md](INTEGRATION.md)

改 pipeline.py 或扩展机制时同步更新 Phase 表（§2.2）和 代码地图（§3）。
