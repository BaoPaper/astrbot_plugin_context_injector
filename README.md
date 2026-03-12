# AstrBot 插件：上下文模板注入器

通用的 AstrBot 上下文模板注入插件。

## 功能说明

- 通过官方 `@filter.on_llm_request()` Hook，在普通聊天请求发给 LLM 前注入模板。
- 支持三种模板来源：`text`、`file`、`command`。
- 支持把指定模板始终追加到 `system_prompt` 末尾。
- 支持在用户提示词中展开 `{{ctx:alias}}` 占位符。
- 支持用户自定义模板包裹格式，可使用 `$alias`、`$source_type`、`$content` 作为占位符。

## 当前已实现的能力

- `文本模板 (text)`：直接注入一段固定文本。
- `文件模板 (file)`：读取文件内容后注入。
- `命令模板 (command)`：执行命令，取输出后注入。

当前版本真正会生效的注入方式有两种：

- `append_aliases`：把指定模板追加到 `system_prompt` 末尾。
- `{{ctx:alias}}`：在用户输入的提示词里按需展开模板。

另外还实现了一个管理员预览指令：

- `/ctx_preview`：预览当前“自动注入到 system_prompt 末尾”的整体内容
- `/ctx_preview <模板别名>`：单独预览某个模板的渲染结果

## 配置怎么理解

插件配置可分成两层：

- 第一层是“全局规则”：决定模板默认怎么包裹、默认怎么报错、默认字符上限是多少。
- 第二层是“单个模板”：决定某个别名具体读什么内容，用什么包裹格式。

## 全局配置说明

### `enabled`

是否启用整个插件。

### `expand_prompt_placeholders`

是否展开用户提示词中的 `{{ctx:alias}}`。

例如提示词中包含：

```text
请结合这些记忆回答：{{ctx:memory}}
```

启用后会将 `memory` 模板渲染后替换进去。

### `append_aliases`

这里填写模板别名列表后，插件会在每次普通聊天请求时，将这些模板渲染后追加到 `system_prompt` 末尾。

例如：

```json
["memory", "role_hint"]
```

表示每次都会自动追加 `memory` 和 `role_hint` 两个模板。

如果某个模板只需要在特定场景手动引用，而不需要每次自动注入，则不应放入 `append_aliases`。

### `append_separator`

多个自动追加模板之间的分隔符，默认是两个换行。

### `missing_behavior`

模板不存在或渲染失败时怎么处理：

- `跳过`：直接忽略。
- `保留原样`：只对 `{{ctx:alias}}` 生效，占位符不替换。
- `插入错误`：把错误信息也渲染进提示词。

### `default_block_template`

这是“默认模板包裹格式”。

每个模板渲染出原始内容后，会再套上这一层外壳。可用变量：

- `$alias`：模板别名
- `$source_type`：模板类型，可能是 `text`、`file`、`command`
- `$content`：模板实际内容

默认值是：

```text
<context_template alias="$alias" source="$source_type">
$content
</context_template>
```

### `error_template`

当 `missing_behavior = 插入错误` 时，错误信息使用的模板。可用变量：

- `$alias`
- `$reason`

### `default_max_chars`

文件模板、命令模板的默认字符上限。

### `default_command_timeout_sec`

命令模板的默认超时时间。

## 上下文模板列表说明

这里是插件的核心配置。每一项都代表一个可复用模板，并且都需要一个 `alias`。

模板可通过两种方式使用：

- 把别名写进 `append_aliases`，让它每次自动注入。
- 在提示词里写 `{{ctx:alias}}`，让它按需展开。

### 通用字段

每种模板都会有一些共通字段：

### `enabled`

是否启用该模板。

### `alias`

模板别名。`append_aliases` 和 `{{ctx:alias}}` 都通过这个名称引用模板。

### `block_template`

这是“单模板包裹格式”。

该字段会覆盖全局的 `default_block_template`。

优先级是：

```text
单模板 block_template > 全局 default_block_template
```

