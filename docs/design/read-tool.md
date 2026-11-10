# `read` 工具 / URI / 选择器 冻结规格

> **用途**：Liorium `read` 工具的行为规格（冻结字面量），实现与测试都以本文为准。
> **判据**：`[冻结]` = 模型见过、prompt 引用过、测试逐字断言过的格式，必须逐字节复刻；`[bug]` = 现状缺陷，Liorium 需显式决策（复刻 or 修正 → 必须同步测试与 prompt）。

## 1. 工具边界：一个 `read` 吃下八个后端
`read.py` 自述 `Unified Read tool — reads files, directories, artifact://, and memory:// URIs.`。模型侧口径是「六种核心工具」（`read`/`execute`/`write`/`edit`/`search`/`find`，见 `agents_template/TASK.md:5`：「替代了旧的 readTextFileTool / sysExecTool / writeTextFileTool / kbReadTool / kbWriteTool / memorySearchTool」），读取侧由 `read` 独揽。

分发是**严格 if/elif 顺序**，命中即 return（`read.py:120-147`）：

| # | scheme | handler | async |
|---|---|---|---|
| 1 | `artifact://` | `_read_artifact` | 同步 |
| 2 | `memory://` | `_read_memory` | ✅ |
| 3 | `skill://` | `_read_skill` | 同步 |
| 4 | `agent://` | `_read_agent` | ✅ |
| 5 | `img_source://` | `_read_img_source` | 同步 |
| 6 | `sourceCode://`（常量 `sourcecode`） | `_read_source_code` | 同步 |
| 7 | 其余（裸路径、未知 scheme） | `_read_file` | 同步 |

- 装饰器 `@register`（外）+ `@tool`（内），函数 `async def read(...)`。
- 每次调用固定两行日志：`read INPUT uri=%s force_plain_text=%s show_line_number=%s with_metadata=%s`、`read OUTPUT len=%d`；debug 级 `read parsed: scheme=%s path=%r selector=%r force_plain_text=%r`。
- **死代码**：`read.py:589-615` `_read_task_section`/`_extract_markdown_section`（旧 VFS 运行时的副本）在 read 内从未调用；`read.py:198` `import asyncio as _asyncio` 未用。Liorium 不要搬。
- `_read_agent` 首行 `del force_plain_text` → 该后端不支持该参数。

## 2. 参数 schema（LLM 看到的就是这份）
`read.args` 实测（`ensure_ascii=False, indent=1`）：

```json
{"uri": {"title": "Uri", "type": "string"},
 "force_plain_text": {"default": false, "title": "Force Plain Text", "type": "boolean"},
 "show_line_number": {"default": false, "title": "Show Line Number", "type": "boolean"},
 "with_metadata": {"default": true, "title": "With Metadata", "type": "boolean"}}
```

签名（`read.py:70-76`）：`uri: str` 必填；后三个 **keyword-only** 且有默认值。

| 参数 | 默认 | 语义 | 生效后端 |
|---|---|---|---|
| `uri` | 必填 | 路径或 URI（可带选择器/查询串） | 全部 |
| `force_plain_text` | `False` | 图片只回元数据文本，不产 `multimodal_tool_result` JSON | 图片文件、artifact 图片、memory 图片附件、img_source |
| `show_line_number` | `False` | 每行前置**文件绝对行号** `N:line`（配合 edit） | 见 §4.4（**并非处处生效**） |
| `with_metadata` | `True` | 列举是否附 `[大小, 行数, 日期]` / `[说明]` | 目录类列举（§6） |

工具名 `read`（`read.name == "read"`）→ artifact id 形如 `read_7`。docstring 是 prompt 的一部分，冻结口径：源码默认只给结构化摘要、`:raw` 关闭、目录一律自动列举（结尾 `/` 可省）、默认每条附 `[大小, 行数, 日期]`（VFS 节点则是 `[用途描述]`）、非视觉模型必须 `force_plain_text=True`。

## 3. URI 解析（`core/uri.py`）

### 3.1 scheme 常量与识别
`SCHEME_ARTIFACT="artifact"`、`MEMORY="memory"`、`SKILL="skill"`、`AGENT="agent"`、`IMG_SOURCE="img_source"`、`SOURCE_CODE="sourcecode"`、`FILE="file"`。`_ALL_SCHEMES` = 前六个（**不含 file**）。识别一律 `lower().startswith(f"{scheme}://")` ⇒ `sourceCode://`/`SOURCECODE://`/`SourceCode://` 等价。

`[实测]` **`file://` 不是 scheme**：`parse("file:///C:/x.txt")` → `scheme="file"`、`path="file:///C:/x.txt"` → 最终 `[文件不存在: file:///C:/x.txt]`。若 Liorium 要支持，属新增能力而非复刻。

### 3.2 选择器正则（唯一语法真源）
```python
SELECTOR_RE = re.compile(r"^(?::raw|:-?\d+(?:(?::|-)-?\d+|\+-?\d+)?(:raw)?)$")
```

### 3.3 `parse()` 两条分支
- **A. 已知 scheme**（`uri.py:180-204`）：`rest = raw[len(prefix):]` → ①先摘 query（`?` 存在则 `urlparse`+`parse_qs`，`rest=rest[:qpos]`；query 只对 scheme URI 有效，read 只有 `img_source://` 真用）→ ②再摘 selector（`_extract_selector`，命中则从尾部裁掉）→ ③`trailing_slash = rest.endswith("/")`、`path = rest.strip("/")`。**scheme 的 path 永无首尾 `/`**，目录靠 `trailing_slash` 单独记。
- **B. 裸路径**（`uri.py:206-220`）：`rest=raw`；含 `:` 时 `candidate=_extract_selector(rest)`，并加 **Windows 盘符守卫**：裁掉 candidate 后的前缀若匹配 `^[a-zA-Z]$`（单字母）则不认该选择器（保证 `D:50` 不被拆）。`path = rest.replace("\\","/").strip()`（**反斜杠统一成正斜杠**，**保留尾部 `/`**）；`trailing_slash = path.endswith("/")`。
- **`_extract_selector`**（`uri.py:127-147`）：收集每个 `:` 位置，从右往左用 `SELECTOR_RE.match(rest[pos:])` 试，**取最长匹配**。这是 `:-20:-10` 不被截成 `:-10`、且 `a/b:raw:raw` → `path="a/b:raw"`+`selector=":raw"` 的原因。

### 3.4 `ParsedURI` 与派生属性
`ParsedURI(scheme, path, selector: str|None, query: dict[str,list[str]], trailing_slash=False)`。派生属性伪代码：
```python
selector_lines:                       # (start,end) 1-indexed，:raw 单独 → None
    if not selector: None
    s = selector.lstrip(":"); if s == "raw": None; if s.endswith(":raw"): s = s[:-4]
    if ":" in s and not s.startswith(("+",":")): return (int(a), int(b))        # :-20:-10
    if "+" in s and not s.startswith("-"): return (start, start+int(len)-1)    # :50+20 → (50,69)
    if re.match(r"^(-?\d+)-(-?\d+)$", s): return (int(g1), int(g2))            # :50-100 / :-20--10
    return (int(s), int(s))                                                    # :42 → (42,42)
is_raw:  selector == ":raw" or selector.endswith(":raw")
is_dir:  selector_lines is None and (path == "" or trailing_slash)
```
`is_dir` 用 `selector_lines is None` ⇒ **`src/:raw` 仍算目录**，`src/main.py:raw` 不算（测试 `test_raw_only_keeps_dir_semantics`）。

