# MarkItDown 借鉴点汇总

> 参考项目：[microsoft/markitdown](参考项目/markitdown/)
> 对比对象：DocIngest（本项目）
> 汇总日期：2026-04-12
> 完成日期：2026-04-12
> 状态：**全部完成** — 10 项借鉴点中 8 项已落地、2 项评估后决定不做

---

## 核对原则

每一条都经过两边源码的双向核对：
1. **MarkItDown 源码证据**：确认它真的这么做，以及实现细节（文件:行号）
2. **DocIngest 现状**：确认它真的没做，以及插入点
3. **改造定位**：具体改哪个文件/函数、依赖什么库
4. **风险边界**：哪些坑要考虑

不要把这份文档当成"要做的事"，它是一份**候选清单**。实际做哪些、什么时候做、按什么顺序做，按立项讨论决定。

---

## 总览

| # | 借鉴点 | 状态 | 实现位置 |
|---|---|---|---|
| 1 | PPTX chart 数据直读 | ✅ **已完成** | `hooks/pptx_chart.py` |
| 2 | DOCX OMML → LaTeX 预处理 | ✅ **已完成** | `hooks/docx_omml.py` + `hooks/_docx_math/` |
| 3 | exiftool 元数据提取 | ✅ **已完成** | `hooks/file_metadata.py` |
| 4 | ZIP 递归展开 | ✅ **已完成** | `utils/zip_expander.py` |
| 5 | 音频/视频 → 转写 | ✅ **已完成**（独立重写，未照抄） | `parsers/media_parser.py` + `models/audio_provider.py` + `utils/url_resolver.py` + `chunkers/timestamp.py` |
| 6 | Outlook .msg 解析 | 🔵 未做（需求不急） | — |
| 7 | EPUB 解析 | 🔵 未做（需求不急） | — |
| 8 | magika 文件类型识别 | ✅ **已完成** | `utils/format_detector.py` |
| 9 | defusedxml | ✅ **已完成**（随 #2 引入） | `hooks/_docx_math/omml.py` |
| 10 | PPTX shape 坐标排序 | ✅ **已完成**（随 #1 引入） | `hooks/pptx_chart.py::_iter_shapes_in_reading_order` |
| 11 | 优先级路由 | ❌ **评估后不做** | 当前 1 个 if + hook 机制够用，过度设计 |
| 12 | entry_points 插件系统 | ❌ **评估后不做** | 无外部用户需求，YAGNI |

---

## DocIngest 相对 MarkItDown 的领先点（别动这些）

这些是 DocIngest 已经做得比 MarkItDown 好的地方，作为参考：

1. **Excel 去噪**：MarkItDown 的 xlsx 走 `pd.read_excel → to_html → HtmlConverter`，对布局型 Excel（仕様書类）裸奔；DocIngest 的 dedup_cells / strip_empty_cells / 50% 安全阈值是 MarkItDown 完全没有的。
2. **CJK token 估算**：MarkItDown 根本不切片（无 chunking）。
3. **反幻觉 Vision prompt**：MarkItDown 的 image caption prompt 只有一句 `"Write a detailed caption for this image."`，markitdown-ocr 的也只是 `"Extract all text from this image..."`。DocIngest 的 `[?]` / `[unreadable]` 强制标记 + 三级优先级是质变差距。
4. **增量缓存**：MarkItDown 完全没有。
5. **Knowledge Map + quality_report**：MarkItDown 完全没有这层。
6. **path_injection（chunks 带来源路径）**：MarkItDown 没这个概念。
7. **Docling 的 AI 版面分析 + TableFormer**：对复杂 PDF，Docling 碾压 MarkItDown 的 pdfplumber 路径。

---

# 借鉴点明细

---

## #1 PPTX chart 数据直读 🟢

**价值**：免 Vision、零幻觉、100% 准确

### MarkItDown 源码证据