规则是：

- 如果某个模板自己的 `block_template` 为空，就用全局默认包裹格式。
- 如果某个模板自己的 `block_template` 有内容，就只对这个模板使用它自己的格式。

该字段用于为特殊模板单独定制包裹格式。

## 三种模板分别怎么配

### 1. 文本模板

适合写固定说明、固定记忆片段、固定角色补充。

额外字段：

- `content`：模板正文

示例：

```json
{
  "__template_key": "text",
  "enabled": true,
  "alias": "role_hint",
  "content": "回答时先给结论，再给理由。",
  "block_template": "<rule name=\"$alias\">\n$content\n</rule>"
}
```

### 2. 文件模板

适合接设定文档、世界观文本、规则文件、日志摘录、项目上下文等。

额外字段：

- `base_dir`：相对路径所基于的目录
- `path`：绝对路径，或相对于 `base_dir` 的路径
- `max_chars`：该模板自己的字符上限

`base_dir` 可选：

- `技能目录`
- `数据目录`
- `插件数据目录`
- `根目录`

示例：

```json
{
  "__template_key": "file",
  "enabled": true,
  "alias": "worldbook",
  "base_dir": "插件数据目录",
  "path": "worldbook.txt",
  "max_chars": 0
}
```

其中：

- `max_chars = 0` 表示改用全局 `default_max_chars`
- 如果 `path` 是绝对路径，也可以用，但仍会做目录范围限制

### 3. 命令模板

适合读取动态信息，比如 git 状态、系统时间、某段命令输出。

额外字段：

- `command`：要执行的命令
- `timeout_sec`：该模板自己的超时时间
- `workdir_base`：工作目录的相对路径基准
- `workdir`：可选工作目录
- `max_chars`：该模板自己的字符上限

示例：

```json
{
  "__template_key": "command",
  "enabled": true,
  "alias": "git_status",
  "command": "git status --short",
  "timeout_sec": 5,
  "workdir_base": "根目录",
  "workdir": "",
  "max_chars": 4000
}
```

其中：

- `timeout_sec = 0` 表示改用全局 `default_command_timeout_sec`
- `max_chars = 0` 表示改用全局 `default_max_chars`

## 几个最容易混淆的点

### `append_aliases` 和 `{{ctx:alias}}` 的区别

- `append_aliases`：自动注入，每次请求都带上。
- `{{ctx:alias}}`：手动注入，只有在提示词中写入时才会展开。

一个模板可以同时被这两种方式使用。

### 为什么我已经在 system_prompt 里看到了模板？

因为当前版本的“自动注入”就是追加到 `system_prompt` 末尾。

### 预览指令预览的是什么？

- `/ctx_preview` 预览的是“当前会被自动追加到 `system_prompt` 末尾的整体片段”。
- `/ctx_preview <模板别名>` 预览的是“单个模板渲染后的结果”。

这两个预览都会走和正式注入相同的渲染逻辑，所以拿来检查模板是否生效会比较直观。

### 文件模板和命令模板的上限谁说了算？

优先级是：

```text
单模板 max_chars > 全局 default_max_chars
单模板 timeout_sec > 全局 default_command_timeout_sec
```

但这里的“>`”不是数值比较，而是“有设置就优先用单模板自己的值”。

如果单模板填 `0`，就回退到全局默认值。

## 一个最小可用示例

可通过以下配置快速验证插件是否正常工作：

- 在 `上下文模板列表` 新建一个 `文本模板`
- `alias` 填 `role_hint`
- `content` 填 `回答时先给结论，再给理由。`
- 在 `append_aliases` 里填入 `role_hint`

这样每次普通聊天请求时，这段文本都会被自动追加到 `system_prompt` 末尾。

## 说明

- 当前版本只影响通过官方插件 Hook 进入的普通聊天请求。
- FutureTask 和后台任务唤醒暂时不在本版本范围内。
- 出于安全考虑，文件路径会限制在 AstrBot 根目录相关范围内。
