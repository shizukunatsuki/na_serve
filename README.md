# na_serve

一个 zsh 脚本，替代 `python3 -m http.server`：用 [Caddy](https://caddyserver.com/)
把当前目录发布到局域网；加 `--share` 时再用
[cloudflared](https://github.com/cloudflare/cloudflared) 开一条临时公网隧道。仅支持 macOS。

## 安装

```bash
brew install caddy
brew install cloudflared   # 只有 --share 才需要
```

[`serve`](serve) 是一个独立脚本（`#!/bin/zsh -f`，用 macOS 自带的 zsh），不依赖你的
交互 shell 是什么。在 `~/.zshrc` 里加一个 alias 指向它即可：

```bash
alias serve=/path/to/na_serve/serve
```

## 用法

```bash
cd 想分享的目录
serve            # 只发布到局域网
serve --share    # 同时开一条 cloudflared 临时公网隧道
serve --help     # 打印用法
```

```
$ serve --share
Serving: /Users/you/some/dir          ← 红色粗体，提醒当前暴露的是哪个目录
Local: http://your-mac.local:61234/
2026-... INF |  https://random-words-here.trycloudflare.com  |
```

- 不带 `--share` 时只有前两行：不启动 cloudflared，也不要求装了它。公网隧道必须显式
  要求才会打开，把目录暴露到整个互联网不应该是默认行为。
- 参数写错（比如 `--shar`）会直接报错退出，什么都不启动，而不是悄悄以不带隧道的
  方式跑起来。
- `Ctrl-C`（或 `Ctrl-\`）停止服务。
- 可以在不同目录里同时开多个 `serve`，每个实例各用各的随机端口，互不干扰。

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 正常停止（`Ctrl-C` 等） |
| `1` | 出错：缺少依赖命令、caddy 起不来或读不到端口、运行中某个服务退出、没有终端 |
| `2` | 参数错误 |
| `137` | 有子进程无视终止信号，5 秒宽限后整组被 `SIGKILL` |

## 安全提示

没有任何鉴权，`--browse` 的目录列表也不隐藏点文件。不带 `--share` 时同一局域网里的
人都能访问；带 `--share` 时则是互联网上的任何人。不要在包含 `.env`、`.git`、密钥等
内容的目录里运行；分享前自己检查一遍目录内容。

## 设计

### 端口与隧道

- **端口由内核分配**：`caddy file-server --browse --listen :0` 拿到一个空闲的临时端口，
  再用 `lsof` 读回实际端口号。不用固定端口——Caddy 的监听器会设置 `SO_REUSEPORT`，
  固定端口下第二个实例不会报错，而是静默共享同一个端口，流量却仍然只进第一个实例，
  这是比"启动失败"更危险的错误。
- **读端口最多等 10 秒**：新装的二进制第一次启动时，可能先被 macOS 的恶意软件扫描
  拖住几秒。
- **读到的端口会被校验**：`lsof` 的输出由 zsh 自己解析，只接受 1–65535 的整数。
  `lsof` 损坏或被劫持时，它的输出不会被拼进 URL 交给 cloudflared，而是当作"读不到
  端口"处理并退出。
- **隧道目标是 `localhost`，不是 `127.0.0.1`**：Caddy 同时监听 IPv4/IPv6。

### 启动前检查

`serve` 会用到的每一个外部命令（`caddy`、`scutil`、`lsof`，以及 `--share` 时的
`cloudflared`）都在启动任何东西之前检查一遍，缺哪个就报哪个的名字并退出。这样环境
缺失不会伪装成别的症状——比如少了 `lsof`，端口就永远读不出来，看起来和"caddy 起不来"
一模一样。

### 进程生命周期：不留孤儿进程

- **只对自己的进程组发信号**：所有清理信号都用 `kill -SIG -$$` 发给 `serve` 自己的
  进程组。调用它的交互 shell 会把脚本作为前台 job 放进独立的进程组，脚本内部不开
  job control，caddy 和 cloudflared 因此留在同一个组里。`kill -0` 只用来探测进程或
  进程组是否存在，从不用来发送信号，所以即使某个 PID 在极端情况下被系统回收复用，
  也不会误杀到无关进程。
- **不是组长就拒绝启动**（例如没有终端时）。是不是组长用 `kill -0 -$$` 直接问内核：
  以自己 PID 为 ID 的进程组存在，当且仅当自己是它的组长。不解析也不信任 `ps` 的
  输出——误判成组长的话，后面所有 `kill -$$` 都会落空，服务就全成了孤儿。
- **什么情况下会停**：`Ctrl-C`、`Ctrl-\`、关闭终端（HUP）、父 shell 被杀、`Ctrl-Z` 后
  父 shell 被杀、输出管道的读端提前退出，都会在几秒内干净地停掉 Caddy 和 cloudflared
  （如果开了）；任何一个服务自己崩溃，也会带着另一个一起收尾。
- **收尾一定会执行**：从启动 caddy 到主循环结束的整段代码包在 zsh 的
  `{ ... } always { 收尾 }` 里，无论这段怎么结束，收尾都会跑。这防的是一类不经过
  普通信号的退出：输出被接进管道、读的一方先退出（例如
  `serve --share 2>&1 | grep -m1 trycloudflare`），之后 `serve` 再打印任何东西都会写到
  断掉的管道上——未处理的 `SIGPIPE` 会当场杀死 `serve`（所以它和 `Ctrl-C` 一样被 trap），
  而即便 trap 了，zsh 也会把这次写失败当成致命错误直接中止脚本。两者都会跳过收尾，
  让 caddy 失去父进程继续提供目录。
- **宽限后强杀**：收尾先对整组发 `SIGTERM`；对无视它的子进程，5 秒宽限后升级为
  `SIGKILL`。

## 已知限制

- **`serve` 进程本身被 `SIGKILL` 时**（例如 `kill -9`、活动监视器里强制退出），Caddy 和
  cloudflared 不会被自动收尾——这是外部对 supervisor 本身动手，不是 `serve` 能在 shell
  层面兜住的场景。这里刻意不加看门狗进程：看门狗自己同样可能被 `SIGKILL`，问题只会
  往下挪一层，还多出一个需要保证不变成孤儿的进程。对整个进程组发 `SIGKILL` 则没有
  这个问题，所有进程会一起结束。
- **`LocalHostName` 没设置时**，就没有 `.local` 名字可打印：`Local:` 那行会退化成
  `http://localhost:PORT/` 并注明原因，服务本身照常运行。

## 测试

[`test/test_serve.py`](test/test_serve.py) 用真实的 caddy 二进制、在伪终端里驱动交互式
zsh，覆盖：

- 单实例、多实例并发；
- 各种中断路径：`Ctrl-C`、`Ctrl-\`、关终端、父 shell 被杀、服务自身崩溃、输出管道的
  读端提前退出；
- 环境缺失：每个外部命令逐一缺席、命令存在却返回垃圾；
- 命令行参数：不带 `--share` 时不启动 cloudflared、写错的参数被拒绝。

每个场景结束后都断言没有残留进程、没有殃及无关的旁路进程。测试只会观察和终止它
自己启动的进程，机器上别的 caddy（包括你自己正在跑的 `serve`）既不会被误判成泄漏，
也不会被碰。默认用一个不联网的 cloudflared 桩替身，加 `--real-tunnel` 换成真实
二进制。用法和前提见 [`test/README.md`](test/README.md)。

## License

MIT，见 [LICENSE](LICENSE)。
