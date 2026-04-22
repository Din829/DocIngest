# DocIngest 架构与扩展指南

一份文档讲清楚：项目怎么跑 + 我要改/加该怎么做。给接手开发者和维护 Agent 用。

用户文档见 [README.md](README.md)；历史借鉴记录见 [MARKITDOWN_BORROW.md](MARKITDOWN_BORROW.md)。

---

## 1. 项目定位与设计原则

### 1.1 定位

通用文档前处理引擎。任意输入（PDF / Office / HTML / 图像 / 音视频 / ZIP / URL）→ Markdown + 分片 + 索引，两个下游共用同一份输出：

- **RAG**：`chunks.jsonl` 做向量检索
- **Agentic Search**：`sources/*.md` 做 grep / glob

核心思想：**Markdown 作为唯一中间格式**。不自己搞检索，不管 embedding，不做 UI。

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
| Embedding / 向量索引 | 下游 RAG 职责 |
| 语义搜索 | 前处理工具，不做检索 |
| Late Chunking / 多粒度索引 | 依赖 embedding 模型，下游职责 |
| Web 爬虫 | 只处理本地文件 + 明确的 URL |
| 实时监控 | 批处理工具 |
| GUI | CLI + MCP server 够了 |

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

`process_single_file` 的完整调用链（按执行顺序）。**加新 Phase / 改流程的主战场**。

