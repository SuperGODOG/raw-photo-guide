# RawTherapee 5.13 RAW 控制参数规范 (v0.7.0)

本文档说明既有基础调整与 RAW 专有控制。新增常用控制见 [common-controls.md](common-controls.md)；实际参数契约以运行时 capabilities 与导出 Schema 为准。字段结构、类型、范围与枚举由导出 Schema 和运行时共用定义校验；继承后的完整性、源格式兼容、曲线单调性与资源有效性由运行时另行校验，禁止直接向 RawTherapee 注入未经验证的任意 PP3 文本。

调整项字符串枚举允许任意大小写并归一化保存，例如 `filmlike`、`FILMLIKE` 均写成 `FilmLike`；字段名保持大小写敏感。整数参数允许 15.0 这样的数学整数，拒绝布尔、非整数小数和数字字符串。

数值 `step` 为建议调节粒度，不是 `multipleOf` 限制。能力查询中的 `default` 是起始参考，不表示省略字段会自动写入；省略项继承 Session 或 Base PP3。白平衡首次自定义必须同时有 temperature/green；高光重建开启时必须有 method；曲线首次定义必须有 mode/points。

---

## 1. 适用范围与引擎约束

- **引擎版本**：所有新增 RAW 调整项严格要求本机 **RawTherapee 5.13**。未知引擎版本若传入新调整将直接被明确拒绝（`UNSUPPORTED_ADJUSTMENTS`）。
- **源文件支持**：
  - **Bayer RAW（本版仅限 Nikon `.nef`）**：开放全部 24 个调整类别，包括 Bayer 专有的去马赛克、行噪消除、双绿均衡及 RAW 黑白点手动校准。
  - **其他 RAW**：支持通用影调、颜色、构图与尺寸调整，另开放通用 RAW 控制（高光恢复、RAW 基础热噪点/坏点预处理、镜头校正、输入色彩配置文件）；Bayer 专属项保持旧入口隔离。
  - **已烘焙 JPEG（`.jpg`, `.jpeg`）**：开放通用影调、色彩、构图与尺寸控制；任何 RAW 专有字段（去马赛克、高光恢复、RAW 预处理、RAW 校准、镜头校正、输入配置）均被严格拒绝（`UNSUPPORTED_ADJUSTMENTS`）。

---

## 2. 调整类别清单与详细参数定义

系统支持 24 个顶层受控调整类别；本节详述原有 14 类，新增 10 类见 common-controls.md（完整名单由 capabilities.available_adjustments 提供）：

```text
通用影调与色彩 (RAW & JPEG) : exposure_ev, contrast, saturation, white_balance, tone_curve, denoise, sharpen, highlight_compression
RAW 专有控制 (RAW only)     : highlight_recovery, raw_preprocess, demosaic, raw_calibration, lens_correction, input_profile
```

### 2.1 通用影调与色彩控制 (RAW & JPEG)

1. **`exposure_ev`**（数值）：
   - 取值范围：`[-2.0, 2.0]`，步长 `0.05`，默认 `0.0`。
   - 映射：`[Exposure] Compensation`。支持顶层或 `adjustments` 内部传入，禁止双重指定。
2. **`contrast`**（整数）：
   - 取值范围：`[-100, 100]`，步长 `1`，默认 `0`。
   - 映射：`[Exposure] Contrast`。
3. **`saturation`**（整数）：
   - 取值范围：`[-100, 100]`，步长 `1`，默认 `0`。
   - 映射：`[Exposure] Saturation`。
4. **`white_balance`**（对象）：
   - `temperature`（整数）：`[1500, 60000]`，步长 `5`。映射至 `[White Balance] Setting=Custom` 与 `Temperature`。
   - `green`（浮点数）：`[0.5, 2.0]`，步长 `0.001`，默认 `1.0`。映射至 `Green`（<1.0 偏品红，>1.0 偏绿）。
   - **语义差异**：RAW 源拥有更宽的白平衡调整裕度，但不自动保证中性校色；JPEG 源为对已烘焙色彩的二次偏移控制，数值不代表拍摄时的实际色温。
