# v0.2 WebUI 插件视图：接口契约

面向维护者。v0.2 在 AstrBot WebUI 里加一个插件视图，提供：带图片的待处理队列、案件历史、统计、试判。机制见 AstrBot 文档 `docs/zh/dev/star/guides/plugin-pages.md`（插件视图，AstrBot ≥ 4.29）。

## 1. 结构与文件归属

| 文件 | 内容 | 归属 |
|---|---|---|
| `core/store.py` | 新增查询、统计、巡查报告持久化（§4） | 后端 A |
| `web/__init__.py`、`web/routes.py` | Web API handler、参数校验、DTO 序列化（§3、§5） | 后端 B |
| `main.py` | 在 `__init__` 里注册 Web API；每轮巡查报告写库；手动巡查改为后台任务供两个入口共用 | 后端 B |
| `services/llm_judge.py` | 支持试判时临时替换吧规（§6） | 后端 B |
| `views/moderator/index.html`、`app.js`、`style.css` | 前端视图（§7） | 前端 |

- `web/routes.py` 不定义 `@filter` 装饰器 handler，只提供普通方法，由 `main.py` 调用 `context.register_web_api` 注册，不受「指令 handler 必须在 main.py」的限制。
- 删除 / 封禁 / 忽略 / 恢复全部走 `CaseService`，与 QQ 指令同一份逻辑与同一把锁。
- Dashboard 负责登录鉴权；能打开插件视图的人就是 WebUI 用户，视为管理员。后端仍要校验所有输入。

## 2. 通用约定

- 路由注册为 `/astrbot_plugin_tieba_moderator/<path>`，视图端用 `bridge.apiGet("<path>")` / `bridge.apiPost("<path>", body)`。
- 成功：`json_response(<业务 JSON>)`，直接返回业务对象（bridge 原样 resolve）。
- 失败：`error_response(<中文消息>, status_code=...)`：
  - 400 参数不合法、`CaseError`（状态不允许、不存在用 404）
  - 404 案件 / 图片序号不存在
  - 409 已有一轮手动巡查在进行
  - 502 `TiebaActionError`、`ImageFetchError`、`JudgeError`（消息经 `_redact` 脱敏）
  - 503 未配置账号凭据（网关未创建）
  - 500 其他异常：只回「服务器内部错误，详见 AstrBot 日志」，日志记脱敏后的 traceback
- 操作人记为 `f"webui:{request.username or 'unknown'}"`。
- 时间一律 Unix 秒（整数）。前端负责格式化。
- 所有外部来源的字符串原样返回，**前端必须用 `textContent` 渲染，禁止 `innerHTML` 拼接**。

## 3. 接口

### 3.1 `GET status`

```json
{
  "has_account": true,
  "patrol_enabled": true,
  "loop_running": true,
  "manual_running": false,
  "interval_seconds": 1800,
  "forums": ["龙"],
  "auto_delete": {"enabled": false, "threshold": 0.9},
  "report_threshold": 0.6,
  "pending": 3,
  "last_report": ReportDTO | null
}
```

### 3.2 `GET cases`

查询参数（全部可选）：`status`（CaseStatus 值，可重复传多个）、`fname`、`category`、`kind`（thread/post/comment）、`q`（在标题、正文、作者昵称、用户名中做子串匹配，≤ 50 字）、`offset`（≥0，默认 0）、`limit`（1–100，默认 20）。排序：`id` 倒序。

```json
{"total": 128, "items": [CaseDTO, ...]}
```

`status` 有三种写法，都接受，可以混用，按请求顺序合并去重：

- 重复键：`?status=pending&status=deleted`
- 数组键：`?status[]=pending&status[]=deleted`（axios 序列化 JavaScript 数组的默认方式）
- 逗号分隔：`?status=pending,deleted`（视图的 bridge 只能传字符串，前端用的是这种）

每个值 strip 后必须严格等于某个 `CaseStatus`（`pending` / `auto_deleted` / `deleted` / `banned` / `ignored` / `restored`），空段忽略，任一值非法返回 400；去重后的数量上限为枚举个数。

### 3.3 `GET cases/<case_id>` → `CaseDTO`

