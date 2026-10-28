# 导出契约、预览元数据与历史保留

仅在需要宽色域输出、手机相册预览、后台资源控制、处理过期试片或清理已提交旧成片时阅读。输入字段以 capabilities.export_parameters 和导出 Schema 为准。

## 输出 ICC：与图像调整分开

render 的 output_profile 为逐次导出选项，不属于 24 类 adjustments，也不通过 reset_fields 控制。省略时沿用经过审核的 Base PP3 输出配置；随附 Base 为 RTv4_sRGB。TIFF16 不会自动切换宽色域。

capabilities.output_profiles 按文件扩展名列出引擎相邻 output 目录中的所有 ICC/ICM 文件，包含 RTv4_Wide、其他随引擎附带或用户放入该目录的配置；不再限制为四个名称。不递归扫描子目录或系统/用户色彩库，目录不存在时返回空数组。每个可读候选返回 name/path/sha256，可直接将 path、sha256 组成 render.output_profile，无需额外 shell 命令。无法读取的候选保留在列表中，返回 sha256=null 和 error.code=PROFILE_UNREADABLE，不能选用；不阻止其他候选发现。列表指纹仅表示查询时的文件内容，不证明是有效 RGB 输出配置，render 会重新解析与校验。需要解析配置描述/数据空间/设备类别或使用其他位置 ICC/ICM 时，显式调用 kind=icc（默认 kind=pp3）：

```json
{"action":"profile","profile":"/实际路径/RTv4_Rec2020.icc","kind":"icc"}
```

profile(kind=icc) 返回 path、sha256 和 icc={description,color_space,device_class}，使用同一份字节计算指纹并解析；损坏 ICC 报 PROFILE_INVALID，缺少 Pillow 报 DEPENDENCY_MISSING。ICC 可以合法描述非 RGB 数据，因此解析成功不证明能用于本工具 RGB 输出；RGB/变换适用性由 render 最终检查，不证明相机匹配或色彩准确。

render.output_profile 必须同时提供 path 与 sha256。工具会解析 ICC、验证可用于 RGB 输出、复核哈希并保存快照，将 file:快照路径写入有效 PP3；不会修改原 Base，也不会在已经渲染的 sRGB 图像上仅换标签。成片实际嵌入 ICC 的 SHA-256 必须与请求一致；引擎回退、重写 ICC 或未嵌入时拒绝交付（OUTPUT_PROFILE_MISMATCH / OUTPUT_INVALID），不假定同名配置等效。

以下路径、全零哈希和 revision 都是占位值，执行时替换为真实资源、当前 revision 与 profile 返回的指纹：

```json
{
  "action":"render",
  "source":"/用户项目/input.NEF",
  "profile":"builtin:neutral-rec2020-rt513",
  "profile_sha256":"0000000000000000000000000000000000000000000000000000000000000000",
  "engine_version":"5.13",
  "output_dir":"/用户项目/output",
  "format":"tiff16",
  "session_path":"/用户项目/session.json",
  "expected_revision":0,
  "output_profile":{
    "path":"/实际路径/RTv4_Rec2020.icc",
    "sha256":"0000000000000000000000000000000000000000000000000000000000000000"
  },
  "preview_metadata":"capture_camera",
  "threads":2
}
```

result.export 与 manifest.export 记录实际 ICC 描述/指纹、是否核验请求、元数据策略、OpenMP 环境请求；Session history 保存同份 export。外部 ICC 的原路径及资源快照放入 manifest.external_resources.output_profile，已有清理器会保护所声明会话中被历史导出引用的原 ICC。

这些是逐次导出设置，**不跨轮隐式继承**。继续同规格时，代理从最近 history.export 显式复用选项：output_profile 用 canonical_path 映射回请求 path、沿用 sha256；切换用途/格式时重新选择。线程记录是环境传参，不是测得的实际线程数。history.export 中 output_profile=null 表示沿用 Base，不表示没有 ICC；请求应省略该字段，不能传 null 或空对象。

宽色域 TIFF 用于后续编辑时才选择相应输出 ICC；普通网页、日常分享 JPEG 优先使用 sRGB。JPEG 不禁止广色域：主成片的实际 ICC 描述未匹配常用 sRGB 名称时，result/manifest.warnings 返回 JPEG_DELIVERY_PROFILE_REVIEW，包括沿用广色域 Base 的情况。该提示按描述保守识别，不是色域数值分析；配置名称不能证明颜色特性，未提示也不证明接收端支持该 ICC。接收端正确应用 ICC 时可正常显示；忽略或移除 ICC 时可能失色，不把所有浏览器或社交应用一概判为不支持。不要只改 ICC 标签，应重新显影为目标输出空间。宽色域和 16-bit 都不保证保留传感器全部颜色或恢复已剪切数据。预览始终由输出栅格经过 ICC 转换为 sRGB，只作整体试看。

## 预览元数据策略

| preview_metadata | 行为 |
|---|---|
| strip（默认） | 不嵌入 EXIF，保留此前的轻量预览行为 |
| capture | 从完整导出中复制已有拍摄时间、时区偏移、亚秒信息 |
| capture_camera | 在 capture 基础上复制 Make/Model、ISO、快门、光圈、焦距、镜头名称等白名单字段 |