5. **`tone_curve`**（对象）：
   - `mode`（字符串枚举）：`"Standard"`, `"FilmLike"`, `"Perceptual"`。映射至 `[Exposure] CurveMode`。
   - `points`（数组）：2 至 16 个控制点 `[[x, y], ...]`。x 必须从 `0.0` 起始至 `1.0` 严格递增；y 取值 `[0.0, 1.0]` 且整体非降序。映射至 `[Exposure] Curve=1;x1;y1;...;`。
6. **`denoise`**（对象）：
   - `enabled`（布尔）：默认 `true`。
   - `luma`（浮点数）：`[0.0, 100.0]`，步长 `0.01`，默认 `0.0`。
   - `chroma`（浮点数）：`[0.0, 100.0]`，步长 `0.01`，默认 `15.0`。
   - 映射：`[Directional Pyramid Denoising]`，自动锁定 `CMethod=MAN` 与 `C2Method=MANU`，杜绝 RT 自动彩噪模式覆盖手动设定。
7. **`sharpen`**（对象）：
   - `enabled`（布尔）：默认 `true`。
   - `amount`（整数）：`[10, 500]`，步长 `1`，默认 `200`。
   - `radius`（浮点数）：`[0.3, 3.0]`，步长 `0.01`，默认 `0.5`。
   - 映射：`[Sharpening]`。
8. **`highlight_compression`**（对象，RAW 与 JPEG 通用）：
   - `amount`（整数）：`[0, 500]`，步长 `1`，默认 `0`。映射至 `[Exposure] HighlightCompr`。
   - `threshold`（整数）：`[0, 100]`，步长 `1`，默认 `0`。映射至 `[Exposure] HighlightComprThreshold`。
   - 说明：控制高光曲线压缩（Roll-off），可平滑过渡过曝边缘，避免刺眼白切。在 JPEG 上用于曲线高光滚降，在 RAW 上可配合高光恢复协同工作。

---

### 2.2 RAW 专有控制项 (RAW Only)

#### A. 高光重建 (`highlight_recovery`)
- **适用源**：RAW 源专用（JPEG 传入报 `UNSUPPORTED_ADJUSTMENTS`）。
- **字段**：
  - `enabled`（布尔）：默认 `false`。映射至 `[HLRecovery] Enabled`。
  - `method`（字符串枚举）：`"luminance"`, `"cielab"`, `"blend"`, `"color"`, `"coloropp"`。开启重建时显式选择，不设通用推荐算法。映射至 `[HLRecovery] Method`。
