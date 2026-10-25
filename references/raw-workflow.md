# RAW 执行与验收 (v0.6.8)

## 原片、引擎与受控参数映射 (0.6.8)

- NEF 原片始终保持只读不变，修改基于 sidecar 与受控参数，输出存入新路径。已有同名输出时生成版本名，除非用户明确授权覆盖。
- **RawTherapee 5.13 引擎要求**：0.6.8 结构化调整（adjustments）严格要求本机 **RawTherapee 5.13**。未知引擎版本若传入新调整将直接被明确拒绝（`UNSUPPORTED_ADJUSTMENTS`），防止生成无效或损坏的 PP3。
- **Base PP3 与参数覆盖**：
  - 多个 `-p` 是参数覆盖而非多次图像滤镜。
  - RawTherapee 5.13 默认工作空间为 Rec2020；Base PP3 必须显式保存 `[Color Management] WorkingProfile` 和 `OutputProfile`。随附中性起始配置锁定 Rec2020 与 RTv4_sRGB。
- **PP3 字段安全映射与实现细节 (14类受控参数)**：
  - `exposure_ev` ➔ `[Exposure] Compensation`
  - `contrast` ➔ `[Exposure] Contrast`
  - `saturation` ➔ `[Exposure] Saturation`
  - `white_balance` ➔ `[White Balance] Enabled=true`, `Setting=Custom`, `Temperature`, `Green`（偏色乘数）
    - 对 RAW 图像：RAW 有更多白平衡调整信息也不自动保证精准校色。
    - 对 8-bit JPEG：JPEG 参数为 RawTherapee 对已烘焙图像的颜色调整控制；相对当前试片调整冷暖与偏色，需预览确认，数值不代表重新设定拍摄白平衡，5500 不保证中性。
  - `tone_curve` ➔ `[Exposure] CurveMode` ('Standard'/'FilmLike'/'Perceptual'), `Curve` 格式为 `1;x1;y1;x2;y2;...;`
  - `denoise` ➔ `[Directional Pyramid Denoising] Enabled`（省略时启用，显式 false 时关闭）, `Luma`, `Chroma`；**重要依赖**：强制锁定 `CMethod=MAN` 与 `C2Method=MANU`，彻底规避 RT 5.13 默认自动彩噪模式忽略手动 Chroma 滑块的陷阱。
  - `sharpen` ➔ `[Sharpening] Enabled`（省略时启用，显式 false 时关闭）, `Amount`, `Radius`
  - `highlight_compression` ➔ `[Exposure] HighlightCompr`, `HighlightComprThreshold`（RAW 与 JPEG 均支持曲线高光压缩）
  - `highlight_recovery` ➔ `[HLRecovery] Enabled`（按请求），`Method`（API 的 luminance/cielab/blend/color/coloropp 分别写为 Luminance/CIELab blending/Blend/Color/Coloropp）
  - `demosaic` ➔ `[RAW Bayer] Method`（15 种单张 NEF 摄影算法）, `DualDemosaicAutoContrast`, `DualDemosaicContrast`（双算法对比度阈值）
  - `raw_preprocess` ➔ `[RAW] HotPixelFilter`, `DeadPixelFilter`, `HotDeadPixelThresh`；Bayer 专有项：`[RAW Bayer] LineDenoise`, `LineDenoiseDirection`（horizontal/vertical/both 分别写为 1/2/3）, `GreenEqThreshold`
  - `raw_calibration` ➔ `[RAW Bayer] PreBlack0` (G1), `PreBlack1` (R), `PreBlack2` (B), `PreBlack3` (G2), `PreTwoGreen`, `Dehablack=false`；`[RAW] PreExposure` (white_scale 线性增益)
  - `lens_correction` ➔ `[LensProfile] LcMode` ('none'/'lfauto'/'lfmanual'/'lcp'), `UseDistortion`, `UseVignette`, `UseCA`, `LFCameraMake`, `LFCameraModel`, `LFLens`, `LCPFile`
  - `input_profile` ➔ `[Color Management] ToneCurve`, `ApplyLookTable`, `ApplyHueSatMap`, `ApplyBaselineExposureOffset`, `DCPIlluminant` (0/1/2), `InputProfile` ('(camera)'/'(cameraICC)'/外部物理绝对路径)
