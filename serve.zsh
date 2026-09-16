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
    for cmd in caddy cloudflared scutil ps lsof; do
      if (( ! $+commands[$cmd] )); then
        print -u2 "serve: $cmd not found"
        exit 1
      fi
    done

    local port line
    local host=$(scutil --get LocalHostName 2>/dev/null)
    local me=$sysparams[pid] parent=$sysparams[ppid]
    local pgid=${$(ps -o pgid= -p $me)// /}
    local caddy_pid cf_pid stop=0 rc=0 i

    # All signals below target our own process group; refuse to run without one
    if [[ -z $me || $pgid != $me ]]; then
      print -u2 "serve: not a process group leader, refusing to start"
      exit 1
    fi

    # Every way the terminal or the parent shell can tell us to stop, Ctrl-\
    # included: an untrapped SIGQUIT would end this subshell without the
    # cleanup below, leaving whatever ignores SIGQUIT running.
    trap 'stop=1' INT HUP TERM QUIT

    # Port 0 makes the kernel hand out a free ephemeral port. A fixed port would
    # not be safe: Caddy sets SO_REUSEPORT, so a second instance would silently
    # share a busy port instead of failing, and all traffic would keep going to
    # the first one.
    caddy file-server --browse --listen :0 </dev/null &
    caddy_pid=$!

    # Wait until caddy is listening and read back the port it was given. Caddy
    # opens a single dual-stack listener for :0, so the first address lsof
    # reports is the only one. The window is generous because the first launch
    # of a freshly installed binary can sit in macOS's malware scan for a few
    # seconds before it runs at all. A Ctrl-C during the poll also hits lsof
    # (same process group); the loop then ends through the trap on the next turn.
    for i in {1..100}; do
      for line in ${(f)"$(lsof -nP -a -p $caddy_pid -iTCP -sTCP:LISTEN -Fn 2>/dev/null)"}; do
        [[ $line == n*:<-> ]] || continue
        port=${line##*:}
        # Anything but a real TCP port number means lsof misbehaved; it must
        # not reach cloudflared as part of a URL. The length check keeps
        # absurd input away from the arithmetic, which would warn about it.
        (( ${#port} <= 5 && port > 0 && port <= 65535 )) || port=
        break
      done
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
      if [[ -n $host ]]; then
        print "Local: http://${host}.local:${port}/"
      else
        print "Local: http://localhost:${port}/ (LocalHostName is not set, so there is no .local name)"
      fi
      cloudflared tunnel --url "http://localhost:${port}" </dev/null &
      cf_pid=$!

      # Run until interrupted, a service exits, or the parent shell dies. A
      # service found gone right after an interrupt is not an error: the same
      # signal reached it directly, and the cleanup below is about to run anyway.
      while (( ! stop && sysparams[ppid] == parent )); do
        if ! kill -0 $caddy_pid 2>/dev/null; then
          (( stop )) || { print -u2 "serve: caddy exited"; rc=1 }
          break
        fi
        if ! kill -0 $cf_pid 2>/dev/null; then
          (( stop )) || { print -u2 "serve: cloudflared exited"; rc=1 }
          break
        fi
        zselect -t 50
      done
    fi

    # Stop everything in our process group (never signal bare PIDs)
    trap '' INT HUP TERM QUIT
    kill -TERM -$me
    kill -CONT -$me
    for i in {1..50}; do
      kill -0 $caddy_pid 2>/dev/null || kill -0 $cf_pid 2>/dev/null || exit $rc
      zselect -t 10
    done
    kill -KILL -$me
  )
}
