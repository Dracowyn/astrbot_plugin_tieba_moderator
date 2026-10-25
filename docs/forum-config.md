# 按吧配置：契约

面向维护者。v0.2.0 起，不同的吧可以用不同的吧规、推送到不同的会话、由不同的吧务处理。做法是「全局默认 + 按吧覆盖」：原有配置项保持不变，作为所有吧的默认值；新增的 `forum_overrides` 只写需要和默认不同的吧，留空的字段沿用全局。

## 1. 配置

`_conf_schema.json` 在 `forums` 之后新增：

```json
"forum_overrides": {
  "description": "按吧单独设置",
  "type": "template_list",
  "hint": "某个吧的吧规、推送群或吧务和全局设置不同时，在这里加一条。这里出现的吧会自动加入巡查，不必再写进「巡查的吧」。",
  "templates": {
    "forum": {
      "name": "吧",
      "display_item": "fname",
      "items": {
        "fname":     {"description": "吧名", "type": "string", "default": "", "hint": "不带「吧」字。"},
        "rules":     {"description": "本吧吧规", "type": "text", "default": "", "hint": "留空沿用「AI 审核设置」里的吧规。"},
        "sessions":  {"description": "本吧推送会话", "type": "list", "default": [], "hint": "留空推送到「推送与权限」里的会话；填了就只推送到这里。可在群里发 /tb bind <吧名> 添加。"},
        "operators": {"description": "本吧吧务", "type": "list", "default": [], "hint": "只能查看和处理本吧案件的用户 ID。全局「可处理案件的用户 ID」与 AstrBot 管理员仍可处理所有吧。"}
      }
    }
  }
}
```

AstrBot 保存后的值是 `list[dict]`，每项带 `__template_key: "forum"`。配置完整性检查不校验列表元素类型，所以解析时必须容错：

- 非 dict 的元素、`fname` 去空白后为空的元素：跳过并记 warning。
- `rules` 非字符串按空处理；`sessions` / `operators` 非列表按空处理，列表元素一律 `str(x).strip()`，丢弃空串，保序去重。
- 同一个吧出现多次：只用第一条，其余记 warning。

## 2. 吧名匹配

吧名比较一律用 `fname.strip().casefold()` 作为键（贴吧吧名不区分大小写，接口返回的写法可能和配置不同）。展示时用配置里的原始写法。不做 Unicode 归一化（NFKC）：零宽字符、全角字母会让两个名字视为不同的吧（结果是匹配不上，不会误配到别的吧）。吧名请手动输入，不要从帖子里复制。

巡查的吧 = `forums` 与 `forum_overrides` 中的吧名，`forums` 在前，按键去重保序。

## 3. 解析结果（`core/forums.py`）

```python
@dataclass(frozen=True, kw_only=True)
class ForumProfile:
    fname: str                      # 配置里的写法
    rules: str                      # 已回落：本吧为空时等于全局吧规
    sessions: tuple[str, ...]       # 已回落：本吧为空时等于全局推送会话
    operators: frozenset[str]       # 本吧吧务（不含全局 operators）

@dataclass(frozen=True, kw_only=True)
class ForumConfig:
    forums: tuple[str, ...]                 # 巡查的吧（§2）
    default_rules: str
    default_sessions: tuple[str, ...]
    global_operators: frozenset[str]
    profiles: Mapping[str, ForumProfile]    # 键是 casefold 后的吧名，只含 forum_overrides 里的吧

    def profile(self, fname: str) -> ForumProfile   # 没有覆盖的吧返回用全局值构造的 profile
```

`parse_forum_config(config) -> tuple[ForumConfig, list[str]]` 返回解析结果和 warning 文案（英文，由 main 记日志）。纯函数，不读写文件。

## 4. 行为

| 场景 | 规则 |
|---|---|
| 审查 | `LlmJudge` 按 `item.fname` 选吧规。各吧的系统提示在构造时建好，不在每次判定时重建。 |
| 试判（`/tb check`、`POST check`） | 临时吧规为空时用该帖所在吧的吧规（帖子取回后才知道吧名）。 |
| 推送 | 按 `case.item.fname` 取 `profile.sessions`；为空时报错文案带吧名：「案件 #N（X 吧）未推送：没有绑定推送会话」。 |
| `/tb bind` | 不带参数：同 v0.1，加入全局推送会话。`/tb bind <吧名>`：加入该吧的 `sessions`；吧名必须是正在巡查的吧，否则拒绝；该吧没有覆盖条目时新建一条（`__template_key: "forum"`，其余字段为空）。保存配置后重新解析，内存里的 `ForumConfig` 立即生效。仅 AstrBot 管理员。 |
| WebUI | 能打开插件视图的人视为管理员，看全部吧。`GET status` 的 `forums` 返回巡查的吧（§2）。 |

## 5. 权限（按吧隔离）

三类人：

- **全局操作员**：AstrBot 管理员，以及 `notify.operators` 里的用户。可处理所有吧。
- **本吧吧务**：出现在某个吧 `operators` 里、但不是全局操作员的用户。只能处理这些吧的案件。
- 其他人：无权限。

| 指令 | 全局操作员 | 本吧吧务 |
|---|---|---|
| `/tb status` | 可以，报告含错误明细 | 可以，报告不含错误明细；待处理数只算自己负责的吧 |
| `/tb run` | 可以 | 不可以（一轮覆盖所有吧） |
| `/tb list` | 所有吧的待处理案件 | 只列自己负责的吧，计数也只算这些吧 |
| `/tb show / del / ban / ignore / restore <编号>` | 可以 | 案件所在吧必须是自己负责的吧 |
| `/tb check`、`/tb bind` | 仅 AstrBot 管理员（不变） | 不可以 |

- 无权限的人执行 `list` / `show` / 处置指令一律回「没有权限」，不回「没有待处理的案件」。`/tb bind` 的吧名参数超过 64 字直接拒绝。
- 本吧吧务对不属于自己的案件，回复与「案件不存在」相同的文案「案件 #N 不存在或不在你负责的吧。」，不泄露其他吧有哪些案件。全局操作员查不存在的案件仍回「案件 #N 不存在。」
- 吧名判断用案件记录里的 `item.fname`（巡查时由贴吧接口写入，之后不变），按 §2 的键比较。
- 存储层为 `/tb list` 提供按多个吧过滤：`query_cases` 增加 `fnames: tuple[str, ...] = ()`，非空时匹配 `lower(json_extract(item, '$.fname')) IN (...)`，调用方传 `fname.strip().casefold()`。SQLite 的 `lower()` 只转换 ASCII：中文与 ASCII 吧名没有问题；含非 ASCII 大写字母的吧名（如 `Ä`）在 `/tb list` 里可能漏列本吧案件（只会少列，不会多列），已知限制，不处理。

## 6. 兼容

- 现有配置（只有 `forums`、全局 `rules` / `sessions` / `operators`）行为完全不变。
- 不迁移、不改写用户已有的配置项。