- [_pptx_converter.py:158-160](参考项目/markitdown/packages/markitdown/src/markitdown/converters/_pptx_converter.py) 检测 `shape.has_chart` 并调用 `_convert_chart_to_markdown`
- [_pptx_converter.py:235-264](参考项目/markitdown/packages/markitdown/src/markitdown/converters/_pptx_converter.py) 核心实现：
  - 从 `chart.plots[0].categories` 读出 category 标签
  - 从 `chart.series` 读出 series 名和数据
  - 组装成 Markdown 表格：`| Category | Series1 | Series2 |`
  - `try/except` 兜 `unsupported plot type`

**关键代码片段**（`_convert_chart_to_markdown`）：

```python
md = "\n\n### Chart"
if chart.has_title:
    md += f": {chart.chart_title.text_frame.text}"
data = [["Category"] + [s.name for s in chart.series]]
for idx, category in enumerate([c.label for c in chart.plots[0].categories]):
    row = [category] + [s.values[idx] for s in chart.series]
    data.append(row)
# → Markdown table
```

### DocIngest 现状

DocIngest **完全没有**从 python-pptx 对象模型读 chart 数据的逻辑。PPT chart 全部走：

```
PPTX → Docling/LibreOffice → PDF → 截图 → Vision 描述
```

Vision 再准也可能把"2024 年 145 亿"看成"145.3 亿"。结构化数据可读时，永远应该结构化提取。

### 改造定位

- **插入点**：`pipeline.py::process_single_file` 增加 **Phase 1.4 `_enrich_pptx_charts`**，在 Phase 1.5 Vision 之前运行；或者更内聚的做法——在 `docling_parser.py::_build_page_data` 里用 python-pptx 打开 file_path 抽 chart 数据写入 metadata
- **依赖**：`python-pptx`（新增 requirement）

### 风险边界

- `unsupported plot type` 必须 try/except 兜底
- chart 要注入到 Markdown 对应的 slide 位置——按 slide 索引匹配 pagebreak 段
- Vision 仍会看到 chart 图片可能重复描述 → 在 Vision prompt 里加一句 "if a chart data table is already present in the pre-extracted text, do not re-describe it"，或代码层把 chart 区域从 Vision 输入剔除（第一个做法更简单）

---

## #2 DOCX OMML 公式 → LaTeX 预处理 🟢

**价值**：公式文档刚需（学术论文、技术 spec）。Docling 对 OMML 的原生处理有限，经常产出 `glyph<c=` 乱码或丢失。

### MarkItDown 源码证据

