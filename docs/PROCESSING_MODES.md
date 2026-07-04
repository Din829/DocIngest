# DocIngest 处理档位（Processing Modes）

> 单一真相源：三个面向场景的档位（`fast` / `balanced` / `best`）如何按**文件类型**展开成底层旋钮。
> 用户/agent 只选档位，DocIngest 按格式自动选最优路径——不必手调 engine / triage / parallel / 抠图 等旋钮。
>
> 这份文档既是用户指南，也是未来 `--mode` CLI 入口的实现规格（每档 → config 覆盖集）。

---

## 为什么是三档，而不是一堆旋钮

DocIngest 的处理质量/成本由多个正交旋钮决定（解析引擎、要不要逐页跳过 Vision、并发数、要不要抠图送 Vision、表格批处理…）。手调这些旋钮门槛高、易错。**三档把它们收成面向场景的预设**：

| 档位 | 一句话 | 选它当… |
|---|---|---|
| **`fast`** | 快，牺牲**可界定**的精度换速度 | 大批量初筛、只要正文大意、赶时间 |
| **`balanced`**（默认） | 成本与质量平衡，日常首选 | 绝大多数场景。不传 `--mode` 就是它 |
| **`best`** | 质量优先，不在乎多花钱 | 合同/规格书/关键文档，**绝不漏一个字** |

> `balanced` 就是 DocIngest 当前的默认行为——所以这套档位**完全向后兼容**：不选档 = `balanced` = 和以前一样。

---

## 核心规律：同一个档，不同文件走不同的路（实测拍板）

最重要的设计点：**档位必须按文件类型分别落地**，因为各格式的最优路径不同。最典型的是 `fast` 档：

- **PDF 的 `fast` = `vision_only`**（跳过 Docling 解析，PyMuPDF 直接渲染整页图给 Vision）——**真省时间**，且完全不经过 docling-parse。
- **PPTX / DOCX / XLSX 的 `fast` ≠ `vision_only`**——它们的页图**必须经 LibreOffice→PDF→截图**（这步 3–7 秒躲不掉），而 Docling 解析本身才 2.5 秒还白送高质量正文文字。跳过 Docling 省的那点时间，远抵不上丢掉的正文质量。所以它们的 `fast` = **Docling + 高并发 + 激进 triage + 关抠图**，靠并发拉满和少送页省，**不换引擎**。

> **一句话**：`vision_only` 的"省时间"卖点**只对 PDF 成立**（PDF 是已排好版的格式，PyMuPDF 渲染极快）。Office 格式卡在 LibreOffice 这个躲不掉的慢步骤上，`fast` 对它们靠的是"少送 Vision"，不是"换引擎"。

（数据：前桥 PPT 实测——Docling 解析 2.5s vs LibreOffice+渲染 8.2s。详见 [三模式设计_测试观察.md](../三模式设计_测试观察.md) 的"PPT 处理耗时拆解"。）

---

## 档位 × 文件类型 映射表

每格列出该档对该格式的旋钮组合。`balanced` 列即当前 config 默认值。

### PDF

| 旋钮 | `fast` | `balanced` | `best` |
|---|---|---|---|
| 解析引擎 | **`vision_only`** | `docling` | `docling` |
| 逐页 triage | 跳过（vision_only 整页都读） | 开 | **全关**（每页都送 Vision） |
| Vision 并发 | **64** | 16 | 16 |
| 抠图送 Vision | 不适用（vision_only 无解析、无抠图） | — | — |
| 取舍 | 跳 Docling，OOM 免疫，最快；丢文字层精度 | 解析准 + 正文兜底 | 零漏页，token 翻倍换"绝不漏" |

### PPTX

| 旋钮 | `fast` | `balanced` | `best` |
|---|---|---|---|
| 解析引擎 | `docling`（**不**换 vision_only） | `docling` | `docling` |
| 逐页 triage | **激进**（多跳纯文字页） | 开 | **全关** |
| Vision 并发 | **64** | 16 | 16 |
| 抠图送 Vision | **关**（省大头） | 开 | 开 |
| 取舍 | 保留 Docling 正文，并发冲 Vision，少送页省钱；提升**有限**（LibreOffice 躲不掉） | 质量地基，治含字图抖动 | 同 balanced + triage 全关 |

