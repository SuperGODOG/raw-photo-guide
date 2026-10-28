# RAW Photo Guide (`raw-photo-guide` v0.7.0)

基于 RawTherapee 5.13 命令行与 ExifTool 的 RAW / JPEG 照片调修与会话管理工具。主要用于 AI Agent 自动化调修，也可作为本地无头（Headless）暗房工具通过命令行调用。

---

## 核心设计原则

1. **审美决策归用户**
   - 不使用生成式 AI 扩散模型进行人脸或物体重绘，不篡改光学成像的真实性。
   - 用户不必先准确描述风格；Agent 可提出探索试片，通过看图选择逐步明确偏好。色彩风格、明暗取舍、细节保留程度由用户决定；剪影、高反差、低饱和、浓郁胶片感都是合理的目标，不预设“必须亮白清晰”为唯一标准。
2. **呈现代价与取舍**
   - 全局曝光提亮主体时可能连带提亮背景，强力降噪在平滑杂色的同时会损失微弱反差纹理。
   - Agent 先按 [原片诊断](references/preflight.md) 取得相关证据，区分原片信息不足、工具能力不足和视觉方向；只说明影响选择的限制，同时提供可行方向，不擅自宣布“最佳版本”。
3. **状态可追溯与干净回退**
   - 采用 CAS（Compare-And-Swap）版本机制记录每轮修改，避免并发写冲突。
   - 当用户想基于历史旧版重新微调时，使用完整快照复位（`reset_adjustments`），彻底清除后续探索引入的参数残留。

---

## 详细功能列表

### 1. 24 类受控图像调整参数 (`available_adjustments`)

工具支持对 RawTherapee 引擎的 24 类受控参数进行精细化调整。参数范围及说明如下：

| 参数类别 | 适用源 | 关键字段与数值范围 | 功能说明 |
|---|---|---|---|
| **曝光补偿** (`exposure_ev`) | RAW / JPEG | `-2.00` ~ `+2.00` (步长 0.05) | 全局线性曝光增益。RAW 重新解算传感器线性数据；JPEG 为烘焙亮度的平移。 |
| **白平衡** (`white_balance`) | RAW / JPEG | `temperature`: `1500` ~ `60000` K<br>`green`: `0.500` ~ `2.000` (绿色色调倍率) | 调整画面色温与色调（Tint）。RAW 基于感光元件物理通道计算，JPEG 为烘焙色彩偏移。 |
| **对比度** (`contrast`) | RAW / JPEG | `-100` ~ `+100` (整数) | 全局影调反差调整。 |
| **饱和度** (`saturation`) | RAW / JPEG | `-100` ~ `+100` (整数) | 全局色彩浓淡与鲜艳度控制。 |
| **色调曲线** (`tone_curve`) | RAW / JPEG | `modes`: `Standard` / `FilmLike` / `Perceptual`<br>`points`: 2~16 个控制点坐标 `[x, y]` (`0.0`~`1.0`) | 针对阴影、中间调、高光的分区曲线微调。 |
| **高光压缩** (`highlight_compression`) | RAW / JPEG | `amount`: `0` ~ `500`<br>`threshold`: `0` ~ `100` | 压缩接近过曝区域的色调曲线滚降，避免亮部生硬死白。 |
| **高光重建** (`highlight_recovery`) | RAW 专属 | `enabled`: `true` / `false`<br>`method`: `luminance` / `cielab` / `blend` / `color` / `coloropp` | 当 RAW 高光通道轻微溢出时，利用未溢出通道的残余信息尝试重建高光细节。 |
| **降噪** (`denoise`) | RAW / JPEG | `enabled`: `true` / `false`<br>`luma`: `0.0` ~ `100.0` (亮度降噪)<br>`chroma`: `0.0` ~ `100.0` (色度降噪) | 消除高 ISO 或暗部提亮后的噪点杂色；调整时需兼顾高频纹理锐度。 |
| **锐化** (`sharpen`) | RAW / JPEG | `enabled`: `true` / `false`<br>`amount`: `10` ~ `500`<br>`radius`: `0.30` ~ `3.00` | USM 反差锐化，提升边缘微反差与清晰度。 |
| **去马赛克** (`demosaic`) | RAW 专属 (Bayer) | `method`: `amaze`, `rcd`, `dcb`, `ahd`, `vng4`, `lmmse` 等 15 种插值算法 | 将 Bayer 滤镜阵列 RAW 数据插值为全彩 RGB。默认采用 AMaZE，针对细腻纹理或高感噪点可切换对应算法。 |
| **镜头校正** (`lens_correction`) | RAW 专属 | 支持 Lensfun 自动/手动几何畸变校正、渐晕（暗角）补偿、色差（TCA）修正；支持加载 Adobe LCP 文件 | 修正广角/大光圈镜头的桶形/枕形畸变与四角失光。 |
| **输入色彩配置** (`input_profile`) | RAW 专属 | 支持 `camera_standard`、`auto_matched` 或自定义 DCP/ICC 配置文件 | 设定 RAW 解码时的相机色彩矩阵。 |
| **传感器预处理** (`raw_preprocess`) | RAW 专属 (Bayer) | 支持热像素/死像素过滤、行降噪（Line Denoise）、双绿色通道均衡（Green Equilibration） | 抑制长曝光或老旧传感器产生的坏点与双绿失衡现象。 |
| **传感器校准** (`raw_calibration`) | RAW 专属 (Bayer) | 支持黑电平、白电平手工微调 | 修正特殊传感器基准电平偏差。 |