- 核心目录：[converter_utils/docx/math/](参考项目/markitdown/packages/markitdown/src/markitdown/converter_utils/docx/math/)
  - `omml.py` 400 行（OMML → LaTeX 转换器，从 [xiilei/dwml](https://github.com/xiilei/dwml) 移植）
  - `latex_dict.py` 273 行（OMML 标签 → LaTeX 命令的映射表）
- 预处理入口：[pre_process.py:118-156](参考项目/markitdown/packages/markitdown/src/markitdown/converter_utils/docx/pre_process.py)
  - 用 `zipfile` 解包 DOCX
  - 只对 `word/document.xml` / `word/footnotes.xml` / `word/endnotes.xml` 三个 XML 做 `_pre_process_math`
  - `_pre_process_math` 用 BeautifulSoup 把 `<oMath>` / `<oMathPara>` 替换为 `<w:r><w:t>$...$</w:t></w:r>`
  - 然后重新打包成 BytesIO 交给 mammoth
- `omml.py` 里用 `from defusedxml import ElementTree as ET`（见 #9）

### DocIngest 现状

完全无公式处理。`grep omml|oMath|latex` 零命中。Docling 对 DOCX 的 OMML 支持有限，复杂公式会丢失或退化。

### 改造定位

- **新增**：`src/docingest/parsers/docx_preprocess/` 目录
  - `omml.py`（纯移植）
  - `latex_dict.py`（纯移植）
  - `pre_process.py`（纯移植 + 微调）
- **注入点**：`pipeline.py::process_single_file` 在 Phase 1（Docling parse）之前，如果是 `.docx`，先用预处理生成新的 BytesIO，交给 Docling 解析
- **依赖**：`beautifulsoup4`、`defusedxml`（都是新增）
- **许可证检查**：`dwml` 是 MIT、MarkItDown 是 MIT，合法移植

### 风险边界

- 预处理后生成的是"新 DOCX"（BytesIO），Docling 需要接受 BytesIO 输入——确认 Docling 支持
- 原文件不变，增量缓存 `cache_key` 仍基于原文件；但 `config_hash` 要把"是否启用 OMML 预处理"纳入 `_RELEVANT_CONFIG_PATHS`（否则改配置不会触发重建）
- 代码量 ~700 行（纯移植，无需理解内部逻辑），维护成本集中在 `latex_dict.py` 的完备性上

---

## #3 exiftool 元数据提取 🟢

**价值**：企业场景加分（扫描件、照片证据、会议照片的 EXIF/GPS/作者/时间信息）

### MarkItDown 源码证据

- 核心：[_exiftool.py](参考项目/markitdown/packages/markitdown/src/markitdown/converters/_exiftool.py) 53 行
  - 用 subprocess 调用 `exiftool -json -` 从 stdin 读字节流
  - **CVE-2021-22204 版本检查**（要求 >= 12.24）
  - 未装 exiftool 静默返回 `{}`
- 图像应用：[_image_converter.py:48-66](参考项目/markitdown/packages/markitdown/src/markitdown/converters/_image_converter.py) 白名单 9 字段写入 Markdown：
  ```
  ImageSize, Title, Caption, Description, Keywords,
  Artist, Author, DateTimeOriginal, CreateDate, GPSPosition
  ```
- 音频应用：[_audio_converter.py:55-76](参考项目/markitdown/packages/markitdown/src/markitdown/converters/_audio_converter.py) 音频字段（Title/Artist/Album/...）
- exiftool 路径探测：[_markitdown.py:154-176](参考项目/markitdown/packages/markitdown/src/markitdown/_markitdown.py#L154-L176) `shutil.which` + 已知路径白名单

### DocIngest 现状

完全没有。`grep exif|ExifTool` 零命中。图像/PDF 的元数据只从 Docling 读（只有 `pages` / `has_images` 等结构字段）。

### 改造定位

- **新增**：`src/docingest/enrichment/exif.py`
- **注入点**：`pipeline.py::process_single_file` Phase 1 之后、Phase 2 之前，为图像/音频/PDF 调用；结果写入 `parse_result.metadata["exif"]`
- **渲染**：`markdown_writer.py::_build_frontmatter` 追加 exif 字段序列化进 frontmatter
- **依赖**：系统级的 `exiftool` 可执行文件（不打包进 Python 依赖，走 `shutil.which` 探测 + `DOCINGEST__exiftool_path` 环境变量兜底）
- **配置**：`exiftool.enabled`（默认 false，装了才开）、`exiftool.path`（覆盖探测）、`exiftool.fields`（白名单字段列表）

### 风险边界

- **必须做 CVE 版本检查**（MarkItDown 的做法直接照搬）
- 增量缓存的 meta.json 要存 exif 结果，避免反复 subprocess
- Windows 下 exiftool 安装路径可能不标准，需要环境变量兜底
- 字段白名单走配置而不是硬编码

---

## #4 ZIP 递归展开 🟢

**价值**：常见场景（客户发来 `archive.zip` 里混着 PDF/Excel/图片）

### MarkItDown 源码证据

- [_zip_converter.py](参考项目/markitdown/packages/markitdown/src/markitdown/converters/_zip_converter.py) 116 行
- 核心逻辑：
  ```python
  with zipfile.ZipFile(file_stream, "r") as zipObj:
      for name in zipObj.namelist():
          z_file_stream = io.BytesIO(zipObj.read(name))
          z_file_stream_info = StreamInfo(
              extension=os.path.splitext(name)[1],
              filename=os.path.basename(name),
          )
          result = self._markitdown.convert_stream(...)
  ```
- 异常只吞 `UnsupportedFormatException` 和 `FileConversionException`
- MarkItDown 的做法是**把所有内容拼成一个大 Markdown**，用 `## File: {name}` 作分隔——这点 DocIngest 不应抄

### DocIngest 现状

`zipfile` 只在 `docling_parser.py::_extract_xlsx_images` 里用过，**是用来提取 xlsx 内部的 `xl/media/*` 嵌入图像**，跟处理用户输入的 `.zip` 归档无关。

`discover_files` 遇到 `.zip` 会当成普通文件递交给 Docling，Docling 失败 → TextParser 失败 → 报错。

### 改造定位

- **插入点**：`pipeline.py::discover_files`
  - 识别 `.zip` → 解压到 tempdir → 递归 `discover_files`
  - 或者更干净：pipeline 预处理阶段 expand zip 到一个临时工作目录，整个目录纳入处理
- **不同于 MarkItDown**：DocIngest 是管线，每个文件对应一个 `sources/*.md`，**zip 解包当成一组独立文件处理**，不要合并
- **依赖**：`zipfile`（stdlib）

### 风险边界

- **嵌套 zip**（zip 里还有 zip）→ 递归处理，需要深度限制
- **增量缓存 cache_key 碰撞**：两个 zip 里都有 `report.md`——现有的 `_resolve_output_path` 的 `_1/_2` 后缀机制可以处理 filename 碰撞，但 `compute_cache_key` 的 `md5(head+tail+filename)` 也会碰撞。需要把 zip 路径作为 filename 前缀
- **zip bomb 防护**：`max_extract_size` / `max_extract_files` 上限
- **日文文件名编码**：Windows 创建的 zip 默认 CP437，`zipfile.ZipInfo.filename` 会乱码。MarkItDown 没处理这个，需要自己加 `name.encode('cp437').decode('cp932')` 兜底

---

## #5 音频 → 转写 🟡

**价值**：会议录音场景

### MarkItDown 源码证据

- [_transcribe_audio.py](参考项目/markitdown/packages/markitdown/src/markitdown/converters/_transcribe_audio.py) 50 行
- 依赖 `speech_recognition` + `pydub`（pydub 需要 ffmpeg）
- 走 **Google 免费 Speech API**（`recognizer.recognize_google`）
  - 无需 API key
  - 每日有限额
  - **没传 `language="ja-JP"`**——MarkItDown 的默认英文识别，日语会失败
- 格式：wav/aiff/flac 直接读；mp3/mp4 先 pydub 转 wav 再读
- 入口：[_audio_converter.py](参考项目/markitdown/packages/markitdown/src/markitdown/converters/_audio_converter.py) 识别扩展名和 mimetype

### DocIngest 现状

完全没有音频支持。`grep audio|transcrib|whisper|speech` 只命中 vision.py 里的注释。

### 改造定位 ⚠️ 方向成立，实现不抄

MarkItDown 的实现质量和语言支持都不够——**日本企业场景必须用 Gemini Audio API / Whisper / Azure Speech**。

- **新增**：`src/docingest/parsers/audio_parser.py`
- **走现有 `models.provider` 层**：Gemini 3 Flash 原生支持音频输入，可以直接在 `litellm.completion` 里塞 audio，语言自动检测，质量远超 `recognize_google`
- **不抄**：`_transcribe_audio.py` 本身
- **依赖**：可选 `pydub`（m4a → wav 预处理时用；Gemini 直接吃 m4a 的话这个也免了）
- **可选 fallback**：本地 Whisper（`faster-whisper`）用于离线场景

### 风险边界

- 音频文件大（几十 MB 起步），Vision cache 的 content_hash_file 策略够用
- API 单次调用可能超 token 限制 → 需要分段上传
- 增量缓存 cache_key 的 head+tail 字节足以区分不同录音文件
- 日语会议场景推荐 `gemini-3-flash` 或 `whisper-large-v3`

---

## #6 Outlook .msg 解析 🟡

**价值**：企业邮件归档场景；同时**暴露了 #11 的架构缺口**

### MarkItDown 源码证据

- [_outlook_msg_converter.py](参考项目/markitdown/packages/markitdown/src/markitdown/converters/_outlook_msg_converter.py) 149 行
- 依赖 `olefile`（BSD，~几十 KB，pure Python，无 native 依赖）
- 解析 OLE 复合文档结构，读取 `__properties_version1.0` 流取出 `Subject/From/To/Body` 写成 Markdown
- `accepts()` 三级识别：
  1. 扩展名 `.msg`
  2. magic bytes（`olefile.isOleFile`）
  3. OLE 内部流结构（`__recip_version1.0_#00000000` Outlook 特征）

### DocIngest 现状

零命中。Docling 对 `.msg` 的支持不明，实测大概率不支持。

### 改造定位

- **新增**：`src/docingest/parsers/msg_parser.py` 实现 `BaseParser`
- **架构问题**：目前 `_DoclingWithFallback` 是硬编码的两级 fallback（Docling → Text），**无法插入第三个专用 parser**
  - **方案 A（小改）**：在 `_DoclingWithFallback.parse()` 里硬编码识别 `.msg` 优先路由到 MsgParser
  - **方案 B（大改）**：做 #11 优先级路由重构
- **依赖**：`olefile`

### 风险边界

- 日文邮件编码（ISO-2022-JP、Shift-JIS）需要 olefile 的 encoding 参数处理
- 做 2-3 个这种"专用 parser"后，方案 A 会变得很丑，届时 #11 的价值会自然凸显

---

## #7 EPUB 解析 🔵

**价值**：手册/电子书场景

### MarkItDown 源码证据

- [_epub_converter.py](参考项目/markitdown/packages/markitdown/src/markitdown/converters/_epub_converter.py) 146 行
- 用 `defusedxml.minidom` 解析 `META-INF/container.xml` → `content.opf`
- 抽取 `dc:title/creator/language/publisher/date/description/identifier`
- 按 manifest 里的 HTML 顺序拼接
- 复用 `HtmlConverter`

### DocIngest 现状

零命中。

### 改造定位 ⚠️ 先验证 Docling 能力

**行动项**：先测试 Docling 的 `.epub` 支持：
```python
from docling.document_converter import DocumentConverter
result = DocumentConverter().convert("sample.epub")
```

- 如果 Docling 支持 → **跳过此条**
- 如果不支持 → 新增 `parsers/epub_parser.py`（~100 行，依赖 `defusedxml`，后者已为 #2 引入）

### 风险边界

- 优先级最低——等有实际 EPUB 需求再做
- EPUB 的 HTML 内容如果 Docling 会解析 HtmlConverter，可以复用 DocIngest 现有的 HTML 路径（但 Docling 默认是 HTML→Markdown 直通，需要确认）

---

## #8 magika 文件类型识别 🟡

**价值**：健壮性提升（扩展名错标、无扩展名文件、流式数据）

### MarkItDown 源码证据

- [_markitdown.py:120](参考项目/markitdown/packages/markitdown/src/markitdown/_markitdown.py#L120) `self._magika = magika.Magika()`
- [_markitdown.py:698-772](参考项目/markitdown/packages/markitdown/src/markitdown/_markitdown.py#L698-L772) `_get_stream_info_guesses`：
  - 调用 `magika.identify_stream`
  - 配合 `charset_normalizer.from_bytes` 做字符集检测
  - 对"扩展名/mimetype/字节内容"做**交叉验证**
  - **不兼容时返回两个 StreamInfo**，让核心 `_convert` 依次尝试
- 精巧之处：**不直接相信扩展名，但也不直接用 magika 覆盖扩展名**，而是两个都当作候选

### DocIngest 现状

完全靠 `file_path.suffix.lower()`。`pipeline.py::process_single_file` 的 `result.format = parse_result.metadata.get("format")`，而 `metadata["format"]` 来自 `docling_parser.py::_extract_metadata` 的 `file_path.suffix.lstrip(".").lower()`。扩展名错标就会进错分支。

**最典型的 bug 场景**：`.xls` 实际是 `.xlsx`，会进 `_xls` 的 denoising 路径（目前没有区分，但未来如果有区分就会错）。

### 改造定位

- **插入点**：`pipeline.py::discover_files` 后 或 `process_single_file` 开头增加 `_detect_true_format(file_path) -> str`
- **依赖**：`magika`（Apache-2.0，~25MB 模型文件）
- **作为可选依赖**：装了就用、没装就 fallback 到扩展名
- **应用范围**：至少让 Excel 去噪路径基于真实类型触发

### 风险边界

- 25MB 模型文件是个包体积成本
- 和增量缓存交互：真实格式**不用进 config_hash**，放 meta.json 作为参考信息即可
- 和 Docling 的冲突：Docling 也有内部格式识别，两个系统并存时谁优先？建议 **magika 只用来修正 DocIngest 自己的分支决策（比如 Excel 去噪路径），Docling 内部解析仍然让它自己判断**

---

## #9 defusedxml 替代 xml.etree 🔵

**价值**：防 XXE 攻击（XML External Entity 注入）

### MarkItDown 源码证据

- [omml.py:9](参考项目/markitdown/packages/markitdown/src/markitdown/converter_utils/docx/math/omml.py#L9) `from defusedxml import ElementTree as ET`
- [_epub_converter.py:3](参考项目/markitdown/packages/markitdown/src/markitdown/converters/_epub_converter.py#L3) `from defusedxml import minidom`

### DocIngest 现状

`grep xml.etree|xml.dom|defusedxml|ElementTree` **零命中**。DocIngest 目前根本没直接用 `xml.etree`，所有 XML 解析都委托给 Docling 内部或其他库。

### 改造定位 ⚠️ 独立价值低

- **不需要主动"替换"**——没有要替换的东西
- **作为 #2 / #4 / #7 的硬约束**：引入的新代码必须用 defusedxml
- **独立立项价值：极低**

---

## #10 PPTX shape 坐标排序 🟢

**价值**：修复阅读顺序（python-pptx 默认按 XML 顺序，不是视觉顺序）

### MarkItDown 源码证据

- [_pptx_converter.py:181-189](参考项目/markitdown/packages/markitdown/src/markitdown/converters/_pptx_converter.py#L181-L189)：
  ```python
  sorted_shapes = sorted(
      slide.shapes,
      key=lambda x: (
          float("-inf") if not x.top else x.top,
          float("-inf") if not x.left else x.left,
      ),
  )
  ```
- group shape 内部同样做了排序

### DocIngest 现状

DocIngest 走 Docling 解析 PPT，Docling 内部**应该**按阅读顺序输出（有 AI 版面分析），但这一条是否成立取决于 Docling 对 PPT 的实际行为，**需要实测**。

### 改造定位 ⚠️ 与 #1 绑定

- 只有在实现 **#1 PPTX chart 直读**时已经开始用 python-pptx，顺手把 shape 坐标排序做了
- **不单独立项**

---

## #11 优先级路由（accepts/convert 分离） 🟡

**价值**：条件性——取决于要加多少"非 Docling"的 parser

### MarkItDown 源码证据

- [_base_converter.py:42-106](参考项目/markitdown/packages/markitdown/src/markitdown/_base_converter.py#L42-L106) `DocumentConverter.accepts()` + `convert()` 分离
- [_markitdown.py:85-91](参考项目/markitdown/packages/markitdown/src/markitdown/_markitdown.py#L85-L91) `ConverterRegistration(converter, priority)` 数据类
- [_markitdown.py:549](参考项目/markitdown/packages/markitdown/src/markitdown/_markitdown.py#L549) 每次 convert 都 `sorted_registrations = sorted(self._converters, key=lambda x: x.priority)`——**稳定排序**是关键
- [_markitdown.py:641-671](参考项目/markitdown/packages/markitdown/src/markitdown/_markitdown.py#L641-L671) `register_converter` 用 `self._converters.insert(0, ...)` 插到头部，稳定排序下后注册的同优先级优先

### DocIngest 现状

`_DoclingWithFallback` 是硬编码的两级 fallback（Docling → TextParser）：

```python
def parse(self, file_path: Path) -> ParseResult:
    result = self._docling.parse(file_path)
    if result.success:
        return result
    fallback_result = self._fallback.parse(file_path)
    ...
```

没有 registry、没有 priority、没有 `accepts()`。

### 改造定位 ✅ 成立但条件性

- **如果只加 1-2 个特殊 parser**（如 .msg）→ 可以通过 `_DoclingWithFallback` 前面加硬编码分支解决，**不需要重构**
- **如果要加 4+ 个**（.msg + .epub + audio + magika 前置 + xlsx 专用）→ 硬编码会很丑，这时引入 registry 才划算
- **决策点**：看打算做多少个新 parser

### 风险边界

- 中等重构：影响 `pipeline.py`、`parsers/__init__.py`
- `compute_config_hash` 的 `_RELEVANT_CONFIG_PATHS` 需要把 parser 路由配置纳入
- 和 `最小侵入性原则` 有冲突，需要权衡

---

## #12 entry_points 插件系统 🔵

**价值**：长期价值高、短期价值低

### MarkItDown 源码证据

- [_markitdown.py:65-82](参考项目/markitdown/packages/markitdown/src/markitdown/_markitdown.py#L65-L82) `_load_plugins` 用 `importlib.metadata.entry_points(group="markitdown.plugin")`
- 插件侧：[markitdown-sample-plugin/pyproject.toml](参考项目/markitdown/packages/markitdown-sample-plugin/pyproject.toml)
  ```toml
  [project.entry-points."markitdown.plugin"]
  sample_plugin = "markitdown_sample_plugin"
  ```
- 插件只需暴露 `register_converters(markitdown, **kwargs)` 函数
- `markitdown-ocr` 就是这么实现的：priority=-1.0 用来"替换"内置 converter

### DocIngest 现状

零命中。完全没有插件机制。

### 改造定位 ✅ 但短期价值低

- **需要 #11 作为前提**
- 只有当"DocIngest 被多个项目使用、有人想加自己的 parser/chunker 但不改主仓"的场景下才有价值
- **短期内单项目使用，不做**
- 未来如果 DocIngest 变成团队共享工具或对外开源，再考虑

---

# 核对过程中的修正和发现

1. **#9 defusedxml** 的独立价值比初估更低——DocIngest 目前根本没直接用 `xml.etree`，"替换"无从谈起。降级为"引入 #2/#4/#7 时的硬约束"，不单独立项。

2. **#6 .msg 暴露了 #11 的架构缺口**：想加任何"Docling 不处理但需要专门 parser"的格式（`.msg` / audio / 定制专用），当前两级 fallback 就会不够用。做完 2-3 个新 parser 后，#11 会自然变得划算。

3. **#10 PPTX shape 坐标排序**不能独立做——只有实现 #1 时顺手加。

4. **#5 音频**必须重写实现路径，不能照抄 MarkItDown（`recognize_google` 不支持日语）。用现有的 `models.provider` 层（Gemini Audio API）才对。

5. **#3 exiftool** 的 CVE-2021-22204 版本检查是个重要细节，移植时不能漏。

6. **#4 ZIP** 要留心日文文件名编码（Windows 创建的 zip 用 CP437）和 zip bomb 防护，MarkItDown 没处理这俩。

---

# 实际执行记录

**第一批：加法 + 质量硬提升** ✅ 2026-04-12 完成
- #1 PPTX chart 数据直读（附带 #10 shape 坐标排序）
- #2 DOCX OMML → LaTeX（附带 #9 defusedxml）
- #3 exiftool 元数据提取 + Docling origin 促进

**补强：策略 C（Vision + 结构化数据互补）** ✅ 2026-04-12 完成
- Vision prompt 加 `{structured_data}` 占位符
- chart hook 写入 `structured_extractions_per_page`，Vision 不重复转录
- `_FRONTMATTER_FIELDS` 从硬编码改为 config 驱动

**补强：外部二进制查找** ✅ 2026-04-12 完成
- `utils/binary_finder.py`：三平台路径扫描（soffice / exiftool / ffmpeg / ffprobe / yt-dlp）
- 替换所有 `shutil.which` 调用

**第二批：输入扩展** ✅ 2026-04-12 完成
- #4 ZIP 递归展开（日文编码恢复 + bomb 防护 + 嵌套递归）
- #8 magika 文件类型识别（弱扩展名修正）

**第三批：音频/视频 parser** ✅ 2026-04-12 完成
- #5 音频/视频 → 转写（独立重写，未照抄 MarkItDown）
  - 字幕优先（SRT/VTT/ASS，零 API 成本）
  - ASR：DashScope Qwen3-ASR-Flash（默认） + OpenAI Whisper（fallback）
  - URL：yt-dlp 1000+ 站点支持（YouTube/B站/抖音/...）
  - 长音频自动分段（ffmpeg 150s 分段 + 并行 ASR）
  - TimestampChunker（按 `[MM:SS]` 标记切 chunk）
- #6 .msg / #7 EPUB：未做（需求不急，等实际场景驱动）

**第四批：架构** ❌ 评估后不做
- #11 优先级路由：当前 1 个 `if` + hook 机制足够，无过度设计必要
- #12 entry_points 插件：无外部用户需求，YAGNI

---

# 最终架构概览

```
用户输入 (file / dir / URL / zip)
    │
    discover_files()
    ├─ URL → url_resolver (yt-dlp) → local files
    ├─ ZIP → zip_expander → extracted files
    └─ normal files
    │
    pre-parse hooks
    ├─ .docx → OMML → LaTeX preprocessing
    └─ others → pass through
    │
    parser routing
    ├─ audio/video → MediaParser (subtitle-first + ASR)
    ├─ documents → Docling (15+ formats)
    └─ plain text → TextParser (fallback)
    │
    post-parse hooks
    ├─ .pptx → chart data injection (structured_extractions_per_page)
    └─ * → file metadata (Docling origin + exiftool)
    │
    Vision enrichment (structured_data aware)
    │
    pre-write hooks
    └─ * → metadata finalization
    │
    output: sources/*.md + chunks.jsonl + index.json
```

# 新增依赖汇总

| 包 | 类型 | 用途 |
|---|---|---|
| `python-pptx>=1.0` | 必选 | PPTX chart 直读 |
| `beautifulsoup4>=4.12` | 必选 | DOCX OMML 预处理 |
| `defusedxml>=0.7` | 必选 | XML 安全解析 |
| `dashscope` | 必选 | Qwen3-ASR（默认 ASR 引擎） |
| `magika>=1.0` | 可选 | 内容格式识别 |
| `pyexiftool>=0.5` | 可选 | EXIF 元数据 |

# 新增文件汇总

```
src/docingest/
├── hooks/                          ← 第一批新增
│   ├── __init__.py                 (hook 注册表 + 运行器)
│   ├── docx_omml.py               (#2 OMML → LaTeX)
│   ├── pptx_chart.py              (#1 chart 直读 + #10 shape 排序)
│   ├── file_metadata.py           (#3 Docling origin + exiftool)
│   └── _docx_math/                (MarkItDown 移植, MIT)
│       ├── omml.py
│       └── latex_dict.py
├── utils/                          ← 第二批新增
│   ├── binary_finder.py           (三平台外部工具查找)
│   ├── zip_expander.py            (#4 ZIP 展开)
│   ├── format_detector.py         (#8 magika 封装)
│   └── url_resolver.py            (#5 yt-dlp URL 下载)
├── parsers/
│   └── media_parser.py            (#5 音频/视频 parser)
├── models/
│   └── audio_provider.py          (#5 ASR 引擎抽象)
└── chunkers/
    └── timestamp.py               (#5 时间戳切片)
```
