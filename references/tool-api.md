# RawTherapee 本地工具 API v0.6.8

这是可执行的 JSON-over-CLI 接口，不是仅供模型阅读的参数示意，也不是已注册的 MCP 工具。宿主通过 shell 调用 `scripts/rt_tool.py`。不需要 OpenAI API key 或 HTTP 服务。RawTherapee 的实际后端是 CLI + PP3；不假定存在 REST API。

## 调用方法

以下路径以公共安装位置为例；迁移后以当前 SKILL.md 所在目录为准：

```bash
python3 "$HOME/.cc-switch/skills/raw-photo-guide/scripts/rt_tool.py" --schema
python3 "$HOME/.cc-switch/skills/raw-photo-guide/scripts/rt_tool.py" --request /绝对路径/request.json
```

也可 `--request -` 从 stdin 接收 JSON。将 JSON 作为文件或 stdin 数据传入，不插入 shell 字符串。`--schema` 返回真实输入契约，与校验器共用字段定义；静态副本见 [tool-schema.json](tool-schema.json)。新增字段须修改实现并重新导出 schema。

`adjustments` 的 14 类嵌套字段已完整展开为 Schema，支持增量 patch；字段名、类型、范围与枚举来自与 `capabilities` 共用的参数定义。Schema 验证不替代继承后的完整性校验、曲线单调性、RAW/JPEG 兼容性、资源存在性和哈希检查。调整项的字符串枚举不区分大小写，写入 Session、manifest 和 PP3 前归一为能力查询所列的规范形式；字段名与 action/operation 仍区分大小写。整数槽位接受数学上的整数（如 15.0），统一转为 int；拒绝小数、布尔、数字字符串与非有限值。`step` 为建议粒度，`default` 为起始参考，均不表示缺省自动注入。

### 维护测试与依赖

完整测试依赖 Pillow、NumPy、jsonschema，已在测试脚本内声明。优先用隔离环境执行：

```bash
uv run --script "$HOME/.cc-switch/skills/raw-photo-guide/scripts/test_rt_tool.py"
```

已安装三项依赖的 Python 也可直接执行该脚本。若缺少依赖，脚本在运行任何测试前非零退出，列出缺项及同一 Python 的安装命令；不会跳过契约测试后声称全通过。`jsonschema` 是测试依赖，不是核心工具的运行依赖；工具仍用随附校验器执行请求。真实 raw_qc 的可选 rawpy/NumPy 依赖另行检查。

成功 stdout：`{"ok":true,"result":{...}}`，退出码 0。  
失败 stdout：`{"ok":false,"error":{"code":"REVISION_CONFLICT","message":"..."}}`，退出码 1。  
RawTherapee 日志不混入 JSON。ExifTool 成功响应只从 stdout 解析，stderr 非致命警告不拼入元数据 JSON；非零退出仍携带输出与错误信息。doctor 调用成功只表示检查完成，仍需检查 result.engine.error。

---

## 接口操作概览 (10 Actions)

