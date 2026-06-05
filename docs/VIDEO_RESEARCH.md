# 视频生成能力调研：DocIngest → 视频（PDF→Video）

> 调研性质文档，记录"把 DocIngest 的文档解析能力接到视频渲染框架，实现 PDF→视频"的可行性分析。**功能暂不开发**，本文留作日后决策与接手依据。
>
> 调研对象：`参考项目/hyperframes`（HeyGen 开源）、`参考项目/remotion`。结论与代码路径均经源码核实，TTS 环节有实测。

---

## 0. 一句话结论

**逻辑通，技术可行。** 选定**稳定模板路线**：DocIngest 出内容 → LLM 编剧层产出严格 JSON → 程序化注入"验证过的固定模板/块" → HyperFrames 渲染 MP4。代价是视频朴素（讲解/信息视频，非惊艳广告片），换来无人值守的绝对稳定。

---

## 1. 两个渲染框架是什么

两者本质相同：**写代码 → headless Chrome 逐帧截图 → FFmpeg 编码 → 确定性 MP4**。区别只在"用什么写"。

| | **HyperFrames** | **Remotion** |
|---|---|---|
| 作者 | HeyGen，Apache 2.0（自由商用） | source-available，Remotion License（商用有门槛） |
| 写法 | 纯 HTML + `data-*` 时间轴属性 | React 组件 |
| 打包 | 不需要，`index.html` 直接跑 | 必须先 bundle |
| 定位 | **为 AI agent 设计**（agent 本就会写 HTML） | 为 React 工程师设计 |
| 技术栈 | TS + Bun + Puppeteer + FFmpeg；lint 用 oxlint/oxfmt | TS + React 19 + Bun + Rspack |

HyperFrames 自己承认受 Remotion 启发。**对"agent/程序驱动"场景，HyperFrames 更合适**——无 React 构建门槛，HTML 是 LLM 和人都能写的通用形态。

---

## 2. 怎么作为库/程序化调用（不碰命令行）

### HyperFrames（推荐）— 两步，喂目录

入口包 `@hyperframes/producer`：

```typescript
import { createRenderJob, executeRenderJob } from "@hyperframes/producer";

const job = createRenderJob({
  fps: { num: 30, den: 1 },   // ← 有理数，不是 30
  quality: "standard",
  format: "mp4",              // mp4 | webm | mov | png-sequence
  // entryFile 默认 "index.html"
});
await executeRenderJob(job, "/项目目录", "/输出/out.mp4", onProgress?, abortSignal?);
//                          ↑ 传"含 index.html 的目录"，不是单个 HTML 文件
```

- 真实签名核自 `packages/producer/src/services/renderOrchestrator.ts:1439`。
- 还能当 **HTTP 服务**常驻：`startServer({ port: 8080 })` → POST /render，适合服务化。
- 有**分布式渲染**原语（`@hyperframes/producer/distributed` 的 `plan`/`renderChunk`/`assemble`），给大视频拆机器渲。
- 三层：producer（推荐）→ engine（Chrome/FFmpeg 底层）→ core（类型/编译）。

> ⚠️ **producer 的 README 已过时**：它写的 `createRenderJob({ inputPath, fps: 30 })` + `executeRenderJob(job, onProgress)` 在当前源码里不存在，照抄会报错。**以上面源码为准**。

### Remotion — 三步，喂打包产物

入口包 `@remotion/renderer` + `@remotion/bundler`：

```typescript
import { bundle } from '@remotion/bundler';
import { getCompositions, renderMedia } from '@remotion/renderer';

const dir = await bundle({ entryPoint: './src/index.tsx' });   // 1. 必须先打包
const comps = await getCompositions(dir);                       // 2. 选组件
await renderMedia({ composition: comps.find(c=>c.id==='MyVideo'),
                    serveUrl: dir, codec: 'h264',
                    outputLocation: 'out.mp4', inputProps: {...} }); // 3. 渲染
```
- 传参走 `inputProps`（进 React props，自动序列化 Date/Map）。
- 云端 `renderMediaOnLambda`（serveUrl 改 S3 URL，返回 renderId 轮询）。

### 共同硬依赖
两者都要 **FFmpeg** + **Chrome/Chromium**（首次自动下 ~200MB headless shell）。

---

## 3. TTS / 字幕 / 时间轴的"准确性"（含实测）

