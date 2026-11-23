# SocketPreflight v0.0.9 三分钟演示

场景依据 systemd 的公开故障报告重建。系统里有旧的 socket 单元占着端口，新单元的启动失败。演示把问题缩小到同一网络命名空间的 TCP 监听，使用两份明确的阶段计划。[systemd #14044](https://github.com/systemd/systemd/issues/14044)

## 0:00–0:35 · 说明切换窗口

打开 `examples/rollout/bad.json`。部署前只启用 `legacy.socket`，切换时两者共存，部署后只启用 `next.socket`。旧单元的 `[::]:45391` 使用双栈，新单元尝试监听 `127.0.0.1:45391`。

## 0:35–1:25 · 提前定位冲突

解压 ZIP 后，在解压目录运行以下命令。

```text
node socket-preflight-v0.0.9.cjs rollout --plan examples/rollout/bad.json
```

预期退出 `1`，在 `[transition]` 下指出 `legacy.socket:3` 和 `next.socket:2`。两端独立阶段是干净的，共存阶段的双栈通配已经覆盖 IPv4 端口。隔离 Linux 实验中，四个快照的 `systemd-analyze verify` 均通过，共存坏例的两种顺序共 6 次均确认 `EADDRINUSE`。

## 1:25–2:05 · 同一输入修正后放行

```text
node socket-preflight-v0.0.9.cjs rollout --plan examples/rollout/fixed.json
```

修正版的共存快照先用空 `ListenStream=` 清除新单元原有监听，再临时改用 `45392`。旧单元退场后的候选阶段仍可使用 `45391`。命令预期退出 `0`，隔离实测的 6 次共存全部成功。

## 2:05–3:00 · 展示评审证据

| 评分维度 | 展示材料 |
| --- | --- |
| 工程深度 | 四份阶段快照和坏、好两条命令。流水线可使用 `--json`。 |
| 技术贡献 | MoonBit 解析生效文件、追溯空赋值与覆盖顺序，再按显式阶段成员判断 TCP 地址重叠。 |
| 生态增量 | 对照静态检查及监听列表；独立 MoonBit 示例直接调用 `analyze_rollout`。 |
| 开发与验证 | 同一个 ZIP 在 Windows 和 Ubuntu 解压测试。隔离启动和内核错误都有原始回执。 |

第三方复跑可用上述两条命令。请记录 ZIP SHA-256、系统、Node.js 版本、两次退出码和是否能独立找到双方配置行。项目源码中的 `verification/` 保存当前验证记录。