### 3.5 解析实测矩阵 `[实测]`
| 输入 | scheme | path | selector | selector_lines | is_dir |
|---|---|---|---|---|---|
| `src/main.py` | file | `src/main.py` | `None` | `None` | F |
| `src/main.py:50-100` | file | `src/main.py` | `:50-100` | `(50,100)` | F |
| `src/main.py:42` / `:50+20` | file | `src/main.py` | `:42` / `:50+20` | `(42,42)` / `(50,69)` | F |
| `src/main.py:raw` | file | `src/main.py` | `:raw` | `None` | F |
| `src/main.py:50-100:raw` | file | `src/main.py` | `:50-100:raw` | `(50,100)` | F |
| `src/main.py:-20:-10` / `:-20--10` | file | `src/main.py` | 同左 | `(-20,-10)` | F |
| `src/main.py:-20` / `:-20-10` / `:20--10` | file | `src/main.py` | `:-20` / `:-20-10` / `:20--10` | `(-20,-20)` / `(-20,10)` / `(20,-10)` | F |
| `src/` | file | **`src/`（保留斜杠）** | `None` | `None` | T |
| `""` | file | `""` | `None` | `None` | T |
| `D:/dev/test.py`、`D:50`、`src/a:b.py` | file | 原样 | `None` | `None` | F |
| `memory://` | memory | `""` | `None` | `None` | T |
| `memory://user` / `memory://user/` | memory | `user` | `None` | `None` | F / T |
| `memory://?q=a&top_k=5` | memory | `""` | `None` | `None` | T（query=`{"q":["a"],"top_k":["5"]}`） |
| `artifact://` / `artifact://read_3:5-7` | artifact | `""` / `read_3` | `None` / `:5-7` | `None` / `(5,7)` | T / F |
| `skill://demo/SKILL.md:raw` | skill | `demo/SKILL.md` | `:raw` | `None` | F |
| `agent://index.md:1-3` | agent | `index.md` | `:1-3` | `(1,3)` | F |
| `sourceCode://` / `sourceCode://backend/` | sourcecode | `""` / `backend` | `None` | `None` | T / T |
| `sourceCode://backend/main.py:1-3` | sourcecode | `backend/main.py` | `:1-3` | `(1,3)` | F |
| `img_source://screenshot?grid=true&scale=0.5` | img_source | `screenshot` | `None` | `None` | F（query 见上） |
| `file:///C:/x/y.txt`、`https://example.com/a` | **file**（未知 scheme） | 原样 | `None` | `None` | F |

### 3.6 边界与坑（逐条决策）
1. `[bug]` `:-5+3` 通过 `SELECTOR_RE`，但 `selector_lines` 走到 `int("-5+3")` **抛 `ValueError`**，穿出 `read()` → 中间件兜成 `工具执行出错\n[完整输出: artifact://…]`（`[实测]`：`EXC ValueError invalid literal for int() with base 10: '-5+3'`）。Liorium 应在解析期拒绝并给文案。
2. `[冻结]` `a/b:raw:raw` → `path="a/b:raw"`、`selector=":raw"`（最长匹配 `:raw`；`":raw:raw"` 不合语法）。
3. `[冻结]` 裸路径保留尾 `/`，scheme 路径去首尾 `/` —— 两分支 `path` 形态**不同**，勿统一。
4. `[冻结]` 裸路径 `\`→`/`（Windows 绝对路径变 `C:/...`，`Path()` 仍可用）。
5. `[冻结]` 未知 scheme 对 read **不是错误**，只是 `[文件不存在: 原串]`；真正的协议拒绝只在 search/find/write（§3.7）。
6. `[冻结]` `:5-3` 不报错，解析 `(5,3)`，由应用层交换（§4.2）。
7. `[冻结]` 单字母守卫只在裸路径分支，scheme 分支没有。

### 3.7 `detect_unsupported_protocol()`（search/find/write 用，read 不用）
```python
for scheme in _ALL_SCHEMES:
    if lower.startswith(f"{scheme}://"):
        if scheme not in supported:
            supported_list = sorted(s for s in supported if s != SCHEME_FILE) or [SCHEME_FILE]
            return (f"{scheme}:// 协议不支持此操作"
                    f"（支持: {', '.join(f'{s}://' for s in supported_list)} 或直接写文件路径）")
        return None
```
`search`/`find`/`write` 传 `{file, memory, agent}` ⇒ `[实测]` 报错为：
`skill:// 协议不支持此操作（支持: agent://, memory:// 或直接写文件路径）`，外层再加 `search: ` / `find: ` / `write: ` 前缀。`file` 被从「支持列表」剔除（只作为「或直接写文件路径」出现）。

## 4. 选择器语义（应用层 `_apply_selector_to_text`）

### 4.1 形式总表（内容 `line1..line400`）

| 写法 | `selector_lines` | 语义 / 实测 |
|---|---|---|
| `:N` | `(N,N)` | 第 N 行（`:5` → `line5`） |
| `:-N` | `(-N,-N)` | 倒数第 N 行（`:-3` → 第 398 行） |
| `:A-B` | `(A,B)` | 闭区间（`:3-5` → 3~5 行） |
| `:-A--B` / `:-A:-B` | `(-A,-B)` | 倒数 A 行 ~ 倒数 B 行（`:-3:-1` → 398~400） |
| `:-A-B` / `:A--B` | `(-A,B)` / `(A,-B)` | 负/正混合，见 §4.2 交换规则 |
| `:A+C` | `(A, A+C-1)` | 从 A 起 C 行（`:5+3` → 5,6,7） |
| `:-A+C` | `[bug]` 抛 `ValueError` | 未定义（§3.6-1） |
| `:raw` | `None` | 关闭结构摘要，回原文（仍有 300 行截断） |
| `:A-B:raw` | `(A,B)` | **与 `:A-B` 完全等价**（行选择本就不做摘要） |

### 4.2 算法（`read.py:618-655`，下为等价压缩写法，原函数用 `def`/`for`）
```python
lines = content.split("\n")
_numbered = lambda sel, first: "\n".join(f"{first+i}:{line}" for i, line in enumerate(sel))
if not selector_lines:
    return content if (not content or not show_line_number) else _numbered(lines, 1)
start, end = selector_lines
resolve = lambda n: len(lines) + n + 1 if n < 0 else n      # -1 = 最后一行
s, e = max(1, resolve(start)), max(1, resolve(end))
if s > e: s, e = e, s                                      # 反向区间自动交换
s, e = min(s, len(lines)), min(e, len(lines))
selected = lines[s-1:e]
return _numbered(selected, s) if show_line_number else "\n".join(selected)
```
- `split("\n")` ⇒ **结尾换行多出一个空元素**，`len(lines)` = 逻辑行数+1；直接影响 `:-1`（拿到结尾空行）与 `:N` 边界。
- 两端 clamp `[1, len]`：`:999-1000` → 最后一行；`:-999` → 第 1 行。
- 反向区间交换：`:10-3` → `line3..line10`。
- 行号永远是**文件绝对行号**（首行=1），与选择器写法无关（`test_negative_range_absolute_line_numbers`）。
- 行选择路径**不截断**：`read("big.py:1-9999")` 原样返回全部行。
- 无选择器 + `show_line_number=True` → 全文按 `1..N` 编号；`content==""` 直接返回 `""`（不编号）。
- 编号分隔符是**半角冒号、无空格**：`{n}:{line}`。

### 4.3 输出格式
```
3:line3
4:line4
5:line5
```
未开编号：只有行内容、`\n` 连接、**末尾不加换行**。

### 4.4 `show_line_number` 生效矩阵（易踩）