| action | 必填输入（除 action 外） | 主要返回 |
|---|---|---|
| **doctor** | 无 | `tool_version` (0.6.8), `architecture`, `engine` (path/version 或 error), `exiftool`, `pillow`, `rawpy`, `numpy`, `raw_qc_available`, `raw_qc_install_hint` |
| **capabilities** | 可选 `source_type` ('raw' 或 'jpeg')、`detail` (summary/full，默认 full)、`parameters`（非空唯一类别数组） | 支持的动作、可用类别或选定参数定义（依 detail/parameters）、输入限制与单机并发建议 |
| **inspect** | `source`；可选 `lens_check`（布尔，默认 false） | 绝对规范路径、`source_type` ('raw' 或 'jpeg')、SHA-256、ExifTool 元数据；启用 lens_check 时附带镜头名称候选与提示；对 JPEG 附带 `has_icc`、`icc_description`、`orientation`；缺依赖明确报错 |
| **profile** | `profile`；可选 `kind` ('pp3', 'dcp', 'icc', 'lcp'，默认 'pp3') | 路径、SHA-256、类型、大小；PP3 返回显式工作/输出空间与曝光可修改性；ICC/ICM 需显式 kind=icc，返回指纹及 icc 描述/数据空间/设备类别；其他外部配置返回完整性信息 |
| **session** | `operation` ('init' / 'load' / 'record_feedback')、`session_path` 及各分支专属字段（详见下文） | 会话状态、当前 `revision`、源与模板哈希、确认需求、keep/change 快照、历史条数、当前有效参数 `effective_adjustments` |
| **raw_qc** | `source`（本版仅限 Nikon `.nef`）；可选 `roi` ({x, y, width, height} 原始未旋转可见 CFA 坐标) | 解码器版本、未旋转可见 CFA 尺寸与布局、各 Bayer 通道（R, G1, B, G2）统计（点数、极值、均值）、黑电平/白电平及依据、饱和溢出比例与低黑电平比例、技术免责与边界声明 |
| **render** | `source`, `profile`, `profile_sha256`, `engine_version` ('5.13'), `output_dir`, `format` ('jpeg' 或 'tiff16')；绑定 session 时必填 `session_path` 与 `expected_revision`；可选 `adjustments`、`exposure_ev`、`reset_adjustments`、`reset_fields`、`timeout_seconds`、`output_profile`、`preview_metadata`、`threads` | 成片路径、manifest 路径、`preview` 元数据与 `warnings`、`source_type`、QC、`session_revision`；竞争中返回 `RENDER_IN_PROGRESS` |
| **qc** | `image` | 尺寸、嵌入 ICC 描述/哈希、亮度和彩色统计、需视觉复查标记（基于 Pillow 栅格解码） |
| **prune_history** | session_path、expected_revision、manifest_paths；可选 apply（默认 false）、protect_session_paths（允许空数组） | 旧图片候选、逻辑字节数、删除结果/错误、Session revision；重复已完成名单返回 no_op 且不推进版本；只删明确选中旧栅格，保留溯源并保护最新成片 |
| **clean** | `output_dir`；互斥的 `session_path`（单会话）或 `session_paths`（多会话数组，元素唯一）；可选 `apply` (默认 false), `min_age_seconds` (默认 1800), `preflight_only` (默认 false) | 孤立产物候选目录列表、释放字节数、跳过目录及原因、是否已应用物理删除 |

输入文件支持 RAW 扩展名（`.nef`, `.nrw`, `.dng`, `.arw`, `.cr2`, `.cr3`, `.raf`, `.orf`, `.rw2`, `.pef`）及大小写不敏感的 JPEG（`.jpg`, `.jpeg`, `.JPG`, `.JPEG`）。Pillow 会严格验证 JPEG 文件头并完整解码数据，拒绝伪装扩展名及损坏截断图。不扩展至 HEIC/TIFF 输入。

---

未标定的内置 LCP 返回 UNVERIFIED_LENS_PROFILE（profile / render / manifest）；仅供显式实验，不自动推荐。inspect 的 experimental_profile_available 与 Lensfun 扫描独立；builtin_profile_available 留给可正式推荐的候选。缺失或指纹不符时不报告可用。已有 LCP 模式下只更新内置别名 path 可继承 mode 并更新哈希；首次或 reset 后仍须 mode，显式错误哈希拒绝。

LCP 的来源核对、现有 API 请求示例及效果验证层级见 [外置 LCP 接入与验收](lens-profiles.md)。profile(kind=lcp) 仅核对文件存在、后缀并计算哈希，不解析 XML 或校验镜头适配。

导出参数不属于 adjustments：output_profile={path,sha256}、preview_metadata=strip/capture/capture_camera、threads=1–256。完整字段见 capabilities.export_parameters，来源/快照/嵌入检查与历史精简示例见 [导出与保留](export-and-retention.md)。error.details 为可选结构化恢复信息，旧 code/message 保持。 JPEG 的实际 ICC 描述未识别为常用 sRGB 时，返回 JPEG_DELIVERY_PROFILE_REVIEW 供交付检查；不静默改色域。

## 按需发现参数

首次用摘要列出当前输入格式可用类别，避免读取全部参数；只有本轮需要调整的类别才取完整定义：

```json
{"action":"capabilities","source_type":"raw","detail":"summary"}
```

```json
{"action":"capabilities","source_type":"raw","detail":"full","parameters":["white_balance","tone_curve"]}
```

摘要省略 controllable_parameters，返回 available_adjustments、引擎版本、structured_adjustments_available、输入限制、runtime_limits，以及携带 path/sha256 的 output_profiles。输出 ICC 指纹可直接传入 render.output_profile；损坏文件仍须由 profile(kind=icc)/render 检出，不把指纹等同解析通过。完整查询兼容旧调用，按 parameters 筛选；JPEG 不提供 RAW 专属类别，显式请求不兼容类别报 UNSUPPORTED_ADJUSTMENTS。参数定义来自同一声明式来源，不需为普通修改再通读全部控制规范。

