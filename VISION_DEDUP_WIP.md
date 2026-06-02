# Vision 去重（supplement 模式）调查 WIP

> 进度交接文档。课题：sources/*.md 里 Docling 抽的文本 和 Vision 抽的内容
> 经常重复两份，想做到「精准去重、不误删正文」。已实测验证 + 改了代码，
> 但发现一个未解的边界问题，**当前卡在根因调查**。

---

## 一、课题与背景

- DocIngest 处理文档时，每页（被 Vision 读过的段）在 `sources/*.md` 里可能有两份：
  Docling 抽的文本 + `<!-- vision-enriched -->` 后的 Vision 输出。
- 现状默认 `output.dedup.enabled: false`（保两份，不丢内容但冗余）；开了去重是
  「长度比硬删」会误删——所以一直保两份。
- 用户问：能不能 100% 精准只留对的一份？

## 二、已做的实测（结论可信，真跑非 mock）

1. **重叠量化**（46 个 vision-enriched 段）：只 **7% 真重复**（≥60%重叠）、
   65% 其实**互补**（Docling 抓文字/表格、Vision 抓图里的字，各管各的）。
   → "一定重复"是误解；真重复是少数图文混排正文页。

2. **A/B prompt 测试**：
   - 现状 prompt（`_PAGE_PROMPT`）让 Vision **输出整页** → 重抄 Docling 已有文字 = 重复根源。
   - 改「只补充」prompt（`_PAGE_PROMPT_SUPPLEMENT`）→ 真重复页完美去重（0 重抄）、
     纯重复表格页正确输出 `(no additional visual content)`。
   - 弱版会漏图表；**B2 强化版**（强调"视觉内容必须抓全、纯文字才跳过"）→ 图表/表格全抓。

3. **端到端真跑**（含图 PDF `0253331`）：5 个 vision-enriched 段重叠从 67-92% 降到 **0-2%**，
   空块零泄漏，图表内容都在。**看似成功。**

4. **误删检查**（full vs supplement 对比）→ **发现真问题**：
   合同首页 `賃貸借契約期間/物件所在地` 等条款，full 模式 Vision 抓到、
   **supplement 模式漏了**。

## 三、当前卡住的根因调查（未完成 ← 接手从这继续）

定位到丢正文的页：`0253331` 合同首页（sec#0）。深挖发现：
- 这页 Docling **抓到了 1816 字符，但是碎片**（`円`/`合計`/`駐車場`/`支払手数料`
  等单字、零散数字散落），没抓到完整的合同条款表格结构。
- supplement 模式下 Vision 看到 page_text 非空 → 以为"正文 Docling 有了"，
  **只补了视觉装饰**（Logo/印章/页脚），**漏了合同条款表格正文**。

**核心难点**：不是"Docling 空 vs 非空"的二元判断（那个我已加 prompt 规则修了），
而是 **Docling 抓了"碎片/不完整"时，Vision 难判断"哪些是 Docling 真漏的"** → 误判跳过。

**用户最后的疑问（调查方向，未做）**：
> Docling 默认不开 OCR（`parsing.ocr.enabled: false`），那这页"碎片化文字"
> （円/合計 单字散落）是哪来的？不是 OCR 的话是什么？

待查假设：
1. 这页 PDF 本身有**文本层**（不是纯扫描图），Docling 抽了文本层但布局/顺序乱 → 碎片？
2. 是 Phase 1.1 的 **garbled fallback（pymupdf 重抽）** 产物？（grep `_pymupdf_fallback`）
3. "这页是扫描件"的判断本身可能不准——要先确认这页 PDF 的真实性质。

**下一步具体动作**（被打断前正要做）：
用 pymupdf 直接读这页 PDF 的原始文本层，看 Docling 碎片是文本层来的还是别的，
从而判断 supplement 漏正文的真正机制。

```bash
# 待跑：检查这页 PDF 有无文本层
python -c "import pymupdf; d=pymupdf.open('test_docs/2/0253331_*.pdf'); print(repr(d[0].get_text()[:500]))"
```

## 四、已改的代码（都还在，未回退）

| 文件 | 改动 | 状态 |
|---|---|---|
| `config/default.yaml` | 加 `parsing.vision.supplement_only: true`（默认开） | 已加 |
| `src/docingest/parsers/vision.py` | 加 `_PAGE_PROMPT_SUPPLEMENT`（只补充版 prompt，已含"Docling空则全转写"强化规则）；`describe_page` 加 `supplement_only` 参数选 prompt；`describe_page_cached` 从 config 读 + cache key 加 `supplement\|full` 版本标记 | 已改 |
| `src/docingest/pipeline.py` | 加 `_is_empty_supplement()` helper；vision 结果收集处跳过 `(no additional visual content)` 不拼空块 | 已改 |

**注意**：
- **批量 prompt（xlsx 多页）还没改**——走另一个 prompt 常量，仍是旧"整页"行为（分步计划的下一步，未做）。
- **cache key 缺陷**：cache key 的 `prompt_tag` 只区分 supplement/full，**不含 prompt 内容版本**。
  我改了 supplement prompt 内容后，tag 没变 → 缓存命中旧结果，重测必须 `force=True`。
  （若 supplement 方案保留，应给 tag 带 prompt 内容版本号。）

## 五、关键文件 path

- prompt + Vision 逻辑：`src/docingest/parsers/vision.py`
  - `_PAGE_PROMPT`（旧/整页，行 ~34）
  - `_PAGE_PROMPT_SUPPLEMENT`（新/只补充）
  - `describe_page` / `describe_page_cached`（cache key 在这）
  - 批量 prompt（行 ~308 附近，注释"Batched multi-image"，**未改**）
- 拼接逻辑：`src/docingest/pipeline.py`
  - `_enrich_with_vision`（行 ~1268）— Vision 结果收集 + 拼回 md
  - `_is_empty_supplement`（行 ~1260，新加）
  - `_dedup_vision`（行 ~1670）— 旧的长度比去重（默认关，与本方案无关）
- config：`config/default.yaml` 的 `parsing.vision` 段（行 ~148）
- 测试样本（真实，含图/扫描）：
  - `test_docs/2/0253331_【デュオメゾン渋谷_３０４】_*.pdf`（合同，丢正文的那个）
  - 现成产物库：`knowledge/jp_final_baseline/`（含高重叠样本 moonmile）、`knowledge/foox_18pages/`
- 架构参考：`ARCHITECTURE.md`（Phase 1.1 garbled fallback、1.5 Vision、1.7 dedup）

## 六、待决策（实测给出的诚实判断）

**supplement 模式在「Docling 抓取碎片化/不完整」的页（扫描件、复杂合同表格）会漏正文，
难用 prompt 稳妥根治**（要 Vision 精确判断"Docling 碎片覆盖了多少"本身不可靠）。

两条路（接手时和用户确认）：
- **A. 回退默认 full（保两份）**：安全不丢，冗余问题改在预览层/RAG 层（显示折叠 / refine 出干净版）。
  保留 supplement 开关给愿意冒险的人。**（被打断前我倾向这个，但用户想先查清碎片根因再定。）**
- **B. 继续调**：先查清碎片根因（第三节），也许能找到"只在 Docling 完整页去重、碎片页保两份"的可靠判据——但同样难，需实测。

## 七、清理状态

- debug 脚本已删（`debug_vision_dedup.py` / `debug_overlap.py` / `debug_lost.py`）。
- 临时产物 `_supp2/` / `_cmp_full/` / `_cmp_supp/` **可能还在项目根**，接手可删
  （`rm -rf _supp2 _cmp_full _cmp_supp`）。
