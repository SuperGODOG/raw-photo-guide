# RAW Photo Guide (`raw-photo-guide`, v0.6.8)

通过 RawTherapee 5.13 处理 RAW/JPEG，提供结构化参数、Session、试片与可复现存档。**审美交给用户：用户决定效果、可接受的代价和定稿版本；Agent 负责技术执行、看图复查和呈现差异。**

## 工作方式

1. 先确认用途、主体、视觉方向与保持项。主体可见度不明确时，用“纹理清楚 / 弱细节 / 轮廓”等具体结果沟通，已有答案不重复问。
2. 按用户目标建立感知基线。中性 partial PP3 不保证相机直出外观，也不等于纯线性输出；剪影、朦胧、偏色都可以是目标，不一律提亮或纠色。
3. 每轮实际打开预览，细节用完整图 100% 局部复查。QC 数字用于提示，不决定审美。目标冲突时按用户明确的优先级执行；优先级未知时展示取舍供选择。
4. 保存用户原话、当前需求、Keep/Change 与用户选中版的实际路径。新增保持项与旧项合并，方向变化同步更新需求。最新渲染不等于用户认可，代理复查不等于审美定稿；明确代选授权按其范围执行。

完整交互、旧版恢复和选版记录见 [dialogue.md](references/dialogue.md)；执行检查见 [raw-workflow.md](references/raw-workflow.md)。Agent 入口为 [SKILL.md](SKILL.md)，按需阅读技术文档。

## 能力与边界

- `scripts/rt_tool.py` 是 JSON-over-CLI 工具，stdout 为 `{ok,result}` 或 `{ok:false,error}`，不是已注册的 MCP 服务。
- 14 类受控参数以 `capabilities` / `--schema` 为准；常规只查 summary，需要调参再取对应类别的 full 定义。没有局部笔刷或生成式消除接口。
- RAW/JPEG 原片只读。JPEG 已烘焙，TIFF16 不恢复源中缺失的信息；中性 Base 与未标定 LCP 都不代表经过相机/镜头画质校准。
- Session 提供意图记录、参数继承与 CAS 版本检查；审美选版记录使用现有文本字段，不是自动偏好识别。`prune_history` 精简明确选中的已提交旧图片，`clean` 清理未提交孤立产物；均须有清理授权并保护用户选中版。

## 运行环境

结构化调整要求 RawTherapee 5.13；先运行 doctor 查看实际引擎与依赖状态。ExifTool 用于 inspect，macOS 缺失时可用 `brew install exiftool`。运行 Python 工具与测试所需的依赖及错误提示由脚本提供；可用 `uv run --script` 在隔离环境运行带依赖声明的脚本。

## 调用示例

在 Skill 目录下运行。`--request` 只接受 UTF-8 JSON **文件路径**或 `-`（stdin），不接受直接跟随的 JSON 字符串。将下面某个请求保存为项目中的 request.json，再运行：

```bash
python3 scripts/rt_tool.py --request /绝对路径/request.json
```

也可从同一文件通过 stdin 传入：

```bash
python3 scripts/rt_tool.py --request - < /绝对路径/request.json
```

以下路径与全零哈希是占位值；执行时替换为用户真实文件和工具返回值。示例风格和参数仅说明契约，不是所有照片的默认配方。每次只执行当前步骤的请求，检查 ok 后继续。

### 1. 检查环境和能力

```json
{"action":"doctor"}
```

```json
{"action":"capabilities","source_type":"raw","detail":"summary"}
```

JPEG 源将 source_type 改为 jpeg。缺依赖按 doctor 提示处理。

### 2. 获取 Base 的真实路径与哈希

```json
{"action":"profile","profile":"builtin:neutral-rec2020-rt513","kind":"pp3"}
```

保存返回的 path 和 sha256；render 中使用同一 Base 与真实哈希。

### 3. 用户确认目标后初始化 Session

```json
{
  "action":"session",
  "operation":"init",
  "session_path":"/用户项目/session.json",
  "source":"/用户项目/input.NEF",
  "base_profile":"builtin:neutral-rec2020-rt513",
  "confirmed_brief":"社交分享；用户希望树干有弱细节、环境保留夜色；若细节与夜色冲突，优先夜色。",
  "keep":["现场暖光"],
  "change":["按已确认目标调整树干可见度"]
}
```

成功返回 revision=0；init 不覆盖已有 Session。只有用户确实表达过这些目标才使用该 brief。

### 4. 绑定会话生成试片

```json
{
  "action":"render",
  "source":"/用户项目/input.NEF",
  "profile":"builtin:neutral-rec2020-rt513",
  "profile_sha256":"0000000000000000000000000000000000000000000000000000000000000000",
  "engine_version":"5.13",
  "output_dir":"/用户项目/output",
  "format":"jpeg",
  "session_path":"/用户项目/session.json",
  "expected_revision":0,
  "adjustments":{"exposure_ev":0.25}
}
```

engine_version 使用 doctor 验证过的版本；profile_sha256 使用步骤 2 的实际值。绑定渲染也必须提供完整的源、Base、哈希和输出参数。曝光值需根据该图目标选择，不能把示例值当通用修图策略。

成功后用 result.preview.path 展示预览、result.output 获取完整图；后续 expected_revision 使用最新 Session revision。实际看图后说明变化与代价，等待用户反馈或按已有授权继续。

### 5. 新增保持项时保留旧项

假设用户实际回复如下，且上一轮 render 成功使 revision=1：

```json
{
  "action":"session",
  "operation":"record_feedback",
  "session_path":"/用户项目/session.json",
  "expected_revision":1,
  "user_feedback":"暖光保持，当前反差也保留，树干再清楚一点，但仍然优先夜色。",
  "keep":["现场暖光","当前反差"],
  "change":["在夜色优先的前提下再增加树干可见度"]
}
```

keep 是全量列表，不能只传“当前反差”丢掉旧暖光要求。用户改变方向时还须更新 confirmed_brief；本例保留既有目标。反馈仅记录意图不会自动调参，下一次 render 由 Agent 翻译成受控参数，并绑定反馈返回的新 revision。

### 6. 恢复会话

```json
{"action":"session","operation":"load","session_path":"/用户项目/session.json"}
```

从返回的 session/history 恢复用户选版和当前目标。继续旧选中版须按 dialogue.md 恢复完整参数快照，不直接沿用最新探索参数。导出 ICC、元数据与清理见 [export-and-retention.md](references/export-and-retention.md)；全部 action 见 [tool-api.md](references/tool-api.md)。

## 验证与资产

```bash
uv run --script scripts/test_rt_tool.py
```

当前套件验证接口、状态、故障路径与 README/references 请求契约；合成渲染测试不证明照片审美符合用户意图。真实 NEF 集成测试单独使用 scripts/smoke_raw.py，调用方式见 tool-api.md。

- 中性 Base：[neutral-rec2020-rt513.pp3](assets/neutral-rec2020-rt513.pp3)，是起始配置，不是 Nikon 标定风格。
- 唯卓仕 LCP 缺少标定依据，只供用户明确要求的实验，规则见 [lens-profiles.md](references/lens-profiles.md)。
- 项目协议：[MIT License](LICENSE)。