runtime_limits 推荐单机同时一个 render，cross_session_throttle_enforced=false；门禁只保护同一 Session。输出目录随机唯一不能代替机器资源节流。外层批处理顺序运行，实测 CPU/内存余量足够后才考虑两个，不按核心数开进程。

## 空间预检、超时与失败回收

render 在会话门禁及状态校验后、创建成片目录与启动显影前，检查输出所在文件系统至少有 500 MiB（524288000 字节）可用空间；输出目录尚未创建时检查最近的已存在祖先。低于阈值返回 INSUFFICIENT_DISK_SPACE，不启动显影、不推进 Session；查询空间失败返回 DISK_CHECK_FAILED。manifest.disk_preflight 记录检查路径、可用字节和阈值。

这是最低余量检查，不是空间预留或对任意尺寸 RAW 的容量保证。直接写文件遇 ENOSPC/EDQUOT 同样返回空间错误；原有引擎/Session 错误保持自身错误码。资源快照、工作目录与预览失败均进入同一回收边界；若回收也失败，保留原错误码并附残留目录路径与原因，不用二次清理错误覆盖原故障。只有完整 manifest 已生成但 Session 未成功提交时保留可审计孤立产物。

可选 timeout_seconds 为整数 30–3600，默认 300；慢机器或大图可显式增加等待上限，不能设无限等待。此参数是调用层控制，不是照片调整项；超时错误仍为 TIMEOUT。

```json
{
  "action":"render",
  "source":"/absolute/input.NEF",
  "profile":"builtin:neutral-rec2020-rt513",
  "profile_sha256":"0000000000000000000000000000000000000000000000000000000000000000",
  "engine_version":"5.13",
  "output_dir":"/absolute/output",
  "format":"tiff16",
  "timeout_seconds":600
}
```

示例哈希须用 profile 动作返回的真实值替换。

## 真实 NEF 冒烟测试（独立于常规合成测试）

`scripts/smoke_raw.py` 要求显式指定真实 NEF 与输出父目录，不内置私人原片、不自动下载样张、不使用 mock。它创建新的隔离工作区，运行真实 RT 5.13 导出 JPEG/TIFF16 与预览，核对 ICC、位深标签、尺寸、manifest/Session 哈希和原片不变，再对该原片的截断副本验证拒绝路径；原片始终只读。

```bash
uv run --script /实际技能目录/scripts/smoke_raw.py --source /绝对路径/真实照片.NEF --output-dir /绝对路径/测试输出
```

产物及 smoke-report.json 保留在新工作区。缺样片、缺依赖、引擎版本不符或任一断言失败均非零退出，不伪报通过。常规测试套件不依赖插着 SD 卡；真实测试需单独运行和报告，不能把合成测试数量当真 RAW 覆盖率。该冒烟验证解码、导出和状态完整性，不评定审美、暗部 banding 或所有损坏类型。

## 预览与输出返回

每次 render 除完整 image.jpg/image.tif 外，生成 preview.jpg，result.preview 返回 path、sha256、width、height、size_bytes、color_space、scope；manifest 保存同份元数据，Session history 保存 preview_path/preview_sha256。

预览从已渲染结果生成，长边最多 1600 像素、不放大小图、遵循导出文件自身的方向标记、按嵌入 ICC 转为带 ICC 的 sRGB JPEG。聊天优先展示 preview.path；纹理/噪点/锐化与交付精度检查用完整 output。预览是 8-bit 显示副本，不影响主文件哈希或恢复 RAW 信息；尺寸与内容决定文件体积，不保证 200KB 或毫秒级打开。它不跳过 RAW 解码，不减少保留的全尺寸成片数量。独立 qc 动作仍是只读统计，不生成旁置文件。

## 镜头库预检

需要自动镜头校正前，可选择额外检查：

```json
{"action":"inspect","source":"/absolute/input.NEF","lens_check":true}
```

inspect 默认不扫描镜头库。启用后返回 lens_support 与 lens_support_hint，区分 exact_name_candidate、not_found_in_scanned_database、incomplete_scan、database_unavailable、missing_metadata；名称查询只使用 EXIF 中可读镜头名称，扫描引擎相邻可发现的 Lensfun XML，不模拟引擎完整匹配算法。