| 路径 | 生效 | 原因 |
|---|---|---|
| 文本文件 + 行选择器 | ✅ | `_apply_selector_to_text(..., show_line_number=)` |
| 文本文件 + `:raw` | ✅ | 先编号再截断（`read.py:699-702`：「截断提示行不带行号」） |
| 文本文件 + **无选择器** | ❌ **被忽略** | 直接 `_truncate_long(content)`（`[实测]` 首行仍是 `line1`） |
| 源码文件 → 结构摘要 | ❌ | `_structural_summary()` 不收该参数 |
| 图片文件 | ❌ | 扩展名分派在选择器之前 |
| `artifact://` 文本 + 选择器 / 仅编号 | ✅ | `show_line_number and content_type not in ("image","multimodal")` |
| `artifact://` 图片 | ❌ | 同上排除 |
| `memory://` 文档（含无选择器） | ✅ | 条件 `selector_lines or show_line_number` |
| `skill://` / `sourceCode://` 文件 | 同普通文件 | 委派 `_read_file`（sourceCode 历史 bug 已修：`test_read_source_code_line_range_with_line_numbers`） |
| `agent://` 节点 | ✅（无选择器也编号） | `_read_agent` 总调 `_apply_selector_to_text` |
| `img_source://` / 目录列举 | ❌ | 无行概念 |

## 5. 各后端输出模板
> 以下均为 f-string 原文；`{...}` 是插值。`[` `]`、全/半角标点、空格、换行位置必须一致。

### 5.1 `file`（裸路径 / 未知 scheme）—— `_read_file`（`read.py:658-707`）
分派顺序：①`path==""` → `"."`（列 CWD）→ ②目录（`parsed.is_dir or (exists and is_dir)`，**末尾无 `/` 的真实目录也列举**）→ `_list_directory` → ③不存在 → 依次试 `WORKDIR_ROOT/path`、`PROJECT_ROOT/path`，都无 → `[文件不存在: {path_str}]` → ④`IMAGE_EXTENSIONS` → `_read_image`（**选择器被忽略**）→ ⑤`open(encoding="utf-8", errors="replace")`，异常 → `读取文件出错: {e}` → ⑥有行选择器 → `_apply_selector_to_text`（**不截断**）→ ⑦`is_raw` → `_truncate_long(_apply_selector_to_text(content, None, show_line_number))` → ⑧`SOURCE_EXTENSIONS` → `_structural_summary` → ⑨其余 `_truncate_long`。

**目录列举**（`_list_directory`，`read.py:744-760`）：排序 `sorted(iterdir(), key=lambda e: (e.is_file(), e.name.lower()))` ⇒ **目录先、文件后**，各自小写升序；每行 2 空格缩进，目录名后加 `/`；元数据行仅 `with_metadata and not is_dir` 时出现，4 空格 + `[...]`。模板 `f"  {entry.name}{'/' if is_dir else ''}"` + `f"    [{meta}]"`。**空目录返回 `""`（空字符串）**`[实测]`。异常 → `列出目录出错: {e}`。

`[实测]`（`empty/`、`sub/`、`alpha.py`(3 行)、`blob.bin`(3B)，`with_metadata=True`）：
```
  empty/
  sub/
  alpha.py
    [21B, 3 lines, 11/04]
  blob.bin
    [3B, 11/04]
```
`with_metadata=False` 时**逐字节**为 `"  sub/\n  alpha.py"`（`test_without_metadata_output_is_unchanged` 断言相等）。

**二进制**：read **没有**二进制检测/跳过 `[冻结]`。非图片后缀会用 `errors="replace"` 读成乱码（可能再被截断）；只有图片扩展名会改变路径。Liorium 若加「跳过二进制」属新增行为，需同步 prompt。

**图片文件**（`_read_image`，`read.py:895-918`）：`force_plain_text=True` 返回（`类型:` 是**原始后缀**，不是 mime）：
```
[图片文件: {path.name}]
大小: {len(raw)} bytes
类型: {path.suffix}
```
否则返回 JSON（`ensure_ascii=False`，key 顺序 `kind`/`text`/`images`）：`{"kind":"multimodal_tool_result","text":"图片文件: {name} ({n} bytes)","images":[{"url":"data:{mime};base64,{b64}"}]}`。`mime_map`：`.png→image/png`、`.jpg/.jpeg→image/jpeg`、`.gif→image/gif`、`.webp→image/webp`、`.bmp→image/bmp`、`.svg→image/svg+xml`；未知后缀 `image/png`。支持集 `IMAGE_EXTENSIONS={".png",".jpg",".jpeg",".gif",".webp",".bmp",".svg"}`（**`.svg` 被当图片**）。

### 5.2 结构化摘要 `_structural_summary`（源码专用，`read.py:763-833`）
触发：无行选择器、非 `:raw`、后缀 ∈ `SOURCE_EXTENSIONS = {".py",".ts",".js",".rs",".go",".java",".cpp",".c",".h",".jsx",".tsx",".vue",".rb",".swift"}`。

逐行算法：①`stripped=line.strip()`；②行首 `@` 跳过（装饰器）；③`startswith(('"""',"'''"))`：已在 docstring 内→结束并 continue；该行同种三引号 ≥2 个→单行 docstring 跳过；否则进入 docstring 状态 continue；④docstring 内行：以三引号结尾/含三引号→退出，continue；⑤命中前缀元组 `("def ","class ","async def ","import ","from ","const ","let ","function ","export ")` → 记 `f"{i}: {stripped}"`（**冒号后有空格**）；⑥否则**子串回退**：对 20 个关键字 `("def","class","async def","import","from","const","let","function","export","include","require","public","private","protected","interface","struct","enum")` 逐个 `if kw in stripped`，命中即 break。`[bug]` 此处模板同样写作 `f"{i}: {stripped}"`，但 `i` 已被关键字循环变量**遮蔽**，实际输出**关键字本身、无行号、无空格**：`[实测]` `interface: x = interface_thing`。⑦零命中 → **回退原文** `_truncate_long(content)`（`[实测]`：纯赋值文件整篇返回）。

尾部提示（一字不差，注意 `用read(` **缺一个空格**）：
```
\n[文件 {path}: {total} 行, 显示结构摘要。用 read("{path}:N-M") 查看具体行范围或用read("{path}:raw")关闭结构摘要]
```
`{path}` 是**磁盘绝对路径**（`str(file_path)`，Windows 带 `\`），不是用户写的 URI；`{total} = len(content.split("\n"))`（含结尾空元素）；摘要与提示间只有一个 `\n`，整体**不追加末尾换行**。

`[实测]`：
```
5: import os
6: from x import y
9: def foo():
13: class MyClass:
14: def method(self):
[文件 C:\...\demo.py: 20 行, 显示结构摘要。用 read("C:\...\demo.py:N-M") 查看具体行范围或用read("C:\...\demo.py:raw")关闭结构摘要]
```
缩进被 `strip()` 抹平 ⇒ **嵌套 `def`/`class` 也以 `N: def ...` 出现**；`from x import y` 因 `from ` 前缀保留。**`:raw` 只在「无行选择器」路径生效**（`:raw` 对 `.py` = 原文；对非源码后缀 = 与不加 `:raw` 相同，同样 300 行截断）。

### 5.3 截断 `_truncate_long`（`read.py:836-840`）
```python
lines = content.split("\n")
if len(lines) <= 300: return content
return "\n".join(lines[:300]) + f"\n[... 共 {len(lines)} 行, 已截断]"
```
阈值按 **split 元素数** ⇒ 有结尾换行的文件实际只显示 **299** 行。`[实测]`：299 行+换行→300 元素原样；300 行+换行→截断报「共 301 行」；301 行无换行→截断「共 301 行」。提示行**不带行号**（即使开了编号）；数字 = split 元素总数。

### 5.4 `artifact://` —— `_read_artifact`（`read.py:150-185`）