### DOCX

| 旋钮 | `fast` | `balanced` | `best` |
|---|---|---|---|
| 解析引擎 | `docling` | `docling` | `docling` |
| 逐页 triage | **激进** + `skip_text_only` | 开 | **全关** |
| Vision 并发 | **64** | 16 | 16 |
| 抠图送 Vision | **关** | 开 | 开 |
| 取舍 | 同 PPT 思路（DOCX 也有 LibreOffice 开销）；**未单独实测，按 PPT 结论类推** | 质量地基 | 绝不漏 |

### XLSX

| 旋钮 | `fast` | `balanced` | `best` |
|---|---|---|---|
| 解析引擎 | `docling`（openpyxl 渲染器） | `docling`（openpyxl） | `docling`（openpyxl） |
| sheet triage | **激进** | 开 | **全关** |
| Vision 并发 | **64** | 16 | 16 |
| 抠图送 Vision | **关** | 开 | 开 |
| supplement / batched | supplement + batched | supplement + batched | supplement + **batched 关**（逐 sheet） |
| 取舍 | 少送 Vision 省钱；**未单独实测，按 PPT 结论类推** | openpyxl 兜底表格文字，Vision 只补视觉；batched 对规整数据表读得和 per-page 一样好 | batched 关：实测 batch 会**漏图表类视觉**（月別売上等 per-page 能补、batch 丢），best 宁可逐 sheet 慢一点也读全 |

> **诚实标注**：
> - **DOCX / XLSX 的 `fast` 路径按 PPT 结论类推先定，未单独实测**——因为它们和 PPT 一样卡在 LibreOffice 同样的开销上，大概率同结论。后续如实测有出入，以实测为准。
> - **PPT/DOCX/XLSX 的 `fast` 提升有限**：瓶颈是 LibreOffice→PDF（躲不掉）和 Vision 次数，不是 Docling 解析。`fast` 只能靠并发 + 少送页省，不像 PDF 那样能跳掉整个解析步骤。

---

## 其它格式（无页图概念）

| 格式 | 三档行为 |
|---|---|
| 图片（png/jpg/…） | 同 PDF：`fast` = vision_only 直读；其余整页 Vision |
| HTML / Markdown / CSV / 文本 | 无 Vision，三档基本无差异（纯解析） |
| 音频 / 视频 | 走 ASR / 原生视频理解，三档影响的是并发与 triage，引擎不变 |

---

## `--mode` 入口实现规格（给下一轮代码）

`--mode <fast|balanced|best>` 不是单个旋钮，而是**展开成一组 config 覆盖**，且覆盖值**按文件格式条件分发**。落地要点：

1. **`balanced` = 不覆盖任何东西**（等同当前默认），保证向后兼容。
2. **`fast` / `best` = 一组 `config_overrides`**，其中 engine 等"按格式分流"的旋钮需要在 per-file 处理时按 `doc_format` 取对应值（不能全局一刀切，否则 PPT 会错误地走 vision_only）。
3. CLI 只暴露 `--mode`；底层旋钮（triage/vision_enrich/parallel…）**不单独暴露**——用户选场景，不碰旋钮。高级用户仍可用 `-c config.yaml` 精调。
4. 与现有 `--purpose`（控制产出哪些文件）正交：`--mode` 管"处理多深/多快"，`--purpose` 管"产出哪些文件"，可叠加。

> 旋钮的真实默认值与控制点见 `config/default.yaml`（单一真相源）：
> `parsing.engine` · `parsing.vision.triage.*` · `performance.parallel_files` ·
> `parsing.<fmt>.image_extraction.vision_enrich` · `parsing.xlsx.vision.supplement_only` ·
> `parsing.vision.batched_call.enabled`。

---

## 速查（agent 用）

- 不确定 → `balanced`（默认，不用传）。
- 大批量初筛 / 只要正文大意 → `fast`（PDF 提速明显，Office 提速有限）。
- 合同 / 规格书 / 关键文档，绝不漏字 → `best`（成本翻倍，质量封顶）。
- `fast` 对 **PDF** 收益最大（vision_only）；对 **Office** 收益有限（卡 LibreOffice）。