engine_match_status 始终 unknown，engine_database_verified、mount_compatibility_verified、optical_correction_verified 均为 false：扫描库可能不同于引擎实际加载库，ExifTool 名称也可能不同于引擎解码名称。条目缺失/名字差异要提示核对 GUI、手动匹配或适配 LCP，不能断言全局不支持；候选命中也不能宣称畸变、暗角、色差已校正。库读取不完整时不把未命中当确定结论。

render 显式使用 lensfun_auto/lensfun_manual 时在 result.warnings 和 manifest.warnings 中提示 LENSFUN_MATCH_UNVERIFIED，不只把此状态藏在 manifest 的 effect_verified 中。

inspect 仅接受 RAW/JPEG 源片；成片 TIFF 用 qc 核验尺寸、ICC、位深标签与缩略图统计。qc 公开接口保持只读，不接收 preview_path/preview_metadata；其同名内部函数的预览参数是 render 的产物生成步骤，不是额外的公开字段。不能将内部函数签名当工具 Schema，也不能用 qc 宣称线性 16-bit 精度已验证。

## 0.6.8 核心能力与受控参数白名单（adjustments）

当前工具对 RawTherapee 5.13 共支持 14 个顶层调整类别（详细定义见 [RAW 控制参数规范](raw-controls.md)）：