| 条件 | 返回 |
|---|---|
| `path==""` 且 store 空 | `(没有可用的 artifact)` |
| `path==""` 且非空 | `"可用的 artifact:\n" + "\n".join(f"  artifact://{aid}" for aid in available[-20:])` |
| id 不存在 | `[找不到 artifact: {output_id}]` |
| 有选择器，或（`show_line_number` 且非图片类） | `_apply_selector_to_text(art.content, selector_lines, show_line_number=)` |
| 图片类（`("image","multimodal")`）+ `force_plain_text` | `art.content or f"图片 artifact: {output_id}"`（只有描述） |
| 图片类 + 未 plain | JSON `{"kind":"multimodal_tool_result","text":"{desc}\n[图片: artifact://{output_id}]","artifact": output_id}` |
| 其它文本 | `art.get()` —— **全文，不截断、不加提示** |

列表只给**最近 20 个**（`list_ids()` 按 `created_at` 升序）。`[实测]`：`artifact://read_71:5-7` → `row5\nrow6\nrow7`；`artifact://nope` → `[找不到 artifact: nope]`。注意「引用形态」是冻结的桥接协议：MultimodalBridgeMiddleware 只认 `kind=="multimodal_tool_result"` + `artifact`（§9）。

### 5.5 `memory://` —— `_read_memory`（`read.py:188-245`）
1. 记忆模块不可用 → `(记忆模块不可用)`。
2. `_path_id(path)`：`path.replace("\\","/").strip().strip("/")` → `f"path:/{p}"`（空 → `"path:/"`，`memory/store.py:139`）。
3. 节点存在且 `content_type` 以 `image/` 开头 → 图片附件：异常 → `读取记忆图片出错: {e}`；`desc = result["description"] or f"记忆图片: {path}"`；`force_plain_text` → `f"[图片附件: {path}]\n描述: {desc}\n类型: {result.get('content_type','')}"`；否则 JSON `{"kind":"multimodal_tool_result","text":desc,"images":[{"url":"data:{ct};base64,{b64}"}]}`。
4. 目录判定 `not path or parsed.is_dir or node_type=="dir"`（⇒ `memory://notes` 无斜杠也列举）→ `tree_list(path or "/", include_metadata=with_metadata, include_line_count=with_metadata)` → `_format_tree`；异常 → `读取记忆树出错: {e}`。
5. 文档：`file_read(path)`；`FileNotFoundError` → `[记忆文档不存在: {path}]`；其它异常 → `读取记忆文档出错: {e}`。返回 `content`；有选择器或 `show_line_number` → `_apply_selector_to_text`。**不截断**。

树格式（`_format_tree`，只列一层）：根行 `f"{prefix}{name}/"`（根 name 为 `"/"` 时得到 **`//`**），子层固定 2 空格，元数据再 4 空格，**只有文件有元数据**：
```
{prefix}{name}/
{prefix}  {child_dir}/
{prefix}  {child_file}
{prefix}    [{meta}]
```
`[实测]`（`memory://notes/`，根 name=`notes`）：
```
notes/
  plain
    [11/04, 2 lines]
  work
    [11/04, 3 lines, #工作, #计划, #重要]
```

### 5.6 `skill://` —— `_read_skill`（`read.py:248-327`）
- `raw_path = str(parsed.path or "").strip("/")`（尾斜杠不影响）。
- **空路径 → 列 skill**：根 `Path(state.AGENT_ROOT)/"skill.d"` 不存在或无子目录 → `(没有可用的 skill)`；否则首行 `skill:// 可用 skill:`，每目录 `f"  skill://{d.name}/"`，`with_metadata=True` 且 `description` 非空时追加 `f"    [{desc}]"`（来自 `_read_skill_meta`）。
- 校验：空名 → `[skill 名称不能为空]`；子路径含 `.`/`..` → `[不允许越界访问 skill 目录]`；目录不存在 → `[skill 不存在: {skill_name}]`；resolved 后不以 skill 根开头 → `[不允许访问 skill 目录外的文件]`。
- **目录（无子路径或目标是目录）→ 列举**：首行 `f"skill://{dir_label}/ 内容:"`；目录行 `f"  skill://{dir_label}/{name}/"`；文件行 `f"  skill://{dir_label}/{name}"` + 可选 `f"    [{meta}]"`。`dir_label` = `"{skill_name}/{'/'.join(relative_parts)}"` 或 `skill_name`。隐藏项（`.` 开头）跳过；目录组在前、文件组在后。
- **文件**：不存在 → `f"[skill 文件不存在: {skill_name}/{'/'.join(relative_parts)}]"`；否则「绝对路径 + 原 selector」重新 `parse()` 后委派 `_read_file` ⇒ 与普通文件完全一致（摘要/`:raw`/行范围/图片/截断），但**不回显 URI**。
- `[实测]`（`skill://demo_skill/`，含 `_meta.json`/`SKILL.md`/`util.py`/`sub/`/`.hidden`）：
```
skill://demo_skill/ 内容:
  skill://demo_skill/sub/
  skill://demo_skill/_meta.json
    [56B, 11/04]
  skill://demo_skill/SKILL.md
    [8B, 11/04]
  skill://demo_skill/util.py
    [14B, 2 lines, 11/04]
```
**`skill://name` 不会自动读 `SKILL.md`**（`test_read_skill_lists_dir_and_reads_explicit_file` 断言 `"skill content" not in listing`）。

### 5.7 `agent://` —— `_read_agent`（`read.py:330-366`）
每次先 `get_agent_vfs(refresh=True)` + `refresh_runtime_nodes(vfs)`。`force_plain_text` 被丢弃。
- **根**：首行 `agent:// 可用资源:`；每项 `f"  agent://{item}{'/' if is_dir else ''}"` + 可选 `f"    [{description}]"`（`with_metadata` 且 `node.description.strip()` 非空）。
- **目录**：首行 `f"agent://{raw_path}/ 内容:"`；子项 `f"  agent://{raw_path}/{item}{'/' if is_dir else ''}"` + 可选描述。顺序 = VFS `sorted(children.keys())`。
- **文件**：`vfs.read_text(normalized, default="")`；**内容为空 → `[未知 agent 资源: {raw_path}]`** `[冻结]`（空节点与不存在无法区分）。非空 → `_apply_selector_to_text(content, selector_lines, show_line_number)`。
- `[实测]`（根片段）：`agent://probe-node` + `    [节点说明]`、`agent://subagents/`、`agent://tool_use.md`。
- `agent://index.md` 是 symbolic 节点：`# agent:// 索引` / `可读取资源：` / 每个 `- agent://{item}{'/' if dir}` / `源码阅读：使用 read("sourceCode://{PATH}")（目录自动列出内容）。` / `建议：优先读取 agent://index.md、agent://plugins/、agent://subagents/。`。`agent://tool_use.md` = `TASK.md` 的 `## 核心工具速查` 整节（`_read_task_section`）。`test_read_agent_index` 断言其中含 `agent://tool_use.md` 与 `sourceCode://{PATH}`。

### 5.8 `sourceCode://` —— `_read_source_code`（`read.py:374-438`）
`repo_root = Path(conf.PROJECT_ROOT).parent`（= 仓库根；`PROJECT_ROOT` 语义等于 `backend/`）。
- **根**：首行 `sourceCode:// 仓库根 内容:`，随后 `sorted(repo_root.iterdir())` 的**单一有序列表**（目录加 `/`，文件可选元数据），跳过 `.` 开头项。**不是目录优先**。
- 子路径含 `.`/`..` → `[不允许越界访问源码目录]`；resolve 后不在根下 → `[不允许访问源码根目录外的文件]`。
- **目录**：首行 `f"sourceCode://{raw_path}/ 内容:"`，**目录组在前、文件组在后**（各自 sorted），行 `f"  sourceCode://{raw_path}/{name}"`（目录名已带 `/`）。
- **不存在** → `[源码文件不存在: {raw_path}]`；**文件** → 绝对路径 + selector 重新 parse 后委派 `_read_file`。
- `[实测]`：`sourceCode://backend/tools/ 内容:` 下 `  sourceCode://backend/tools/__pycache__/`、`  .../__init__.py` + `    [986B, 33 lines, 10/04]`。