- **增量继承、单项复位（reset_fields）与完整校验约束**：
  - 增量请求（patch）先独立校验已传字段类型与范围；随后与会话历史深度合并；最后执行完整性校验。
  - 支持单项复位白名单（14类顶层类别）。语义为：继承历史 ➔ 移除指定类别 ➔ 合并新 patch ➔ 完整校验。
  - **白平衡复位语义**：移除白平衡调整后，RawTherapee 回归应用 Base PP3 模板预设白平衡，**绝不等于恢复相机拍摄时的原始白平衡**。复位后重设白平衡必须成对提供完整字段。
  - `lens_correction` 与 `input_profile` 切换模式必须使用 `reset_fields`。
  - `reset_fields` 与全局 `reset_adjustments` 互斥，两者同时传入直接拒绝。

---

## 感知基线与可见度复查门禁

### 先看起片，不把中性参数当成正常亮度

随附配置的实际别名为 builtin:neutral-rec2020-rt513。它是 partial PP3：锁定 Auto=false、Compensation=0、HistogramMatching=false 及工作/输出空间；未列出的工具继承固定引擎的中性默认。它没有承诺套用 Nikon Picture Control 或相机 JPEG 的自动匹配外观，**也不是“未经色彩与输出变换的纯线性图像”**。RAW 数据、嵌入相机 JPEG 和本工具显影结果是不同参考；不能因相机预览可读就假定中性起片也可读，亦不能假定所有中性起片一定欠曝。

感知基线是**本张照片、该主体在已确认用途下应达到的可见度**，不是固定中灰数值或全图均值。按用户确认的清晰、弱细节、朦胧或轮廓目标复查；不把纹理清晰或明亮规定为所有照片的必要条件。先用实际试片判断，缺少可见度目标按 dialogue.md 补问，不用参数小幅度代替目标完成度。

主体不足时，在现有受控范围内调整 exposure_ev、tone_curve 的暗部/中灰形状，结合高光压缩等已支持控制，复看主体与背景。达到目标需要较大修正时，不把“弱修图”自行解释成固定小曝光值；用户明确限制变化幅度时遵从，无法兼顾则说明取舍；也不固定每张都加曝光/抬黑点或强制额外出一张基线图。主体已满足时直接进入风格或交付。

曝光仍限 −2 至 +2，且是当前绝对补偿，不是每轮累加指令；不得通过改 Base、将导出反复作为新输入叠加曝光或滥用 raw_calibration.white_scale/黑电平绕过边界。全局曲线不能保证只亮主体而不亮同影调地面；遇到无法兼顾的目标，实看片说明取舍并按用户明确的优先级执行；优先级未知时让用户选择。

### 每次交付前：数据提示、实际看图、再做决定

1. **打开实际产物**：使用宿主的图像查看工具打开本轮 result.preview.path，有用户已选版时以其为对照；否则明确说明本次比较的参考版本，不默认最新版已获认可。按 confirmed_brief 定位主体，检查其形态/关键纹理是否达到目标；高光、背景和地面是否符合用户要求的视觉主次与 Keep。纹理、噪点和锐化判断使用完整 output 的 100% 局部。预览链接存在不等于已经看过。
2. **解释 QC 的范围**：mean_luma、shadow_clip_pct、highlight_clip_pct 是 <=1024px 编码 RGB/L 缩略图统计，不是线性光曝光或主体 ROI 测量。mean_luma<0.22 可作为偏暗复查提示；0.22 是本 SOP 的经验提醒值，非画质标准、判废线或强制提亮目标。不同输出 ICC/TRC 的编码统计不能按同一阈值直接比较；若需要同尺度参考，可对已转换为 sRGB 的 result.preview.path 调用现有 qc。全图均值 >=0.22 也必须看主体，不能让亮天空/灯光掩盖暗脸。
3. **依据目标分流**：主体未达到已确认可见度时，不能称“氛围保留成功”直接定稿。已授权范围内先修正再看；明确剪影/低调意图下，可保留低均值，但仍需确认其轮廓/弱细节达到所选目标。单点 RGB=51 不代表主体整体亮度，纯黑占比也不能独自说明主体失败。确实无法提亮而不严重破坏高光、背景或噪点时，具体说明瓶颈与折中，必要时展示标为“待选择”的对照，不伪装成达标最终图。
4. **交付需有可核查观察**：简述已打开哪张试片、主体与目标的关系、可见代价。无需向普通用户堆 QC 数字；没有图像查看能力或细节证据时，标明“未视觉验收的诊断试片”，让用户参与判断，不能宣称质检通过。连续两次同方向试片无可见改善时，暂停尝试并说明限制；保留候选，由用户选版，已有明确代选授权时遵照授权。代理复查完成不等于用户审美认可。

