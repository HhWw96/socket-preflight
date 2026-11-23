# SocketPreflight v0.0.9

SocketPreflight 在部署前检查一组拟同时启动的 systemd `.socket` 单元。它从配置快照选出真正生效的主文件和 drop-in，计算 TCP 监听地址，再报告哪两个端点会争抢同一个端口，以及各自生效的配置行。

[三分钟演示](DEMO.md) 展示如何找到只会在部署切换窗口出现的冲突。

## 运行

解压发行包，在解压目录用 Node.js 24 运行。示例中的 `public.socket` 监听 `[::]:45391`，并允许 IPv4 共用 IPv6 监听。`admin.socket` 的配置覆盖另外加入了 `127.0.0.1:45391`。

```text
node socket-preflight-v0.0.9.cjs scan --root examples/conflict --ipv6 enabled admin.socket public.socket
```

程序退出 `1`，指出 `etc/systemd/system/admin.socket.d/20-site.conf:2` 和 `usr/lib/systemd/system/public.socket:5`。同名但低优先级的 `20-site.conf` 不参与判定。下面的对照样本用 `90-reset.conf` 清除 admin 之前的两个监听地址，再加一个新端口。退出 `0`，输出重置发生的位置。

```text
node socket-preflight-v0.0.9.cjs scan --root examples/fixed --ipv6 enabled admin.socket public.socket
```

加 `--json` 可接入流水线。JSON 包含 `schema_version: 1`、选中的文件、被覆盖的文件、被清除的原始监听、可比较端点、冲突及未覆盖条件。每个冲突给出两侧规范化地址和从 1 开始的行号。跨物理行的配置另带结束行号。

| 退出码 | 状态 | 含义 |
| --- | --- | --- |
| `0` | `clear` | 在声明的输入和本版覆盖范围内未发现跨单元冲突。 |
| `1` | `conflict` | 有确定的地址覆盖冲突。若另有未覆盖的配置，也保留在报告里。 |
| `2` | `inconclusive` | 覆盖不足，或目标环境的关键条件未知。 |
| `3` | `input_error` | 参数、快照文件、配置结构或输入容量有错误。若有其他单元的冲突，仍保留已计算的结果。 |

## 部署前比较

`compare` 以同一组单元及目标内核设置比较部署前后的两个快照。下例模拟把修正后的快照换成有冲突的候选，退出 `1`。互换两个路径，已消除的冲突仍显示来源，命令退出 `0`。

```text
node socket-preflight-v0.0.9.cjs compare --baseline examples/fixed --candidate examples/conflict --ipv6 enabled --json admin.socket public.socket
```

结果包含 `new_conflicts`、`existing_conflicts`、`resolved_conflicts` 和两份扫描报告。按单元和规范化的绑定地址去重，移动配置行或改变等价的地址写法不会产生新的发现。如果某个配对的另一侧没有足够证据，差异留在 `uncertain_new` 或 `uncertain_resolved`。详情截断也会使比较不完整。

对比命令中，`0` 表示比较完整且没有新增冲突，`1` 表示存在已确认的新增冲突，`2` 表示没有已确认的新增冲突且比较不完整，`3` 表示输入有误。部署流水线可按退出码分别处理回归、覆盖缺口和输入故障。

## 部署切换阶段

`rollout` 接受一份 UTF-8 JSON 计划，把候选和可选的共存阶段分别与部署前比较。旧单元和新单元分别能够监听，但在切换窗口同时启用会抢同一端口。两条命令分别退出 `1` 和 `0`。

```text
node socket-preflight-v0.0.9.cjs rollout --plan examples/rollout/bad.json
node socket-preflight-v0.0.9.cjs rollout --plan examples/rollout/fixed.json --json
```

计划采用 `schema_version: 1`，含 `environment`（`ipv6` 和 `bindv6only`）、`baseline`、`candidate` 及可选 `transition`。每个阶段填写 `root` 和 `units`。相对目录以计划文件所在目录为准。每个 `units` 列表显式声明会同时监听的 0–64 个单元，空列表表示该阶段没有监听单元。声明存在却缺少配置会返回输入错误。

报告的 `transition` 和 `candidate` 分别包含一份相对于基线的比较报告。任一阶段确认新增即退出 `1`，覆盖不足时退出 `2`，输入错误优先退出 `3`。退出 `0` 表示所有已声明阶段覆盖完整且没有新增。

## 快照与判定范围

