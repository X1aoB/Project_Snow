# 复用已发布头像与已审核 U4 素材

`scripts/build_character_media_release.py` 可以在新版本目录中复用已有头像发布包与私有角色素材包。源文件只读；新的公开包仍需通过全部角色审批、来源与公共运行时校验，不能通过修改私有包的公开标志完成发布。

已有头像入口必须同时提供 `--existing-avatar-release-root`、`--existing-avatar-manifest-sha256` 和 `--existing-avatar-checksums-sha256`。输入必须是 `project-snow-avatar-media-3` 的完整公开包，包含准确的 22 个角色与分析员的 46 张 96/200 像素 WebP。构建校验原有来源/署名字段、图像格式、尺寸和所有文件哈希，按原字节复制，不要求重新获取或裁剪旧原图。原始来源与许可记录保持原值；这不代表本次重新完成了独立权利核验。

已审核私有包入口必须同时提供 `--expression-package-root`、`--expression-package-manifest-sha256`、`--expression-package-checksums-sha256` 以及 `--rights-sha256`。`--expression-root` 和 `--approval-root` 仍指向私有原始来源与已封存的逐角色批准。构建先运行既有审批、原生图层合成与来源验证，再核对私有包的完整清单、批准预览哈希、图层原始 PNG 哈希、图层位置和剧情动作回退。通过后保留已审核 PNG/WebP 字节，仅在新包的运行时清单中加入这次发布决定与来源绑定。私有运行时元数据逐层限定公开字段；静态契约和转换说明必须与构建器的固定公开值一致，禁止未知字段或已知字段中的私有对象、备注进入新清单。

发布决定继续采用 `project-snow-character-expression-rights-1`，须列出全部角色对应的原始来源和批准清单哈希，以及完整基础状态/剧情动作数量。采用用户指令的权利核验豁免时，`rights` 必须准确记录 `independent_verification:false`、`verification_status:"not_performed"`、`public_use_authorized:true` 和明确的 `waiver` 范围，不能据此声称取得第三方许可或完成权利清查。

复用私有包的 `rights.publication_authorization` 必须含下列字段：

- `method:"user_instruction"`、`source_thread_id`、`source_turn_id`、带时区的 `recorded_at`。
- `statement_sha256`：原始用户发布指令的摘要；正文留在私有授权记录。
- `authorization_record_sha256`：私有授权记录摘要；不将私有记录或本机路径复制进公开包。
- `approval_event_sha256`：与私有素材包一致的本批用户视觉批准事件。
- `private_package_manifest_sha256` 与 `private_package_checksums_sha256`：本次获准公开的私有包指纹。

输出采用 `project-snow-character-media-4`，保留所有头像、分析员、18 态静态后备、剧情动作与原生图层，并记录两个来源包和本次发布决定的摘要。`SHA256SUMS` 覆盖全部输出文件；构建再次通过实际 `PublicMediaCatalog` 的完整校验后，才将临时目录改名为新的版本目录。版本目录已存在、输入变化、符号链接/重解析点/硬链接、额外文件、缺失文件、重复校验项和目录逃逸均拒绝。构建本身不上传服务器、不切换流量，也不修改旧版本。
