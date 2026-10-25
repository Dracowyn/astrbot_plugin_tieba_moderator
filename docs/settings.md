# v0.3 设置迁入插件页：契约

面向维护者。v0.3.0 起，所有设置都在 WebUI 插件视图里编辑，存进插件自己的 SQLite，**不再使用 AstrBot 插件配置**（删除 `_conf_schema.json`）。设置分三块：全局设置（含各吧默认值）、吧务账号、吧。不同的吧可以用不同的吧务账号、吧规、推送会话、本吧吧务、审核模型、自动删除阈值、巡查范围和白名单。

不做迁移：v0.2 的 AstrBot 配置（`forums`、`forum_overrides` 等）被忽略，生产上重新录入。案件、标记、巡查报告等已有表保持不变。

数据类在 `core/settings.py`（契约，不要改字段；确需改动在最终回答里提出）。v0.2 的 `docs/forum-config.md` 中，§2 吧名匹配、§5 权限隔离继续有效；§1 配置与 §4 中依赖 AstrBot 配置的部分被本文取代。

## 1. 取值与继承

### 1.1 全局（`GlobalSettings`）

| 字段 | 默认 | 范围 |
|---|---|---|
| `patrol_enabled` | true | bool |
| `interval_seconds` | 180 | 60–86400 |
| `max_items_per_round` | 60 | 1–500，所有吧合计 |
| `judge_concurrency` | 2 | 1–8 |
| `judge_timeout_seconds` | 60 | 10–600 |
| `review_images` | true | bool |
| `max_images` | 4 | 0–9 |
| `sessions` | [] | ≤ 20 条，每条 1–512 字符，无控制字符 |
| `operators` | [] | ≤ 200 条，每条 1–64 字符，无空白和控制字符 |
| `defaults` | 见 1.2 | |

### 1.2 各吧选项（`ForumOptions` / `ForumOverrides`）

| 字段 | 默认 | 范围 |
|---|---|---|
| `rules` | v0.2 的默认吧规文本 | ≤ 8000 字符；默认值不能为空（全局），覆盖值为空串视为「沿用」并存成 `None` |
| `provider_id` | "" | ≤ 128 字符；"" 表示 AstrBot 默认对话模型；非空时必须是当前存在的提供商 id（保存时校验） |
| `auto_delete_enabled` | false | bool |
| `auto_delete_threshold` | 0.9 | 0–1 |
| `report_threshold` | 0.6 | 0–1，同一份载荷里两者都给出时必须 ≤ `auto_delete_threshold` |
| `thread_pages` | 1 | 1–5 |
| `lookback_hours` | 6 | 1–168 |
| `review_posts` / `review_comments` / `skip_top_and_good` / `skip_bawu` | true | bool |
| `whitelist`（仅全局默认与 `ForumEntry`） | [] | ≤ 1000 条，每条 1–128 字符 |

继承（`resolve`）：覆盖值为 `None` 用全局默认；`whitelist` 取全局与本吧的并集；`sessions` 本吧为空用全局；解析后 `report_threshold = min(report_threshold, auto_delete_threshold)`（全局改动可能让某吧的组合失效，解析时夹紧，不拒绝保存）。

### 1.3 账号（`Account`）

- `name`：1–32 字符，唯一（casefold 比较）。最多 20 个账号。
- `bduss`：1–512 字符，`stoken`：1–256 字符，均为可打印 ASCII 且不含空白。
- **只写不读**：任何接口、日志、报告、异常消息都不得出现凭据原文。读取只返回 `has_credentials`。
- 更新时 `bduss` / `stoken` 省略或为空串表示不改。
- 删除仍被吧引用的账号返回 409「仍有 N 个吧在使用该账号」。
- 存储为明文（与 v0.2 的 AstrBot 配置一致），README 写明。

### 1.4 吧（`ForumEntry`）

- `fname`：去首尾空白后 1–64 字符，不含控制字符，不以「吧」结尾时原样保存（以「吧」结尾时提示用户去掉；不自动改）。按 `casefold()` 唯一，重复 409。最多 50 个吧。
- `account_id`：可空；非空必须指向存在的账号。
- `sessions` / `operators` / `whitelist` 范围同全局。
- 吧巡查条件：`enabled` 且 `account_id` 非空。否则在状态里标出原因（「已停用」「未选择账号」）。