### 5.9 `img_source://` —— `_read_img_source`（`read.py:441-478`）
空路径或目录 → 固定三行（一字不差）：
```
img_source:// 可用资源:
  img_source://screenshot
  img_source://camera_0
使用 read("img_source://screenshot?grid=true&scale=0.5") 截图，使用 read("img_source://camera_0") 访问摄像头。
```
- `screenshot`：`pyautogui.screenshot()` → `convert("RGBA")` → 变换 → 描述 `f"屏幕截图: {w}x{h}"`，metadata `{"grid": bool, "scale": float}`。
- `camera_{id}`：`cv2.VideoCapture(id)`（未装 OpenCV → `RuntimeError("OpenCV 未安装，无法读取摄像头")`），描述 `f"摄像头 {id}: {w}x{h}"`，metadata `{"camera_id": id, "grid": bool, "scale": float}`。
- 变换顺序：先 `scale`（`0 < scale <= 1`，`LANCZOS`，`max(1,int(w*scale))`），后 `grid`（64px、`(180,180,180,180)`）。
- 输出：`force_plain_text` → `f"[{description}]\n大小: {len(raw)} bytes\n类型: image/png"`，metadata 非空再追加 `"\n" + "\n".join(f"{key}: {value}" …)`；否则 JSON 加可选 `"meta"`。
- 参数错误（`_query_flag`/`_query_scale` 抛 `ValueError`）被外层捕获 → `读取图像源出错: {e}`，`[实测]`：`读取图像源出错: scale 必须满足 0 < scale <= 1`。未知资源 → `[未知 img_source 资源: {path}]`。
- `_query_flag` 接受 `1/true/yes/on` 与 `0/false/no/off`（大小写无关，取最后一个值）。
- 未知 scheme（`http://`、`file://` 等）走 `_read_file` → `[文件不存在: {原字符串}]`（§3.6-5）。

## 6. 元数据（`with_metadata`，默认 `True`）

### 6.1 文件条目 `_file_metadata_line`（`read.py:730-741`；下为等价压缩写法）
```python
parts = [_format_size(st.st_size)]
if (n := _count_source_lines(path)) is not None: parts.append(f"{n} lines")
parts.append(time.strftime("%m/%d", time.localtime(st.st_mtime)))
return ", ".join(parts)      # 顺序固定：大小, 行数, 日期
```
- 尺寸：`<1024` → `f"{size}B"`；`<1MiB` → `f"{size//1024}KB"`（整除）；否则 `f"{size/(1024*1024):.1f}MB"`。
- 行数：**仅当**后缀 ∈ `SOURCE_EXTENSIONS`、`st_size <= MAX_METADATA_LINE_SCAN`（2 MiB）、UTF-8 可解码（`read_text(encoding="utf-8")` 成功）→ `len(text.splitlines())`；否则**整段省略**（`[实测]`：`.bin`/`.md`/`_meta.json` 都无 lines）。
- 日期：本地时间 `%m/%d`（零填充）。
- `stat()` 失败 → `""`（调用方不输出元数据行）。渲染：条目下一行、4 空格、方括号 `f"    [{meta}]"`。

### 6.2 排序/缩进（各后端差异，勿统一）

| 后端 | 排序 | 目录标记 |
|---|---|---|
| FS 目录 | `(is_file, name.lower())` → 目录先 | 尾 `/` |
| `sourceCode://` 根 | `sorted(iterdir())` 单一列表 | 尾 `/` |
| `sourceCode://` 子目录 | 目录组 + 文件组，各自 sorted | 尾 `/` |
| `skill://` 目录 | 目录组 + 文件组，各自 sorted | 尾 `/` |
| `agent://` | VFS `sorted(children)` | 尾 `/` |
| `memory://` | `({"dir":0,"file":1}, name.lower())`（`store.py:640`） | 尾 `/` |

**没有 `目录` 字面量标记**——目录只用尾 `/`。「目录」二字只出现在拒绝文案里（`[不允许越界访问源码目录]`、`[不允许越界访问 skill 目录]`、`[不允许访问 skill 目录外的文件]`）。标题行用半角 `内容:`。

### 6.3 `memory://` 元数据 `_memory_entry_metadata`（`read.py:852-865`）
```python
parts = []
if updated: parts.append(_iso_to_mmdd(updated))     # 解析失败返回 "" → 该段省略
if isinstance(line_count := node.get("line_count"), int): parts.append(f"{line_count} lines")
parts += [f"#{t}" for t in tags[:3]]                 # 只取前 3 个 tag
return ", ".join(parts)
```
顺序与 FS **相反**：`MM/DD, N lines, #tag…`。`_iso_to_mmdd`：`fromisoformat(value.replace("Z","+00:00"))` → `astimezone()` → `%m/%d`。第 4 个及以后 tag 丢弃。`line_count` 由 `tree_list(include_line_count=True)` 给（内容 ≤2 MiB 且 UTF-8 可解码，否则 `None` 省略）。

### 6.4 VFS `description`
来源 `write(..., description=)` / `mkdir(..., description=)` / `set_description(path, text)`（节点不存在抛 `FileNotFoundError(f'VFS node not found: {normalize_path(path)}')`）。`write`/`edit` 改写内容**不丢 description**。空/空白描述**不输出**元数据行；`with_metadata=False` 时完全看不到描述（`test_listing_shows_description_and_blank_is_omitted` 断言 `"测试节点说明" not in plain`）。

### 6.5 frontmatter / `_meta.json`（否定 + 唯一例外）
`read` 本身**不解析任何 frontmatter**：文件正文按纯文本读，开头的 `---` 块也照读（`_structural_summary` 的 docstring 逻辑只认三引号）。YAML frontmatter 只在 **skill 元数据链路**被解析：`_read_skill_meta`（`agent/skill.py:139`）优先读 `_meta.json`；**缺失时** `_parse_skill_frontmatter`（`skill.py:94`）取 `SKILL.md` 顶部 `---` 块（剥离 BOM，首行 `---`，找下一处 `---` 收尾；有 `yaml` 用 `safe_load`，否则退化为 `key: value` 行解析并剥引号），再 `setdefault("slug", skill_path.name)` / `setdefault("version","0.0.0")`；`_meta.json` 解析失败也只回 `{slug, version:"0.0.0"}`。read 的 `skill://` 列举**只展示 `description`**（`f"    [{desc}]"`），不展示 name/version/license。memory 的 `description`/`tags`/`updated_at` 来自存储层元数据（`store._get_meta`），非正文 frontmatter。测试分别锁 `_meta.json`（`TestSkillListingMetadata`）、VFS description（`TestVfsDescription`）、memory meta（`TestMemoryTreeMetadata`）。