| Phase | 做什么 | 实际入口 | 位置 |
|---|---|---|---|
| **1.0 pre_parse hook** | 改写原文件内容（如 DOCX OMML → LaTeX） | `run_pre_parse_hooks()` | pipeline.py:1349 |
| **1 Parse** | Docling / Media / Text 解析为 markdown + pages | `parser.parse()` | pipeline.py:1354 |
| **1.1 Garbled fallback** | 检测 `glyph<` 乱码 → pymupdf 重抽文本 | `_detect_garbled` + `_pymupdf_fallback` | pipeline.py:1376 |
| **1.2 Excel denoise** | xlsx/xls/csv 行内去重 + 空格剥离 | `_clean_excel_markdown` | pipeline.py:1384 |
| **1.2.5 通用表格去噪** | 非 Excel 格式的合并单元格去重 | `_denoise_markdown_table_rows` | pipeline.py:1400 |
| **1.3 页图生成** | xlsx/docx/pptx → LibreOffice → PDF → 截图 | `_ensure_{excel,docx,pptx}_page_images` | pipeline.py:1409 |
| **1.4 post_parse hook** | 注入结构化数据给 Vision（如 PPTX chart 直读） | `run_post_parse_hooks(phase="post_parse")` | pipeline.py:1420 |
| **1.4.5 语言检测** | CJK 字符分布 → 填 `metadata["language"]` | `_detect_language` | pipeline.py:1428 |
| **1.5 Vision 增强** | 逐页调 Vision（8 层 triage + 并发缓存） | `_enrich_with_vision` | pipeline.py:1432 |
| **1.6 pre_write hook** | 写盘前最后处理（exiftool、PII sanitize） | `run_post_parse_hooks(phase="pre_write")` | pipeline.py:1439 |
| **1.7 Vision dedup** | 去掉 Docling 和 Vision 重复段落 | `_dedup_vision` | pipeline.py:1445 |
| **2 Write** | sources/*.md + YAML frontmatter + assets/ | `write_markdown` | pipeline.py:1448 |
| **3 Chunk** | 按策略切分 + 保护块 + 片段合并 + 路径注入 | `chunker.chunk()` + `_postprocess_chunks` + `inject_paths` | pipeline.py:1472+ |

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
│  ├─ cli.py                         typer CLI：run / inspect / refine / doctor
│  ├─ mcp_server.py                  MCP server（6 个工具给 Agent）
│  ├─ config.py                      YAML 加载 + 环境变量 + 模型 defaults 注入
│  ├─ pipeline.py                    主编排（run_pipeline + process_single_file + Phase 实现）
│  ├─ inspect.py                     docingest inspect 预检
│  ├─ doctor.py                      docingest doctor 环境体检
│  ├─ refine.py                      docingest refine 独立命令
│  ├─ incremental.py                 增量缓存（cache_key / config_hash / meta.json）
│  ├─ parsers/
│  │  ├─ base.py                     BaseParser + ParseResult + PageData + PAGEBREAK_MARKER
│  │  ├─ __init__.py                 create_parser + _DoclingWithFallback 路由
│  │  ├─ docling_parser.py
│  │  ├─ media_parser.py             音视频：subtitle-first + ASR fallback
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
│  └─ output/
│     ├─ markdown_writer.py          sources/*.md + frontmatter
│     ├─ index_builder.py            index.json
│     ├─ chunks_writer.py            chunks.jsonl + chunk_id 生成
│     ├─ knowledge_map.py            Phase 4 + AI summary
│     ├─ keyword_extractor.py        Sudachi / regex 双后端
│     └─ quality_report.py           Vision marker 扫描
└─ tests/
```

### 3.2 代码导航速查

| 想看什么 | 去哪儿 |
|---|---|
| 整体流程骨架 | `pipeline.py` `run_pipeline` + `process_single_file` |
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

1. **永不 raise**：hook 抛异常只会被 `logger.warning` 吞掉（生产环境 log level 下可能完全看不到）。要么自己 try/except 并返回 None，要么只在调试时临时 raise
2. **从 config 读自己的开关**：惯例路径 `config["hooks"][<name>][<key>]` 或 `parsing.<format>.<feature>`
3. **按扩展名派发**：`_register_post("pre_write", ["docx", "doc"], my_hook)` 或 `["*"]` 匹配全部
4. **pre_parse 的 BytesIO 约定**：返回非 None 会**替代**原文件内容喂给 Docling。多个 pre_parse hook 都返回 BytesIO 时，**第一个非 None 赢**
5. **别 mutate `file_path` 指向的文件**：原文件永远只读

#### 调试技巧

hook 被吞异常是最常见的坑。调试时把 `run_pre_parse_hooks` / `run_post_parse_hooks`（[hooks/__init__.py:109-120, 142-149](src/docingest/hooks/__init__.py)）里的 `except Exception` 临时改成 `raise`，或把 `logger.warning` 升级为 `logger.exception`（会打印 traceback）。

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

`Media parser` ([parsers/media_parser.py](src/docingest/parsers/media_parser.py)) 是个好参考：`accepts()` 方法 / subtitle-first 策略 / ASR fallback / 长音频自动分段 + 并发。

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

#### 并发 + 缓存

- `ThreadPoolExecutor` 并发调用，worker 数读 `performance.parallel_files`（**存在语义冲突，见 §9.1**）
- 每次调用按**图片内容哈希 + structured_data hash** 做 cache key，同页同内容重跑零成本

### 5.2 Excel 去噪策略

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

### 5.4 Docling-Vision 去重

Vision 的 prompt 里**送了 Docling 文本作为参考**，所以 Vision 输出往往是 Docling 的超集。两者并存会让 `sources/*.md` 内容重复、token 翻倍。

去重策略（`pipeline.py::_dedup_vision`）：
- 逐页（按 `<!-- pagebreak -->` 切分）判断是否有 `<!-- vision-enriched -->` 区块
- Vision 长度 ≥ Docling 长度 × `vision_ratio_threshold`（默认 0.7）→ 只保留 Vision
- 否则保留两者（Vision 可能漏了内容）

**默认开启**。关闭后 `sources/*.md` 会同时包含 Docling 原文和 Vision 加强版。

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
    retry_on_truncation: true
    retry_max_tokens: 65536

  vision:
    max_response_tokens: 32768
    primary: { provider: "google", model: "...", api_key_env: "GEMINI_API_KEY" }
    fallback: { provider: "openai", model: "...", api_key_env: "OPENAI_API_KEY" }

  chunking_assist: { ... }
  audio_transcription: { ... }
```

Primary 失败自动切 fallback。所有 provider 走 litellm（DashScope 原生 SDK，因为 `qwen3-asr-flash` 的 base64 multimodal litellm 不原生支持）。

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
- 需要并发/异步，参考 `_enrich_with_vision`（[pipeline.py:962](src/docingest/pipeline.py)）的 ThreadPoolExecutor pattern
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

### 9.1 文件级并行未实装

`performance.parallel_files` 配置**实际只被** Vision 页内并发 + ASR 分段并发使用。`run_pipeline` 对 `to_process` 列表是**串行**处理。真正的文件级并行还未实装。

### 9.2 `vision.parallel_calls` 幽灵配置

`config/default.yaml` 的注释里提到过 `parsing.vision.parallel_calls`，但这个键从未真正存在。实际读的是 `performance.parallel_files`。

### 9.3 `_RELEVANT_CONFIG_PATHS` 不完整

当前漏了以下会影响输出的配置（改这些不会触发 cache 失效，需修复）：
- `sanitize.enabled` / `sanitize.rules`
- `output.dedup.enabled` / `output.dedup.vision_ratio_threshold`
- `parsing.vision.max_pages`

### 9.4 Refine 不重试截断

`knowledge_map.py::enrich_with_ai` 实现了 `retry_on_truncation`，`refine.py` 没有——截断时只 warning + 加 marker。两个路径的策略应该统一（抽到 `text_completion` 内部）。

### 9.5 Parser / Chunker 路由未注册中心化

加新 Parser / Chunker 需要改 `parsers/__init__.py` / `chunkers/__init__.py` 的 `create_*()` 工厂和 `_DoclingWithFallback` / `AutoChunker` 的 if-elif。不是插件化注册模式。

### 9.6 DOCX 无页图时的 Vision 注入降级

DOCX 经 LibreOffice 渲染后，Docling 的 text 结构和 PDF 页 index **不对应**（DOCX 文本是流式的，没有原生页概念）。此时 Vision 结果不能按页对齐注入，退化为在文档末尾 append 所有 Vision 结果（Mode B，`pipeline.py::_enrich_with_vision` 注释详述）。

### 9.7 `_has_unexpected_scripts` 回退偏严

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

## 附：文档维护

- 架构 / Phase / 扩展点变更 → 本文档
- 用户指南（装、跑、MCP 配置）→ [README.md](README.md)
- 借鉴历史 → [MARKITDOWN_BORROW.md](MARKITDOWN_BORROW.md)

改 pipeline.py 或扩展机制时同步更新 Phase 表（§2.2）和 代码地图（§3）。
