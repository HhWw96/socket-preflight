# SocketPreflight 验证记录

## v0.0.6 至 v0.0.9 · 部署切换

测试配置依据公开事故重建。实验使用独立 Ubuntu 24.04.5 / WSL2 的 systemd 255，四组快照分别装入 manager。启动操作限制在各自的私有网络命名空间。[原始问题](https://github.com/systemd/systemd/issues/14044)

| 阶段 | 静态检查 | 实际启动 |
| --- | --- | --- |
| 旧单元单独监听 | 通过 | 3/3 成功 |
| 旧、新单元共存 | 通过 | 两种顺序共 6/6 次第二个单元失败，内核均返回 `EADDRINUSE` |
| 新单元单独监听 | 通过 | 3/3 成功 |
| 共存阶段重置并换端口 | 通过 | 两种顺序共 6/6 次同时监听成功 |

第一次实验供 v0.0.6 和 v0.0.7 使用。第二次复跑另外记录 `systemctl list-sockets` 在两种单独监听状态下的实际输出。两轮实验各 18 次。共存坏例在启动前的列表为空，静态检查退出 0。两者提供状态与语法信息，`rollout` 补充阶段、端口重叠和双方来源。[首次对照](v0.0.6-rollout-systemd.jsonl) · [第二次对照](v0.0.8-rollout-systemd.jsonl)

| 版本 | 归档验证 |
| --- | --- |
| v0.0.6 | [共存坏例和修正版分别退出 1、0](v0.0.6-rollout-windows.json) |
| v0.0.7 | [15 项切换检查](v0.0.7-rollout-windows.json) |
| v0.0.8 | [Windows](v0.0.8-release-windows.json) · [Ubuntu](v0.0.8-release-linux.json) |
| v0.0.9 | [Windows](v0.0.9-release-windows.json) · [Ubuntu](v0.0.9-release-linux.json) · [演示执行记录](v0.0.9-demo.txt) · [冻结清单](v0.0.9-freeze.json) · [源码快照](v0.0.9-source.zip) |

跨平台检查由项目在本地和临时 Ubuntu 上完成。第三方复跑状态待回执。[三分钟演示及复跑方法](../DEMO.md)

用 v0.0.5 的五组基准比较最终归档对应的 CLI。两端的业务输出完全一致。每版每组采样五次。Windows 双快照比较中位数由 1278.7 ms 变为 1298.9 ms，Ubuntu 由 147.3 ms 变为 144.7 ms。Ubuntu 两单元场景由 41.2 ms 变为 47.2 ms。[Windows 原始数据](v0.0.5-v0.0.9-benchmark-windows.json) · [Ubuntu 原始数据](v0.0.5-v0.0.9-benchmark-linux.json)

## v0.0.2 至 v0.0.5

四次构建均通过类型检查和 11 项 MoonBit 测试。归档复验在临时目录解包执行，所有发行文件的 SHA-256 保留在 [SHA256SUMS](../dist/SHA256SUMS)。

| 版本 | Windows ZIP 复验 | 本轮新增证据 |
| --- | --- | --- |
| v0.0.2 | [29 项通过](v0.0.2-cli-windows.json) | [systemd 文件选择和两种启动顺序，6 次均通过](v0.0.2-systemd-linux.jsonl) |
| v0.0.3 | [41 项通过](v0.0.3-cli-windows.json) | [映射地址的 7 个场景，39 次均通过](v0.0.3-systemd-linux.jsonl) |
| v0.0.4 | [54 项通过](v0.0.4-cli-windows.json) | 对比新增、已有、解除和无法确认的变化，覆盖输出截断及退出码 |
| v0.0.5 | [55 项通过](v0.0.5-cli-windows.json) | [Linux ZIP 57 项通过，含文件和目录符号链接](v0.0.5-cli-linux.json) |

Windows 每版均有 1 项符号链接检查因创建权限跳过。最终在 Ubuntu 24.04.5 / WSL2 用同版本 Node.js 24.13.0 补全了文件和共享目录的符号链接检查。两批新增 systemd 回执共 45 次试验。最终 ZIP 逐组与首版及这两批回执核对，合计覆盖 13 个场景、75 次实际试验。

前缀继承场景检查实际选中的文件及生效监听。映射场景检查点分和十六进制表示、IPv4 通配、不同 IPv4 地址、双栈通配和 IPv6-only 通配。每个冲突再次调用同一网络命名空间内的内核绑定，确认 `EADDRINUSE`。显式 IPv6-only 的映射配置通过静态语法检查，但单独启动时三次均返回 `Invalid argument`。

加载顺序和内核绑定分支的参考实现见 [systemd 255 的 drop-in 查找](https://raw.githubusercontent.com/systemd/systemd/v255/src/shared/dropin.c) 和 [Linux 6.18 的 IPv6 bind](https://raw.githubusercontent.com/torvalds/linux/v6.18/net/ipv6/af_inet6.c)。临时 Ubuntu 实例已清理。

在独立的临时 Linux VM 中复跑新增实测：

```text
sudo env CI=true python3 scripts/verify_extended_systemd.py --disposable-vm
```

在项目目录用最终 ZIP 对照回执：

```text
python scripts/verify_cli.py dist/socket-preflight-v0.0.5.zip --systemd-report verification/v0.0.1-systemd-linux.jsonl --extended-systemd-report verification/v0.0.2-systemd-linux.jsonl --extended-systemd-report verification/v0.0.3-systemd-linux.jsonl
```

## 性能基准

用同一批快照交替运行 v0.0.4 和 v0.0.5。先为每个版本预热两次，再记录包含 Node.js 启动、读取快照、判定及输出 JSON 的整条命令耗时。每次运行都核对退出码和完整 JSON，忽略自报版本号。文件系统调用次数来自另外的运行。

[Linux 基准原始记录](v0.0.4-v0.0.5-benchmark-linux.json) 在临时 Ubuntu 24.04.5 / WSL2 上每版每场景采样 13 次。配置了共享 drop-in 的 64 单元扫描中位耗时由 177.3 ms 降至 89.5 ms，目录打开次数由 256 次降至 4 次。两份 64 单元快照的比较由 302.7 ms 降至 131.6 ms。512 个监听的无冲突与密集冲突场景分别由 151.0 ms 降至 90.9 ms 和由 160.2 ms 降至 102.4 ms。

[Windows 基准原始记录](v0.0.4-v0.0.5-benchmark-windows.json) 每版每场景采样 9 次。带共享 drop-in 的 64 单元扫描由 4.21 s 降至 0.71 s，两份 64 单元快照的比较由 7.96 s 降至 1.23 s。512 个监听的无冲突与密集冲突场景分别由 1.63 s 降至 0.30 s 和由 1.66 s 降至 0.31 s。

```text
python scripts/benchmark.py dist/socket-preflight-v0.0.4.cjs dist/socket-preflight-v0.0.5.cjs --report verification/local-benchmark.json
```

## v0.0.1

验证日期为 2026-11-20（北京时间）。构建使用 `moon 0.1.20261104` 和 `moonc v0.10.20+65efd5b93`。类型检查与 11 项 MoonBit 回归均通过。

[Windows 归档复验](v0.0.1-cli-windows.json) 从发行 ZIP 解压到带中文和空格的新目录，用 Node.js 24.13.0 运行编译后的 CLI。25 项通过。Windows 权限不允许创建测试文件符号链接，因此该项记录为跳过。复验检查了双方行号、被遮蔽的配置、空赋值重置、退出码和无法确定的环境条件。

[真实 systemd 回执](v0.0.1-systemd-linux.jsonl) 来自临时 Ubuntu 24.04.5 / WSL2，systemd 版本为 `255.4-1ubuntu8.19`。将随包的配置以及三个派生变体装入 manager，测试端点在单独的网络命名空间内。每组按两种启动顺序各运行三遍，记录实际生效监听、文件来源、第二个单元的退出结果、日志及 `ss` 监听。Windows 归档复验将各组 CLI 的监听列表逐项与 Linux 结果交叉核对。

| 场景 | `systemd-analyze verify` | 真实监听结果 | 预检器 |
| --- | --- | --- | --- |
| IPv6 双栈通配与 IPv4 环回地址 | 通过 | 6/6 次第二个单元因端口占用失败 | `1` |
| IPv6 双栈通配与 IPv4 通配地址 | 通过 | 6/6 次第二个单元因端口占用失败 | `1` |
| 显式 IPv6-only 与 IPv4 环回地址 | 通过 | 6/6 次都能同时监听 | `0` |
| 空 `ListenStream=` 重置旧地址 | 通过 | 6/6 次都能同时监听 | `0` |
| 来自不同目录、文件名长度不同的追加与重置 | 通过 | 6/6 次都能同时监听 | `0` |

两次问题都保留了对应用例。早期 MoonBit 实现的普通字符串排序没有产生所需的文件名字节顺序，随后改为逐字符比较 ASCII 文件名。首次验证脚本把 manager 输出中的多条 `Listen=` 当成单值，遗漏了前面的监听。修正脚本后重新跑完 30 次，最终回执包含完整列表。临时 Ubuntu 实例已清理。

Ubuntu 基础镜像来自 [官方 Ubuntu Base 24.04 镜像目录](https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/)，镜像 `ubuntu-base-24.04.5-base-amd64.tar.gz` 的 SHA-256 为 `e77b6f10c2590cef872b33ee9f635a0e3fd1f57fb074c0e52b5c7f56147a0c86`。镜像校验后只用于对照实验。上述结果覆盖此环境和这些样本。

```text
python scripts/build.py
python scripts/verify_cli.py dist/socket-preflight-v0.0.1.zip --systemd-report verification/v0.0.1-systemd-linux.jsonl
```