- **选择与复核**：`coloropp`（Inpaint opposed）利用剪切区域附近的颜色信息，受白平衡及邻近高光颜色影响；不要只按文件类型或 ISO 自动套用。复杂霓虹、强人造光或可疑洋红边时，保持白平衡、曝光与曲线相同，比较 coloropp 与 blend；必要时试 luminance，后者恢复为中性灰，可能损失高光色彩。任何方法都不能保证消除色边或还原已丢失的原始颜色。
- **停止条件**：若已证实 RAW 各通道信息不可恢复，保留合理白色与过渡，不因压暗变灰就声称找回纹理。8-bit 预览剪切不证明 RAW 真剪切；两次同方向比较无改善即暂停试参，保留候选并说明限制；由用户选版或按明确代选授权执行，不无限换算法。
- 官方依据：[RawPedia 高光重建](https://rawpedia.pixls.us/exposure/#highlight-reconstruction)。本 Skill 不将某张照片的特定偏色归因于单一算法，须用实片对比判断。

#### B. 去马赛克 (`demosaic`)
- **适用源**：仅开放 **Nikon `.nef` Bayer RAW**（排除 Fuji X-Trans 及非 Bayer 源；JPEG 拒绝）。
- **字段**：
  - `method`（字符串枚举）：严格限制为 **15 种单张 NEF 摄影算法**，排除非摄影/多帧算法（无 `pixelshift`, `mono`, `none`, `fast`）：
    - 单一算法：`"ahd"`, `"amaze"`, `"dcb"`, `"eahd"`, `"hphd"`, `"igv"`, `"lmmse"`, `"rcd"`, `"vng4"`。
    - 双重混合算法（Dual Demosaic）：`"amazebilinear"`, `"amazevng4"`, `"rcdbilinear"`, `"rcdvng4"`, `"dcbbilinear"`, `"dcbvng4"`。
    - 默认推荐：`"amaze"`（细致纹理）或 `"rcd"`（平滑低噪）。
  - `dual_contrast_auto`（布尔）：双重去马赛克是否自动对比度阈值，默认 `true`。映射至 `[RAW Bayer] DualDemosaicAutoContrast`。
  - `dual_contrast`（数值，可含小数）：`[0, 100]`，手动对比度阈值。映射至 `[RAW Bayer] DualDemosaicContrast`。
  - **契约规则**：传入 `dual_contrast` 数值且省略 auto 字段，即自动写入 `dual_contrast_auto=false`，包括历史曾启用自动的情况；同一 patch 同时指定 `dual_contrast_auto=true` 与阈值则拒绝。仅传 `dual_contrast_auto=true` 会移除继承的手动阈值，保留算法与其他调整。对比度控制只能用于上述六种双重算法，method 可从 Session 继承；首次使用必须给出双重算法 method。只提交单一算法 method 时，会自动移除历史 dual_contrast/dual_contrast_auto；本轮若显式为单一算法附带任一双重控制字段，仍拒绝。切回双重算法不复活已丢弃的手动阈值。

#### C. RAW 传感器预处理 (`raw_preprocess`)
- **适用源**：RAW 源专用。
- **通用字段**（所有 RAW）：
  - `hot_pixel_filter`（布尔）：默认 `false`。映射至 `[RAW] HotPixelFilter`。
  - `dead_pixel_filter`（布尔）：默认 `false`。映射至 `[RAW] DeadPixelFilter`。
  - `hot_dead_pixel_threshold`（整数）：`[20, 200]`，起始参考 `100`。映射至 `[RAW] HotDeadPixelThresh`。
- **Bayer 专有字段**（仅限 Nikon `.nef`）：
  - `line_noise`（整数）：`[0, 1000]`，`0` 关闭，正值表示强度。映射至 `[RAW Bayer] LineDenoise`。
  - `line_noise_direction`（字符串枚举）：`"horizontal"`, `"vertical"`, `"both"`。起始参考 `"both"`。映射至 `[RAW Bayer] LineDenoiseDirection`（依次为 1/2/3）。不承诺特定相机的 PDAF 条纹修复。
  - `green_equilibration`（整数）：`[0, 100]`，`0` 关闭，正值为双绿均衡阈值。映射至 `[RAW Bayer] GreenEqThreshold`；不接受布尔值。

**可执行请求示例**（替换源片、模板、模板哈希和输出路径；数值是接口示例，不是所有照片的推荐值）：

```json
{
  "action": "render",
  "source": "/absolute/input.NEF",
  "profile": "/absolute/base.pp3",
  "profile_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "engine_version": "5.13",
  "output_dir": "/absolute/output",
  "format": "tiff16",
  "adjustments": {
    "demosaic": {"method": "amazevng4", "dual_contrast": 20.5},
    "raw_preprocess": {
      "hot_pixel_filter": true,
      "dead_pixel_filter": true,
      "hot_dead_pixel_threshold": 100,
      "line_noise": 120,
      "line_noise_direction": "both",
      "green_equilibration": 15
    }
  }
}
```

#### D. RAW 黑白点手动校准 (`raw_calibration`)
- **适用源**：仅限 **Nikon `.nef` Bayer RAW**。
- **字段**：
  - `black_offsets`（对象）：传感器黑点相对偏移量（单位：码值偏移）：
    - `red`（浮点数）：`[-2048.0, 2048.0]`，默认 `0.0`。映射至 `[RAW Bayer] PreBlack1`。
    - `green1`（浮点数）：`[-2048.0, 2048.0]`，默认 `0.0`。映射至 `[RAW Bayer] PreBlack0`。
    - `blue`（浮点数）：`[-2048.0, 2048.0]`，默认 `0.0`。映射至 `[RAW Bayer] PreBlack2`。
    - `green2`（浮点数）：`[-2048.0, 2048.0]`，默认 `0.0`。映射至 `[RAW Bayer] PreBlack3`。
    - 支持单通道增量 patch，未传通道继承历史或 Base PP3，不额外写成 0.0。若 `green1 != green2`，PP3 自动生成 `PreTwoGreen=false`；两绿相互独立不覆盖。
    - **防覆盖机制**：当 `black_offsets` 显式生效时，PP3 自动写入 `Dehablack=false`，防止 Base PP3 的自动去黑斑/自动黑点估计覆盖用户手动微调。仅调整 `white_scale` 时不改动 `Dehablack`。
  - `white_scale`（浮点数）：`[0.1, 16.0]`，默认 `1.0`。映射至 `[RAW] PreExposure`（RAW 线性预曝光缩放因子）。
- **重要概念澄清**：
  - `black_offsets` 是 RawTherapee 的**黑电平相对微调偏移**，`white_scale` 是**RAW 数据线性增益乘数**；**绝非传感器的物理绝对 BlackLevel 或 WhiteLevel**。
  - 不自动用于替代常规曝光补偿（`exposure_ev`），也不盲目套用 `raw_qc` 测得的未旋转 visible CFA 饱和电平。

#### E. 镜头校正 (`lens_correction`)

校正前可用 inspect 的 lens_check=true 查引擎相邻镜头库中的精确名称候选；这不验证 RT 实际加载的数据库、机身卡口匹配或光学校正效果。缺少候选时考虑名称差异、手动匹配或适配 LCP，不把 exit 0 当成已校正。依据：[RawTherapee 镜头匹配说明](https://rawpedia.pixls.us/lens--geometry/)、[Lensfun 数据格式与卡口关系](https://lensfun.github.io/manual/latest/database.html)。
- **适用源**：RAW 源专用。
- **字段**：
  - `mode`（字符串枚举）：
    - `"none"`：禁用镜头校正（`LcMode=none`）。
    - `"lensfun_auto"`：Lensfun 自动根据 EXIF 匹配（`LcMode=lfauto`）。
    - `"lensfun_manual"`：Lensfun 手动指定机身镜头库（`LcMode=lfmanual`）。
    - `"lcp"`：Adobe 镜头配置文件校正（`LcMode=lcp`）。
    - *注：RT 5.13 的 `metadata` 模式在 Nikon NEF 上不支持，请求传入时严格拒绝。*
  - `distortion`（布尔）：是否启用畸变校正（`UseDistortion`）。
  - `vignetting`（布尔）：是否启用暗角校正（`UseVignette`）。
  - `ca`（布尔）：是否启用色散/色差校正（`UseCA`）。
  - **`lensfun_manual` 模式专属字段**：
    - `camera_make`（非空单行字符串）：机身品牌（`LFCameraMake`）。
    - `camera_model`（非空单行字符串）：机身型号（`LFCameraModel`）。
    - `lens`（非空单行字符串）：镜头名称（`LFLens`）。
  - **`lcp` 模式专属字段**：
    - `path`（字符串）：LCP 文件路径（`.lcp` 后缀）或受支持的 builtin:lcp 别名；在已继承 lcp 模式时可只更新 path。
    - `sha256`（64位小写十六进制字符串）：LCP 指纹。显式绑定内置别名时可省略，由工具复核包内固定指纹后填充；外部文件仍须显式给出。内置唯卓仕模型未标定，仅供用户明确选择的实验，并返回警告。
- **模式切换与物理绑定规范**：
  - 切换模式（如从 `lensfun_manual` 切至 `lcp` 或 `none`）时，**必须在请求中包含 `reset_fields: ["lens_correction"]`**，防止异构模式残留字段导致验证失败。
  - **路径物理锚定**：传入相对路径的 LCP 文件在首次绑定校验后，**立即解析并固定为绝对规范物理路径**存入 Session `effective_adjustments`，防止跨工作目录调用时失效。

需要外置 LCP 时，按 [接入与验收流程](lens-profiles.md) 核对来源、指纹、会话绑定与对照；文件完整性不代表光学校准有效。

#### F. 输入色彩配置文件与 DCP 控制 (`input_profile`)
- **适用源**：RAW 源专用。
- **字段**：
  - `mode`（字符串枚举）：
    - `"camera_standard"`：映射至官方机身标准矩阵 `(camera)`。
    - `"auto_matched"`：映射至官方自动匹配机身色彩 `(cameraICC)`。
    - `"custom"`：加载外部自定义 DCP 或 ICC/ICM 配置文件。
  - **`custom` 模式专属字段**：
    - `path`（字符串）：配置文件路径（`.dcp`, `.icc`, `.icm` 后缀）。
    - `sha256`（64位小写十六进制字符串）：配置文件的 SHA-256 校验和。
  - **DCP 专属微调开关**（仅当使用 DCP 配置文件或支持 DCP 的机身时生效）：
    - `tone_curve`（布尔）：应用 DCP 内嵌色调曲线（`ToneCurve`）。
    - `look_table`（布尔）：应用 DCP 风格查找表（`ApplyLookTable`）。
    - `hue_sat_map`（布尔）：应用 DCP 色相/饱和度/明度三维映射表（`ApplyHueSatMap`）。
    - `baseline_exposure`（布尔）：应用 DCP 基准曝光偏移（映射至固定 5.13 键 `ApplyBaselineExposureOffset`）。
    - `dcp_illuminant`（整数枚举 `0`, `1`, `2`）：DCP 标定光源选择（`0`=自动根据白平衡插值，`1`=第一标定光源，`2`=第二标定光源）。
- **模式切换与路径规范**：
  - 切换模式时**必须传入 `reset_fields: ["input_profile"]`**。
  - 相对路径在首次校验后自动解析并绝对锚定至 `effective_adjustments`。

自定义 input_profile 已有 mode=custom 时，后续可仅补 path 与对应 sha256，模式和其他项继续继承，路径按当次工作目录解析为绝对路径。换文件应显式提供新指纹；不会为普通路径自动信任新文件。首次或 reset 后仍需 mode/path/sha256。

---

## 3. 继承、单项复位 (`reset_fields`) 与模式切换机制

1. **增量 Patch 深度合并**：
   - 每次提交的 `adjustments` patch 仅需包含待修改项。
   - 系统先校验 patch 合法性，再与会话上一轮已生效参数深度合并，最后执行全量有效性校验。
2. **单项复位（`reset_fields`）机制**：
   - 接受非空数组，元素必须为 24 个顶层类别之一（如 `["white_balance", "lens_correction"]`），不支持点分路径。
   - 执行顺序：**继承历史 ➔ 移除指定类别 ➔ 合并新 patch ➔ 全量校验**。
   - `reset_fields` 与全局 `reset_adjustments` 严格互斥。
3. **白平衡复位特殊语义**：
   - 复位 `white_balance` 是从有效调整中移除白平衡，使 RawTherapee **回归应用 Base PP3 模板的预设白平衡**；**绝对不等于恢复相机拍摄时的原始白平衡**。
   - 若在复位同时重新指定白平衡，必须成对提供完整的 `temperature` 与 `green`。
4. **复合模式字段切换（Lens / Input Profile）**：
   - `lens_correction` 与 `input_profile` 内部字段具有模式互斥性（例如 `lcp` 模式要求 `path`/`sha256`，而 `lensfun_manual` 要求机身镜头字符串）。
   - 从一种模式切换至另一种模式，必须使用 `reset_fields: ["lens_correction"]` 或 `reset_fields: ["input_profile"]` 清空旧状态后再注入新参数。

---

## 4. RAW CFA 诊断 (`raw_qc`) 与调校边界

- 工具提供独立的只读诊断 Action：`raw_qc`。
- **环境依赖**：惰性加载 `rawpy` 与 `numpy`。若环境缺少依赖，返回标准的 `DEPENDENCY_MISSING` 错误封包，并提供同一 Python 环境的安装建议；**绝不导致进程崩溃或抛出 Python Traceback，且绝不影响核心 `render` 渲染能力**。
- **诊断范围**：直接读取未旋转的原始可见 CFA 阵列（`raw_image_visible`, `raw_colors_visible`），返回：
  - 解码器与 LibRaw 版本、可见画幅尺寸、CFA 模式与通道分布描述。
  - 各 Bayer 物理通道（R, G1, B, G2）的采样点数、最小/最大/均值。
  - 解码器标称的黑电平（`black_level`）与白电平（`white_level`）及其来源依据。
  - 达到或超过白电平的像素比例（`clip_white_pct`）及低于或等于黑电平的像素比例（`sub_black_pct`）。
- **边界与诚实声明**：
  - `raw_qc` 测量的是解码器可见的原始数字码值，**不证明 16-bit 线性精度、ADC 硬件物理饱和、感光元件动态范围余量或美学观感**。
  - `raw_qc` 的统计结果仅供人类或代理评估场景曝光倾向，**禁止自动将测得的数值直接换算并套用为 `raw_calibration` 的黑点偏移或白点缩放**。
