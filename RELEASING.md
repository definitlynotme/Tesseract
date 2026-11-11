# Releasing Tesseract / 发布流程

Maintainer notes. 面向维护者。

## 两个仓库 / Two repositories

- **私有工作仓库**（本目录）：日常开发，历史里有个人身份信息，**永远不推送**。
- **公开仓库**（默认 `..\Tessract-public`，远端 `https://github.com/definitlynotme/Tessract`）：
  历史里只有以 `Tesseract contributors <283048617+definitlynotme@users.noreply.github.com>`
  身份、用 SSH 密钥签名的提交。`scripts/push_to_github.ps1` 把私有仓库里未被忽略的文件树
  复制过去、做匿名/密钥审计、签名提交，再推送。第一次运行时由
  `scripts/bootstrap_public_repository.py` 建一个只有一个根提交的新仓库。

所以 `.gitignore` 决定了什么会公开：被忽略的（`.build/`、`.claude/`、本地配置等）不会出去。

## 一次性准备 / One-time setup

1. **GitHub 登录**：`gh auth login`，再 `gh auth setup-git`，让 `git push` 用同一份凭据。
2. **SSH 签名密钥**：默认用 `%USERPROFILE%\.ssh\id_ed25519`（必须是 Ed25519）。在 GitHub →
   Settings → SSH and GPG keys 里把它的公钥再添加一次，类型选 **Signing Key**，这样
   GitHub 才会把提交和标签显示为 Verified（发布工作流要求标签签名通过验证）。
3. **私有黑名单**：`%USERPROFILE%\.tesseract-release\public-denylist.txt`，每行一个绝不能
   出现在公开仓库或发布文件里的值（真实姓名、个人邮箱、本机用户名、私人域名……，至少 4 个
   字符）。它必须在仓库外面。
4. **GitHub 仓库设置**（`definitlynotme/Tessract` → Settings）：
   - Environments 里新建 `release`，加上需要审批的保护规则；
   - 在该环境里添加 secrets：
     - `TESSERACT_UPDATE_PRIVATE_KEY_B64`、`TESSERACT_UPDATE_KEY_PASSWORD`：更新签名私钥，
       对应 `app_info.py` 里的 `APP_UPDATE_SIGNING_PUBLIC_KEY`；
     - `PUBLIC_RELEASE_PRIVATE_DENYLIST`：上面那份黑名单的内容。
   - 私钥丢失时用 `scripts/generate_release_signing_key.py` 生成新密钥，并把新公钥加入
     `APP_UPDATE_SIGNING_PUBLIC_KEYS`（已安装的旧版只信任旧公钥，需要一个过渡版本）。

## 每次发布 / Each release

**一键发布**（推荐）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\publish_release.ps1 -DryRun   # 只检查和本地审计
powershell -ExecutionPolicy Bypass -File scripts\publish_release.ps1           # 正式公开发布
```

`publish_release.ps1` 先做只读检查（已提交、GitHub 登录与 `workflow` /
`admin:ssh_signing_key` 权限、SSH 签名密钥已登记、更新签名私钥与 `app_info.py` 里的公钥
一致、仓库状态、标签未用过），列出要做的事，问一次 y，然后：按需创建**公开**仓库、建
`release` 环境（只允许 `v*` 标签；`-RequireApproval` 再加人工审批）并写入 3 个 secrets、
调用 `push_to_github.ps1 -Release`，最后等发布工作流跑完，并像应用一样下载最新 Release、
用内置公钥验证签名（`scripts/publish_release_helper.py verify-live`）。
更新签名私钥默认在 `%USERPROFILE%\.tesseract-release\update-signing-key.pem`；还没有任何
公开 Release 时若找不到它，脚本会生成新密钥并把公钥写进 `app_info.py` 再提交；已经发布过
就拒绝换钥匙（已安装的副本只认旧公钥）。**这个文件和密码一定要备份。**
已存在的私有仓库如果有内容，脚本绝不把它改成公开（会连同历史一起公开），而是把它改名为
`<仓库名>-private-<日期>`（仍是私有，不删任何东西），再建一个新的公开仓库；本目录里指向
公开仓库地址的远端会改指向这个归档（没有归档时直接删掉这个远端），免得一次 `git push`
把私有历史推进公开仓库。gh 登录的账号必须是仓库所有者（`app_info.APP_REPOSITORY_URL`
里的那个），不是时脚本会先 `gh auth switch`，再提示用登着所有者账号的浏览器重新登录。

下面是脚本内部做的分步流程，手动发布时参考：

1. 在 `app_info.py` 设置 `APP_RELEASE_BASE_VERSION`（候选版用
   `APP_RELEASE_CANDIDATE_NUMBER`），在私有仓库里提交。
2. （推荐）本地完整构建一次：

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -SkipInstallerAcceptance
   ```

   产物在 `release\`：安装包、便携版、第三方源码包、`sbom.cdx.json`、
   `release-metadata.json`、`SHA256SUMS.txt`。在管理员终端里去掉
   `-SkipInstallerAcceptance` 还会做隔离安装验收。
3. 一键推送：

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\push_to_github.ps1 -Release
   ```

   - 不带 `-Release`：只更新公开仓库的 `main`。
   - 带 `-Release`：再创建签名标签 `v<版本>` 并推送，触发
     `.github/workflows/release-windows.yml`：构建 → 匿名/密钥审计 → 在 `release` 环境里签名
     `SHA256SUMS.txt` → 构建来源证明 → 发布 GitHub Release。
   - `-NoPush`：只在本地准备并审计公开仓库，什么都不推送（第一次建议先这样跑一遍）。
   - 远端已有不相关的历史时推送会被拒绝；脚本从不强推，是否覆盖由你自己决定。

## 不用 CI 的手动发布 / Manual release without CI

只在 CI 不可用时这样做：把本地 `release\` 里的文件上传到一个新的 GitHub Release，
用 `scripts/sign_release_manifest.py` 为 `SHA256SUMS.txt` 生成 `SHA256SUMS.txt.sig`
一并上传，再用 `scripts/audit_public_release.py --artifact release` 审计一遍。应用内更新
只接受签名能用内置公钥验证的版本。
