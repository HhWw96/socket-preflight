# SocketPreflight v0.0.1 历史发行清单

本版为历史二进制归档，附当时生成的 CLI 和发行 ZIP。这个标签对应发行清单。

[下载历史发行包](https://github.com/HhWw96/socket-preflight/releases/tag/v0.0.1)

历史独立源码以项目已冻结的源码版本为准。

- 首版离线检查同一网络命名空间内的跨单元 systemd TCP 监听冲突。
- 实现三层主文件选择、drop-in 同名遮蔽和跨目录排序，记录监听累加、重置及来源。
- 区分 IPv4、IPv6-only、IPv6 双栈和未确定的目标环境，输出可读报告及带退出状态的 JSON。
- 提供冲突与修正样本、可独立解压运行的 Node.js 命令行、MoonBit 回归及隔离 Linux 复验脚本。
