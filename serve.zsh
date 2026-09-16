# ---------------- serve: temporary HTTP file server + public tunnel ----------------
# Serve the current directory with Caddy on a kernel-assigned port (LAN) and a
# cloudflared quick tunnel (public). Ctrl-C stops both. Signals are only ever
# sent to our own process group, so nothing outside `serve` can be killed by
# mistake. Several instances can run at once, each on its own port.
serve() {
  emulate -L zsh
  setopt monitor nobgnice

  (
    zmodload zsh/system zsh/zselect || exit 1

    # Everything serve shells out to. A missing helper is checked here so it
    # fails with its own name, instead of surfacing later as a symptom that
    # points at the wrong thing: without lsof the port simply never turns up,
    # which is indistinguishable from caddy never managing to listen.
    local cmd
    for cmd in caddy cloudflared scutil ps lsof sed head; do
      if (( ! $+commands[$cmd] )); then
        print -u2 "serve: $cmd not found"
        exit 1
      fi
    done

    local port
    local host=$(scutil --get LocalHostName)
    local me=$sysparams[pid] parent=$sysparams[ppid]
    local pgid=${$(ps -o pgid= -p $me)// /}
    local caddy_pid cf_pid stop=0 rc=0 i

    # All signals below target our own process group; refuse to run without one
    if [[ -z $me || $pgid != $me ]]; then
      print -u2 "serve: not a process group leader, refusing to start"
      exit 1
    fi

    trap 'stop=1' INT HUP TERM

    # Port 0 makes the kernel hand out a free ephemeral port. A fixed port would
    # not be safe: Caddy sets SO_REUSEPORT, so a second instance would silently
    # share a busy port instead of failing, and all traffic would keep going to
    # the first one.
    caddy file-server --browse --listen :0 </dev/null &
    caddy_pid=$!

    # Wait until caddy is listening and read back the port it was given. A
    # Ctrl-C during the poll also hits lsof/sed/head (same process group); the
    # result is then empty and the loop ends through the trap on the next turn.
    for i in {1..30}; do
      port=$( { lsof -nP -a -p $caddy_pid -iTCP -sTCP:LISTEN -Fn \
                | sed -n 's/^n.*:\([0-9]*\)$/\1/p' | head -n 1 } 2>/dev/null )
      # Anything but a real TCP port number means the pipeline misbehaved; it
      # must not reach cloudflared as part of a URL
      [[ $port == <-> ]] && (( port > 0 && port <= 65535 )) || port=
      [[ -n $port ]] && break
      (( stop )) && break
      kill -0 $caddy_pid 2>/dev/null || break
      zselect -t 10
    done

    if (( stop )); then
      # Interrupted during startup: the terminal's SIGINT also reached caddy
      # directly, so its state says nothing, and nothing else gets started
      :
    elif ! kill -0 $caddy_pid 2>/dev/null; then
      print -u2 "serve: caddy failed to start"
      rc=1
    elif [[ -z $port ]]; then
      print -u2 "serve: could not read the port caddy is listening on"
      rc=1
    else
      # The tunnel is public and nothing is hidden (dotfiles included), so make
      # the exposed directory hard to miss. '%' is doubled to survive -P.
      print -rP -- "%B%F{red}Serving: ${PWD//\%/%%}%f%b"
      print "Local: http://${host}.local:${port}/"
      cloudflared tunnel --url "http://localhost:${port}" </dev/null &
      cf_pid=$!

      # Run until interrupted, a service exits, or the parent shell dies
      while (( ! stop && sysparams[ppid] == parent )); do
        if ! kill -0 $caddy_pid 2>/dev/null; then
          print -u2 "serve: caddy exited"
          rc=1
          break
        fi
        if ! kill -0 $cf_pid 2>/dev/null; then
          print -u2 "serve: cloudflared exited"
          rc=1
          break
        fi
        zselect -t 50
      done
    fi

    # Stop everything in our process group (never signal bare PIDs)
    trap '' INT HUP TERM
    kill -TERM -$me
    kill -CONT -$me
    for i in {1..50}; do
      kill -0 $caddy_pid 2>/dev/null || kill -0 $cf_pid 2>/dev/null || exit $rc
      zselect -t 10
    done
    kill -KILL -$me
  )
}
