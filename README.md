# SocketPreflight v0.0.23

SocketPreflight 在部署前检查拟同时启动的 systemd `.socket` 单元。它从配置快照选出生效的主文件和 drop-in，计算 TCP 监听地址，报告同一单元内或不同单元间争抢端口的两条配置及其生效行号。

[三分钟演示](DEMO.md) 展示如何找到只会在部署切换窗口出现的冲突。

## 发布与安装

GitHub 保留 v0.0.1 至 v0.0.13 的独立发行包。v0.0.9 和 v0.0.13 对应冻结源码，并已发布到 Mooncakes。其余版本为历史二进制归档。[GitHub Releases](https://github.com/HhWw96/socket-preflight/releases) · [Mooncakes](https://mooncakes.io/docs/HhWw96/socket-preflight@0.0.23/)

在 MoonBit 项目中安装当前版本。

```text
moon add HhWw96/socket-preflight@0.0.23
```

## 运行

从导出目录发现 `.socket` 文件时，运行 `node socket-preflight-v0.0.23.cjs inventory --root examples/conflict --json`。清单列出候选名称、每个片段的路径和类型，以及该路径是否参与扫描。它只读取目录和文件类型，单元是否会启动仍由部署计划决定。退出码 `2` 表示某些目录或条目无法读取，已发现的候选仍保留。

解压发行包，在解压目录用 Node.js 24 运行。示例中的 `public.socket` 监听 `[::]:45391`，并允许 IPv4 共用 IPv6 监听。`admin.socket` 的配置覆盖另外加入了 `127.0.0.1:45391`。

```text
node socket-preflight-v0.0.23.cjs scan --root examples/conflict --ipv6 enabled admin.socket public.socket
```

程序退出 `1`，指出 `etc/systemd/system/admin.socket.d/20-site.conf:2` 和 `usr/lib/systemd/system/public.socket:5`。同名但低优先级的 `20-site.conf` 不参与判定。下面的对照样本用 `90-reset.conf` 清除 admin 之前的两个监听地址，再加一个新端口。退出 `0`，输出重置发生的位置。

```text
node socket-preflight-v0.0.23.cjs scan --root examples/fixed --ipv6 enabled admin.socket public.socket
```

加 `--json` 可接入流水线。JSON 包含 `schema_version: 1`、选中的文件、被覆盖的文件、被清除的原始监听、可比较端点、冲突及未覆盖条件。每个冲突给出两侧规范化地址和从 1 开始的行号。跨物理行的配置另带结束行号。

| 退出码 | 状态 | 含义 |
| --- | --- | --- |
| `0` | `clear` | 在声明的输入和本版覆盖范围内未发现监听冲突。 |
| `1` | `conflict` | 有确定的地址覆盖冲突。若另有未覆盖的配置，也保留在报告里。 |
| `2` | `inconclusive` | 覆盖不足，或目标环境的关键条件未知。 |
| `3` | `input_error` | 参数、快照文件、配置结构或输入容量有错误。若有其他单元的冲突，仍保留已计算的结果。 |

## 部署前比较

`compare` 以同一组单元及目标内核设置比较部署前后的两个快照。下例模拟把修正后的快照换成有冲突的候选，退出 `1`。互换两个路径，已消除的冲突仍显示来源，命令退出 `0`。

```text
node socket-preflight-v0.0.23.cjs compare --baseline examples/fixed --candidate examples/conflict --ipv6 enabled --json admin.socket public.socket
```

结果包含 `new_conflicts`、`existing_conflicts`、`resolved_conflicts` 和两份扫描报告。按单元和规范化的绑定地址去重，移动配置行或改变等价的地址写法不会产生新的发现。如果某个配对的另一侧没有足够证据，差异留在 `uncertain_new` 或 `uncertain_resolved`。详情截断也会使比较不完整。

对比命令中，`0` 表示比较完整且没有新增冲突，`1` 表示存在已确认的新增冲突，`2` 表示没有已确认的新增冲突且比较不完整，`3` 表示输入有误。部署流水线可按退出码分别处理回归、覆盖缺口和输入故障。

主机网络设置也可单独比较。对 `compare` 传入 `--candidate-ipv6` 或 `--candidate-bindv6only` 即可覆盖候选环境，基线继续使用原来的环境选项。即使两份配置相同，也能检查将 `net.ipv6.bindv6only` 从 `1` 改为 `0` 是否引入双栈重叠。目标值未知且会影响判定时，结果仍为未确定。MoonBit 提供显式的 `compare_target_reports` 接口，常规 `compare_reports` 继续要求两侧使用同环境。

代码审查可对 `scan` 或 `compare` 使用 `--sarif` 输出 SARIF 2.1.0。例如从工作区根目录执行 `node socket-preflight-v0.0.23.cjs compare --baseline examples/fixed --candidate examples/conflict --ipv6 enabled --sarif admin.socket public.socket > preflight.sarif`。结果保留两处来源和各自的行号。比较输出包含新增冲突、待确认变化和覆盖问题。路径对应当前工作区，退出码沿用原命令。

## 部署切换阶段

`rollout` 接受一份 UTF-8 JSON 计划，把候选和可选的共存阶段分别与部署前比较。旧单元和新单元分别能够监听，但在切换窗口同时启用会抢同一端口。两条命令分别退出 `1` 和 `0`。

```text
node socket-preflight-v0.0.23.cjs rollout --plan examples/rollout/bad.json
node socket-preflight-v0.0.23.cjs rollout --plan examples/rollout/fixed.json --json
```

计划采用 `schema_version: 1`，含 `environment`（`ipv6` 和 `bindv6only`）、`baseline`、`candidate` 及可选 `transition`。每个阶段填写 `root` 和 `units`。相对目录以计划文件所在目录为准。每个 `units` 列表显式声明会同时监听的 0–64 个单元，空列表表示该阶段没有监听单元。声明存在却缺少配置会返回输入错误。

报告的 `transition` 和 `candidate` 分别包含一份相对于基线的比较报告。任一阶段确认新增即退出 `1`，覆盖不足时退出 `2`，输入错误优先退出 `3`。退出 `0` 表示所有已声明阶段覆盖完整且没有新增。

多批次切换可用 `schema_version: 2`。以有序的 `checkpoints` 数组替代 `transition`，每个阶段包含唯一的 `name`、`root` 和 `units`。最多支持八个检查点，所有阶段合计最多 4 Mi 字符的生效配置。结果顺序与计划一致，各检查点分别和同一基线比较。JSON 中基线记录放在 `candidate.baseline`，检查点下的比较结果省略重复的基线。发行包内的 `examples/rollout/checkpoints.json` 展示前一检查点正常、后一检查点冲突且最终候选正常的情况。MoonBit 调用 `analyze_checkpoint_rollout` 使用相同的判定。

Schema 2 可设置 `"policy": "require-clear"`。任一检查点或最终候选出现已确认的冲突时，整体状态为 `conflict`，退出 `1`。这个策略也阻拦从基线沿用的冲突，允许已在各阶段解决的基线冲突。默认策略 `new-conflicts` 只阻拦已确认的新增冲突。部署 ZIP 中的策略随计划一同进入产物校验。MoonBit 可调用 `analyze_strict_checkpoint_rollout`。

## Debian 发行包样本

`python scripts/build_debian_example.py --out verification/debian` 从锁定的 Debian 13 软件包抽取原始 `cockpit.socket` 和 `ssh.socket`，保留版权信息及校验值。它生成两个部署配置 ZIP。坏例让 SSH 在共存阶段抢占 Cockpit 的 `9090`，修正版在该阶段让 SSH 临时使用 `19091`。两例的部署后阶段相同。解压样本后可以把 `plan.json` 直接交给 `rollout --plan`。

## 部署 ZIP 门禁

其他 GitHub 仓库可在上传部署产物后调用可复用门禁。将下面片段放入调用方的 `jobs`，其中 `build` 用 `actions/upload-artifact` 上传产物 `deployment-input`，产物根目录包含 `deployment.zip`。从发行页的 `SHA256SUMS` 取 ZIP 摘要，从同一版本固定工作流引用和发行标签。

```yaml
  preflight:
    needs: build
    uses: HhWw96/socket-preflight/.github/workflows/preflight.yml@v0.0.22
    with:
      deployment_artifact: deployment-input
      release_tag: v0.0.22
      release_sha256: '<发行 ZIP 的 SHA-256>'
      receipt_artifact: socketpreflight-receipt
```

发布作业依赖 `preflight`。取回门禁回执后，用同一发行版的 `deployment_pipeline.py release` 核对实际交付 ZIP 和 CLI 的哈希，再继续部署。工具包核验失败会在门禁运行前终止，已有门禁回执的失败也会保留回执。

在一个独立目录中准备 `plan.json` 和导出的阶段目录，用发行包中的 `python package_deployment.py --input prepared --output deployment.zip` 打包。相同的文件和目录内容会生成字节一致的 ZIP。工具保留空阶段目录，校验包内根路径，拒绝链接、超限内容和已存在的输出，并打印 ZIP 的 SHA-256。打包完成后仍须执行下面的门禁判定。

将现有格式的 `plan.json` 放在部署 ZIP 根目录，阶段的 `root` 指向包内目录。在解压后的 SocketPreflight 发行包目录运行以下命令。需要 Python 3.10+ 和 Node.js 24。

```text
python gate_bundle.py --bundle deployment.zip --cli socket-preflight-v0.0.23.cjs --report receipt.json
```

门禁在独立临时目录解包，用 MoonBit CLI 检查计划。回执记录实际读取的 ZIP 和 CLI 的 SHA-256、退出码及完整的 `rollout` JSON。退出 `0` 表示回执已写入且可放行，`1` 表示新增冲突，`2` 表示覆盖不足，`3` 表示输入、执行或回执写入失败。ZIP 上限为 32 MiB，最多 4096 条目，展开上限 64 MiB。目录、文件和阶段路径使用可携带的 ASCII 文件名。

发行包中的 `samples` 已包含两份锁定的 Debian 示例。解压后可执行本地流水线演练，每个 `--out` 使用新目录。

```text
python deployment_pipeline.py run --bundle samples/debian-bad.zip --cli socket-preflight-v0.0.23.cjs --out run-bad
python deployment_pipeline.py run --bundle samples/debian-fixed.zip --cli socket-preflight-v0.0.23.cjs --out run-fixed
```

坏例退出 `1` 并保留回执。修正版再次核对部署 ZIP 和工具 SHA-256 后生成 `would-deploy.json`。该记录代表演练放行。仓库源码中的 GitHub Actions 工作流以指定的发布标签和发行包 SHA-256 为输入，分别可运行 `bad` 和 `fixed`。门禁步骤失败时下游作业跳过，门禁回执仍可下载。

## 快照与判定范围

源码中的 `scripts/verify_systemd_runtime.py` 可在一次性 Debian 13 或 Ubuntu 24.04 环境对照指定发行包。用 root、`CI=true`、systemd PID 1、Node.js 24 和 `--disposable-vm --release-zip <发行ZIP> --sha256 <发行ZIP摘要> --report <新JSONL>` 运行。每个环境记录 6 个场景和 30 次启动，逐例保存生效监听、静态检查、配置来源与实际结果。日常离线门禁仍可在没有 systemd 的环境执行。

`examples/rollout` 包含依据公开故障重建的部署前、共存、部署后和修正快照。用下面两条命令单独检查共存窗口，预期先退出 `1`，再退出 `0`。 [systemd #14044](https://github.com/systemd/systemd/issues/14044)

```text
node socket-preflight-v0.0.23.cjs scan --root examples/rollout/overlap --ipv6 enabled legacy.socket next.socket
node socket-preflight-v0.0.23.cjs scan --root examples/rollout/fixed --ipv6 enabled legacy.socket next.socket
```

`--root` 应指向静态导出目录。将文件放在 `etc/systemd/system`、`run/systemd/system`、`usr/local/lib/systemd/system` 或 `usr/lib/systemd/system`，相对根目录保留这个结构。命令行列出的 1–64 个单元构成**一组会同时监听且位于同一网络命名空间的 system 单元**。只指定一个单元时，检查其生效监听之间的冲突，例如未清除原监听就追加覆盖相同地址的 drop-in。部署方负责选定单元并确认快照完整。请勿将活动主机根目录当作完整的离线快照。

- 主文件按 `/etc`、`/run`、`/usr/local/lib`、`/usr/lib` 的顺序择一。drop-in 在每条路径内先查完整单元名，再由长到短查带连字符的前缀，最后查所有路径下的类型级 `socket.d`。同名取第一个，不同文件名按字节顺序跨目录应用。支持常规 ASCII 单元名和 drop-in 名、UTF-8、CRLF、注释和续行。`ListenStream` 的重复非空赋值累加。已知类型的空 `Listen*=` 会清除共享列表，包括其他类型的旧监听。
- 仅比较普通 TCP 的 `ListenStream`。支持字面 IPv4、无地址域的 IPv6，以及 `1–65535` 的十进制裸端口。IPv6 通配为双栈时也覆盖 IPv4。可双栈绑定的 IPv4 映射单播地址会占用对应的 IPv4 地址，例如 `[::ffff:7f00:1]:45391` 与 `127.0.0.1:45391`。JSON 保留原配置，并以 `mapped_ipv4` 标出映射。
- `--ipv6` 表示目标环境的 IPv6 能力。未知时裸端口无法确定使用哪个地址族，显式 IPv6 也会提示未覆盖。裸端口在目标不支持 IPv6 时回退到 IPv4 通配。若生效的 `BindIPv6Only` 为默认，IPv6 通配只有提供目标 `--bindv6only 0` 或 `1` 后才知道是否覆盖 IPv4。参数缺省都视为 `unknown`，程序不会拿运行机器的 IPv6 状态代替目标机器。
- IPv6-only 配置里的 IPv4 映射地址会提示不能绑定。映射的 IPv4 通配、IPv6 地址域、生效的其他监听类型、模板与别名、符号链接、设备绑定、`ReusePort`、自定义网络命名空间和非默认协议等超出本版。发现生成器输出等超出范围的路径会标记相应单元未覆盖。未导出的文件、启动依赖、集合外进程占用、服务自身的额外监听和网络地址是否存在需要另行验证。

单文件上限 256 KiB，每个 drop-in 目录最多 128 个候选文件，选中文本总量上限 4 Mi 字符，可比较的监听条目上限 512。JSON 始终给出发现的冲突总数，详情最多保留 256 对。

同一端口没有通配地址时，扫描按具体地址索引并直接计算重复绑定数。遇到通配地址时，沿用覆盖判定并保留原有的详情顺序。可用 `scripts/benchmark.py` 比对优化前后五组扫描和比较结果，其中两组分别使用 512 个同端口独立地址和 512 个同端口重复地址。

目录探测和共享 drop-in 名单在每个快照内缓存。打开生效文件前重新核对路径类型。`compare` 对两份快照各自建立缓存。请在导出结束后运行，整个检查期间保持目录内容稳定。

行为参考 [systemd.unit（v257）](https://www.freedesktop.org/software/systemd/man/257/systemd.unit.html)、[systemd.socket（v257）](https://www.freedesktop.org/software/systemd/man/257/systemd.socket.html) 和 [systemd.syntax（v257）](https://www.freedesktop.org/software/systemd/man/257/systemd.syntax.html)。实际需求实例见 [systemd #14044](https://github.com/systemd/systemd/issues/14044)。

## 从源码构建与验证

配置解析、覆盖顺序、地址规范化和冲突判定由 MoonBit 实现。Node.js 适配层只负责读取所选快照。源码构建不安装第三方包，使用 MoonBit 标准库。`select_files` 和 `analyze` 也可独立接受调用方提供的已导出文件。

```text
python scripts/build.py
python scripts/verify_cli.py dist/socket-preflight-v0.0.23.zip
python scripts/benchmark.py dist/socket-preflight-v0.0.4.cjs dist/socket-preflight-v0.0.5.cjs --report verification/local-benchmark.json
```

构建脚本执行类型检查和 MoonBit 单元测试并生成 `dist/`。归档测试会在独立临时目录解压并运行编译后的 CLI。在单独可丢弃的 Linux VM 中，`sudo env CI=true python3 scripts/verify_systemd.py --disposable-vm` 以私有网络命名空间运行相同样本和变体，对照 `systemd-analyze verify`、manager 加载结果和真实监听。

在项目源码目录，Windows 和 Linux 可用同一条命令核对发给用户的 ZIP。它解压运行旧接口和部署切换案例，并核对已保存的 systemd 实验回执。下游 MoonBit 模块调用 `analyze_rollout(RolloutInput)`，传入已按加载顺序选择的配置文本。下方第二条命令在一个新的临时 workspace 里运行独立下游模块。

```text
python scripts/verify_release.py dist/socket-preflight-v0.0.23.zip
python scripts/verify_consumer.py
```

要从头重做四阶段 systemd 对照，可在一台可丢弃的 Linux VM 执行 `sudo env CI=true python3 scripts/verify_rollout_systemd.py --disposable-vm`。脚本用私有网络命名空间启动测试单元，并输出可保存的 JSONL。

首版在 Ubuntu 24.04.5 / systemd 255 上跑完了 5 个场景、30 次实际启动对照。`systemd-analyze verify` 在 5 个场景均通过，其中两个双栈场景的第二个监听在真实启动时失败。预检器在同两组数据上报告冲突，其余三组与 manager 的生效监听一致。完整记录保存在源码目录的 `verification/`。

版本从 `v0.0.1` 起按 `v0.0.X` 更新。每次修改版本时同步 `moon.mod`、`model.mbt`、示例命令和 [CHANGELOG.md](CHANGELOG.md)。构建时校验 CLI 自报版本与模块版本一致，并写入 `SHA256SUMS`。