新增 10 类常用控制：`shadows_highlights`、`vibrance`、`hsv_equalizer`、`black_white`、`local_contrast`、`graduated_filter`、`transform`、`perspective`、`crop`、`resize`。按用户目标选接口及裁切/导出流程见 [常用控制指南](references/common-controls.md)；数值范围按需查询 capabilities，避免重复读取大表。

> **边界说明**：本工具不提供局部选区画笔、图层蒙版、人脸磨皮美颜或生成式消除功能。

---

### 2. 支持的 CLI 操作指令 (`actions`)

`rt_tool.py` 包含 10 个标准动作，通过统一的 JSON 协议进行调用：

- `doctor`：环境诊断。检查 RawTherapee 5.13 引擎、ExifTool 是否就绪，输出空间检查由 render 执行（低于 500 MiB 停止渲染）。
- `capabilities`：能力自省。查询针对当前输入源（RAW 或 JPEG）支持的具体参数列表、取值范围与内置色彩配置。
- `inspect`：源片校验与元数据提取。调用 ExifTool 获取相机、镜头与曝光信息；图像统计用 qc/raw_qc。
- `profile`：色彩与模板配置查询。获取内置或外部 PP3 模板、LCP 镜头文件、ICC 色彩空间的绝对路径与 SHA-256 校验和。
- `session`：多轮修图会话管理。支持 `init`（创建会话）、`load`（加载恢复）、`record_feedback`（记录用户原话与意图变更）。
- `render`：物理图像显影。绑定 Session 与 CAS 版本号，渲染输出图（JPEG / TIFF16，可指定 crop/resize）并同步生成轻量预览缩略图。
- `qc`：成片统计检查。计算渲染产物的平均亮度（`mean_luma`）、暗部溢出率（`shadow_clip_pct`）与高光溢出率（`highlight_clip_pct`）。
- `raw_qc`：RAW 底层数据分析。直接对感光元件未旋转的原始 Bayer CFA 阵列进行统计诊断。
- `prune_history`：精简历史试片。安全删除中间无用的试片图片以节约磁盘，保留版本元数据，并保护最新成片与用户选中的定稿。
- `clean`：临时孤立文件清理。清理渲染异常残留或未提交的中间垃圾产物。