## 7. 冻结字面量总表
**read.py**：工具名 `read`；`(没有可用的 artifact)`；`"可用的 artifact:\n"`+`  artifact://{aid}`（`[-20:]`）；`[找不到 artifact: {output_id}]`；`图片 artifact: {output_id}`；`{desc}\n[图片: artifact://{id}]`；`(记忆模块不可用)`；`读取记忆图片出错: {e}`；`记忆图片: {path}`；`[图片附件: {path}]\n描述: {desc}\n类型: {ct}`；`读取记忆树出错: {e}`；`[记忆文档不存在: {path}]`；`读取记忆文档出错: {e}`；`(没有可用的 skill)`；`skill:// 可用 skill:`；`  skill://{name}/`；`    [{desc}]`；`[skill 名称不能为空]`；`[不允许越界访问 skill 目录]`；`[skill 不存在: {name}]`；`[不允许访问 skill 目录外的文件]`；`skill://{label}/ 内容:`；`  skill://{label}/{dir}/`；`  skill://{label}/{file}`；`[skill 文件不存在: {name}/{rel}]`；`  {label}{suffix}`；`agent:// 可用资源:`；`agent://{path}/ 内容:`；`[未知 agent 资源: {path}]`；`sourceCode:// 仓库根 内容:`；`[不允许越界访问源码目录]`；`[不允许访问源码根目录外的文件]`；`sourceCode://{path}/ 内容:`；`[源码文件不存在: {path}]`；img_source 三行菜单（§5.9）；`屏幕截图: {w}x{h}`；`摄像头 {id}: {w}x{h}`；`[{description}]\n大小: {n} bytes\n类型: image/png`；`读取图像源出错: {e}`；`[未知 img_source 资源: {path}]`；`{n}:{line}`（编号，无空格）；`{i}: {stripped}`（摘要，有空格）；`{kw}: {stripped}`（`[bug]` 关键字分支）；`\n[文件 {path}: {total} 行, 显示结构摘要。用 read("{path}:N-M") 查看具体行范围或用read("{path}:raw")关闭结构摘要]`（`用read` 缺空格）；`\n[... 共 {n} 行, 已截断]`；`{n}B`/`{n}KB`/`{n:.1f}MB`；`{n} lines`；`%m/%d`；`  {name}{'/' if dir}`；`列出目录出错: {e}`；`[文件不存在: {path}]`；`读取文件出错: {e}`；`[图片文件: {name}]\n大小: {n} bytes\n类型: {suffix}`；`图片文件: {name} ({n} bytes)`；`//`（memory 根）；`{prefix}{name}/`、`{prefix}  {name}`、`{prefix}    [{meta}]`。

**output_store.py**：`[{desc} — {n}张图片: artifact://{id}]`（em dash `—`）；`[图片: artifact://{id}]`；`(空输出)`；`\n[内容过长,已经截断.请使用行数选择器读取完整输出: artifact://{id}]`（**全半角混排**：`过长`后半角逗号、`截断`后半角句点）；`[找不到 artifact: {id}]`。

**middleware/__init__.py**：`工具执行出错\n[完整输出: artifact://{output_id}]`（`[完整输出: …]` 的**唯一真实出处**，工具抛异常时产生）；`{text}\n[{count} 张图片已保存: artifact://{output_id}]`。

**write.py / edit.py（`↳` ACK 通道）**：`已写入 agent://{path} ({size} bytes)`、`已编辑 {path} (1 处替换)`、`写入 agent 资源出错: {e}`、`已写入 {abs_path} ({size} bytes)`；ACK 追加格式 **换行 + `↳ ` + handler 原文（多行保留）**：`result += f"\n↳ {ack}"`（`write.py:125`、`edit.py:180`）。契约：仅**非空 `str`** 算 ACK；`None`/`""`/dict/list/int 归一为 `None`（`test_vfs_handler_ack.py`）。

**uri.py**：`skill:// 协议不支持此操作（支持: agent://, memory:// 或直接写文件路径）`（由 `detect_unsupported_protocol` 拼装）。

**search.py**：`错误: 搜索模式不能为空`；`正则表达式错误: {e}`；`[文件系统] {n} 条结果:`；`  {rel}:{line}: {text}`；`[文件系统] 未找到匹配: {pattern}`；`[文件系统] 未找到匹配: {pattern}（已达到遍历上限 {n} 个文件，可调大 max_file_walks_fs 再试）`；`[达到文件遍历上限 {n}，结果可能不完整]`；`[memory://] {n} 条结果:`；`  memory://{path}  (score={score:.2f})`（score 两侧各两空格）；`    {snippet}`；`[memory://] 未找到匹配: {query}`；`[agent://] {n} 条结果:`；`  agent://{path}`。

**find.py**：`[文件系统] {n} 个文件:`；`  {rel}`；`  ... 还有 {n} 个文件`；`[文件系统] 未匹配到文件: {globs}`；`[memory://] {n} 个文档:`；`  memory://{path}`；`  ... 还有 {n} 个`；`[agent://] {n} 个节点:`；`没有匹配任何文件`；`[达到文件遍历上限 {n}，结果可能不完整]`。

## 8. 测试断言清单（Liorium 必须逐条保住）

### 8.1 `test_read_metadata.py`（唯一专门的 read 测试；辅助 `_entry_meta` 要求条目下一行以 `    [` 开头、`]` 结尾）
- `TestFsListingMetadata`：①`alpha.py`(3 行) 元数据 `\d+B, 3 lines, \d{2}/\d{2}`；`blob.bin` 为 `\d+B, \d{2}/\d{2}`（**无 lines**）；`sub/` **无元数据行**。②不传 `with_metadata` 也带元数据（默认 True）。③`False` 时**逐字节** `"  sub/\n  alpha.py"`。④400k 行 `big.py` 元数据含 `MB`、以 `, MM/DD` 结尾、不含 `lines`。⑤不可解码 `b"\xff\xfe\x00broken"` 不含 `lines`。
- `TestSourceCodeListingMetadata`：⑥`sourceCode://backend/tools/` 的 `read.py` 为 `\d+KB, \d+ lines, \d{2}/\d{2}`。⑦`sourceCode://` 的 `README.md` 为 `\d+(\.\d)?(B|KB|MB), \d{2}/\d{2}`。
- `TestSkillListingMetadata`（fixture 造 `agents/demo_agent/skill.d/demo_skill/{SKILL.md,util.py,_meta.json}` + monkeypatch `state.AGENT_ROOT`）：⑧`skill://` 匹配 `skill://demo_skill/\n    \[演示技能说明\]`；`with_metadata=False` 时描述不出现。⑨`skill://demo_skill/` 的 `util.py` 为 `\d+B, 2 lines, \d{2}/\d{2}`、`SKILL.md` 为 `\d+B, \d{2}/\d{2}`。
- `TestVfsDescription`：⑩列举含 `agent://meta-test-node\n    \[测试节点说明\]`、`agent://meta-test-dir/\n    \[测试目录说明\]`；无描述节点不得出现 `\n    [`；plain 无描述。⑪`set_description` 生效；不存在节点抛 `FileNotFoundError`。⑫`write`/`edit` 重写**保留** description。
- `TestMemoryTreeMetadata`：⑬`/notes/work`(3 行, 4 tags) 匹配 `work\n    \[\d{2}/\d{2}, 3 lines, #工作, #计划, #重要\]`，`#第四个` **不出现**。⑭`with_metadata=False` 时含 `work` 且**完全不含 `[`**。

