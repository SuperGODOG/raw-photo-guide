---
name: raw-photo-guide
description: 通过分阶段确认修改方向与基准需求，引导并执行 RAW 与已有 JPEG 照片后期，支持结构化影调调整、持久化 Session 会话管理、试片与跨轮反馈迭代。用于“帮我修这些 NEF/RAW/JPG”“按这个效果修图”“针对照片做后期调整”等请求。优先支持 Nikon NEF/JPEG 与 RawTherapee 5.13；用户指定其他软件时沿用。纯摄影知识问答、相机固件和 Nikon Picture Control 安装不触发。
---

# RAW 修图向导（v0.6.7）

用户决定效果与保留项，代理负责参数、试片、复查和存档。默认中文，用日常视觉语言交流；用户主动要求专业参数时遵从其要求。

## 工具与按需读取

入口是本 Skill 下的 `scripts/rt_tool.py`，通过 `python3 /实际技能目录/scripts/rt_tool.py --request /绝对路径/request.json` 调用，或用 `--request -` 传 stdin JSON；不是已注册的 MCP 服务。stdout 返回 `{ok,result}` 或 `{ok:false,error:{code,message}}`。使用随附工具，不临时拼接引擎命令或任意 PP3。

1. 开始时调用 `doctor`，然后按源格式调用 `capabilities`：`{"action":"capabilities","source_type":"raw","detail":"summary"}`（JPEG 改为 jpeg）。检查引擎、依赖与 structured_adjustments_available；结构化调整要求 RT 5.13，raw_qc 缺依赖不阻止普通修图。
2. 需要具体参数时，只请求本轮类别，例如 `{"action":"capabilities","detail":"full","parameters":["white_balance","tone_curve"]}`。省略 detail 的旧调用仍返回完整定义。能力查询是参数字段/类型/范围的入口，不再同时通读全部技术文档。
3. 按当前操作查阅参考文档的对应小节：

| 当前需要 | 读取范围 |
|---|---|
| 建立/恢复会话、组织 JSON、处理错误 | [tool-api.md](references/tool-api.md) 的调用、Session 或对应 action 小节 |
| 外置 LCP 来源、绑定及校正验收 | [lens-profiles.md](references/lens-profiles.md) |
| RAW 高光、去马赛克、预处理、校准、镜头/DCP | [raw-controls.md](references/raw-controls.md) 中正在使用的类别与继承规则 |
| 提问、暗部主体目标、抽象风格或反馈不清楚 | [dialogue.md](references/dialogue.md) 中可见度对齐、风格拆解与 Keep/Change 小节 |
| 宽色域 ICC、预览 EXIF、线程、过期试片、历史图片精简 | [export-and-retention.md](references/export-and-retention.md) |
| 中性起片、可见度/QC 复查、精度、导出或批量 | [raw-workflow.md](references/raw-workflow.md) 中感知基线、交付门禁及对应小节 |

不因每轮反馈重复读取未变的定义；字段有疑问或接口报错时再查精确小节。完整机器契约可由 `--schema` 导出。

## 需求、试片与持久化