---

## 运行环境与安装依赖

1. **RawTherapee 5.13**：
   - 官方下载并安装 RawTherapee 5.13。
   - macOS 默认路径：`/Applications/RawTherapee.app/Contents/MacOS/rawtherapee-cli-bin`（Linux/Windows 请确保 `rawtherapee-cli` 在 PATH 中）。
2. **ExifTool**：
   - macOS：`brew install exiftool`
   - Linux：`sudo apt-get install libimage-exiftool-perl`
3. **Python 3.10+**：
   - 推荐使用 [uv](https://docs.astral.sh/uv/) 直接执行，自动解析脚本依赖。

---

## 命令行调用指南与完整示例

在项目根目录下运行。`--request` 参数接受 **UTF-8 JSON 文件路径** 或 `-`（代表通过标准输入 stdin 传入）：

```bash
# 方式 1：通过文件传入
python3 scripts/rt_tool.py --request /path/to/request.json

# 方式 2：通过标准输入传入
python3 scripts/rt_tool.py --request - < /path/to/request.json
```

初学者偏好模糊时可先提出标为待选的探索方向，再根据试片反馈收敛；具体流程见 [对话指南](references/dialogue.md)。以下为已有明确目标的一次完整会话示例（示例中的路径和哈希为占位符，实际运行时请替换为真实文件路径）：

### 步骤 1：检查运行环境

```json
{"action":"doctor"}
```

### 步骤 2：查询工具能力（以 RAW 为例）

```json
{"action":"capabilities","source_type":"raw","detail":"summary"}
```

### 步骤 3：获取基础底片（Base PP3）的路径与哈希

```json
{"action":"profile","profile":"builtin:neutral-rec2020-rt513","kind":"pp3"}
```
*(工具将返回该底片的绝对路径与 SHA-256 校验和，后续渲染需原样填入)*

### 步骤 4：确认用户意图后初始化 Session

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

### 步骤 5：提交调参并渲染第一版试片

通过 `expected_revision: 0` 绑定刚刚创建的会话版本，执行渲染：

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
渲染成功后，会返回 `result.output`（全尺寸成片）与 `result.preview.path`（轻量预览图），Session 版本推进至 `revision: 1`。Agent 需实际查看预览图，向用户汇报画面改善与代价。

### 步骤 6：沉淀用户反馈与全量 Keep 继承

用户看图后提出修改意见。调用 `record_feedback` 记录原话，并将新旧保持项全量合并到 `keep` 中：

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

### 步骤 7：重新加载会话或从历史版本继续

如需中断后恢复，或查询历史选版记录：

```json
{"action":"session","operation":"load","session_path":"/用户项目/session.json"}
```

如果用户想要**回退到历史版本（如第 1 版）重新微调**，使用 `reset_adjustments: true` 并传入历史快照参数，可干净清除后续探索中引入的冗余参数。

---

## 自动化测试与工程验证

本项目内置完整的单元测试与自动化验证套件，涵盖接口 Schema 校验、CAS 并发冲突、版本回退参数隔离、文档示例契约验证等。

使用 `uv` 运行全部 154 项测试：

```bash
uv run --script scripts/test_rt_tool.py
```

或使用系统 Python：

```bash
python3 scripts/test_rt_tool.py
```

---

## 资源与说明

- **中性底片模板**：[`assets/neutral-rec2020-rt513.pp3`](assets/neutral-rec2020-rt513.pp3)，提供中性色彩空间的起始参数配置，非相机原厂标定。
- **实验性镜头文件**：[`assets/lens_profiles/viltrox-af-50f2-z.lcp`](assets/lens_profiles/viltrox-af-50f2-z.lcp)，针对唯卓仕 AF 50mm f/2.0 Z 的实验性配置，未经官方实验室校准，仅供技术测试，默认不强制启用。
- **开源协议**：[MIT License](LICENSE)。