## 2. 存储（`core/store.py`）

新表（`CREATE TABLE IF NOT EXISTS`）：

- `settings_global(id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL, revision INTEGER NOT NULL)`：`GlobalSettings` 的 JSON。
- `accounts(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, bduss TEXT NOT NULL, stoken TEXT NOT NULL, created_at INTEGER, updated_at INTEGER)`。
- `forums(id INTEGER PRIMARY KEY AUTOINCREMENT, fname TEXT NOT NULL, fname_key TEXT NOT NULL UNIQUE, data TEXT NOT NULL, created_at INTEGER, updated_at INTEGER)`：`data` 是 `enabled`、`account_id`、`sessions`、`operators`、`whitelist`、`overrides` 的 JSON。

方法：`load_settings() -> SettingsSnapshot`（库里没有全局行时返回默认值）、`save_global(GlobalSettings) -> int`（返回新 revision）、`insert_forum` / `update_forum` / `delete_forum`、`insert_account` / `update_account` / `delete_account`。全部参数化，写操作在事务里，并让 revision +1。

## 3. 运行时（`services/settings_service.py`）

```python
class SettingsError(Exception): ...          # 消息为中文，可直接给用户看
class SettingsNotFound(SettingsError): ...   # → 404
class SettingsConflict(SettingsError): ...   # → 409（重名、账号被引用、上限）

class SettingsService:
    def snapshot(self) -> SettingsSnapshot
    def resolve(self, fname: str) -> ResolvedForum      # 未配置的吧返回 configured=False、account_id=None 的默认值
    async def save_global(self, value: GlobalSettings) -> SettingsSnapshot
    async def create_forum(self, draft: ForumDraft) -> ForumEntry
    async def update_forum(self, forum_id: int, draft: ForumDraft) -> ForumEntry
    async def delete_forum(self, forum_id: int) -> None
    async def create_account(self, draft: AccountDraft) -> Account
    async def update_account(self, account_id: int, draft: AccountDraft) -> Account
    async def delete_account(self, account_id: int) -> None
    async def bind_session(self, fname: str | None, session: str) -> BindResult | str   # /tb bind，语义同 v0.2
```

- `ForumDraft` / `AccountDraft`：已定义在 `core/settings.py`；`AccountDraft.bduss` / `stoken` 为 `None` 表示不改。由 `core/validation.py` 产出。
- 所有写操作串行（一把 `asyncio.Lock`），写库成功后替换内存快照，再调用 `on_change(old, new)` 回调。
- 引用完整性在服务层检查（账号存在、吧名唯一、上限），抛 `SettingsConflict` / `SettingsNotFound`。

网关池与网关新增方法（后端 A 实现，后端 B 的 `check` 接口调用）：

```python
class GatewayPool:                                   # services/gateway_pool.py
    def get(self, account_id: int) -> TiebaGateway | None
    def any(self) -> TiebaGateway | None             # 任一可用账号，试判取帖用
    async def close(self) -> None

@dataclass(frozen=True, kw_only=True)
class SelfInfo:                                      # services/tieba_gateway.py
    user_id: int
    portrait: str
    user_name: str
    nick_name: str

TiebaGateway.get_self_info() -> SelfInfo             # 凭据无效时抛 TiebaActionError
TiebaGateway.is_bawu(fname: str, me: SelfInfo) -> bool | None   # 查询失败返回 None
```

### 3.1 热生效（`main.py` 的 `on_change`）

- 改动立即生效，不需要重载插件。
- `patrol_enabled` / `interval_seconds` 变化：停止并按新值重启定时循环（不打断正在进行的一轮，下一次等待按新间隔）。
- 账号新增 / 凭据变化 / 删除：网关池（`services/gateway_pool.py`）按账号 id 维护 `TiebaGateway`。被替换或删除的网关在当前使用者结束后关闭（进行中的巡查轮、处置操作用开始时取到的网关直至结束）；插件 `terminate` 时全部关闭。
- 其余设置：巡查每轮开始时取一次快照，整轮使用同一快照；处置、推送、判定在调用时取当前快照。