### 3.4 `GET cases/<case_id>/images/<index>`

`index` 从 0 开始，必须小于该案件的 `item.image_count`（且 ≤ 8）。后端用 `TiebaGateway.fetch_images([url], 1)` 现取（复用图床白名单、大小上限与超时），返回：

```json
{"data_url": "data:image/jpeg;base64,..."}
```

只能取数据库里该案件记录的图片 URL，不接受任何 URL 参数。进程内按 `case_id:index` 缓存，最多 64 张且 data URL 总字符数不超过 32 MB，超出时按最近最少使用淘汰；单张超过 4 MB 字符（`IMAGE_CACHE_MAX_CHARS // 8`）的照常返回但不缓存。

### 3.5 案件操作

| 接口 | 请求体 | 对应 |
|---|---|---|
| `POST cases/<case_id>/delete` | 无 | `CaseService.delete` |
| `POST cases/<case_id>/ban` | `{"days": 1 \| 3 \| 10}` | `CaseService.ban` |
| `POST cases/<case_id>/ignore` | 无 | `CaseService.ignore` |
| `POST cases/<case_id>/restore` | 无 | `CaseService.restore` |

成功返回更新后的 `CaseDTO`。`days` 必须是 JSON 整数（不接受字符串、布尔、浮点）。

### 3.6 `GET stats`

查询参数：`days`（1–90，默认 30），统计 `created_at >= now - days*86400` 的案件。

```json
{
  "since": 1790000000,
  "total": 120,
  "by_status": {"pending": 3, "auto_deleted": 0, "deleted": 40, "banned": 12, "ignored": 60, "restored": 5},
  "by_category": [
    {"category": "广告引流", "total": 50, "pending": 1, "auto_deleted": 0, "deleted": 30,
     "banned": 10, "ignored": 8, "restored": 1}
  ],
  "confidence_buckets": [
    {"low": 0.6, "high": 0.7, "total": 30, "confirmed": 10, "false_positive": 20}
  ],
  "review_failed": 2
}
```

- 「已确认违规」confirmed = deleted + banned + auto_deleted（未被恢复）；「误报」false_positive = ignored + restored。
- `confidence_buckets`：0.0–1.0 按 0.1 分档，只统计非「审查失败」案件，空档也返回。
- 「审查失败」案件（`verdict.category == "审查失败"`）单独计数 `review_failed`，不进 `by_category` 与分档。

### 3.7 `GET reports`

查询参数：`limit`（1–100，默认 20）。返回最近的巡查报告（新的在前）：`[ReportDTO, ...]`。

### 3.8 `POST patrol/run`

在后台启动一轮手动巡查，立即返回 `{"started": true}`；已有手动巡查在进行返回 409。QQ 指令 `/tb run` 与本接口共用同一个「手动巡查进行中」标志。巡查结束后报告照常写库。

### 3.9 `POST check`

请求体：`{"tid": <正整数>, "rules": <可选字符串，≤ 8000 字>}`。只审查不处置：按 tid 取主题帖（`get_thread_item_by_tid`），按配置下载图片，用 `rules`（为空则用配置里的吧规）判定。返回：

```json
{
  "item": ItemDTO,
  "verdict": VerdictDTO,
  "action": "none" | "report" | "auto_delete",
  "images_reviewed": 2,
  "images_total": 3
}
```

同一时间只允许一次试判（另一请求返回 409「已有一次试判在进行」）。

## 4. 存储新增（`core/store.py`）

```python
def query_cases(self, *, statuses: tuple[CaseStatus, ...] = (), fname: str = "",
                category: str = "", kind: ItemKind | None = None, q: str = "",
                offset: int = 0, limit: int = 20) -> tuple[int, list[Case]]
def case_stats(self, since: int) -> list[tuple[str, CaseStatus, float]]
    # 返回 (category, status, confidence) 三元组列表，聚合在 web 层做（数据量小，逻辑留在 Python 里更好测）
def save_report(self, report: PatrolReport) -> None
def list_reports(self, limit: int) -> list[PatrolReport]
```

