# test/test_serve.py

端到端测试，用真实 `caddy` 二进制、在伪终端里驱动一个交互式 `zsh -f -i` 来跑
`serve`。每个场景结束后都会断言：没有残留的 caddy / cloudflared / 桩进程，
以及场景开始时启动的一个无关"诱饵"进程仍然存活——后者用来确认清理逻辑真的
只对 `serve` 自己的进程组发信号，没有误伤旁路进程。

```bash
python3 test_serve.py              # 默认用不联网的 cloudflared 桩替身
python3 test_serve.py --real-tunnel # 换成真实 cloudflared，校验隧道 URL 和 --url 参数
python3 test_serve.py --only single_instance concurrent_instances
```

## 前提

- macOS；`caddy` 必须在 PATH 上（`brew install caddy`）。
- 只有 `--real-tunnel` 才需要 `cloudflared` 在 PATH 上。默认场景用一个只是
  `sleep` 的占位脚本代替它，因为 Cloudflare 对同一出口 IP 的 quick tunnel
  接口有频率限制，连续跑测试很容易在几次之内就撞到 `429`。
- 运行前不能已经有 `caddy`/`cloudflared` 在跑（脚本会检测并拒绝启动，避免
  跟测试要清理的进程搞混）。

## 已知会跳过的场景

真实 `cloudflared` 的 metrics 端口在固定的 5 个默认端口（`20241`–`20245`）都被
占用后会回退到随机端口——这个行为只有六个真实 `cloudflared` 实例同时跑起来
才测得到，会显著加大撞上 Cloudflare 频率限制的概率，这里没有把它做成常规
测试的一部分。
