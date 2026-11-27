# SocketPreflight v0.0.31 三分钟演示

两份样本使用 Debian 13 发行包里的原始 `cockpit.socket` 和 `ssh.socket`。实验通过单独的 drop-in 把 SSH 迁到 Cockpit 正在使用的管理端口。样本内的 `provenance.json` 记录各自来源和校验值。[Debian Cockpit](https://packages.debian.org/trixie/amd64/cockpit-ws/download) · [Debian OpenSSH](https://packages.debian.org/trixie/amd64/openssh-server/download)

## 0:00–0:35 · 说明切换窗口

将 `socket-preflight-v0.0.31.zip` 和 `SHA256SUMS` 下载到一个空目录。Windows 用 `Get-FileHash socket-preflight-v0.0.31.zip -Algorithm SHA256` 核对 ZIP，Linux 用 `sha256sum socket-preflight-v0.0.31.zip`。确认 SHA-256 与清单中同名条目一致后解压。[GitHub v0.0.31 发行页](https://github.com/HhWw96/socket-preflight/releases/tag/v0.0.31)

打开发行包中 `samples/debian-bad.zip` 内的 `plan.json`。部署前只启用 Cockpit，切换时两者共存，部署后只启用 SSH。目标允许双栈监听，Cockpit 的 `9090` 覆盖了 SSH 想使用的 `127.0.0.1:9090`。

## 0:35–1:25 · 提前定位冲突

安装 Python 3.10+ 和 Node.js 24 后，解压 SocketPreflight 发行包并在解压目录运行以下命令。

```text
python deployment_pipeline.py run --bundle samples/debian-bad.zip --cli socket-preflight-v0.0.31.cjs --out demo-bad
```

预期退出 `1`。在 `demo-bad/receipt.json` 查看 `rollout.transition.new_conflicts`，它指出原版 `cockpit.socket:7` 和实验 `20-move-port.conf:3`。Debian 13 / systemd 257 上，坏例两种顺序共 6 次均确认 `EADDRINUSE`。四个快照的 `systemd-analyze verify` 均通过。

可运行 `python -X utf8 summarize_receipt.py demo-bad/receipt.json` 查看 GitHub Actions 上同样的门禁摘要。

## 1:25–2:05 · 同一输入修正后放行

```text
python deployment_pipeline.py run --bundle samples/debian-fixed.zip --cli socket-preflight-v0.0.31.cjs --out demo-fixed
```

修正版在共存阶段让 SSH 临时改用 `19091`。Cockpit 退场后，SSH 再用 `9090`。命令预期退出 `0` 并写出 `demo-fixed/would-deploy.json`，其中的部署包 SHA-256 与门禁回执一致。隔离实测的 6 次共存全部成功。

如需演示从现有工作目录打包，可先用 `python -m zipfile -e samples/debian-fixed.zip prepared` 导出阶段目录，再运行 `python package_deployment.py --plan prepared/plan.json --output prepared/deployment.zip`。把这个 ZIP 交给同一条放行命令，换一个新的输出目录核对回执。

## 2:05–3:00 · 展示评审证据

| 评分维度 | 展示材料 |
| --- | --- |
| 工程深度 | 从发行包原件经历变更预检，再由下游核对同一份部署 ZIP。 |
| 技术贡献 | MoonBit 解析生效文件、追溯覆盖顺序，并定位只在共存阶段出现的双栈冲突。 |
| 生态增量 | 可直接接入本地流水线的门禁脚本，以及按发布标签和校验值运行的 GitHub Actions 工作流。 |
| 开发与验证 | Windows 与 Debian 发行包复验。原始 systemd 监听、内核错误和哈希交接都有回执。 |

第三方复跑可直接使用上述两条命令，每次使用新的输出目录。记录发行包 SHA-256、系统、Python 和 Node.js 版本、两次退出码、冲突双方配置行和放行标记的哈希。源码的 `verification/debian/` 保存实测资料。