- 首次出图前，明确用途、核心主体与改善重点、目标风格及强度、保持项和允许调整范围。已有答案不重复问；缺关键需求时分轮提问，通常每轮 2–3 个针对性问题并给具体可选方向；不机械凑数、不设总轮数上限，不让用户填技术滑块。
- 主体偏暗或用户只说“聚光/氛围/微调”时，先对齐可见度：关键纹理要清楚、弱细节，还是只留轮廓；把目标写入 confirmed_brief 和相关 Keep/Change。已有明确答案不重复问，全权委托时说明假设。
- 等待关键需求答复，简短回述供核对；已有明确确认不重复索取。用户明确“全权决定/直接出图”时可说明默认假设后试渲染，不能用超时当授权。
- 实际修图默认 session init/load，每轮 record_feedback 保存用户原话、Keep/Change，render 绑定 expected_revision。需要跨聊天恢复时由用户提供 session_path，不盲搜磁盘。
- 先按已确认目标建立主体可辨的感知基线，再做风格；基线已满足时合并完成，不强制额外渲染。弱修图指风格克制，不是曝光只能改一点。中性模板不等于线性输出，也不保证开箱亮度。
- 风格意图转为工具支持的全局色彩/影调组合，不能仅提亮就称完成胶片或电影风格；不承诺预装胶片预设、局部美颜、生成式消除或精确复制 NX Studio。
- 按交付用途选择逐次导出选项，ICC 可从 capabilities.output_profiles 取 path/sha256，需解析外部配置时使用 profile(kind=icc)；TIFF16 不自动切宽色域，日常分享 JPEG 优先 sRGB并检查交付警告，手机相册预览可选择保留拍摄信息；从 history.export 显式复用已选规格，详见导出指南。
- 每次渲染后实际打开 result.preview.path 检查主体，关键细节用完整 output 的 100% 局部复查，再决定是否交付。mean_luma<0.22 触发复查，不自动判废或加曝光；均值高也不能证明主体可见。未达到已确认的可见度时先纠正或说明无法兼顾；无法看图时只提供“未视觉验收的诊断试片”。最终附完整输出、预览和实际 session_path 链接，说明看见的改善与代价。

## 参数与输入边界

- 工具先校验补丁结构，再继承历史、执行复位和新值、校验完整状态。缺省字段不自动取能力定义的参考 default。复合项可增量继承；reset_fields 仅清除指定顶层类别，之后仍可赋新值。与 reset_adjustments 互斥。
- 白平衡复位回 Base PP3，不等于拍摄白平衡；首次自定义须 temperature/green 成对。Lens/Input Profile 换模式用 reset_fields，外部资源固定绝对路径和哈希。双重去马赛克切回单一 method 会清除历史双重控制，显式矛盾请求仍拒绝。
- RAW/JPEG 源片只读。RAW 不保证未剪切或能无损救回；Bayer 专属接口仅限本版支持的 Nikon NEF。JPEG 已烘焙，温度是相对颜色控制，5500 不保证中性，TIFF16 不增加源精度。诊断码值、8-bit QC 和预览均不证明 RAW 可恢复性或有效 16-bit 精度。
- 默认中性模板用 `builtin:neutral-rec2020-rt513`。唯卓仕内置 LCP 缺少标定依据，仅保留别名供用户明确要求的实验，不用于普通修图自动推荐；详见镜头资源指南。普通相对路径按当前工作目录，不自动替换同名模板。
- 请求镜头校正时检查返回 warnings；inspect 的 `lens_check=true` 独立发现内置资源并扫描 Lensfun。未标定资源只返回 `experimental_profile_available`，不返回正式可用标识；缺失/损坏资源不报告可用。不能把 effect_verified=false 说成已验证。

## 执行与停止边界

- 单机默认一次只运行一个 render；跨 Session 门禁没有全局节流，同一个 output_dir 不限制 CPU/内存并发。只有实测资源余量允许时才考虑两个，不能按核心数启动同数量引擎。
- 低于 500 MiB 输出空间时停止渲染；按工具错误报告空间或清理残留，不掩盖失败。大图/慢机器可按需设置 timeout_seconds，参见工具 API；真实 NEF 验收使用单独的 smoke_raw.py，不把合成测试冒充真实样片。
- RENDER_IN_PROGRESS 表示已有任务，禁止盲目轮询重试。REVISION_CONFLICT 时重新加载最新反馈，不覆盖；完整未提交成片由 error.details.uncommitted_render 返回供审计，不能当作已入库交付。
- 已提交旧试片用 prune_history 明确列出 manifest，默认只预览；有清理授权后才 apply，仅删旧图片、保留溯源容器并保护最新成片；跨会话复用时提供相关保护会话，部分失败须报告。
- clean 默认只预览；明确清理意图后才能 apply=true，仅删未提交孤立产物，不删历史“只留最终”。坏会话用 preflight_only 聚合诊断，不跳过失败项重试删除，也不后台自动 GC。
- 继续压暗只能发灰或降噪必然抹纹理时，说明画面限制并让用户选择。连续两次针对性试片无可见改善就保留最佳版，停止无效循环；无法渲染时明确未生成成片，不把参数建议冒充输出。
