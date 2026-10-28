# 常用调整与能力边界（v0.7.0）

按本轮目标选类别；先 capabilities(detail=summary)，需要范围时只查所用类别。新增类别均支持 RAW/JPEG、增量继承和 reset_fields，结构化操作仍限定 RT 5.13。JPEG 颜色已烘焙，转换不能恢复其未记录的信息。

## 从用户目标选工具

| 用户想看到的变化 | 受控接口 | 实际边界 |
|---|---|---|
| 暗部清楚些、亮部压下来 | shadows_highlights | 按影调范围，不识别人脸；不能代替 RAW 高光重建 |
| 颜色更鲜活，尽量保留肤色 | vibrance | pastels 管低饱和颜色、saturated 管高饱和颜色；protect_skin 是颜色范围保护，非人脸分割 |
| 天空蓝色淡一点、某些颜色偏移 | hsv_equalizer | 全画面按颜色调整 HSV，其他同色物体也会变化；V 不等于感知亮度或 HSL 的 L |
| 尝试黑白 | black_white | 接入基础 Desaturation，不包含高级黑白混色器或一键胶片保证 |
| 纹理更有层次 | local_contrast | 整张图的局部邻域反差，不是空间选区；强度过大可能出光晕 |
| 上方压暗、下方更亮 | graduated_filter | 按空间渐变，不绕开人物边缘，也不是语义天空蒙版 |
| 转正、拉平地平线、镜像 | transform | rotate 为额外顺时针 0/90/180/270；straighten 为微旋转；autofill 可能裁边 |
| 建筑横竖线更正 | perspective | simple 水平/垂直透视；不是自动识别建筑或完整相机模型 |
| 去掉分散视线的边缘 | crop | 用同源同几何完整试片作坐标参考；不修改原始 NEF/JPEG |
| 分享图长边缩到某尺寸 | resize | Lanczos、保持比例；默认不放大；影响完整输出，不只是 preview |

既有接口继续覆盖曝光、白平衡、总曲线、反差、饱和度、降噪、USM 锐化、高光重建/压缩、去马赛克、RAW 预处理/校准、镜头及输入色彩配置。当前不接入任意画笔/多选区蒙版、语义选区、去雾专用工具、完整 RGB/Lab 曲线、任意 LUT/胶片模拟、多图对齐/HDR/降噪/景深合成。不能以“支持24类”宣称覆盖整款软件。

## 数值与启用规则

数值范围由 capabilities 提供；本指南不复制完整 Schema。以下为执行语义：

- 新类别内省略子字段会继承 Session 或 Base PP3；不会填满能力表的参考值。带 enabled 的新工具在类别存在且未显式 disabled 时启用，唯 black_white 只有 enabled 开关须明确提供。
- vibrance 固定 PastSatTog=false，使 pastels/saturated 独立；resize 固定长边模式、作用于裁切区域、Lanczos，allow_upscaling 缺省 false；black_white 固定 Desaturation/Auto=false；perspective 固定 simple。这些是此受控模式的明确算法选择。
- `enabled:false` 暂停该类别但保留历史数值；再次启用继续旧值。reset_fields 移除整个类别，回归 Base PP3（不一定是软件的关闭状态）。transform/perspective 无 enabled，用 reset_fields 回到 Base；微旋转归零可只传 straighten=0。
- resize 首次启用须 long_edge；HSV 首次启用须至少一条曲线；crop 首次启用须完整矩形与 reference_manifest。以后仅传待改子字段即可。曲线列表整条替换，其他曲线继承。
- graduated_filter 的 strength 是衰减档数：正值压暗，负值提亮；degree=0 影响上方，180/-180 反向。center_x/y 为相对画面中心的 -100..100 控制，不是像素；0 在中心。先看片确认受影响区域，勿把 strength 当全局 exposure_ev 的同向替代。
- HSV 曲线是周期 FlatCurve，点为 [x,y]：x 在 [0,1) 严格递增，y 在 [0,1]，0.5 中性；固定平滑切线。红=0、黄=1/6、绿=2/6、青=3/6、蓝=4/6、洋红=5/6，相邻颜色平滑过渡。不要重复 x=1，不要求 y 单调。它不是色相角度或百分比滑块；Agent 应按相对目标小步试片，不能承诺精确品牌色。

查询本轮定义：

```json
{"action":"capabilities","source_type":"jpeg","detail":"full","parameters":["shadows_highlights","vibrance","hsv_equalizer"]}
```

