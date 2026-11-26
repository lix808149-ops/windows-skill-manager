# Windows 技能管理器

同一个 AI 技能可以先在草稿里改，审查后再同步到使用的多个 AI 工具。工具维护技能来源、分发和恢复点，保留目标应用里已有的独立条目。

## 从克隆安装

适用 Windows x64，首次需要 Git 和网络。先打开希望对接的 AI 应用一次，让它创建用户目录。把 Git 文件或仓库链接交给这台电脑的 AI，并发送这句话。

> 请把我给你的 Windows 技能管理器 Git 文件或仓库克隆到本机固定目录，运行 setup.cmd --install，安装后告诉我识别到了哪些 AI 应用，以及哪些技能目录通过了验证。

本机固定目录建议用 `%LOCALAPPDATA%\AI-Skill-Manager`。如果提供的是 `.bundle` 文件，先核对随附 SHA-256，再用 `git clone <路径> <安装目录>`，Git 仓库链接使用同样的克隆命令。在克隆目录运行一个命令即可。

```powershell
.\setup.cmd --install
```

该命令校验依赖、预检安装路径与冲突、应用配置并独立核验文件。完整结果保存在 `install-result.json`。如已有同目录安装，再执行会校验现有配置。要先单独检查，可以用 `setup.cmd --plan`。

导入第三方技能前，在首次使用时运行 `Manage-Skills.cmd scan-setup` 下载并校验扫描器、初始化病毒库。普通工具和 MCP 安装包可用 `Manage-Skills.cmd scan <文件路径>` 扫描。

首次会从固定发布地址下载 Python、PyYAML 和 skillshare，再核对 SHA-256。默认只安装管理入口。加 `--with-starters` 可附带一个完全合成的示例技能。若应用实际使用非默认路径，可以加 `--target 'codex=D:\实际的技能目录'`。可选根键是 `codex`、`agents`、`antigravity`、`workbuddy-old`（WorkBuddy）和 `workbuddy`（WorkBuddy AI）。

本工具会按用户目录探测已知扫描位置。安装预览用于检查路径和冲突，`verify` 校验实际文件及链接。文件通过之后，再在各应用新会话确认它能发现所需技能。遇到同名条目先检查提示和实际来源。

## 编辑与恢复

用 `Manage-Skills.cmd draft <技能目录名>` 创建草稿，编辑命令返回的候选目录。运行 `Manage-Skills.cmd review <草稿ID>` 预览差异，再按返回的另一个 ID 执行 `Manage-Skills.cmd apply <更新ID>`。手动菜单 10、16、13 对应这三步。各应用随后可在新会话读取更新。

回退时运行 `Manage-Skills.cmd history` 查完整 ID，先用 `restore-plan` 查看范围再用 `restore` 恢复。若中断，先从 `status` 读取待恢复事务 ID。

用 `reconcile-plan` 查看仍与上次分发版本一致的落后副本，用 `reconcile` 补齐。手动改过的副本会保留，并继续提示差异。中断后用 `recover-plan` 预览当前事务的回退范围，用 `recover` 执行。

## 测试

在这个仓库的 PowerShell 中运行下列命令。首个测试从本地 Git 历史创建一个干净克隆，并在临时用户目录测试首次安装、审查、发布和恢复。第二项需要已经准备好的依赖，包含复制与换入失败的隔离演练。

```powershell
.\_python\python.exe -B -X utf8 _tools/portable/test_git_install.py .
.\_python\python.exe -B -X utf8 _tools/test_handoff_fixes.py
```
