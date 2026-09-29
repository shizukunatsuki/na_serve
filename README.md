# na_serve

`serve`：把当前目录临时发布成一个 HTTP 文件服务器，用来替代 `python3 -m http.server`。
默认只在局域网内可访问；加 `--share` 时，再开一条 Cloudflare 临时公网隧道。

- 文件服务由 [Caddy](https://caddyserver.com/) 提供，公网隧道由
  [cloudflared](https://github.com/cloudflare/cloudflared) 提供。
- 只支持 macOS。脚本本身用系统自带的 zsh 运行（`#!/bin/zsh -f`），和你平时用什么
  交互 shell 无关。

## 安装

```bash
brew install caddy
brew install cloudflared   # 只有 --share 需要
```

在 `~/.zshrc` 里加一个 alias，指向仓库里的脚本：

```bash
alias serve=/path/to/na_serve/serve
```

## 用法

```bash
cd 要分享的目录
serve            # 只发布到局域网
serve --share    # 另外开一条公网隧道
serve --help     # 打印用法
```

按 `Ctrl-C` 停止。可以在不同目录里同时开多个 `serve`，每个实例用各自的端口，互不影响。

### 输出

`serve` 自己往 stdout 写两行：

```
Serving: /Users/you/some/dir
Local: http://your-mac.local:61234/
```

- **`Serving:`**：被发布目录的绝对路径。stdout 是终端时显示为红色粗体，重定向到文件或
  管道时不带颜色。
- **`Local:`**：局域网访问地址。主机名取自 macOS 的 LocalHostName（系统设置里"本地
  主机名"）。没有设置时改为 `http://localhost:端口/`，并在行尾注明原因。

stderr 上还有另外两类输出：

- **Caddy 和 cloudflared 的日志**：原样输出，所以终端里能看到 Caddy 启动和关闭的几行
  日志。
- **公网隧道地址**：`--share` 时，隧道地址出现在 cloudflared 的日志里，形如
  `https://<随机单词>.trycloudflare.com`。

`serve` 自己的错误信息也写到 stderr，都以 `serve: ` 开头。

## 行为说明

### 参数

参数从左到右逐个处理：

| 参数 | 行为 |
| --- | --- |
| （无） | 只发布到局域网。不启动 cloudflared，也不要求安装 cloudflared。 |
| `--share` | 另外启动 cloudflared quick tunnel，指向 `http://localhost:端口`。可以重复写。 |
| `-h`、`--help` | 把用法打印到 stdout，退出码 0，什么都不启动。 |
| 其他任何参数 | stderr 打印 `serve: unknown option: <参数原文>` 和用法，退出码 2，什么都不启动。 |

`--help` 和未知参数谁先出现，就由谁决定结果。所以 `serve --help --x` 退出码是 0，
`serve --x --help` 是 2。

### 启动

启动按以下顺序进行，前三步失败时，什么都还没有启动：

1. **检查依赖命令**：依次检查 `caddy`、`cloudflared`（仅 `--share`）、`scutil`、
   `lsof`。缺哪个，就打印 `serve: <命令> not found`，退出码 1。
2. **读取 LocalHostName**：通过 `scutil` 读取。
3. **确认自己是进程组组长**：不是的话打印
   `serve: not a process group leader, refusing to start`，退出码 1。
   - 从交互式 shell 直接运行时，`serve` 总是组长。
   - 以下情况不是组长：在管道里不是第一个命令（如 `true | serve`）；被一个没开 job
     control 的脚本当作普通命令调用。
   - 例外：如果脚本进程本身是组长，并且用 `exec` 把自己换成 `serve`（`zsh -c` 的
     最后一条命令会自动这样做），`serve` 就继承了组长身份，可以正常运行。这时通常
     没有终端，只能靠信号或父进程退出来停止。
4. **启动 Caddy**：发布当前目录，开启目录列表，监听所有网络接口，端口由内核分配
   一个空闲端口。
5. **读回端口**：用 `lsof` 读回 Caddy 实际监听的端口，最多等 10 秒。
   - 只接受 1–65535 的整数，`lsof` 给出的其他内容都当作"读不到"。
   - Caddy 在这期间退出：打印 `serve: caddy failed to start`，退出码 1。
   - 10 秒内读不到端口：打印 `serve: could not read the port caddy is listening on`，
     退出码 1。
   - 这期间收到停止信号（见下一节），或者父进程已经退出：直接进入收尾，不打印任何
     内容，也不启动 cloudflared，退出码 0。
6. **打印并开隧道**：打印 `Serving:` 和 `Local:` 两行；`--share` 时启动 cloudflared。

### 什么时候停止

启动完成后，`serve` 一直运行，直到发生下列事件之一，然后进入收尾：

| 事件 | 退出码 |
| --- | --- |
| **停止信号**：终端上按 `Ctrl-C`、`Ctrl-\`，关闭终端，或从外部给 `serve` 发 `INT` `HUP` `TERM` `QUIT` `PIPE` `ALRM` `USR1` `USR2` `VTALRM` `PROF` `XCPU` `XFSZ` `ABRT` `EMT` `SYS` 中的任何一个 | 0 |
| **父进程退出**，例如启动它的 shell 被杀 | 0 |
| **Caddy 退出**：打印 `serve: caddy exited` | 1 |
| **cloudflared 退出**：打印 `serve: cloudflared exited` | 1 |

补充说明：

- **父进程检查**：启动完成后每 0.5 秒检查一次父进程是否还在；等端口期间每一轮都检查。
- **输出管道的读端提前退出**：只有 `serve` 或它的服务下一次往这个管道写东西时才会
  被发现。在那之前什么都不写的话，`serve` 会继续运行。之后的结果取决于谁先写：
  - `serve` 自己先写：它收到 `SIGPIPE`，按停止信号收尾，退出码 0。zsh 还会在 stderr
    上报几行 `write error: broken pipe`。
  - Caddy 或 cloudflared 先写：它被 `SIGPIPE` 结束，`serve` 按"服务退出"收尾，退出码 1。

  想保留输出又不影响运行，可以把输出交给一个不会提前退出的读端，比如
  `serve --share |& tee serve.log`。
- **`Ctrl-Z`**：`serve` 和它的服务一起暂停，`fg` 后继续运行。暂停期间父 shell 退出的话，
  内核会向这个进程组发送 `SIGHUP` 和 `SIGCONT`，`serve` 照常收尾。
- **其他信号**：
  - `TSTP`、`TTIN`、`TTOU`、`STOP` 只让 `serve` 暂停，`CONT` 让它继续。
  - `CHLD`、`WINCH`、`URG`、`IO`、`INFO` 没有影响。
  - 未列出的致命信号见[已知限制](#已知限制)。
- **启动前的信号**：进入启动第 4 步之前收到停止信号时，`serve` 立即结束，退出码为
  128 + 信号编号（如 `Ctrl-C` 是 130）。这时还什么都没有启动；它当时正在等待的
  `scutil` 也会被一并结束。

### 收尾

所有停止路径都会走同一套收尾流程：

1. **不再响应停止信号**：收尾开始后，上面列出的停止信号全部被忽略，收尾不会被
   打断。
2. **通知整个进程组**：先向整个进程组发 `SIGTERM`，再发 `SIGCONT`，唤醒其中暂停的
   进程。
3. **等待退出**：之后每 0.1 秒检查一次，Caddy 和 cloudflared 都已退出时，`serve` 就以
   上面表格里的退出码结束。
4. **补发信号**：约第 0.5 秒和约第 1 秒时，各向整个进程组再发一轮 `SIGINT` + `SIGTERM`。
   有下载正在传输时，Caddy 收到第一个信号后会一直等传输结束，要再收到 `SIGINT`
   才会立即退出。
5. **强制结束**：约第 5 秒时还有没退出的，就向整个进程组发 `SIGKILL`。`serve` 自己也
   在组里，会一起结束，退出码 137。

### 进程边界

**`serve` 的进程组包括哪些进程**：

- `serve` 自己、Caddy 和 cloudflared 在同一个进程组里，组 ID 就是 `serve` 的 PID。
- 组里还可能有同一个管道里的其他命令，比如 `serve |& tee serve.log` 里的 `tee`。
  它们会顺带收到 `serve` 发给进程组的停止信号。这是"只对自己的进程组发信号"这个做法
  的副作用，不是刻意保证的行为：不要依赖它，也不要把需要在 `serve` 停止后继续运行的
  命令（比如 `less`）放在同一个管道里。

**`serve` 保证**：

- **只对自己的进程组发信号**：所有信号都只发给这个进程组，从不发给单个 PID，不会
  波及组外的任何进程。
- **走完收尾就不留下服务**：只要 `serve` 走完收尾流程，它退出时 Caddy 和 cloudflared
  都已经结束。

**`serve` 不保证**：收尾本身被跳过的情况，见[已知限制](#已知限制)。

### 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 正常停止：停止信号或父进程退出 |
| `1` | 出错：缺少依赖命令、不是进程组组长、Caddy 启动失败或读不到端口、运行中某个服务退出 |
| `2` | 参数错误 |
| `137` | 收尾约 5 秒后仍有进程未退出，整组被 `SIGKILL`；`serve` 被外部 `SIGKILL` 时也是这个值 |
| 其他 128 + N | `serve` 被信号 N 直接结束，没有走收尾。可能是启动前收到的信号，也可能是下文的致命信号 |

## 安全提示

- **没有任何访问控制**。不带 `--share` 时，同一局域网里的任何人都能访问；带
  `--share` 时，互联网上的任何人都能访问。
- **目录列表不隐藏点文件**。不要在含有 `.env`、`.git`、密钥等内容的目录里运行，分享
  前先检查目录内容。

## 设计说明

- **端口交给内核分配**：`--listen :0` 由内核给出一个空闲端口，再读回来。多个实例
  同时运行时不会争抢同一个端口。
- **读端口最多等 10 秒**：新装的程序第一次运行时，可能要先等 macOS 的安全扫描几秒
  才真正启动。
- **隧道指向 `localhost` 而不是 `127.0.0.1`**：Caddy 用一个同时接受 IPv4 和 IPv6 的
  监听器，`localhost` 解析成哪个地址都能连上。
- **组长检查直接问内核**：用 `kill -0 -$$` 判断。以自己 PID 为 ID 的进程组存在，
  当且仅当自己是这个组的组长。如果误判成组长，后面发往进程组的信号全都会落空，
  服务就会变成孤儿。
- **收尾放在 zsh 的 `{ ... } always { ... }` 里**：不论运行部分怎么结束，收尾都会
  执行，包括运行部分因为 zsh 自身报错而中止的情况，那种情况下没有信号可以捕获。
- **不设看门狗进程**：`serve` 被 `SIGKILL` 时无法自救。但看门狗自己同样可能被
  `SIGKILL`，问题只会往下挪一层，还多出一个要保证不变成孤儿的进程。

## 已知限制

- **收尾被跳过的情况**：`serve` 被 `SIGKILL`（如 `kill -9`、活动监视器里的"强制退出"），
  或者收到 `SEGV`、`BUS`、`ILL`、`FPE`、`TRAP` 这类致命信号时，不会执行收尾，Caddy
  和 cloudflared 会继续运行。
  - 后面这几个信号不捕获，是因为它们真正发生时，处理函数返回后会重新执行出错的
    那条指令，捕获了只会卡死。
  - 残留进程仍在原来的进程组里，可以按组一次结束。组 ID 就是当时 `serve` 的 PID。
    用 `SIGKILL` 而不是 `SIGTERM`，因为有下载正在传输时，Caddy 收到 `SIGTERM` 后会
    一直等下去：

    ```bash
    pgrep -lf 'caddy file-server|cloudflared tunnel --url'   # 找残留进程
    ps -o pgid= -p <PID>                                        # 查它的进程组
    kill -KILL -<PGID>                                          # 按组结束
    ```
- **`lsof` 卡死时，10 秒上限不起作用**：截止时间只在两次 `lsof` 调用之间检查，单次
  调用卡死时 `serve` 会一直等。可以按 `Ctrl-C` 停止，收尾照常执行。
- **`Serving:` 行偶尔会和日志挤在同一行**：`serve` 往 stdout 写 `Serving:` 行的同时，
  Caddy 或 cloudflared 可能正往 stderr 写日志。两者都输出到同一个终端，一条较长的日志
  可能被拆开，`Serving:` 行就接在日志的前半段后面，不如平时醒目。stdout 本身的内容
  不受影响：把 stderr 重定向掉（如 `2>/dev/null`）就能看到完整的两行。
- **`scutil` 卡死时，`serve` 停在启动之前**：可以按 `Ctrl-C` 退出，这时还什么都没有
  启动。
- **必须是进程组组长才能运行**：所以不能在管道的非首位使用，也不能被没开 job
  control 的脚本当作普通命令调用。

## 测试

[`test/`](test/) 里是端到端测试。它依据的是独立的测试计划
[`test/PLAN.md`](test/PLAN.md)，而不是本 README。计划被展开成几百个 case，用真实的
caddy 在伪终端里运行 `serve`，并检查每个 case 结束后不留下任何进程：

```bash
python3 test/run.py
```

测试会故意让一些进程崩溃，macOS 因此会在 `~/Library/Logs/DiagnosticReports` 里留下
崩溃报告，有的在运行结束后二三十分钟才写出来，测试脚本不会自动清理。macOS 看起来会
自动清掉旧报告，但仍建议**跑完后按 [`test/README.md`](test/README.md#副作用崩溃报告)
的说明手动清理。**

设计和用法见 [`test/README.md`](test/README.md)。

## License

MIT，见 [LICENSE](LICENSE)。
