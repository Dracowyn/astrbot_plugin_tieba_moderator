# 设计与模块契约

本文是 v0.1.0 的实现契约：模块边界、接口签名、行为规则。面向维护者；使用说明见 README。

## 1. 目标与边界

- 定时巡查配置的若干个吧：主题帖（标题 + 首楼）、楼层、楼中楼。
- 每条内容交给一个多模态 LLM，按吧务填写的吧规判断是否违规，图片一并审查。
- 处置分级：
  - 置信度 ≥ `auto_delete_threshold` 且开启了自动删除 → 直接删除，再通知吧务（可恢复）。
  - 置信度 ≥ `report_threshold` → 推送到吧务群，吧务用指令确认删除 / 删除并封禁 / 忽略。
  - 其余不处理。
- **封禁永远不自动执行**，只能由吧务确认；LLM 给出的封禁天数只是建议。
- 自动删除默认关闭，新装插件先只推送，吧务观察一段时间判定质量后再开启。
- 不做：网页爬取（网页端有百度安全验证）、循环封禁、多账号。

## 2. 依赖与平台

- 贴吧接口用 [aiotieba](https://github.com/lumina37/aiotieba)（客户端 protobuf 接口，不走网页，不受网页验证码影响）。
- LLM 调用用 AstrBot 的 `context.llm_generate(chat_provider_id=..., prompt=..., image_urls=..., system_prompt=...)`。
- 推送用 `context.send_message(umo, MessageChain)`；返回 False 表示没找到平台，要当失败处理。
- 插件内部一律相对导入；第三方依赖在模块顶层导入，不要 try/except 降级（AstrBot 靠导入失败触发按 requirements.txt 自动安装）。
- 数据库：`StarTools.get_data_dir()` 下的 `moderator.db`，SQLite WAL。

## 3. 目录与文件归属

| 文件 | 内容 |
|---|---|
| `core/models.py` | 共享数据模型、异常、`decide_action`。**契约文件，实现方不得修改**，需要改先提出 |
| `core/store.py` | SQLite 存储：去重标记、失败计数、案件 |
| `core/judge.py` | 纯逻辑：系统提示词、用户提示词、解析 LLM 输出为 `Verdict` |
| `services/tieba_gateway.py` | aiotieba 封装：拉取内容转 `ContentItem`、下载图片、删除 / 封禁 / 恢复 |
| `services/patrol.py` | 巡查编排：一轮巡查的完整流程 |
| `services/case_service.py` | 吧务对案件的操作：删除、封禁、忽略、恢复 |
| `services/llm_judge.py` | 调 AstrBot LLM 产生 `Verdict`，实现 `JudgeFn` |
| `main.py` | 插件入口：配置解析、后台任务、指令、权限、推送文案 |
| `_conf_schema.json` | 配置定义（键名见 §8） |

`core/` 不依赖 astrbot 和 aiotieba；`services/` 可以依赖 aiotieba，`llm_judge.py` 可以依赖 astrbot。

## 4. 存储 `core/store.py`

```python
class Store:
    def __init__(self, db_path: Path) -> None
    def close(self) -> None

    # 去重
    def get_thread_mark(self, tid: int) -> int | None      # 上次看到的 last_time；None=从未见过
    def set_thread_mark(self, tid: int, last_time: int) -> None
    def get_post_mark(self, pid: int) -> int | None        # 上次看到的楼中楼数 reply_num；None=从未见过
    def set_post_mark(self, pid: int, reply_num: int) -> None
    def is_seen(self, key: str) -> bool                    # key = ContentItem.key
    def mark_seen(self, key: str) -> None
    def record_failure(self, key: str) -> int              # 返回累计失败次数
    def purge(self, older_than: int) -> None               # 清理早于该时间戳的去重 / 失败记录

    # 案件
    def create_case(self, item: ContentItem, verdict: Verdict, status: CaseStatus, now: int) -> Case
    def get_case(self, case_id: int) -> Case | None
    def list_cases(self, status: CaseStatus, limit: int) -> list[Case]
    def count_cases(self, status: CaseStatus) -> int
    def transition(self, case_id: int, *, from_: tuple[CaseStatus, ...], to: CaseStatus,
                   by: str, now: int, note: str = "") -> Case | None
```

- `transition` 用单条 `UPDATE ... WHERE id=? AND status IN (...)` 做原子条件更新，没更新到行返回 None。两个吧务同时操作同一案件，只有一个成功。
- 案件里的 `item` / `verdict` 存 JSON，读回时还原成冻结数据类。
- 全部是同步方法（SQLite 本地调用很快）；调用方在协程里直接调用。

## 5. 贴吧网关 `services/tieba_gateway.py`

```python
@dcs.dataclass(frozen=True, kw_only=True)
class ThreadSnapshot:          # 帖子列表里的一行，用于决定要不要深入
    tid: int; fid: int; fname: str; title: str
    last_time: int; create_time: int; reply_num: int
    is_top: bool; is_good: bool; is_livepost: bool
    author: Author

class RequestBudgetExceeded(Exception): ...  # 本轮读请求额度已耗尽，区别于贴吧接口错误

class ReadBudget:
    def __init__(self, limit: int) -> None
    used: int                                  # 已发起的内容读取请求数
    remaining: int                             # 只读属性，剩余额度
    def consume(self) -> None                   # 无额度时抛 RequestBudgetExceeded

@dcs.dataclass(frozen=True, kw_only=True)
class PostBatch:
    pairs: tuple[tuple[ContentItem, int], ...] = ()   # 仅 create_time > since 的待审楼层
    caught_up: bool = True                          # 到达时间边界或帖子末页；False 表示页数截断
    comment_parents: tuple[tuple[ContentItem, int], ...] = ()  # 已取页面中用于楼中楼增量的父楼信息

class TiebaGateway:
    def __init__(self, bduss: str, stoken: str) -> None
    async def close(self) -> None
    async def list_threads(self, fname: str, pages: int, *, budget: ReadBudget | None = None) -> list[ThreadSnapshot]
        # 按回复时间排序；每页计一次读请求
    async def get_thread_item(self, snap: ThreadSnapshot, *, budget: ReadBudget | None = None) -> tuple[ContentItem, int]
        # ASC 首楼完整内容 + 首楼楼中楼数量；新帖即使 DESC 不含首楼也能检查首楼楼中楼
    async def get_thread_item_by_tid(self, tid: int) -> ContentItem          # 供 /tb check，标题与吧名取自响应
    async def get_new_posts(self, snap: ThreadSnapshot, since: int, max_pages: int,
                            *, budget: ReadBudget | None = None) -> PostBatch
        # DESC 从第 1 页向后取，直到 create_time <= since、末页或 max_pages 页
        # pairs 不包含时间边界及更旧的正文；1 楼若属于新增内容，以 THREAD 返回
        # comment_parents 仅供楼中楼计数检查，不把旧楼正文重新加入待审队列
    async def get_comments(self, post: ContentItem, reply_num: int, previous: int | None,
                           max_pages: int, *, budget: ReadBudget | None = None) -> tuple[list[ContentItem], bool]
        # 按新增数量从末页向前取最多 max_pages 个内容页；bool 表示是否覆盖所需增量
    async def fetch_images(self, urls: Sequence[str], limit: int) -> list[str]
        # 下载前 limit 张，返回 data URL（data:image/<type>;base64,...）；任一张失败抛 ImageFetchError
        # 只接受 http/https 且主机在贴吧图床白名单内的 URL（不是的话静默跳过）；
        # 自己用 aiohttp 流式下载：连接 / 读取超时共 20 秒，单张上限 5 MiB，最多 3 次重定向，
        # 不跟随重定向到白名单外的主机；需要审的图一张都取不到时同样抛 ImageFetchError
    async def delete(self, item: ContentItem) -> None        # 主题帖 del_thread，楼层 / 楼中楼 del_post
    async def restore(self, item: ContentItem) -> None       # recover_thread / recover_post
    async def block(self, item: ContentItem, days: int, reason: str) -> None
```

- 写操作失败抛 `TiebaActionError`，消息里带贴吧错误码和 errmsg。
- `ContentItem.text`：文字碎片按顺序拼接，链接渲染为 `文字(url)`，@ 保留为文字，表情忽略；视频置 `has_video=True` 并把封面加入 `image_urls`。
- 图片 URL 用 aiotieba 的 `src`（宽 720，体积小，够判定）。单张超过 5 MB 视为失败。
- portrait 去掉 `?t=` 后缀。
- `block` 的 `days` 只接受 `ALLOWED_BAN_DAYS`，其他值抛 `ValueError`。
- 楼层每页请求 30 条，以服务端分页信息判断末页。`since` 为该帖上次记录的 `last_time`，首次见到传 0；只把 `create_time > since` 的楼层作为新增待审条目。已获取页面中的父楼信息单独用于楼中楼增量检查，避免时间边界使旧楼的新楼中楼失去入口。若首楼被固定在 DESC 页头，其旧时间不应遮住同页较新的回复；不因父楼计数检查继续读取时间边界之后的历史页。DESC 是否始终包含首楼未经真机验证，未出现的旧父楼仍无法通过本轮已取页面发现增量。
- 楼中楼新增数量为 `max(0, reply_num - (previous or 0))`。先请求 ASC 第 1 页取得服务端 `page_size`、`total_page` 等分页信息，再从末页往前覆盖新增数量；计算必须考虑末页未满，避免新增内容跨两页却只取一页。第 1 页若也属于目标内容页，复用已取结果。最多返回 `max_pages` 个内容页；元数据探测页另计一次读请求，最多需要 `max_pages + 1` 次请求。分页大小不得猜测，跨页重复条目按 key 去重。
- `ReadBudget` 每轮独立创建，显式传入采集调用。在调用 aiotieba 的 `get_threads` / `get_posts` / `get_comments` 前逐次扣减，失败的调用也计数；预算不足时不再调用接口，而是抛 `RequestBudgetExceeded`。此处统计内容 API 的逐页调用，不统计 Client 初始化内部请求；图片下载、删除/恢复/封禁和未传本轮预算的 `/tb check` 不占该轮额度。


## 6. 巡查 `services/patrol.py`

```python
@dcs.dataclass(frozen=True, kw_only=True)
class PatrolSettings:
    forums: tuple[str, ...]
    thread_pages: int = 1
    lookback_hours: int = 6
    review_posts: bool = True
    review_comments: bool = True
    skip_top_and_good: bool = True
    skip_bawu: bool = True
    whitelist: frozenset[str] = frozenset()     # user_name 或 portrait
    max_items_per_round: int = 60
    review_images: bool = True
    max_images: int = 4
    judge_concurrency: int = 2
    auto_delete_enabled: bool = False
    auto_delete_threshold: float = 0.9
    report_threshold: float = 0.6
    max_failures: int = 3
    max_consecutive_judge_failures: int = 3   # 连续判定失败到此数即停止本轮审查
    max_tieba_requests_per_round: int = 200  # 每轮内容读取请求上限，0 表示不采集
    max_post_pages: int = 3                 # 每帖最多向后取的 DESC 楼层页数
    max_comment_pages: int = 3              # 每个父楼最多取的楼中楼内容页数

class Patrol:
    def __init__(self, *, gateway, store, judge: JudgeFn, notify: NotifyFn,
                 settings: PatrolSettings, clock: Callable[[], float] = time.time) -> None
    async def run_once(self) -> PatrolReport
```

一轮流程（每个吧依次处理）：

1. `list_threads`；跳过 `is_livepost`；开启 `skip_top_and_good` 时跳过置顶和精品。
2. 主题帖：`get_thread_mark(tid)` 为 None → 生成主题帖条目待审。
3. `last_time` 比记录的新（或首次见到）且开启 `review_posts` → 调 `get_new_posts(snap, since=max(旧标记或0, cutoff), max_pages=max_post_pages)`，其中 `cutoff = started_at - lookback_hours * 3600` 为本轮统一的回看边界，避免为窗口之外的旧楼层继续翻页；新增楼层未 `is_seen` 则待审。按 key 合并新帖 ASC 首楼与 DESC 结果，不重复审正文。结合已取页面的 `comment_parents` 检查父楼计数；开启 `review_comments` 且 `reply_num` 比 `get_post_mark` 大时，传入旧计数及 `max_comment_pages` 调 `get_comments`。父楼首次见到（`previous is None`）只检查末页楼中楼，不进行历史多页回溯，也不产生截断告警；已见过的父楼仍按新增数量多页回溯。未见过的楼中楼条目待审。仅对以前见过的帖子（`mark is not None`），楼层翻满仍未追平时记 `errors`：`帖子 <tid> 两轮之间新增楼层过多，较早的 N 页之外的新楼层未审查`；已见过的父楼发生楼中楼截断时，也记录带帖子、父楼 pid 和页数上限的告警。首次见到的帖子翻满上限不报「两轮之间新增过多」。
4. 过滤（命中的直接 `mark_seen`，计入 `skipped`）：`create_time` 早于 `now - lookback_hours`；作者在白名单；`skip_bawu` 且作者是吧务。
5. 待审条目超过 `max_items_per_round` 的部分不标记，留到下一轮，计入 `deferred`。
6. 审查（`asyncio.Semaphore(judge_concurrency)` 并发）：需要图片时先 `fetch_images`，再 `judge(item, data_urls)`。
   - `ImageFetchError` / `JudgeError` / 其他异常 → `record_failure`；未达到 `max_failures` 则不标记，下一轮重试。
   - 达到 `max_failures` → 不再静默丢弃：建一个 `PENDING` 案件交给吧务人工判断（`verdict.category = FAILED_CATEGORY`，`violation=False`，`confidence=0`，`reason` 写明失败次数与最后一次错误），`notify` 后 `mark_seen`。这样故意让图片下载失败也无法把违规内容挤出审查队列。
   - `JudgeError` 连续出现 `max_consecutive_judge_failures` 次（中途有一次成功即清零）→ 判定 `judge_tripped`，本轮剩余待审条目不计失败、不标记，直接顺延到下一轮（计入 `deferred`），避免模型故障时把整批内容的失败计数刷满。
   - 得到 `Verdict` → `decide_action`：
     - `AUTO_DELETE`：`gateway.delete` 成功 → 建 `AUTO_DELETED` 案件；删除失败 → 降级建 `PENDING` 案件，`note` 写失败原因。
     - `REPORT`：建 `PENDING` 案件。
     - 两者都调用 `notify(case)`；`notify` 抛异常只记入 `errors`，不影响案件。
     - 最后 `mark_seen`。
7. 某一帖已取得的所有条目处理完（没有因审查条数上限、模型熔断或失败而遗留）后，更新 `set_thread_mark` / `set_post_mark`。**翻页上限截断本身不阻止标记推进**，避免每轮从最新页重复翻取而一直占用预算；首次检查按有界采样静默建立标记，不承诺补审历史内容，后续已见过来源的截断增量通过告警披露，也不承诺下一轮补审。其他遗留仍不推进标记，下一轮重入时已 `is_seen` 的条目不会重审。
8. 单个吧或单个帖子抛出的异常记入 `errors` 后继续下一个，一轮不因单点失败中断。
9. 每轮末尾 `purge(now - 30 天)`。
10. 每轮创建独立的 `ReadBudget(max_tieba_requests_per_round)`。列表请求页数不超过当前剩余额度，保留已列出的帖子；额度耗尽后不再读取后续吧列表。采集途中预算耗尽时，当前帖子及后续已列出的可巡查帖子顺延，每帖计入一次 `deferred`，不推进对应帖子和父楼标记；已经采集完整的帖子仍可判定并正常推进标记。一轮仅记一次 `本轮贴吧请求达到上限，剩余帖子顺延`。尚未列出的吧无法得知剩余帖子数，不为它们虚构 `deferred` 数量。上述三个预算/页数设置使用代码默认值，不新增配置界面字段。


`run_once` 内部用 `asyncio.Lock`，手动触发与定时任务不会重叠（重叠时第二个调用等待前一个结束后直接返回同一份报告或新跑一轮均可，实现时二选一并在 docstring 写明）。

## 7. 案件操作 `services/case_service.py`

```python
class CaseService:
    def __init__(self, *, gateway, store, clock=time.time) -> None
    async def delete(self, case_id: int, operator: str) -> Case                 # PENDING → DELETED
    async def ban(self, case_id: int, days: int, operator: str) -> Case
        # PENDING → 先删除再封禁 → BANNED；AUTO_DELETED / DELETED → 只封禁 → BANNED
    async def ignore(self, case_id: int, operator: str) -> Case                 # PENDING → IGNORED
    async def restore(self, case_id: int, operator: str) -> Case                # AUTO_DELETED / DELETED / BANNED → RESTORED（不解封）
```

- 案件不存在、状态不允许、`days` 不在 `ALLOWED_BAN_DAYS` → 抛 `CaseError(中文提示)`。
- 四个方法在同一把 `asyncio.Lock` 内执行（读案件 → 调贴吧 → 条件迁移），两个吧务同时操作同一案件时，后到者读到新状态后直接报 `CaseError`，不会重复调用贴吧。
- 先调贴吧，成功后再 `transition`；`transition` 返回 None → 抛 `CaseError`（锁外的写入者，例如将来的 WebUI，仍由条件更新兜底）。贴吧失败抛 `TiebaActionError`，状态不变。
- `ban` 中删除成功、封禁失败：状态转为 `DELETED`，`note` 记封禁失败原因，再抛 `TiebaActionError`。
- 封禁理由：`f"{verdict.category}：{verdict.reason}"` 截断到 50 字。

## 8. 配置键（`_conf_schema.json`）

```
account.bduss, account.stoken                       # 吧务账号凭据
forums                                              # list，吧名
patrol.enabled, patrol.interval_seconds (默认 180，最小 60)
patrol.thread_pages, patrol.lookback_hours, patrol.max_items_per_round
patrol.review_posts, patrol.review_comments, patrol.skip_top_and_good, patrol.skip_bawu
patrol.whitelist                                    # list
judge.provider_id (select_provider), judge.rules (text)
judge.review_images, judge.max_images, judge.concurrency, judge.timeout_seconds
action.auto_delete (默认 false), action.auto_delete_threshold (0.9), action.report_threshold (0.6)
notify.sessions                                     # list，unified_msg_origin
notify.operators                                    # list，除 AstrBot 管理员外可处理案件的发送者 ID
```

## 9. 指令（`/tb`）

| 指令 | 作用 |
|---|---|
| `/tb status` | 巡查状态、上一轮报告、待处理数 |
| `/tb run` | 立即巡查一轮 |
| `/tb list` | 列出待处理案件 |
| `/tb show <id>` | 案件详情 |
| `/tb del <id>` | 确认删除 |
| `/tb ban <id> <1\|3\|10>` | 删除并封禁 |
| `/tb ignore <id>` | 判为误报 |
| `/tb restore <id>` | 恢复被删内容 |
| `/tb check <tid>` | 只审查不处置，用于调试吧规 |
| `/tb bind` | 把当前会话加入推送列表 |

除 `status` 外全部要求操作者是 AstrBot 管理员或在 `notify.operators` 里。

## 10. v0.2 计划：WebUI 插件视图

AstrBot ≥ 4.29 的插件视图：`views/<name>/index.html` 经 bridge 调用 `context.register_web_api` 注册的接口，鉴权由 Dashboard 负责。v0.1 后端终审通过后开工，Web API 契约届时补到本节。

- 待处理队列：看图（插件后端代理取图，默认模糊、点击显示）、LLM 理由，一键删除 / 封禁 / 忽略（复用 `CaseService`，与 QQ 指令同一份逻辑）。
- 历史案件与处理记录，可按状态 / 吧 / 类别筛选。
- 统计：各类别误报率（IGNORED 占比）、置信度分布、巡查报告，用于决定何时开启自动删除及阈值。
- 试判：输入 tid 或临时吧规文本，只判定不处置。

## 11. 测试

- 在插件目录用 AstrBot 的 `.venv` 跑 `pytest -q --cov=astrbot_plugin_tieba_moderator --cov-report=term-missing tests`（测试按包名导入，`--cov` 也要写包名）；`core/` 与 `services/` 各模块行覆盖 ≥ 80%。
- 在插件目录跑测试时导入 astrbot 会生成 `data/`，已在 `.gitignore` 中忽略。
- aiotieba 一律用假对象替身，不发真实网络请求。
- 导入自检在 AstrBot 根目录执行：`__import__("data.plugins.astrbot_plugin_tieba_moderator.main")`。