`examples/rollout` 包含依据公开故障重建的部署前、共存、部署后和修正快照。用下面两条命令单独检查共存窗口，预期先退出 `1`，再退出 `0`。 [systemd #14044](https://github.com/systemd/systemd/issues/14044)

```text
node socket-preflight-v0.0.9.cjs scan --root examples/rollout/overlap --ipv6 enabled legacy.socket next.socket
node socket-preflight-v0.0.9.cjs scan --root examples/rollout/fixed --ipv6 enabled legacy.socket next.socket
```

`--root` 应指向静态导出目录。将文件放在 `etc/systemd/system`、`run/systemd/system` 或 `usr/lib/systemd/system`，相对根目录保留这个结构。命令行列出的 2–64 个单元构成**一组会同时监听且位于同一网络命名空间的 system 单元**。部署方负责选定这组单元并确认快照完整。请勿将活动主机根目录当作完整的离线快照。

- 主文件按 `/etc`、`/run`、`/usr/lib` 的顺序择一。drop-in 在每条路径内先查完整单元名，再由长到短查带连字符的前缀，最后查所有路径下的类型级 `socket.d`。同名取第一个，不同文件名按字节顺序跨目录应用。支持常规 ASCII 单元名和 drop-in 名、UTF-8、CRLF、注释和续行。`ListenStream` 的重复非空赋值累加。已知类型的空 `Listen*=` 会清除共享列表，包括其他类型的旧监听。
- 仅比较普通 TCP 的 `ListenStream`。支持字面 IPv4、无地址域的 IPv6，以及 `1–65535` 的十进制裸端口。IPv6 通配为双栈时也覆盖 IPv4。可双栈绑定的 IPv4 映射单播地址会占用对应的 IPv4 地址，例如 `[::ffff:7f00:1]:45391` 与 `127.0.0.1:45391`。JSON 保留原配置，并以 `mapped_ipv4` 标出映射。
- `--ipv6` 表示目标环境的 IPv6 能力。未知时裸端口无法确定使用哪个地址族，显式 IPv6 也会提示未覆盖。裸端口在目标不支持 IPv6 时回退到 IPv4 通配。若生效的 `BindIPv6Only` 为默认，IPv6 通配只有提供目标 `--bindv6only 0` 或 `1` 后才知道是否覆盖 IPv4。参数缺省都视为 `unknown`，程序不会拿运行机器的 IPv6 状态代替目标机器。
- IPv6-only 配置里的 IPv4 映射地址会提示不能绑定。映射的 IPv4 通配、IPv6 地址域、生效的其他监听类型、模板与别名、符号链接、设备绑定、`ReusePort`、自定义网络命名空间和非默认协议等超出本版。发现生成器输出等超出范围的路径会标记相应单元未覆盖。未导出的文件、启动依赖、端口已被集合外进程占用、服务自身的额外监听、网络地址是否存在及同一单元内部重复绑定不在检查范围内。

单文件上限 256 KiB，每个 drop-in 目录最多 128 个候选文件，选中文本总量上限 4 Mi 字符，可比较的监听条目上限 512。JSON 始终给出发现的冲突总数，详情最多保留 256 对。

目录探测和共享 drop-in 名单在每个快照内缓存。打开生效文件前重新核对路径类型。`compare` 对两份快照各自建立缓存。请在导出结束后运行，整个检查期间保持目录内容稳定。

行为参考 [systemd.unit（v257）](https://www.freedesktop.org/software/systemd/man/257/systemd.unit.html)、[systemd.socket（v257）](https://www.freedesktop.org/software/systemd/man/257/systemd.socket.html) 和 [systemd.syntax（v257）](https://www.freedesktop.org/software/systemd/man/257/systemd.syntax.html)。实际需求实例见 [systemd #14044](https://github.com/systemd/systemd/issues/14044)。

## 从源码构建与验证

配置解析、覆盖顺序、地址规范化和冲突判定由 MoonBit 实现。Node.js 适配层只负责读取所选快照。源码构建不安装第三方包，使用 MoonBit 标准库。`select_files` 和 `analyze` 也可独立接受调用方提供的已导出文件。

```text
python scripts/build.py
python scripts/verify_cli.py dist/socket-preflight-v0.0.9.zip
python scripts/benchmark.py dist/socket-preflight-v0.0.4.cjs dist/socket-preflight-v0.0.5.cjs --report verification/local-benchmark.json
```

构建脚本执行类型检查和 MoonBit 单元测试并生成 `dist/`。归档测试会在独立临时目录解压并运行编译后的 CLI。在单独可丢弃的 Linux VM 中，`sudo env CI=true python3 scripts/verify_systemd.py --disposable-vm` 以私有网络命名空间运行相同样本和变体，对照 `systemd-analyze verify`、manager 加载结果和真实监听。

在项目源码目录，Windows 和 Linux 可用同一条命令核对发给用户的 ZIP。它解压运行旧接口和部署切换案例，并核对已保存的 systemd 实验回执。下游 MoonBit 模块调用 `analyze_rollout(RolloutInput)`，传入已按加载顺序选择的配置文本。下方第二条命令在一个新的临时 workspace 里运行独立下游模块。

```text
python scripts/verify_release.py dist/socket-preflight-v0.0.9.zip
python scripts/verify_consumer.py
```

要从头重做四阶段 systemd 对照，可在一台可丢弃的 Linux VM 执行 `sudo env CI=true python3 scripts/verify_rollout_systemd.py --disposable-vm`。脚本用私有网络命名空间启动测试单元，并输出可保存的 JSONL。

首版在 Ubuntu 24.04.5 / systemd 255 上跑完了 5 个场景、30 次实际启动对照。`systemd-analyze verify` 在 5 个场景均通过，其中两个双栈场景的第二个监听在真实启动时失败。预检器在同两组数据上报告冲突，其余三组与 manager 的生效监听一致。完整记录保存在源码目录的 `verification/`。

版本从 `v0.0.1` 起按 `v0.0.X` 更新。每次修改版本时同步 `moon.mod`、`model.mbt`、示例命令和 [CHANGELOG.md](CHANGELOG.md)。构建时校验 CLI 自报版本与模块版本一致，并写入 `SHA256SUMS`。