### 3.2 各组件怎么用

- **巡查**：每轮遍历可巡查的吧（§1.4），用该吧账号的网关读取与自动删除，用该吧解析后的选项筛选与判定。读请求预算（200 / 轮）按**账号**分别计。`max_items_per_round` 与判定并发为全局。
- **判定**：`LlmJudge` 按 `item.fname` 取该吧的 `provider_id` 与吧规；系统提示按吧规文本缓存，不在每次判定时重建。
- **处置**：`CaseService` 按 `case.item.fname` 找当前配置的吧和账号网关；该吧未配置或没有账号时抛 `CaseError`「X 吧没有可用的吧务账号」。
- **推送**：按 `ResolvedForum.sessions`。
- **试判**：用任一可用账号的网关按 tid 取帖（没有账号返回 503），再按帖子所在吧的解析选项判定与给出处置建议；临时吧规为空时用该吧吧规。
- **权限**：`core/forums.py` 的 `ForumConfig` / `OperatorScope` 改为由快照构造（全局操作员 = `GlobalSettings.operators`，本吧吧务 = `ForumEntry.operators`），§5 的规则不变。
- **脱敏**：`_redact` 覆盖当前所有账号的凭据，以及本进程内曾加载过、已被替换的凭据。

## 4. Web API（新增，`web/settings_routes.py`）

全部需要 WebUI 登录（Dashboard 层负责），视为管理员。写操作用 POST（bridge 只有 `apiGet` / `apiPost`）。错误码同 `docs/webui-api.md` §2；校验失败 400，消息指明字段（如「interval_seconds 必须是 60 到 86400 的整数」）。

| 接口 | 请求 | 返回 |
|---|---|---|
| `GET settings` | | `SettingsDTO` |
| `POST settings/global` | `GlobalDTO`（完整对象） | `SettingsDTO` |
| `POST forums` | `ForumDraftDTO` | `SettingsDTO` |
| `POST forums/<id>` | `ForumDraftDTO`（完整对象） | `SettingsDTO` |
| `POST forums/<id>/delete` | | `SettingsDTO` |
| `POST accounts` | `{name, bduss, stoken}` | `SettingsDTO` |
| `POST accounts/<id>` | `{name, bduss?, stoken?}` | `SettingsDTO` |
| `POST accounts/<id>/delete` | | `SettingsDTO` |
| `POST accounts/<id>/check` | | `{"ok": bool, "user_name", "nick_name", "message", "forums": [{"fname", "is_bawu": bool \| null}]}` |
| `GET providers` | | `[{"id", "label", "supports_image": bool \| null}]` |

```
SettingsDTO  = {"revision", "global": GlobalDTO, "forums": [ForumDTO], "accounts": [AccountDTO]}
GlobalDTO    = GlobalSettings 各字段（列表用 JSON 数组；defaults 为 OptionsDTO）
OptionsDTO   = ForumOptions 各字段
ForumDraftDTO= {"fname", "enabled", "account_id": int|null, "sessions", "operators", "whitelist",
                "overrides": {ForumOverrides 各字段，null = 沿用}}
ForumDTO     = ForumDraftDTO + {"id", "created_at", "updated_at",
                "effective": OptionsDTO, "patrolled": bool, "problem": "" | "已停用" | "未选择账号"}
AccountDTO   = {"id", "name", "has_credentials": true, "forums": [fname...], "created_at", "updated_at"}
```

- 写接口按「完整对象」替换，未知字段 400（防拼写错误静默丢失）；JSON 类型严格（int 不接受 bool 和字符串，float 接受 int）。
- `check`：用该账号取自身信息，并对引用它的每个吧查是否为吧务（`is_bawu` 查不到为 null）。同一时间只允许一次检测（409）。失败只返回脱敏后的简短原因。
- `GET providers`：列出 AstrBot 已配置的对话提供商；`supports_image` 取提供商配置里的模态信息，拿不到为 null。

### 4.1 已有接口的变化