### 核心机制：音频是基准，不是脚本
HyperFrames 的设计哲学（`step-4-vo.md`）：**先生成配音音频 → 转写出每个词的精确时间戳 → 用真实时间戳驱动字幕和页面时长**。顺序不是"先定时长让音频凑"，而是"音频出来后，一切迁就音频"。

### 三种"准确"分层

| 准确性 | 能否保证 | 原因 |
|---|---|---|
| 字幕 ↔ 配音**对齐** | ✅ 100% | 字幕从配音音频转写而来，**同源 = 天然同步**，逐词时间戳就是被念出的真实时间 |
| 字幕**文字** ↔ 配音内容 | ✅ | 我们的场景里，LLM 摘要出的同一份文字既是配音稿又是字幕源，天然一致 |
| 配音**念得准** | ⚠️ 可控 | Kokoro（免费本地）会念错产品名/缩写（API、专有名词）；用更好的 TTS 或程序化发音替换表解决 |

### 实测结果（2026-06-05）
在 `参考项目/hyperframes` 真跑：
- ✅ **TTS 跑通**：`hyperframes tts` 用本地 Kokoro-82M，一句英文 → 5.867s wav。免费、无 key、不联网。声音含 en/zh/ja（中文 `zf_xiaobei` 等以后可用）。环境：bun 1.3.5 / node 22 / ffmpeg / Python 的 kokoro_onnx+soundfile（在 WindowsApps python3）。
- ⏸️ **transcribe 没跑成**：它要 `whisper-cpp`（要编译的二进制），本机没装。**不是死路**：
  - 选**自带词级时间戳的 TTS**（如 HeyGen v3）→ 转写整步跳过，更稳。
  - 或补任意转写引擎；HyperFrames 的 transcribe 支持导入 whisper-cpp/OpenAI/SRT/VTT 多种格式（`packages/cli/src/whisper/normalize.ts`）。

**结论**：字幕准、对齐准是架构天然保证；配音念得准靠选型+替换可控。**唯一要花工程的是"页面时长自动跟真实音频走"**（把官方的人工"时长对账"做成程序化）——这是无人值守管线里风险第二高的环节。

---

## 4. PDF→视频 的可行性判断（核心）

### 4.1 完整逻辑链

```
PDF
 │ ① DocIngest（Python，已成熟）        → sources/*.md + 知识地图
 ▼
 │ ② LLM 编剧层（要新建 ← 最大工作量/最高风险）
 │    全文 → 严格 schema JSON：{封面标题, 要点[], 图表数据[]}
 ▼
 │ ③ 程序化注入（把 JSON 填进"验证过的固定模板/块"）
 ▼
 │ ④ HyperFrames 渲染（调 producer 或 HTTP server）→ 确定性
 ▼
MP4（+ TTS 配音 + transcript 驱动的字幕）
```

### 4.2 关键决策：走"稳定模板路线"，放弃 HyperFrames 原生编剧法

**这是本调研最重要的判断。** HyperFrames 自带一套很完整的 7 步编剧管线 `website-to-hyperframes`（capture→design→brief→**storyboard**→vo→build→validate），其 `step-3-storyboard.md` 是一套**"反模板、追电影感"的创意方法论**：每个 beat 是"镜头"不是"页面"，强制运镜、禁止网页式布局、禁止静止超 1.5 秒、每 beat 2-4 种动画技法、全程人工 gate 审查。

**它的价值在于创意自由度拉满——这与"绝对稳定、模板化"是哲学冲突的：**

| | HyperFrames 原生（路线甲） | 稳定模板（路线乙，**已选**） |
|---|---|---|
| 哲学 | AI 自由发挥出好视频 | 锁死模板保证不出错 |
| 质量 | 上限高，有电影感 | 朴素，"带动画的信息视频" |
| 稳定性 | 不稳定、看天吃饭 | **绝对稳定、可复现** |
| 自动化 | 半自动、需人在环+审查 | **无人值守，一键出片** |
| 用到 HyperFrames 的 | 渲染引擎 + 编剧大脑 | **只用渲染引擎 + 现成块** |

**选路线乙**：用途是"把文档批量变讲解/数据视频"，可靠性 > 惊艳度，这个取舍是对的。

### 4.3 模板层用什么搭：catalog 块（"半成品积木"）

HyperFrames 有 catalog（`registry/blocks/`，如 `data-chart`、各种 caption、`logo-outro`），`npx hyperframes add <name>` 装进来，用 `data-composition-src` 在 index.html 里声明式拼装，各块独立时间轴、运行时自动同步（`wiring-blocks.md`）。