1. **`exposure_ev`**：`[-2.0, 2.0]`，步长 0.05。绝对曝光补偿，映射至 `[Exposure] Compensation`。
2. **`contrast`**：`[-100, 100]` 整数，映射至 `[Exposure] Contrast`。
3. **`saturation`**：`[-100, 100]` 整数，映射至 `[Exposure] Saturation`。
4. **`white_balance`**：`temperature` (`[1500, 60000]`) 与 `green` (`[0.5, 2.0]`)。
5. **`tone_curve`**：`mode` (`Standard`, `FilmLike`, `Perceptual`) 与 `points`（2 至 16 点）。
6. **`denoise`**：`enabled`, `luma` (`[0.0, 100.0]`), `chroma` (`[0.0, 100.0]`)。锁定手动彩噪模式。
7. **`sharpen`**：`enabled`, `amount` (`[10, 500]`), `radius` (`[0.3, 3.0]`)。
8. **`highlight_compression`**（RAW & JPEG）：`amount` (`[0, 500]`), `threshold` (`[0, 100]`)。曲线高光压缩。
9. **`highlight_recovery`**（RAW only）：`enabled`, `method` (`luminance`, `cielab`, `blend`, `color`, `coloropp`)。
10. **`demosaic`**（Nikon .nef Bayer only）：`method`（严格限制为 15 种单张摄影算法，无 fast/mono/pixelshift/none；含 amaze, rcd, amazebilinear, rcdbilinear 等），`dual_contrast_auto` (布尔), `dual_contrast`（数值 `[0, 100]`，允许小数）。给出阈值且省略 auto 即设为手动；仅指定 auto=true 会清除历史手动阈值；同一 patch 同时指定 auto=true 与阈值则拒绝。对比度控制仍须配双重算法，可从 Session 继承；只提交单一算法 method 会自动移除历史双重控制字段，本轮显式冲突仍拒绝。
11. **`raw_preprocess`**（RAW only）：`hot_pixel_filter`、`dead_pixel_filter` 为布尔值；`hot_dead_pixel_threshold` 为整数 `[20, 200]`。Bayer 专有项（Nikon .nef）：`line_noise` 为整数 `[0, 1000]`、`line_noise_direction` 为枚举 (`horizontal`, `vertical`, `both`)、`green_equilibration` 为整数 `[0, 100]`。强度/阈值不接受布尔值，完整映射与可执行示例见 raw-controls.md。
12. **`raw_calibration`**（Nikon .nef Bayer only）：`black_offsets` (`red`, `green1`, `blue`, `green2` 取值 `[-2048.0, 2048.0]`，分别映射至 PreBlack1, PreBlack0, PreBlack2, PreBlack3，并自动置 `Dehablack=false` 防止 Base 自动黑点覆盖），`white_scale` (`[0.1, 16.0]`，映射至 PreExposure 线性缩放）。
13. **`lens_correction`**（RAW only）：`mode` (`none`, `lensfun_auto`, `lensfun_manual`, `lcp`)，`distortion`, `vignetting`, `ca`；manual 模式需 `camera_make`, `camera_model`, `lens`；lcp 模式需 `path` 与 `sha256`；显式绑定受支持的内置别名时可省略 sha256，由工具校验包内固定指纹后填充。
14. **`input_profile`**（RAW only）：`mode` (`camera_standard`, `auto_matched`, `custom`)；custom 需 `path` 与 `sha256`；DCP 开关：`tone_curve`, `look_table`, `hue_sat_map`, `baseline_exposure` (映射至 `ApplyBaselineExposureOffset`), `dcp_illuminant` (`0`=auto, `1`=第1标定光源, `2`=第2标定光源)。

### 增量继承、单项复位 (`reset_fields`) 与模式切换规则
- **增量 Patch 深度合并**：绑定 session 渲染时，请求可仅传入增量字段，先校验 patch 类型与合法性，再与上一轮累积参数深合并，最后校验整体完整性。
- **单项复位（`reset_fields`）**：
  - 支持传入非空、无重复的顶层调整类别数组（14 类之一）。
  - 执行顺序：**继承历史 ➔ 移除指定类别 ➔ 合并新 patch ➔ 全量完整校验**。
  - `reset_fields` 与全局 `reset_adjustments` 互斥。复位与新值可同时提交：先移除历史项，再应用新值。例如 reset_fields 包含 exposure_ev 且顶层 exposure_ev=0.8，结果就是 0.8；adjustments 内的新值语义相同。若顶层和 adjustments 同时指定曝光，则因重复来源拒绝。
  - **白平衡复位语义**：移除白平衡后，RawTherapee 回归应用 Base PP3 模板中的预设白平衡，**绝不等于恢复相机拍摄时的原始白平衡**。若复位后重新提供白平衡 patch，必须完整提供 `temperature` 与 `green`。
- **复合模式切换与资源重绑**：`lens_correction` 或 `input_profile` 切换模式时（如从 `lensfun_manual` 切至 `lcp`），必须在请求中包含 `reset_fields` 清除旧模式残留字段。外部资源（LCP/DCP/ICC）在首次绑定校验后自动固化为绝对规范物理路径存入 Session；资源丢失损坏时可通过 `reset_fields` 清除并重新绑定。

---

## 内置模板与路径规则

可使用显式模板标识，任意工作目录均有效：

```json
{"action":"profile","profile":"builtin:neutral-rec2020-rt513"}
```

同一标识可用于 `session init.base_profile` 和 `render.profile`。先用 profile 动作获取实际路径/哈希，再绑定渲染；Session 固化真实绝对路径。`capabilities.builtin_profiles` 列出可解析别名，并非推荐清单；builtin_profile_metadata 单独给出实验资源的校准状态。中性模板仅支持 kind=pp3，镜头别名仅支持 kind=lcp；未知标识、路径穿越或错配类型请求拒绝。

普通相对路径始终相对于当前工作目录，包括 `assets/...`；不回退到 Skill 目录，不猜测调用方想要哪一份同名文件。外部 LCP/DCP/ICC 规则不变。

## 批量清理的只读预检

`preflight_only: true` 检查所列全部会话，返回逐会话结果、失败代码、成功/失败计数和 complete/partial/failed 状态，绝不删除。此时顶层 `ok:true` 仅表示预检完成，必须检查 result.status、failed；不得把部分成功说成全部通过。

```json
{
  "action":"clean",
  "output_dir":"/absolute/output",
  "session_paths":["/absolute/photo-a.session.json","/absolute/photo-b.session.json"],
  "preflight_only":true
}
```

`preflight_only=true` 与 `apply=true` 互斥。预检候选数量只是单会话诊断，未聚合失败会话的保护信息，不可据此绕过正常清理。修正路径或恢复损坏会话后，正常 clean 再统一读取所有目标并核验；任何会话缺失或损坏仍停止删除。不要自动移除失败会话后重试删除，共享资源可能被它们引用。

session 的 init/record_feedback 与 render 均接受顶层 exposure_ev（有限数值 −2 至 +2），与 adjustments.exposure_ev 等价；同时提供两处，即使值相同也拒绝 INVALID_REQUEST。record_feedback 仅传顶层曝光也会更新 effective_adjustments、保留其他调整与 Keep/Change，并保存旧参数快照；reset_fields 后再应用新值。不沿用调用方过期 revision。

```json
{"action":"session","operation":"record_feedback","session_path":"/用户项目/session.json","expected_revision":2,"user_feedback":"稍微再亮一点，保持暖光","exposure_ev":0.25}
```

## Session 容器规范、Render Gate 门禁与生命周期清理设计

当前 Session 使用本地 JSON 容器、Render Gate 防重入与孤立产物生命周期管理：

1. **结构要素**：`schema_version: 1`, `session_id`, `created_at`, `updated_at`, `source`（规范路径、SHA-256、类型）, `base_profile`（规范路径、SHA-256）, `confirmed_brief`, `keep`, `change`, `revision`, `effective_adjustments`, `history`。
2. **Session 三分支操作规范**：
   - **`init`**：必填 `action`, `operation`, `session_path`, `source`, `base_profile`, `confirmed_brief`；可选 `session_id`, `keep`, `change`, `exposure_ev`, `adjustments`。支持在初始化建档时直接注入初始调整参数，**`revision` 保持为 0，`history` 保持为空数组**。若参数校验失败严格失败闭合，不产生半成文件。禁止传入 `expected_revision`, `user_feedback`, `reset_adjustments`, `reset_fields`。
   - **`load`**：必填 `action`, `operation`, `session_path`。严格禁止传入其他字段（`additionalProperties: false`）。
   - **`record_feedback`**：必填 `action`, `operation`, `session_path`, `expected_revision`；可选 `user_feedback`, `confirmed_brief`, `keep`, `change`, `exposure_ev`, `adjustments`, `reset_adjustments`, `reset_fields`。禁止传入 `source`, `base_profile`, `session_id`。
3. **Render Gate 门禁与进程竞争防重入**：
   - 每 session 在会话文件旁维护独立门禁锁文件（`<session_path>.render.gate`）。
   - 先通过非阻塞 `fcntl.flock(LOCK_EX | LOCK_NB)` 获取门禁 ➔ 持有短锁读取会话快照 ➔ 锁外执行耗时渲染与 QC ➔ 短锁原子 CAS 提交 ➔ 在 `finally` 块中释放门禁。
   - 遭遇竞争立即返回 `RENDER_IN_PROGRESS`，**严禁盲目重试轮询循环**。
4. **短锁 CAS 并发安全与晚反馈冲突审计**：
   - 门禁锁不占用会话读写锁（`<session_path>.lock`），读取与记录反馈无阻塞进行。
   - 耗时渲染期间若用户追加了新反馈使 revision 递增，CAS 阶段原子提交中止并返回 `REVISION_CONFLICT`。生成的孤立试片（携带 manifest）完整留存供比对审计，绝不覆盖用户最新反馈。
5. **单个 Commit 权威（Single Commit Authority）与 Manifest 协议**：
   - Manifest 文件写入整数协议版本 `'cleanup_protocol_version': 1`，以及不可变的 `'commit_authority': 'session_history'`。
   - Session 文件的原子提交是成片备案的唯一权威依据。
6. **多 Session 孤立产物生命周期清理（Clean Action）**：
   - 支持互斥的单会话 `session_path` 或多会话 `session_paths`（非空唯一数组）。
   - 默认安全预览（`apply: false`），需用户明确授权方可传入 `apply: true` 执行删除。**严禁后台私自自动执行 GC**。
   - **Clean 仅清理未提交的孤立试片，绝不支持且不能实现“删除历史仅留最终”**。已提交历史目录受永久保护。
   - 执行删除前对目标会话进行新鲜读取（Fresh Read）与身份核验；处理 macOS APFS 不区分大小写文件系统时使用严格物理身份判断，遭遇损坏会话严格零删除失败闭合。

---

## 完整可运行 JSON 工作流示例

所有请求示例均已通过真实 JSON Schema 严格校验：

### 步骤 1：检查本地环境与依赖 (doctor)
```json
{
  "action": "doctor"
}
```
*返回工具版本、RT 5.13 路径、ExifTool、Pillow 以及可选 raw_qc 依赖（rawpy/numpy）状态。*

### 步骤 2：查询可用类别（摘要），需要时再取本轮参数
```json
{
  "action": "capabilities",
  "source_type": "raw",
  "detail": "summary"
}
```
*返回针对 RAW 的可用类别及限制；本轮需要算法或参数范围时，再用 detail=full 与 parameters 获取。*

### 步骤 3：检查源文件元数据 (inspect)
```json
{
  "action": "inspect",
  "source": "/用户项目/raw/DSC_0001.NEF"
}
```
*返回源文件的绝对规范路径、SHA-256 及 EXIF 拍摄参数。*

### 步骤 4：检查配置文件完整性 (profile)
```json
{
  "action": "profile",
  "profile": "/用户项目/presets/neutral-rec2020-rt513.pp3",
  "kind": "pp3"
}
```
*返回 PP3 SHA-256、显式工作与输出色彩空间及曝光补偿可写入性。*

### 步骤 5：可选 RAW CFA 曝光诊断 (raw_qc)
```json
{
  "action": "raw_qc",
  "source": "/用户项目/raw/DSC_0001.NEF",
  "roi": {
    "x": 100,
    "y": 100,
    "width": 1200,
    "height": 800
  }
}
```
*返回未旋转可见 CFA 原始码值统计（min/max/mean）、黑白电平与过曝像素比例，辅助判断高光是否硬剪切。*

### 步骤 6：初始化项目 Session 并注入初始参数 (session init)
```json
{
  "action": "session",
  "operation": "init",
  "session_path": "/用户项目/session.json",
  "source": "/用户项目/raw/DSC_0001.NEF",
  "base_profile": "/用户项目/presets/neutral-rec2020-rt513.pp3",
  "confirmed_brief": "日常社交分享；逆光主体面部结构与关键细节需看清，背景保留现场暖光且不喧宾夺主；自然真实微调",
  "keep": ["现场黄昏暖光氛围", "面部自然轮廓"],
  "change": ["整体提亮半档", "暗部阴影更通透"],
  "adjustments": {
    "exposure_ev": 0.35,
    "highlight_recovery": {
      "enabled": true,
      "method": "coloropp"
    },
    "demosaic": {
      "method": "amaze"
    }
  }
}
```
*创建 session.json，固化初始参数至 `effective_adjustments`，初始 `revision: 0`，`history: []`。*

### 步骤 7：首轮绑定渲染 (render round 1)
```json
{
  "action": "render",
  "source": "/用户项目/raw/DSC_0001.NEF",
  "profile": "/用户项目/presets/neutral-rec2020-rt513.pp3",
  "profile_sha256": "44ee44e31464455799a7719602f0fcb7e09802871fa21fe7c7359ef1f99c8577",
  "engine_version": "5.13",
  "output_dir": "/用户项目/output_r1",
  "format": "jpeg",
  "session_path": "/用户项目/session.json",
  "expected_revision": 0
}
```
*Render Gate 忙碌时立即报错，不排队；取得门禁后应用会话初始参数渲染，写入不可变 manifest，CAS 提交推进 revision 至 1。*

### 步骤 8：跨聊天会话恢复 (session load)
用户开启新窗口，仅提供 session 文件路径：
```json
{
  "action": "session",
  "operation": "load",
  "session_path": "/用户项目/session.json"
}
```
*瞬时恢复全部上下文、累积有效参数、基准需求与 Keep/Change 历史。*

### 步骤 9：记录用户针对性反馈 (session record_feedback)
```json
{
  "action": "session",
  "operation": "record_feedback",
  "session_path": "/用户项目/session.json",
  "expected_revision": 1,
  "user_feedback": "暖光感觉很好，但边缘建筑有桶形畸变，暗角也稍明显，请校正镜头并稍微加一点对比度",
  "keep": ["黄昏暖色调", "高光层次"],
  "change": ["校正镜头畸变与暗角", "微调反差"]
}
```
*记录用户原话快照，`revision` 推进至 2。*

### 步骤 10：第二轮增量渲染 (render round 2 with RAW controls)
```json
{
  "action": "render",
  "source": "/用户项目/raw/DSC_0001.NEF",
  "profile": "/用户项目/presets/neutral-rec2020-rt513.pp3",
  "profile_sha256": "44ee44e31464455799a7719602f0fcb7e09802871fa21fe7c7359ef1f99c8577",
  "engine_version": "5.13",
  "output_dir": "/用户项目/output_r2",
  "format": "jpeg",
  "session_path": "/用户项目/session.json",
  "expected_revision": 2,
  "adjustments": {
    "contrast": 15,
    "lens_correction": {
      "mode": "lensfun_auto",
      "distortion": true,
      "vignetting": true,
      "ca": true
    }
  }
}
```
*新参数与上轮的曝光、高光恢复、去马赛克深度合并；session 推进至 3。*

### 步骤 11：受控复位渲染 (render with reset_fields)
用户希望“放弃前面调的所有白平衡，回归模板预设白平衡，但保留镜头校正与对比度，并增加锐化”：
```json
{
  "action": "render",
  "source": "/用户项目/raw/DSC_0001.NEF",
  "profile": "/用户项目/presets/neutral-rec2020-rt513.pp3",
  "profile_sha256": "44ee44e31464455799a7719602f0fcb7e09802871fa21fe7c7359ef1f99c8577",
  "engine_version": "5.13",
  "output_dir": "/用户项目/output_r3",
  "format": "jpeg",
  "session_path": "/用户项目/session.json",
  "expected_revision": 3,
  "reset_fields": ["white_balance"],
  "adjustments": {
    "sharpen": {
      "enabled": true,
      "amount": 250,
      "radius": 0.6
    }
  }
}
```
*系统自动继承历史 ➔ 移除白平衡（恢复 Base PP3 预设白平衡） ➔ 保留镜头校正与对比度 ➔ 合并新锐化参数；session 推进至 4。*

### 步骤 12：孤立中间产物安全清理 (clean)
多轮迭代后用户明确要求清理无用中间试片：

**首先执行 Dry-Run 安全预览：**
```json
{
  "action": "clean",
  "session_paths": [
    "/用户项目/session.json"
  ],
  "output_dir": "/用户项目",
  "apply": false,
  "min_age_seconds": 1800
}
```
*返回孤立候选目录清单、预计释放总字节数及受保护跳过的目录与理由；零物理删除。*

**用户明确确认后执行物理清理 (apply: true)：**
```json
{
  "action": "clean",
  "session_paths": [
    "/用户项目/session.json"
  ],
  "output_dir": "/用户项目",
  "apply": true,
  "min_age_seconds": 1800
}
```
*对目标会话进行新鲜读取核验，保护已提交历史成片，物理删除确认无引用的老孤立试片。*

---

## 边界与诚实声明

1. **引擎版本要求**：0.6.8 结构化 adjustments（白平衡、去马赛克、高光恢复、RAW 校准、镜头校正等）严格要求本机 **RawTherapee 5.13**。未知引擎版本若传入新调整将直接被明确拒绝（`UNSUPPORTED_ADJUSTMENTS`），防止生成无效或损坏的 PP3。
2. **测试验证现状（诚实表达）**：
   - 本地自动化套件覆盖契约、状态与跨进程竞争；测试结果以对应版本的实际运行记录为准。
   - 验证了 Render Gate 竞争排队、晚反馈 CAS 冲突留存孤立试片、单项 `reset_fields` 语义、APFS 大小写不敏感目录误删保护、相对资源路径跨 CWD 绝对锚定，以及 `raw_qc` CFA 码值计算与依赖缺失优雅退避。
   - **重要声明**：测试套件基于合成图像与契约验证，尚未进行真实多机型大光比 NEF 的端到端色彩与宽容度评测；不可夸大宣传真实 NEF 成片画质；JPEG 验证基于 8-bit 已烘焙图像，白平衡滑块表现为相对冷暖偏移，不得宣称等同于 RAW 传感器的调整空间或物理绝对开尔文色温。
3. **真实剩余技术限制**：
   - **网络文件系统锁限制**：基于 `fcntl.flock` 的会话锁与 Render Gate 依赖本地 POSIX 内核支持；在部分 CIFS/SMB 或未启用锁支持的 NFS 挂载卷上，文件锁语义可能退化。
   - **进程外并发删除与非原子目录移动**：Clean 操作与外部文件系统操作（如用户在 shell 外部直接 `rm -rf` 整个 output_dir）存在固有非原子边界。Clean 内部严格按产物锁 ➔ 会话锁规避死锁，并保持删除前新鲜读取。
   - **Clean 绝不修改已提交成片**：Clean 的职能仅限于回收未引用的孤立产物（Orphan Renders），不能也不应被用于“删除历史成片仅留最终结果”。
4. **无法实现的功能**：
   - 严禁承诺人像局部提亮/面部美颜笔刷；
   - 严禁承诺生成式 AI 消除/路人擦除；
   - 严禁声称存在即开即用的官方现成胶片滤镜预设。可与用户讨论风格倾向，但必须明确解释为全局光学与影调的组合试片。
5. **用户反馈文本定位**：
   - `keep` 与 `change` 中记录的用户原话，系统仅忠实保存于 session 与 manifest 快照，供人机回溯审计；**不声称具备机器自动理解自然语言并约束物理画面的神奇能力**。
