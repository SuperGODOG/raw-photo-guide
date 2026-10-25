# 外置 LCP 的接入与验收

仅在用户需要镜头校正、Lensfun 缺少候选或已提供 LCP 时阅读。本流程使用现有 profile、session、render API，不新增注册工具或自动下载服务。

## 选择资源与保存依据

- 先核对 inspect 返回的机身、镜头、焦距、光圈及可用的对焦距离信息；lens_check 的未命中仅限被扫描数据库，不能推断所有 Lensfun 版本均未收录。
- 优先使用来源明确、适配该镜头型号、卡口、传感器画幅和 RAW 输入的实测配置。核对配置覆盖的焦距、光圈与距离；同为“50mm f/2”不足以证明适配。
## 内置实验资源与外置校准配置

- 当前随包的 `builtin:lcp:viltrox-af-50f2-z` **缺少系数来源、标定目标和误差测量记录**，不能称为已标定或已验证准确的专属校正配置。保留旧别名和原文件字节用于兼容与显式实验；普通修图不自动采用它，也不以其代替可信校准文件。
- `inspect(lens_check=true)` 在扫描 Lensfun 之前独立检查内置文件。实验资源通过完整性检查时返回 `experimental_profile_available` 和 `builtin_profile_status=unverified`；不返回正式 `builtin_profile_available`。Lensfun 不可用不妨碍发现内置资产；缺失/损坏资产返回 unavailable，不报告可用。
- `capabilities.builtin_profiles` 是可解析别名目录，不是光学推荐列表；同时查看 `builtin_profile_metadata` 的 calibration_status、recommended 和 usage。profile 也返回 builtin_profile 元数据及 warnings。
- 仅当用户明确要求评估该实验模型时使用旧别名。绑定时自动补齐受包内固定指纹约束的哈希；显式错误哈希仍被拒绝。哈希证明字节一致性，不证明镜头适配或光学准确。
- 普通修图使用来源明确的外部 `.lcp`：可放入项目 profiles/，按绝对路径和哈希绑定。该目录只是组织约定，不自动下载、扫描或导入资源。
- 不凭镜头名称编造径向或暗角系数；中心不变、四角提亮和普通夜景像素差只能证明处理有响应，不能证明模型准确或 EXIF 自动匹配成功。
- 将来源说明、配置版本、适用条件写入 confirmed_brief 或 user_feedback。缺少来源时如实记录未核实；不要新增 Schema 未支持的字段。

## 指纹、绑定与增量更新

以下为用户明确要求的内置实验流程。普通外置配置改用真实文件绝对路径，并以返回的 path/sha256 绑定；不从文件名推断已校准。先获取规范路径和指纹：

```json
{
  "action": "profile",
  "profile": "builtin:lcp:viltrox-af-50f2-z",
  "kind": "lcp"
}
```

profile(kind=lcp) 检查文件存在、后缀并计算哈希；已知内置文件还须符合固定包内指纹。verification_status=file_integrity_verified 不表示 XML 语法、RT 可解析性、镜头适配或光学效果已验证。该实验资产返回 calibration_status=unverified、recommended=false，以及 UNVERIFIED_LENS_PROFILE 警告；通过原绝对路径使用同一资产仍保留此标记。

已有会话先 load，随后使用返回的 revision 替换下面的 0。仅在用户明确选择实验后绑定别名，自动解析绝对路径与哈希；此请求不代表光学效果已被验证：

```json
{
  "action": "session",
  "operation": "record_feedback",
  "session_path": "/用户项目/session.json",
  "expected_revision": 0,
  "user_feedback": "用户明确要求评估未标定的唯卓仕实验 LCP；开启畸变处理，保持自然暗角；不作为准确校正交付。",
  "reset_fields": ["lens_correction"],
  "adjustments": {
    "lens_correction": {
      "mode": "lcp",
      "path": "builtin:lcp:viltrox-af-50f2-z",
      "distortion": true,
      "vignetting": false,
      "ca": false
    }
  }
}
```

已有 mode=lcp 的会话还可只更新 path，无须重复 mode；工具从历史继承模式并填入新别名对应的指纹。若 reset_fields 已清除该类别或首次绑定，仍须提供 mode。外部文件的哈希不自动推导，改绑外部文件时须显式给出匹配指纹。

未传 keep/change 会保留已有列表；只有用户修改了保持项/修改项时才更新相应列表。随后 render 使用反馈返回的新 revision，其他必填参数见 [tool-api.md](tool-api.md)。render 会复核哈希并保存资源快照，资源缺失或改变时停止，不静默改用其他文件。

## 区分调用成功和校正准确

1. 配置指纹成功：文件存在、哈希已记录；尚不证明引擎能解析。
2. render 成功：实验资产的 UNVERIFIED_LENS_PROFILE 警告写入返回值及 manifest，不因渲染成功升级校准状态。检查 manifest 的资源快照、有效 PP3 和完整输出；exit 0、serialized 或 lcp_file_integrity=verified 仍不证明校正准确。保持 effect_verified / optical_correction_verified 的工具原值，不人工改成 true。
3. 对照验收：固定曝光、白平衡、曲线与其他参数，仅切换镜头校正。用完整输出检查直线、边界裁切、角落亮度/噪点及 100% 彩边；有几何重采样时，同坐标像素差不能当作同一场景点的光度误差。光学准确性需要标定目标或可信参考，普通夜景 A/B 只能支持“效果发生且视觉可接受”。

暗角可按用户意图保留，不因开启畸变校正就一并开启暗角/色差校正。检查 Base PP3 中是否已有手动几何、暗角或平场处理，避免重复或互斥；工具未提供相应滑块时，使用经 GUI 审核的 Base，不能另造未知 PP3 字段。

RawTherapee 文档指出 LCP 色差校正需要 EXIF 对焦距离；缺少信息时不能承诺 ca=true 生效。当前工具将整个 lens_correction 类别限制为 RAW，这是封装边界，并非声称 RT 本身完全不能处理 JPEG 镜头畸变。

依据：[RawTherapee Lens Geometry（LCP、互斥关系及限制）](https://rawpedia.pixls.us/lens--geometry/)。
