# Tesseract

[English](README.md) · **简体中文**

Tesseract 是一款 Windows 桌面代理客户端，内核是
[Mihomo（Clash.Meta）](https://github.com/MetaCubeX/mihomo)。它把你自己的服务器、
付费订阅、网上收集的公共应急节点和可选的固定出口分成几层：上一层走不通，就自动换到
下一层；它恢复了，再换回来。

> 本项目与 Tesseract OCR 无关。Tesseract 只是代理客户端，不提供节点或订阅服务。

## 下载

在 [Releases](https://github.com/definitlynotme/Tessract/releases) 下载最新版本：

| 文件 | 说明 |
|---|---|
| `Tesseract-Setup-<版本>-win-x64.exe` | 安装包（推荐）。装到 `Program Files`，添加开始菜单项；缺少 Microsoft Edge WebView2 运行时会自动安装。 |
| `Tesseract-<版本>-win-x64-portable.zip` | 便携版。解压到任意位置，运行 `Tesseract.exe`。需要 WebView2 运行时（新版 Windows 10/11 已自带）。 |
| `SHA256SUMS.txt` + `SHA256SUMS.txt.sig` | 所有文件的校验和，以及它的 Ed25519 签名。 |
| `sbom.cdx.json`、`Tesseract-<版本>-third-party-sources.zip` | 软件物料清单，以及许可证要求公开的第三方源码。 |

系统要求：Windows 10 或 11，64 位。安装包没有 Authenticode 代码签名，第一次运行时
Windows SmartScreen 可能会要求确认；不放心的话，可以把文件的 SHA-256 和
`SHA256SUMS.txt` 对一下。

## 三步开始

1. 在「层级」页点「导入」，放进节点链接、订阅地址，或从其他客户端导出的文本。
2. 回到「总览」，点「启动服务」。
3. 看到「运行中」，就连上了。关掉窗口也没关系，它会留在任务栏右下角继续工作。

第一次打开时会有一封简短的「给用户的信」讲这些；想再读，点左上角的 Tesseract 就行。

## 层级

| 层级 | 放什么 | 怎么工作 |
|---|---|---|
| **Vanguard** | 你自己的服务器（VPS、自建节点） | 平时都走这里。同一台服务器可以有多个协议，按顺序回退。 |
| **Sentinel** | 订阅来的节点 | Vanguard 都走不通时接上。 |
| **Bastion** | 网上收集来的公共节点（自动节点池叫 **Garrison**） | 最后的备用，默认关闭。运营者不明：流量走 Bastion 时，别登录账号，也别输入密码。 |
| **Gateway** | 固定出口 | 可选。想让网站总看到同一个地址时再用；当前层级先连到 Gateway，再由它访问目标。 |

这几层是优先顺序，不是固定的多跳链路。一个节点有多个协议时，在任何地方都只算一个节点。

## 接管方式

「配置」里有两个独立的开关：

- **系统代理**：大多数时候用它就够了，浏览器和多数软件都会经过 Tesseract。
- **虚拟网卡模式（TUN）**：有些软件不理会系统代理，比如一些游戏和命令行工具。打开它，这台电脑的
  所有流量都会经过 Tesseract。需要以管理员身份打开 Tesseract。

如果别的代理软件改掉了系统代理，「总览」上会提示；关掉那个软件，再点「重启服务」就好。

**熔断保护**：所有层级都断开时，它用 Windows 防火墙规则拦下流量，免得你的真实 IP
露出去。放行例外可以在「配置」里设置。没有特别的理由，别关掉它。

## 路由、DNS 和其他配置

- **代理路由规则**：一张有顺序的规则表（直连 / 代理 / 拦截），可以填域名、IP、
  `geosite:` / `geoip:` 集合和规则集。表里显示什么，运行的就是什么，后面不会偷偷追加
  规则。新配置自带两条地区默认项（`geosite:CN`、`geoip:CN` → 直连），可以删除或替换。
- 还有 **DNS 解析**、**延迟上限**、**Bastion 设置**、**提醒与通知**、**检查更新**。
- 界面语言：简体中文、English、Deutsch、Español、Français、日本語、한국어、
  Português (Brasil)、Русский。

支持的常见节点协议有 VLESS、VMess、Trojan、Shadowsocks、SSR、SOCKS5、Hysteria2、
TUIC、AnyTLS、Naive（HTTPS）和 WireGuard；具体能填的字段取决于协议和导入方式。

## 你的数据

| 内容 | 位置 |
|---|---|
| 配置（`config.yaml`、`config.local.yaml`、`sources.yaml`） | `%APPDATA%\Tesseract` |
| 日志、缓存、运行数据 | `%LOCALAPPDATA%\Tesseract` |

节点链接、订阅地址和导出的配置里都有访问凭据，请像保护密码一样保护它们。
诊断包（在「运行日志」里导出）里有你访问过的网址和 IP，发给别人之前先看一眼。
卸载不会删除这两个文件夹；想彻底清除，请手动删除。停止使用时请在应用里停止服务
（不要直接结束进程），这样系统代理和防火墙规则才会恢复原样。

## 从源码构建

Tesseract 由三部分组成：

| 目录 | 组成部分 |
|---|---|
| `shell/` | Tauri 2 外壳（Rust、WebView2）：窗口、托盘、单实例、通知。产出 `Tesseract.exe`。 |
| `ui/` | 网页界面（原生 JavaScript，用 esbuild 打包）。 |
| 仓库根目录 | Python 后台（`runtime_main.pyw`、`bridge_*.py`、`desktop_runtime.py`、`manager.py`、`tier3_*.py` 等），负责运行 Mihomo、健康检查、路由和分层逻辑。用 PyInstaller 打包成 `tesseract-runtime.exe`。 |

工具链（Windows x64）：Python 3.12.7、Node.js 24、Rust 1.96（MSVC）、
Inno Setup 6.7，以及许可证工具 `cargo-about 0.9.3`（`--features cli`）和
`cargo-deny 0.20.2`。

```powershell
# 完整的本地发布构建：锁定依赖、测试、许可证/SBOM 检查、外壳 + 界面 + 后台构建、
# 冒烟测试、便携版、安装包和校验和。产物在 release\，候选目录是 dist-release\Tesseract。
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -SkipInstallerAcceptance
```

在管理员终端里运行并去掉 `-SkipInstallerAcceptance`，还会把安装包装进一个隔离目录、
做冒烟测试再卸载。`-SkipTests`、`-SkipPackagedSmoke`、`-SkipInstaller` 可以做更快的
部分构建。Python 测试用 `python -m pytest tests`，界面单元测试用 `npm --prefix ui test`。
外壳的开发构建见 `shell/README.md`。

正式版本由 `.github/workflows/release-windows.yml` 在推送带签名的 `vX.Y.Z` 标签时构建：
先在 GitHub Actions 上无签名构建，再由受保护的环境用更新密钥签名 `SHA256SUMS.txt`，
最后连同构建来源证明一起发布。应用内更新只接受清单能用内置公钥验证通过的版本。

## 许可证

Tesseract 以 [GNU 通用公共许可证第 3 版（仅限该版本）](LICENSE) 发布。第三方组件
及其许可证见 [THIRD_PARTY_NOTICES.shell.md](THIRD_PARTY_NOTICES.shell.md)；每个发布
版本都附带完整的许可证文本和 SBOM。