增量修改示例（路径、revision 和用户原话换为实际值）：

```json
{"action":"session","operation":"record_feedback","session_path":"/用户项目/session.json","expected_revision":1,"user_feedback":"暗部亮一些，颜色别太艳。","adjustments":{"shadows_highlights":{"shadows":25},"vibrance":{"pastels":5,"saturated":-10}}}
```

按颜色试调的示例，仅是相对控制，须看片验收；并非固定胶片预设：

```json
{"action":"session","operation":"record_feedback","session_path":"/用户项目/session.json","expected_revision":2,"adjustments":{"hsv_equalizer":{"saturation_curve":[[0,0.5],[0.333333333,0.5],[0.666666667,0.4]]}}}
```

## 裁切坐标与几何变换

1. 使用已有未裁切、未缩图的成功试片，保留 manifest.json 与同目录 effective.pp3。可以复用第一轮，不强制重复渲染。Base 含裁切/缩图时，先显式关闭 crop/resize 生成参考。
2. 如需旋转、镜像、拉直、透视或镜头校正，先完成这些几何设置，再以该版完整输出作为参考。改变 transform/perspective/lens_correction/demosaic 后要重新取参考，不能继承旧坐标。
3. x/y 从参考完整输出左上角计算，width/height 为输出像素；参考 QC 给尺寸。聊天预览坐标须按实际完整输出比例换算，不使用传感器 raw_qc ROI 坐标。工具校验矩形未越界，并绑定参考源片/Base/引擎/几何状态与文件哈希。
4. 首次只需传 reference_manifest；工具自动补绝对路径/reference_sha256，后续可仅更新矩形子字段。主动更换 reference_manifest 会绑定新文件；传入显式 reference_sha256 则校验它，陈旧哈希报错。
5. render 返回 manifest.geometry 的参考尺寸与绑定信息；启用 crop 且无受控 resize 时检查实际输出等于矩形尺寸，启用 resize 时检查长边。此检查不是自动确认构图好看，也不取代打开成片验收。

```json
{"action":"session","operation":"record_feedback","session_path":"/用户项目/session.json","expected_revision":3,"user_feedback":"裁去下方杂乱的部分。","adjustments":{"crop":{"x":0,"y":0,"width":6056,"height":3400,"reference_manifest":"/用户项目/output/完整参考版/manifest.json"}}}
```

裁切与缩图可组合；参考 manifest 与 effective.pp3 在后续仍需存在。prune_history 保留溯源容器，通常不影响坐标参考；clean 若删除未提交参考产物则会使绑定失效，不能把待清理的失败产物当长期参考。旧版本恢复继续按 dialogue.md 的完整快照规则。

## 最终导出别继承低分辨率

resize 是 adjustments，会随 Session 继承；它与逐次传入的 output_profile/format 不同。分享版可设 long_edge=2048 等，细节评价和最终全尺寸版应清除 resize 或显式 disabled。放大不会新增真实细节，启用前向用户说明目的。缩小后的 preview 也不能用于原片 100% 纹理验收。

```json
{"action":"session","operation":"record_feedback","session_path":"/用户项目/session.json","expected_revision":4,"reset_fields":["resize"]}
```

裁切后需要回到完整构图时单独 reset_fields=["crop"]；不要顺带清除用户认可的色彩和影调。若 Base 自带裁切或 resize，复位会回到 Base，须 enabled=false 才能明确关闭。

## 验证与上游依据

- 参数序列化与 GUI 范围：[RT 5.13 procparams.cc](https://github.com/RawTherapee/RawTherapee/blob/5.13/rtengine/procparams.cc)、[GUI 工具源码](https://github.com/RawTherapee/RawTherapee/tree/5.13/rtgui/tools)。
- [阴影/高光](https://rawpedia.pixls.us/shadows--highlights/)、[裁切](https://rawpedia.pixls.us/crop/)、[缩图](https://rawpedia.pixls.us/resize/)。
- 运行 `uv run --script scripts/test_rt_tool.py` 检查契约、继承、CAS 和失败路径；可选 `uv run --script scripts/smoke_controls.py --output-dir /绝对路径/测试输出` 启动真实 CLI，使用合成 JPEG 检查像素变化、方向和尺寸。它不等于真实 NEF 画质或用户审美验收；真实 RAW 继续使用 smoke_raw.py 与用户原片。
