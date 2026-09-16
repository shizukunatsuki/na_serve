# na_serve

一个 zsh 函数：用 [Caddy](https://caddyserver.com/) 把当前目录发布到局域网，同时用
[cloudflared](https://github.com/cloudflare/cloudflared) 开一条临时公网隧道，替代
`python3 -m http.server`。仅支持 macOS。

## 依赖

```bash
brew install caddy cloudflared
```

## 安装

把 [`serve.zsh`](serve.zsh) 里的函数复制进 `~/.zshrc`，或者直接 source 它：

```bash
echo 'source /path/to/na_serve/serve.zsh' >> ~/.zshrc
```

## 用法

```bash
cd 想分享的目录
serve
```

```
Serving: /Users/you/some/dir          ← 红色粗体，提醒当前暴露的是哪个目录
Local: http://your-mac.local:61234/
2026-... INF |  https://random-words-here.trycloudflare.com  |
```

`Ctrl-C` 停止服务。可以在不同目录里同时开多个 `serve`，每个实例各用各的随机端口，互不干扰。

## 设计

- **端口**：`caddy file-server --browse --listen :0`，由内核分配一个空闲的临时端口，
  再用 `lsof` 读回实际端口号。不用固定端口——Caddy 的监听器会设置 `SO_REUSEPORT`，
  固定端口下第二个实例不会报错，而是静默共享同一个端口，流量却仍然只进第一个实例，
  这是比"启动失败"更危险的错误。
- **隧道目标是 `localhost`，不是 `127.0.0.1`**，同时监听 IPv4/IPv6。
- **生命周期不变量**：所有清理信号只发给 `serve` 自己的进程组（`kill -SIG -$pgid`），
  且只在该组的组长存活时发送。`kill -0` 只用来探测进程是否存活，从不用来发送信号。
  这样即使某个 PID 在极端情况下被系统回收复用，也不会误杀到无关进程。
- Ctrl-C、关闭终端（HUP）、父 shell 被杀、`Ctrl-Z` 后父 shell 被杀，都会在几秒内
  干净地停掉 Caddy 和 cloudflared；任何一个服务自己崩溃也会带着另一个一起收尾。
  对忽略普通终止信号的子进程，5 秒宽限后升级为 `SIGKILL`。

## 安全提示

公网隧道没有任何鉴权，`--browse` 的目录列表也不隐藏点文件。不要在包含
`.env`、`.git`、密钥等内容的目录里运行；分享前自己检查一遍目录内容。

## 已知限制

- 运行 `serve` 的那个子 shell 本身如果被 `SIGKILL`，Caddy 和 cloudflared 不会被自动
  收尾——这是外部对这个 shell 本身动手，不是 `serve` 能在 shell 层面兜住的场景。
- 如果机器的 `LocalHostName` 没设置，`Local:` 那行会显示成 `http://.local:PORT/`。

## 测试

`test/test_serve.py` 用真实的 caddy / cloudflared 二进制、在伪终端里驱动交互式 zsh，
覆盖单实例、多实例并发、各种中断路径（`Ctrl-C`、关终端、父 shell 被杀、服务自身崩溃、
二进制缺失等），每个场景结束后都断言：没有残留进程、没有殃及无关的旁路进程。
默认用一个不产生真实网络连接的 cloudflared 桩替身运行（Cloudflare 的 quick tunnel
接口对同一出口 IP 有频率限制，连续测试很容易撞上），加 `--real-tunnel` 换成真实
二进制、经外部网络验证公网隧道确实能取到文件。用法见 [`test/README.md`](test/README.md)。

## License

MIT，见 [LICENSE](LICENSE)。