- `GET status`：`forums` 改为 `[{"fname", "patrolled", "problem", "account_name", "auto_delete": {"enabled", "threshold"}, "report_threshold"}]`；保留顶层 `auto_delete` / `report_threshold`，含义改为全局默认；新增 `"configured": bool`（有至少一个可巡查的吧）。`has_account` 改为「至少有一个账号」。
- `GET stats`：新增可选 `fname`（≤ 64 字符），只统计该吧。
- `POST check`：返回新增 `"fname"` 与 `"thresholds": {"auto_delete": {"enabled", "threshold"}, "report_threshold"}`（该吧解析后的值）。

### 4.2 实现时确定的口径

以下几条契约原文有歧义，实现按此执行：

- **账号更新的 `name` 必填**：`POST accounts/<id>` 与新增一样是完整对象，改名时也要带上名称；只有 `bduss` / `stoken` 可以省略或为空串表示不改。
- **`check.ok` = 无 false 且无 null**：只有所有被该账号引用的吧都确认是吧务时才为 `true`；某个吧查询失败（`is_bawu` 为 `null`）说明没查清，不算通过。
- **`effective.whitelist` 是并集**：`ForumDTO.effective` 是解析后的 `ForumOptions`，其中 `whitelist` 已经并入全局白名单，前端不要把它当成可在本吧单独编辑的列表（编辑请用 `ForumDraftDTO.whitelist`）。
- **本吧 `sessions` ≤ 20 条**：与全局推送会话同一个上限。
- **吧名以「吧」结尾的提示由前端负责**：后端按原样保存，不拒绝、不自动改写。
- **`modalities == []` 视为支持图片**：与 AstrBot 自身的判断一致（`astrbot/core/astr_main_agent.py:1372-1376`），空列表是历史配置的「未设置」状态。
- **`/tb bind` 的会话字符串与 Web 接口同一套字符规则**：1–512 字符，拒绝 C0/C1 控制字符、零宽字符与 bidi 覆盖字符。

## 5. 前端（`views/moderator/`）

新增第五个标签页「设置」，分三块：

1. **吧**：列表（吧名、账号、是否巡查 / 问题、独立设置的项数）+ 新增 / 编辑 / 删除。编辑表单：吧名、启用、账号（下拉，含「未选择」）、推送会话、本吧吧务、本吧白名单（多行文本，一行一条），以及每个可覆盖选项一行：「沿用全局（当前：X）」复选框，取消勾选后出现输入控件。模型用 `GET providers` 下拉，标出不支持图片的提供商。
2. **账号**：列表（名称、被哪些吧使用）+ 新增 / 编辑 / 删除 / 检测。凭据用 `type="password"` 输入框，编辑时始终为空，占位符「已设置，留空不修改」；不在任何地方显示凭据。检测结果逐吧显示是否吧务。
3. **全局**：巡查开关、间隔、每轮上限、并发、超时、图片审查与张数、默认推送会话、全局操作员，以及各吧默认选项。

- 保存后用返回的 `SettingsDTO` 整体刷新；400 把消息显示在表单顶部，不清空用户输入。删除前确认。
- 首次使用（`status.configured` 为 false）时，待处理页的空状态改为引导「先在设置里添加账号和吧」，带跳转按钮。
- 状态条按吧展示（吧名 + 问题标记），不再只显示一个全局自动删除开关。
- 统计页加吧筛选，提示句用所选吧的阈值（未选时用全局默认）。
- 安全要求同 v0.2（只用 textContent，链接与图片白名单）。

## 6. 文件归属

| 文件 | 归属 |
|---|---|
| `core/settings.py`（只追加 `resolve` 等纯函数，不改已有类）、`core/store.py`、`services/settings_service.py`、`services/gateway_pool.py`、`services/patrol.py`、`services/case_service.py`、`services/llm_judge.py`、`services/tieba_gateway.py`（新增取自身信息与查吧务的方法）、`core/forums.py`（改为由快照构造）、`main.py`、删除 `_conf_schema.json` | 后端 A |
| `core/validation.py`、`web/settings_routes.py`、`web/routes.py`（§4.1 变化）、Web 路由注册（在后端 A 完成后接入 `main.py`）、脱敏、安全复核 | 后端 B（安全） |
| `views/moderator/*` | 前端 |
| README、CHANGELOG（v0.3.0）、metadata、`docs/design.md` 链接本文 | 后端 A |