- 条件全部参数化；`q` 用 `LIKE ? ESCAPE '\'`，转义 `%`、`_`、`\`。
- 报告表只保留最近 500 条（`save_report` 时裁剪）。
- 案件的 `item` 是 JSON 列；按 `fname` / `kind` / 标题正文过滤用 SQLite JSON 函数（`json_extract`），先确认 AstrBot 运行环境的 SQLite 自带 JSON1（3.38 起内置）；不可用时退化为 Python 侧过滤也可，但要在 docstring 说明。

## 5. DTO

```
CaseDTO = {
  "id", "status", "created_at", "handled_by", "handled_at", "note",
  "review_failed": bool,          # verdict.category == "审查失败"
  "item": ItemDTO, "verdict": VerdictDTO
}
ItemDTO = {
  "kind", "fname", "tid", "pid", "floor", "title", "text", "link",
  "image_count", "has_video", "create_time",
  "author": {"nick_name", "user_name", "portrait", "level", "is_bawu"}
}
VerdictDTO = {"violation", "category", "confidence", "reason", "ban_days"}
ReportDTO = {"started_at", "finished_at", "forums", "reviewed", "reported",
             "auto_deleted", "skipped", "deferred", "errors"}
```

`ItemDTO` 不返回图片 URL，图片只能经 §3.4 取。

## 6. 试判时临时替换吧规

`LlmJudge` 增加 `async def judge_with_rules(self, item, image_data_urls, rules: str) -> Verdict`（或等价的可选参数），用临时吧规构建系统提示，其余行为与 `__call__` 完全一致（图片缺失拒判、超时、错误包装）。不修改实例上的吧规。

## 7. 前端视图（`views/moderator/`）

- 原生 HTML + ES module + CSS，不引入框架或 CDN 依赖（离线可用、开源友好）。
- 通过 `window.AstrBotPluginView` bridge 调接口；`await bridge.ready()` 后再请求。
- 主题：CSS 变量，`[data-theme="dark"]` 覆盖；跟随 `bridge.onContext`。
- 四个标签页：
  1. **待处理**：卡片列表（`GET cases?status=pending`），每张卡片显示吧名、类型 / 楼层、作者、类别、置信度、理由、建议封禁、标题 + 正文、贴吧链接；图片按 `image_count` 懒加载（进入视口才请求 §3.4），**默认高斯模糊，点击切换显示**；按钮：删除、封禁（1/3/10 天下拉）、忽略；操作成功后从列表移除，失败显示错误。「审查失败」案件醒目标出，按钮同样可用。
  2. **历史**：表格 + 筛选（状态多选、吧、类别、类型、关键词）+ 分页；点击行展开详情（同卡片），已删除 / 已封禁的可恢复，自动删除的可封禁 / 恢复。
  3. **统计**：时间范围（7 / 30 / 90 天）；状态汇总数字；各类别的确认 / 误报堆叠条形；置信度分档的确认 / 误报堆叠条形（用 CSS 或内联 SVG 画，不引图表库）；「审查失败」计数；最近巡查报告列表（`GET reports`），错误行可展开。顶部给出一句提示：按当前上报阈值以上各档的误报率，评估是否适合开启自动删除。
  4. **试判**：输入 tid，可选展开一个文本框临时改吧规（默认填入提示「留空使用配置中的吧规」），提交后显示判定结果、按当前阈值的处置、图片审查张数。
- 顶部状态条：巡查是否运行、吧、间隔、自动删除开关、待处理数，按钮「立即巡查一轮」（`POST patrol/run`，进行中禁用，结束后通过轮询 `status` 刷新）。
- 文案中文，所有外部字符串用 `textContent`。
- 窄屏（≤ 600px）可用：卡片单列，表格可横向滚动。

## 8. 测试与验收

- 后端：`tests/test_store.py` 补新方法；`tests/test_web.py` 覆盖每个接口的成功、参数非法、权限 / 状态错误、贴吧失败、未配置账号；用假的 `request` 上下文（读 `astrbot/api/web` 实现决定怎么替身）。
- 前端：用一个本地 stub bridge 页面在浏览器里逐个标签页截图核对（亮 / 暗两套），再接真实 AstrBot 做一次端到端。
- 全量 `pytest`、导入自检、`ruff` 同 v0.1。