### 8.2 `test_harness.py`（等价于 `test_uri.py` + `test_read_*`）
- `TestURIParse`（26 个用例）：§3.5 全表，含 `:50+20`、`:raw`、`:-20:-10`、`:-20--10`、`:-20`、`:-20-10`、`20--10`、目录尾斜杠、`memory://` 根、query、`D:/dev/test.py` 不被当选择器。
- `TestOutputStore`：短摘要无 `artifact://`；60001 字符摘要含 `artifact://` 与 `…`；`peek` 的 `lines==1`/`size==4`；`get(offset,limit)`；`list_ids`；缺失 `找不到`；`clear`；id 前缀 `shell_`/`py_`。
- `TestReadTool`：artifact 全文/区间/缺失；`_format_tree` 单层 `"//\n  diary/\n  user/\n  auto_index.md"`（断言 `"2026-07-11" not in text`）；`is_dir` 矩阵；结构摘要含 `foo`/`bar`/`MyClass` 与 `结构摘要` 或 `[文件`；`:raw` 与原文**完全相等**且摘要不含 `return 42`；skill `:raw` 相等、编号 `"1:def download(url):\n2:    return url\n3:"`；raw 编号截断时第 300 行 `300:line300`、第 301 行以 `[` 开头含 `已截断`；`sourceCode://…:1-3` 首行以 `1:` 开头；memory 文档编号 `"1:alpha\n2:beta"`；文件区间 3-5；目录列举；不存在；`agent://index.md` 含 `agent://tool_use.md` 与 `sourceCode://{PATH}`；`sourceCode://README.md`；越界 `不允许`；根/子目录列举（含无尾斜杠）；memory 目录无尾斜杠；agent 目录无尾斜杠；skill 列举 + 显式文件；skill 越界；img_source 截图（`屏幕截图`/`grid: True`/`scale: 0.5`）、`scale 必须满足`、摄像头（`摄像头 2`/`camera_id: 2`）。
- `TestReadImage`/`TestReadForcePlainText`：multimodal JSON 的 `kind`、`text` 含 `图片文件`、`images[0].url` 前缀 `data:image/png;base64,`；plain 不含 `multimodal_tool_result`/`base64`/`data:image`；artifact 图片 plain 含描述。
- `TestMiddlewareMultimodal`：结果只带引用（`len < 1000`）；artifact 存 `content_base64`/`mime_type`；已有引用形态**不再复制** artifact。
- `TestToolOutputStoreBinding`：store 重置后新 artifact 仍可被 `read(artifact://…)` 解析。
- `TestNegativeSelectorApplication`：无选择器编号 `"1:line001\n2:line002\n3:line003"`、空串 → `""`；`(-20,-10)` 于 40 行首行 `21:line021`、末行 `31:line031`、共 11 行；`(-1,-1)` → `"40:line040"`；`(-50,-40)` clamp 到 `1:line001`；`(5,10)` → `5:line005`..`10:line010`。

### 8.3 其它
- `test_mm_bridge.py`：桥接只认 `kind=="multimodal_tool_result"` + `artifact`；artifact 不在 → 不注入；**绝不删消息**；幂等（重启后不重复注入）；只注入未桥接源；无扫描窗口限制；Anthropic → `image` block（`source.type=="base64"`、`media_type=="image/png"`）、Gemini → `inline_data`。
- `test_vfs_handler_ack.py`：只有非空 `str` 算 ACK；`↳ 已提交 task_7（队列 1/5）`、多行 `↳ line1\nline2`；无 ACK 时不含 `↳`；handler 抛异常 → `写入 agent 资源出错` + 原文；edit 只触发 `edit_handler`。
- `test_araya_runtime.py:206-211`：图片附件结果不含 `data:image`/`base64`、含 `artifact://`、`len < 1000`。

## 9. read 的外围契约：OutputStore + 中间件（不能只搬 read.py）
所有工具都经 `wrap_tool_output()`（`middleware/__init__.py`），read 的返回值**不等于**模型看到的文本。完整实现细节见 `docs/design/vfs-output-mw.md`（§4.6 截断/§4.7 `read artifact://` 重发语义），此处只列 Liorium 必须保住的行为：
1. 结果 ≤120 字符**且无换行** → 原样返回（不建 artifact）。
2. 否则 `store.put(output, tool_name)` → 返回 `Artifact.summary()`：多模态 → `[{desc} — {n}张图片: artifact://{id}]`；`"image"` → `[图片: artifact://{id}]`；空 → `(空输出)`；**≤300 行且 ≤60000 字符 → 原样**（多数 read 输出因此对模型透明）；否则截断 + `\n[内容过长,已经截断.请使用行数选择器读取完整输出: artifact://{id}]`。
3. 工具抛异常 → `store.put(str(e), metadata={"status":"error"})` + `工具执行出错\n[完整输出: artifact://{id}]`。
4. 多模态 dict/multimodal JSON → `put_multimodal()`（存 base64 + `image_count`），文本变 `"{text}\n[{count} 张图片已保存: artifact://{id}]"`；已是引用形态（有 `artifact`、无 `images`）则原样透传。
5. artifact id = `f"{short_tool_name}_{counter}"`；`short` 剥后缀 `Tool`/`_tool`/`Func` 后查表（`sys_exec→shell`、`python_exec→py`、`read_text_file→read`、`write_text_file→write`、`list_directory→ls`、`memory_search→mem`、`request_human_approval→hil`、`show_nimble_window→nimble`），否则 `name.lower()[:12]` ⇒ read 产物为 `read_N`。
6. 单例以 `state.AGENT_ROOT/artifact.json` 为存储路径；路径变化或 `reset_output_store()` 后重建 ⇒ 包装器必须**每次调用现取** store（`test_artifact_written_after_store_reset_is_resolvable`）。
7. `Artifact.get(offset=0, limit=None)`：0-based 行偏移，`limit=None` 返回剩余全部，`\n` join；`size = len(content) + len(content_base64 or "")//4*3`；`lines = content.count("\n") + (1 if content else 0)`。

## 10. search / find 的结果格式与拒绝行为