不编造缺失日期，不整包复制 EXIF、MakerNote、GPS、机身/镜头序列号或旧缩略图。含 EXIF 时预览方向写为 1，像素尺寸按旋转后的预览重写，色彩标记与 sRGB 一致；metadata_policy 与 retained_exif_tags 说明实际保留项。策略只作用于 preview.jpg，不重写完整输出。手机是否采用相应字段排序仍由相册应用决定。

## 单次渲染线程请求

threads 接受整数 1–256，仅向本次 RT 子进程设置 OMP_NUM_THREADS 与 OMP_THREAD_LIMIT；不修改父进程环境。省略时继承环境或引擎默认。实际线程数量仍取决于引擎和任务阶段，工具记录 effective_thread_count_verified=false。

此选项不是跨 Session 全局并发门禁。单机仍默认串行；后台可显式降低 threads，不按逻辑核数启动同数量 RT 进程。

## 过期试片的结构化信息

继续允许渲染期间记录反馈。若完整 manifest 生成后提交失败，错误码保持 REVISION_CONFLICT / SESSION_COMMIT_FAILED，并在 error.details.uncommitted_render 中返回 output、manifest、preview、input_revision 与 status=uncommitted。

这只是已生成但未提交的审计产物，不冒充会话成功成片。先 load 最新 Session，再按最新意图决定下一次渲染；不把过期参数写回，不自动循环重试。未完成 manifest 的失败仍清理半成品，不返回完整试片声明。

## 独立的历史图片精简：prune_history

clean 继续只处理未提交孤立目录。新 action prune_history 专门处理**已提交的、明确选中的旧试片图片**，默认仅预览，要求 session_path、expected_revision 和 manifest_paths。先 load 会话选取真实旧记录，不按文件名猜测。

```json
{
  "action":"prune_history",
  "session_path":"/用户项目/session.json",
  "expected_revision":4,
  "manifest_paths":["/用户项目/output/某个旧试片目录/manifest.json"],
  "protect_session_paths":["/用户项目/相关会话.json"]
}
```

仅在已有明确清理授权且预览名单符合意图时，使用当前 revision 与相同名单设 apply=true；已有授权不重复确认。最新成功成片始终保护；其他希望保留的成片不放入 manifest_paths。清理前读取 confirmed_brief 中用户选中/定稿版的实际路径，即使它不是最新版本也要排除；工具不解析文字偏好来自动保护旧版。涉及其他会话复用图片时，在 protect_session_paths 提供所有已知相关会话；不盲搜磁盘，也不宣称能发现未提供的跨会话引用。无额外会话时 protect_session_paths=[] 与省略等价；非空列表仍检查路径与物理唯一性。坏保护会话会阻止删除。

只 unlink 经过历史/manifest 哈希核验的 image.jpg/image.tif 与该版 preview.jpg；不删除目录、manifest、PP3、日志或 ICC/LCP/DCP 快照。保留最新成片、源片、Base、目标会话以及明确提供的保护会话中的引用。占用渲染门禁，并按路径顺序持有相关会话锁，防止清理期间写入新引用；不扩大普通渲染期间的锁范围。

apply 时先原子记录 prune_pending 并推进 revision，再删除图片；完成后记录 artifact_state=pruned。删除不全时 result.status=partial、errors 列出失败项，状态为 prune_partial。最终状态写入失败时保留已有 pending 记录并报 SESSION_COMMIT_FAILED；重新 load 后可使用新 revision 和同一名单恢复。不能把部分成功说成全部清理完成。相同名单已全部 pruned 且图片不存在时返回 status=no_op，不重写 Session、不推进 revision；仍先检查 expected_revision 与完整性。混合名单只更新仍待清理的记录，已完成记录的时间戳保持。prune_pending/prune_partial 即使已无图片，也需提交状态收尾，仍会推进 revision。

manifest 不可变，记录“当时生成了什么”，不是当前文件存在性账本。脱离 Session 的独立核验必须将文件不存在报告为“缺失/生命周期未知”，不能仅凭 manifest 判定损坏或合法精简；绑定会话后须同时查询 Session history.artifact_state 区分 pruned、pending/partial 与意外缺失。Session history 继续保留原始输出路径和指纹用于审计，artifact_state 表明文件已移除；history.status=SUCCESS 表示当时显影成功，不表示图片现在仍存在。prune_history 不删除历史事件，不将其转成可被 clean 删除的孤立目录。reclaimable_logical_bytes 只是候选文件逻辑大小之和，不保证相同物理磁盘释放量。

依据：[ICC 接收端色彩管理测试](https://www.color.org/version4html/)、[RawTherapee 输出色彩管理](https://rawpedia.pixls.us/color_management/#output-profile)、[RT 5.13 ICC 加载实现](https://github.com/Beep6581/RawTherapee/blob/5.13/rtengine/iccstore.cc)、[Pillow EXIF API](https://pillow.readthedocs.io/en/stable/reference/Image.html#PIL.Image.Exif)。
