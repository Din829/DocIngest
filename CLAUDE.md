# DocIngest — AI 协作备忘

## 验证改动时,先排除环境假象（重要）

改了 DocIngest 的解析/输出逻辑后做实测验证,**这三个坑会让你拿到假结果、误以为代码没生效**。踩过,记下来:

1. **缓存会让你看到旧结果**。`docingest run` 是增量缓存的:同一个文件第二次跑会命中缓存,**根本不跑你的新代码**,index/chunks 都是旧的。验证新代码时,**用一个全新的输入文件名 + 全新的输出目录**(或确认日志没显示 "cache hit"),否则你验的是上一版。

2. **`--force` 不删磁盘缓存**。它只是"忽略缓存逻辑",不清 `<output>/.cache/`,重跑可能仍命中。别指望 `--force` 给你干净环境——要干净就换新文件名/新目录。
   （另注:日志里的 "cache hit" 也可能是 AI 结果缓存 `.docingest_cache/cache.db`,那是 Vision 调用缓存,和 index 内容无关,别被它误导成"index 没更新"。）

3. **`python` 和 `python3` 在本机是两个解释器**。`python` 装了 docingest + 依赖(litellm/PIL/yaml),`python3` 没有。**核对脚本一律用 `python`**,否则会冒出 `ModuleNotFoundError: docingest` / `No module named 'PIL'` 这类假报错,浪费时间排查一个根本不存在的问题。

**一句话**:实测"没生效"时,先怀疑是上面三个假象,再怀疑代码。先用新文件名重跑确认,别急着改代码。

## 加"文件级 index 字段"要四处同步

往 `index.json` 加一个文件级字段(像 `element_boxes` / `page_sizes`),要同步多处,漏一处**静默失败**(数据悄悄丢,不报错)。`tests/unit/test_index_file_level_fields.py` 是守门测试,漏改会红并点名——改完跑它确认。