### 10.1 `search(pattern, *, paths=None, max_file_walks_fs=1000)`
- 归一：`paths=None` → `["."]`；`str` → 先 `json.loads`（兼容 `'["memory://"]'`），失败当单路径；`list` → 逐项 `str()`。
- 逐 path 先 `detect_unsupported_protocol(p, {file,memory,agent})`，任一不支持 → 立即 `search: {err}`。
- 分组：`memory://` → mem scope（`strip("/")` 后补前导 `/`，空则 `/`）；`agent://` → vfs scope；其余 fs。执行顺序固定 **memory → agent → filesystem**，多段 `"\n\n"` 连接。空 pattern → `错误: 搜索模式不能为空`；三组皆空 → `错误: 没有有效的搜索路径`。
- **文件系统**：相对路径基准 `WORKDIR_ROOT`，绝对路径原样；`re.compile` 失败 → `正则表达式错误: {e}`；`os.walk` 跳过 `.` 开头目录/文件；后缀黑名单 `(".pyc",".pyd",".dll",".so",".exe",".bin",".mp3",".wav")` **整文件跳过**；读文件用 `errors="replace"` ⇒ **永不编码失败**，故 `"(二进制文件，已跳过内容)"` 分支是**死代码**；逐行 `regex.search`，记 `{"file": str(rel), "line": i(1-based), "text": line.strip()[:150]}`；**结果上限 20**、**遍历上限** `max_file_walks_fs`（默认 1000，每读一文件 +1）。
- 输出：
```
[文件系统] {n} 条结果:
  {rel}:{line}: {text}
[达到文件遍历上限 {n}，结果可能不完整]
```
（`rel` 为 `str(Path)`，Windows 用 `\`）未命中：`[文件系统] 未找到匹配: {pattern}`；到过上限时追加全角括号文案 `（已达到遍历上限 {n} 个文件，可调大 max_file_walks_fs 再试）`。
- **记忆库**：`store.search(query, scope=…, top_k=5, return_mode="snippets", use_graph=False)`；异常仅 warning；汇总后**只展示前 10**（header 的 `{n}` 是全部命中数）：`[memory://] {n} 条结果:` / `  memory://{path}  (score={score:.2f})` / 可选 `    {snippet[:120]}`；空 → `[memory://] 未找到匹配: {query}`。
- **VFS**：`vfs.search(scope, pattern, include_symbolic=True)`（**子串**匹配）；去重后展示前 20（header 计全部去重数）：`  agent://{item.lstrip('/')}`；空 → `[agent://] 未找到匹配: {pattern}`。
- 描述里固定「结果是摘要，命中后用 `read("路径:行号-行号")` 看真实上下文」——Liorium 应保留该行为约定。

### 10.2 `find(patterns, *, max_file_walks_fs=1000)`
- `patterns: list[str]` 必填；逐项 `strip()` + 协议检查 → `find: {err}`。分组顺序 **filesystem → memory → agent**，`"\n\n"` 连接；三组皆空 → `没有匹配任何文件`。
- **文件系统**：`globmod.glob(WORKDIR_ROOT/pattern, recursive=True)`，只留 `is_file()`，按绝对路径字符串去重，受 `max_file_walks_fs` 限；**按 mtime 倒序**（异常退化为路径排序）；输出 `[文件系统] {n} 个文件:` + `  {rel}`（相对 WORKDIR_ROOT，不可相对给绝对）；`>50` 追加 `  ... 还有 {n} 个文件`；到限追加 `[达到文件遍历上限 {n}，结果可能不完整]`；空 → `[文件系统] 未匹配到文件: {', '.join(globs)}`（到限再缀全角括号）。
- **记忆库**：glob 去 `memory://`、`rstrip("*")`、`rstrip("/")` 得 scope → `tree_list(scope or "/")`；`_extract_paths_from_tree` 只取 `type=="file"` 节点的**自带 `path`**（不拼前缀，防 `user/user/...`）；输出 `[memory://] {n} 个文档:` + `  memory://{path}`，`>50` 追加 `  ... 还有 {n} 个`。
- **VFS**：`vfs.walk("/")` 全节点 + 自写 glob→regex（`**/`→`(?:.*/)?`、`**`→`.*`、`*`→`[^/]*`、`?`→`[^/]`）；以 `/` 结尾的 pattern 补 `/**` 递归；目录补 `/`；`sorted()` 后前 50：`[agent://] {n} 个节点:`；空 → `[agent://] 未匹配: {', '.join(globs)}`。

## 11. Liorium 迁移清单

### 11.1 必须逐字节复刻
1. §7 全部字面量，尤其 `N:line`（无空格）、`N: line`（摘要，有空格）、`[... 共 N 行, 已截断]`、footer 里 `用read(` 缺空格、`[内容过长,已经截断.` 全半角混排、`↳ ` 前缀、`[完整输出: artifact://…]`。
2. `split("\n")` 驱动的行计数（含结尾空元素）、300 行截断边界、`[1,N]` clamp、负数 `len+n+1`、反向交换。
3. `:raw` 只影响「无行选择器」的源码摘要路径；`:A-B:raw` ≡ `:A-B`。
4. 目录列举排序与缩进：FS/memory 目录优先，`sourceCode://` 根**不**目录优先；元数据 4 空格 + 方括号。
5. FS 元数据顺序 `大小, N lines, MM/DD` vs memory `MM/DD, N lines, #tag…`（前 3 个 tag）。
6. 空目录 → 空字符串；空 agent 节点 → `[未知 agent 资源: …]`；artifact 只列最近 20。
7. `skill://name` 列目录而**不**自动读 `SKILL.md`。
8. `show_line_number` 生效矩阵（§4.4），特别是「普通文件无选择器时被忽略」。
9. 中间件三态：≤120 无换行直通 / 长文本进 artifact / 多模态只留引用。

### 11.2 建议在 Liorium 修正（须同步测试与 prompt）

| 现状 | 问题 | 建议 |
|---|---|---|
| `:-5+3` 抛 `ValueError` | 解析期未覆盖 | 解析期拒绝并给文案，或定义语义 |
| 摘要子串回退输出 `kw:` | 变量遮蔽 bug（丢行号） | 改 `f"{i}: {stripped}"`，并收窄关键字集合（`require`/`include`/`public` 噪声大） |
| 300 行阈值按 split 元素 | 有结尾换行时少显示一行 | 改 `splitlines()` 并显式声明语义 |
| read 对未知 scheme 只说「文件不存在」 | 误导（`file://`/`http://`） | read 也调用 `detect_unsupported_protocol` |
| `file://` 不被识别 | 与直觉不符 | 显式支持并 normalize |
| 空目录返回 `""` | 模型收到空 ToolMessage | 返回 `(空目录)` |
| agent 空内容与不存在同文案 | 无法区分 | 区分 `(空节点)` 与未知资源 |
| 二进制无检测 | 乱码进上下文 | 加 NUL/解码探测，返回 `(二进制文件, N bytes)` |
| `read.py` 内 `_read_task_section` 重复实现 | 死代码 | 删除，统一走 VFS 节点 |
| 文档「500 字符摘要」（`agent-basics.md:102`、`devinstall.md:113`） | 与代码（60000 字符/300 行）漂移 | 以代码为准更新文档 |


## 12. 与 `liorium-core/uri.py` 现状的偏差（S-READ 必读）

`liorium/packages/liorium-core/src/liorium/core/uri.py` 已实现**挂载点**模型（`<scheme>://a/b` ≡ `/<scheme>/a/b`，见 `docs/design/INTERFACES.md` §3），与本文冻结语义存在下列差异：

| 项 | 规格（本文，冻结） | liorium 现状 |
|---|---|---|
| scheme 识别 | 白名单 6 个；其余当裸路径 | 通用正则 `^(?P<scheme>[a-zA-Z][\w+.-]*)://`，任意 scheme 进 VFS |
| `file://` | 不支持 → `[文件不存在: …]` | 支持，映射真实绝对路径 `/C:/x/y.txt` |
| scheme 名 / path | `skill`、`sourcecode`（单数）；`rest.strip("/")` | 示例 `skills://`（复数）；保留 `rest`，再 `normalize_vfs_path` |
| query | 仅 scheme URI 解析 | 所有输入（含裸路径）先剥 `?`/`#` |
| `D:50` 盘符 | 单字母守卫 → 当路径 | **无守卫** → 拆成 `D` + `:50`（Windows 风险） |
| `..` 越界 / `:-5+3` | 各 handler 字符串判断；抛 `ValueError` | 统一 `VfsError("VFS 路径越界（.. 逃出根）")`；**同样抛** |

结论：选择器语义（`SELECTOR_RE`/`selector_lines`/`is_raw`/`is_dir`）两边**行为一致**（Liorium 少一个恒真的 `not s.startswith(":")` 判断），可直接复用；差异集中在 scheme 白名单、`file://`、path 归一与盘符守卫。若采用挂载点模型（推荐），需补「scheme → 挂载点」映射（`skill://x` ↔ `/skills/x`），否则 `read("skill://demo/SKILL.md")`、`read("agent://index.md")` 的解析与本文不符。**工具名必须仍叫 `read`**（artifact id 前缀、prompt、测试都依赖），内部按 scheme 拆 8 个 handler。

## 13. 复现命令
```powershell
.venv\Scripts\python.exe -m pytest backend/tests/test_read_metadata.py -v
.venv\Scripts\python.exe -m pytest backend/tests/test_harness.py -v -k "URIParse or ReadTool or ReadImage or NegativeSelector"
.venv\Scripts\python.exe -m pytest backend/tests/test_mm_bridge.py backend/tests/test_vfs_handler_ack.py -v
```
> 仓库里**不存在** `backend/tests/test_uri.py`、`test_file_engine.py`、`test_read_*.py`（除 `test_read_metadata.py`）；URI 与 read 的测试实际在 `test_harness.py`（`TestURIParse`/`TestReadTool`/`TestReadImage`/`TestReadForcePlainText`/`TestNegativeSelectorApplication`/`TestMiddlewareMultimodal`）。
