# Source Markdown Contract

这份文档给 parser / writer 作者看：只要 `sources/*.md` 尽量产出这些稳定形态，chunker 就能用结构切分，而不是靠关键词猜内容。

目标不是“任意 Markdown 都完美”，而是：

- 常见文档能按语义边界切。
- 格式漂移时不靠具体语言词兜底。
- 最坏情况仍交给 `RecursiveChunker` 按段落、句子、字符继续切。

## 总原则

1. 用 Markdown 结构表达内容类型，不用字面关键词表达内容类型。
2. 能用标准语法就用标准语法：heading、table、blockquote、code fence、list。
3. 不要制造“看起来像列表、实际不是列表”的长文本。
4. 一种边界只用一种稳定形态，不要同一个 parser 今天用 heading，明天用纯文本标题。
5. parser 可以输出不完美内容，但不要输出会误导 chunker 的伪结构。

## Chunker 当前识别的结构

| 结构 | 写法 | 当前作用 |
|---|---|---|
| 标题 | `#` / `##` / `###` | auto 评分、heading 切分、title_path |
| 页/幻灯片/工作表边界 | `<!-- pagebreak -->` | slide / sheet 优先用它切 |
| 表格 | 连续 `| ... |` 行，最好有 separator 行 | table 保护；超大时按行切并重复表头 |
| 列表 | `- ` / `* ` / `+ ` / `1. ` | list 保护；超大时按 item 切 |
| 引用 | `> ` | quote 保护；适合短说明、图片描述、备注 |
| 代码块 | 三反引号 fence | code 保护；默认不拆 |
| 时间戳 | 行首 `[MM:SS]` 或 `[HH:MM:SS]` | timestamp 切分；再委托 recursive |

这些是结构规则，不依赖“说 / 画面 / slide / page / 表格”这类语言词。

## 各来源建议输出形态

### PDF / DOCX

- 保留真实标题层级：`#`、`##`、`###`。
- 有页边界时用 `<!-- pagebreak -->`。
- 表格输出为标准 Markdown table。
- 图片占位用 HTML comment，例如 `<!-- image: name.png -->`。
- 不要把普通段落伪装成列表。

### PPTX

- 每页之间优先输出 `<!-- pagebreak -->`。
- 页内标题可以继续用 Markdown heading。
- 如果没有 pagebreak，slide chunker 只会再尝试 HR 或短编号 heading；都没有就走 recursive。
- 不要靠“Slide 1 / Page 1 / 幻灯片 1”这类词来表达边界，边界应该是结构。

### XLSX / CSV

- 每个 sheet 优先用 `<!-- pagebreak -->` 分隔。
- 每个 sheet 开头建议有 `## SheetName`，方便写入 `sheet_name` / `title_path`。
- 表格必须是标准 Markdown table：表头行、separator 行、数据行。
- 如果有 sheet 标题行 + 真正列名行，保持这种结构即可；sheet chunker 会用结构启发式保留真正列名。

### Audio / Video / Subtitle

- 时间戳必须在行首：`[00:12]`。
- 同一时间点下的说话内容、画面描述、旁白等，用普通段落或加粗标签段落。
- 可以这样写：

```md
[00:12]
**说**: 这里是一段转写内容。

**画面**: 这里是画面描述。
```

- 不要这样写长文本：

```md
- **说**: 这里是一大段转写内容……
- **画面**: 这里是一大段画面描述……
```

这不是列表，会误触发 list 保护。`markdown_writer` 对 video 做了结构 normalize，但新 parser 不应该依赖补丁兜底。

### Text / Markdown 透传

- 原文是什么就尽量保留。
- 如果上游能补结构，优先补 heading / table / timestamp，不要补语言关键词。
- 没结构也可以交给 recursive，但切分质量会只剩段落、句子、字符兜底。

## 常见反例

### 假列表

坏：

```md
- **解说**: 很长一段正文……
```

好：

```md
**解说**: 很长一段正文……
```

原因：`- ` 在 Markdown 里就是 list item。chunker 会先把它当列表保护。

### 非标准表格

坏：

```md
Name | Value
A | 1
```

好：

```md
| Name | Value |
| --- | --- |
| A | 1 |
```

原因：sheet/table 逻辑按 Markdown table 行识别。

### 时间戳不在行首

坏：

```md
Speaker [00:12]: text
```

好：

```md
[00:12]
Speaker: text
```

原因：timestamp chunker 只认行首时间戳。

## 改 parser 时的检查清单

- 新输出有没有引入新的伪结构？
- 长文本是不是会被误认为 list / quote / code / table？
- 页、slide、sheet、时间戳边界是不是用结构表达？
- 表格是否仍是标准 Markdown table？
- 一份真实 `sources/*.md` 跑 chunker 后，是否没有异常超大 chunk？
- 关键内容是否还在：表头、时间戳、图片占位、标题路径。

如果答案不确定，先加一个真实 `source.md` fixture，再改 parser。