**核实块的真实形态**（`registry/blocks/data-chart/data-chart.html`）：块是**自包含单文件**（HTML+CSS+GSAP 动画+数据），数据是**脚本里的 JS 常量数组**：
```javascript
const months = ["Jan", "Feb", ...];
const revenueData = [8, 12, 15, ...];   // ← 要换成 PDF 的数据，得改这几行常量
```

**判断**：

| 维度 | 评价 |
|---|---|
| 拼装机制 | ✅ 极稳：声明式、确定性、自动同步时间轴 |
| 块本身质量 | ✅ 高：动画/排版官方调好，远超手写 |
| 内容注入 | ⚠️ **是"改数据常量"，不是"填 `{{slot}}` 槽位"** |

即：**复制块 → 程序化替换里面的数据常量和标题 → 拼装**。比手写死 HTML 好（借官方设计/动画），比理想槽位模板险一点（改 JS 常量要做长度/转义防护）。

### 4.4 稳定性怎么落地（每层兜底）
1. 块是官方验证过的 → lint 必过，结构不破坏就必渲染成功。
2. LLM 只产 JSON 数据、碰不到 HTML/动画 → 碰不坏。
3. 注入前 schema 校验 + 转义 + 用块原本的数据形状 → 坏数据（超长/特殊字符/空数组）进不去，不合规降级到纯文字块。
4. 拼完每步 `hyperframes lint`/`validate` → 再渲染。

**"适当灵活"来源**：块库可扩、按 PDF 类型选不同块组合、配色/时长参数化。灵活 = "在验证过的块里选和配"，不是自由生成。

---

## 5. 技术栈与边界

- **主体语言：Python 主导**。理由：现有生态（DocIngest/AIPowerPoint）在 Python；编剧层调 LLM 在 Python 最顺；HyperFrames 提供 CLI 和 HTTP server 两个非-TS 调用口，可当黑盒后端。
- **边界**：DocIngest(Python) 出文件 → Python 编剧层读文件、注入模板生成 HTML → 丢给 HyperFrames(Node) 渲染。三段用"文件 + 进程/HTTP 调用"解耦，**不在一个进程里混两个语言栈**。
- 唯一要碰 TS 那边的：HTML 模板（静态资产，不算写 TS 工程）。

---

## 6. 风险排序（按真实大小）

| 风险 | 等级 | 说明 |
|---|---|---|
| **编剧层质量**（内容→分镜 JSON） | 🔴 高 | 唯一"看天吃饭"的环节，决定视频好不好。前面全是"能不能跑通"（已基本绿），这个是"跑通了好不好"（没验证） |
| **音频驱动时间轴** | 🟡 中 | 无人值守关键：把官方人工"时长对账"做成程序化，让页面时长自动跟真实音频 |
| 块注入的健壮性 | 🟡 中 | 改 JS 常量需做长度/转义/降级防护 |
| 渲染/TTS/解析 | 🟢 低 | 均已验证可行 |

---

## 7. 下一步（功能启动时）

**最小端到端实测**（把纸面可行变亲眼可行）：
1. `hyperframes add data-chart`，程序化改掉它的数据常量（模拟注入 PDF 数据）；
2. 写最简 index.html 把它 + 一个文字封面块拼起来；
3. `hyperframes lint` → producer 渲染 → 亲眼看 MP4。

一次验证三件事：块改数据后能否正常渲染、拼装机制通不通、视频质量值不值得继续。跑通后再设计 LLM 编剧层的 JSON schema + 块库选型。

---

## 附：关键文件路径（参考项目内）

| 内容 | 路径 |
|---|---|
| HyperFrames 程序化渲染主函数 | `packages/producer/src/services/renderOrchestrator.ts`（createRenderJob:1349 / executeRenderJob:1439） |
| producer 公开 API 导出 | `packages/producer/src/index.ts` |
| TTS 实现（Kokoro，调 Python） | `packages/cli/src/tts/synthesize.ts` |
| 转写格式归一化（支持多引擎导入） | `packages/cli/src/whisper/normalize.ts` |
| 编剧方法论（路线甲，反模板） | `skills/website-to-hyperframes/references/step-3-storyboard.md` |
| 块拼装机制 | `skills/hyperframes-registry/references/wiring-blocks.md` |
| 块真实形态（数据=JS常量） | `registry/blocks/data-chart/data-chart.html` |
| Remotion 渲染 API | `packages/renderer/src/index.ts`（renderMedia/getCompositions/bundle） |