这是代理的视觉复查流程，**不是 rt_tool.py 的自动亮度阻断器**。不新增请求键、不修改已写入 manifest；manifest.visual_review=pending 和 effect_verified=false 仍按工具原义保留，不能擅改为自动通过。主体目标、用户优先级和选中版按 dialogue.md 持久化于现有 Session 字段；恢复会话后仍需实际看图复查。

依据：[RawPedia 入门中的相机预览与 RAW 显影](https://rawpedia.pixls.us/getting_started/)、[曝光补偿与色调曲线](https://rawpedia.pixls.us/exposure/)。文中 0.22 只是本 SOP 的经验提示，不来自官方曝光标准。

## 真实样片验收与运行余量

常规测试覆盖契约、状态与故障注入；真实 NEF 用独立 smoke_raw.py 显式指定输入，验证 JPEG/TIFF16/预览与截断副本的失败路径。对暗部色阶、白平衡或高光算法的画质判断仍需原片与 100% 局部实证，不由 16-bit 标签或冒烟成功替代。

render 先检查输出文件系统至少有 500 MiB 余量；这不预留空间。需要长时间显影时可显式传 timeout_seconds（30–3600，默认 300）。故障回收失败必须保留原始错误并报告残留路径，不能宣称已全部清理。

## 预览与单机资源约束

需要宽色域、预览拍摄信息、线程控制、过期试片或已提交历史图片精简时，按 [导出契约与历史保留](export-and-retention.md) 操作；默认行为保持兼容。

render 同时交付完整成片与 result.preview.path 的轻量预览。预览最多 1600px、sRGB、8-bit JPEG；只用于聊天与整体影调比较，100% 局部与输出精度使用完整文件。QC 与预览共用本次导出栅格解码，完整成片不被二次保存。preview 的哈希写入 manifest 和 Session history，失败时不提交半成会话。

同一 Session 的门禁避免重复渲染，跨 Session 不做全局节流；无论是否共用 output_dir，单机默认串行一个 RT CLI，监测资源后才提高到两个。预览改善展示与传输体积，不是快速 RAW 解码模式，也不是丢弃全尺寸试片的存储策略。

## 交付与可复现存档

| 用途 | 交付文件与含义 |
|---|---|
| 聊天、移动端整体试看 | preview.jpg 是最多 1600px 的 8-bit sRGB JPEG；可供分享，但不保证社交平台不再缩放或压缩 |
| 全尺寸展示或分享 | image.jpg 是完整分辨率的有损渲染；检查 ICC 和用途，不用预览代替细节验收 |
| 后续编辑的栅格母版 | image.tif 是 16-bit 渲染结果；不保留传感器 CFA、原始白平衡空间或全部 RAW 宽容度，不能替代 NEF |
| 重新显影与审计 | 保留原始 NEF、Session、manifest、Base/有效 PP3、外部 LCP/DCP/ICC 及其快照；记录并保存可用引擎版本，哈希本身不能恢复资产，也不保证跨版本或硬件逐字节复现 |

交付前必须完成上文可见度复查；未达到目标的图只能明确标为待选择/未验收试片。首次确认交付用途时再选 JPEG 或 TIFF16，不默认每轮生成两种完整格式。光学校正与胶片风格分开确认；没有校准资源或经验证的预设时，用已支持参数进行明确标注的试片，不将合成 LCP、单张样片或社区配方称为已验证的通用配置。

## 阶段化工作流：Session 驱动与 Render Gate 并发控制

在 0.6.8 中，实际修图任务严格由本地 Session 容器驱动，采用 Render Gate 独立门禁 + 短锁快照 + 锁外渲染 + 短锁 CAS 原子提交架构：

```
[ doctor / capabilities ] ➔ 验证环境依赖与当前源支持边界
        │
[ inspect / raw_qc (可选) ] ➔ 检查元数据与原始可见 CFA 码值分布
        │
[ session init ] ➔ 建立 session.json，锁定源片/模板哈希、基准需求 (支持注入初始 adjustments，rev 保持 0)
        │
[ record_feedback ] ➔ 记录用户 Keep/Change 与原话，版本递增
        │
[ render gate ] ➔ 获取 .render.gate 门禁 (忙碌立即 RENDER_IN_PROGRESS，禁盲重试)
        │
[ render (bound) ] ➔ 短锁读快照 ➔ 锁外耗时 RT/QC ➔ 短锁 CAS 核验版本并原子提交
        │          (若发生 REVISION_CONFLICT，保留孤立试片供审计，最新反馈不被覆盖)
        │
[ visual review ] ➔ 实际打开试片，按主体可见度目标与 QC 提示复查；未达标先纠正/说明取舍，再展示与收集反馈
        │
[ clean (可选) ] ➔ 默认 dry-run 预览孤立试片 ➔ 用户明确意图后 apply: true 安全清理
        │          (仅清理未提交孤立试片，不删历史仅留最终；APFS 严格大小写不敏感物理判断)
        │
[ session load ] ➔ (换新对话时) 凭 session_path 秒级恢复上下文继续下一轮
```

---

## JPEG 分支处理与技术边界（诚实表达）

当用户输入为已有 JPEG（.jpg/.jpeg）而非 RAW 时，管线严格遵循 JPEG 物理特性限制：
1. **已烘焙 8-bit 特征**：JPEG 是经过相机机内成像引擎完成色彩空间转换、白平衡与色调曲线烘焙后的 8-bit 有损 RGB 图像。
2. **高光与物理动态范围**：不存在 RAW Bayer 原始线性电平。高光一旦达到 (255, 255, 255) 即物理硬剪切；任何强压高光只会使纯白区域发灰，无法无损还原纹理结构。
3. **白平衡机制**：JPEG 参数为 RawTherapee 对已烘焙图像的颜色调整控制；相对当前试片调整冷暖与偏色，需预览确认，数值不代表重新设定拍摄白平衡，5500 不保证中性；RAW 有更多白平衡调整信息也不自动保证精准校色。
4. **PP3 工具适用性**：去马赛克（Demosaicing）、RAW 传感器去噪、RAW 专有色差修复、RAW 黑白点校准、镜头校正与相机输入配置文件对 JPEG 不生效且被严格拒绝。仅 `highlight_compression` 可作为曲线高光滚降使用。
5. **输出位深**：支持导出为 16-bit TIFF（`-b16 -t`），但这仅是后续编辑的容器，无法提升原生 8-bit 源的动态范围或精度（`precision_verified` 恒定为 false）。
6. **测试验证现状声明**：
   - 本套件已通过合成图像与真实文件验证了反差、饱和度、白平衡、色调曲线、降噪与锐化在 RT 5.13 下均产生明确的物理像素变化。
   - **重要声明**：测试套件尚未包含真实摄影 Nikon NEF 的端到端色彩与宽容度评测，不可夸大宣传真实 NEF 成片画质；JPEG 验证基于 8-bit 已烘焙图像，不得宣称等同于 RAW 传感器的调整空间。

---

## 质量验证与验收标准

| 检查尺度 | 必查内容 |
|---|---|
| **全图意图复查** | 按用户目标核对可见度、明暗、冷暖、氛围与构图；不自动纠正用户选择的剪影、偏色、朦胧或倾斜 |
| **100% 局部代价** | 检查伪色、纹理损失、锐化白边与噪点，说明变化及对目标的影响；有意保留的颗粒或柔化不自动判为审美失败 |
| **导出文件** | 能完整解码，尺寸和方向正确，ICC 存在且符合用途，无错误黑图/截断图 |
| **Session 状态** | `manifest.json` 与 Session `history` 哈希完全匹配，意图快照准确留存 |
| **审美定稿** | 用户明确选定，或在明确代选授权内定稿；最新渲染、QC 通过和代理看图均不等同于用户认可 |
| **参考系定位** | Nikon NX Studio 仅作为原厂色调风格参考，非逐像素真值 |

### 单 Commit 权威设计：
- Manifest 文件在渲染完成 QC 通过后立即写入磁盘，标记不可变的 `'cleanup_protocol_version': 1`、`'commit_authority': 'session_history'` 与 `'commit_status': 'reference'`。
- Session 文件的原子提交是成片绑定入库的**唯一权威依据**。
- 若 Session commit 突发故障（如磁盘写满），抛出 `SESSION_COMMIT_FAILED`，session 状态不推进；磁盘残留的成片目录由于未在 session history 中记录，可明确被判别为未提交的孤立产物（Orphan Render）。

---

## 官方依据

- 入门和固定处理顺序：https://rawpedia.pixls.us/getting_started/
- 曝光与高光恢复：https://rawpedia.pixls.us/exposure/
- 降噪：https://rawpedia.pixls.us/noise_reduction/
- 捕获锐化：https://rawpedia.pixls.us/capture_sharpening/
- 色彩管理：https://rawpedia.pixls.us/color_management/
- PP3 侧边栏文件配置：https://rawpedia.pixls.us/sidecar_files_-_processing_profiles/
- CLI 命令参数：https://rawpedia.pixls.us/command-line_options/
- Nikon 原厂对照：https://nikonimglib.com/nxstdo/onlinehelp/en/index.html
