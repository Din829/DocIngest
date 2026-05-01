# AGENTS.md

DocIngest = 文档前处理引擎。任意输入（PDF / Office / HTML / 图像 / 音视频 / ZIP / URL）→ Markdown + chunks + 索引。
**不做检索、不做 embedding、不做答案生成。**

## 30 秒上手

```bash
pip install -e ".[mcp]"
cp .env.example .env          # 填 GEMINI_API_KEY / DASHSCOPE_API_KEY
docingest doctor              # 体检：缺什么依赖一目了然
docingest run ./docs/ -o ./knowledge/
```

## 怎么调

| 通道 | 入口 | 何时用 |
|---|---|---|
| CLI | `docingest run / inspect / refine / doctor` | 命令行直接跑；`--json` 输出给 agent / 子进程消费 |
| Python 库 | `import docingest; docingest.ingest(...)` | 嵌入到别的 Python 项目 |
| MCP server | `python -m docingest.mcp_server` | Claude Desktop / Code / Cursor 等通过 MCP 调用 |

## Agent 工作流

未知 / 大文件 → **先 inspect 看成本，再 run**：

1. `inspect(paths)` → 看 `est_cost_usd` 和 `recommendation`
2. `run(paths, output_dir)` → 实际处理（默认增量，二次跑命中缓存秒级）
3. 浏览产物：`index.json`（文件清单）→ `sources/*.md`（grep / read）→ `chunks.jsonl`（喂下游 RAG）
4. 可选：`refine(files, skill="refine_faithful")` → 给人看的版本

## 重要习惯

- Vision 是最大成本来源——**每页一次 API 调用**。大文件先 `inspect`。
- `run` 默认增量。没真有理由（改了 chunking 策略且 cache 没自动失效）别用 `--force`。
- `safety.mode=strict` 触发 abort 时，**先把 violation 报告给用户**，不要盲目 `--yes` 重试。
- `sources/*.md` 是 RAG / agentic search 的真源；`refine` 只为人可读，**不是** RAG 流水线的一步。
- CLI 子进程消费：`docingest run --json` / `inspect --json` 把 JSON 写 stdout，banner 走 stderr。

完整功能、配置、Python API、MCP 客户端配置见 [README.md](README.md)。
