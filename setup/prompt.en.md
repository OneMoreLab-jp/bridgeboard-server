I want to use the BridgeBoard iPad app with this Mac. BridgeBoard server is a small read-only program that lets my iPad, over my own Tailscale network, list the Claude Code sessions on this Mac and open their conversations and project files. Please install it for me by following the steps below exactly. Its source is https://github.com/OneMoreLab-jp/bridgeboard-server (MIT license). It needs Tailscale signed in on this Mac and **MagicDNS turned on** for my tailnet (the DNS page of the Tailscale admin console); the check in step 2 reports it as `magicdns`.

## Rules

- Run only the commands in the steps, in order. Do not edit any script or any file you download, and do not "fix" a failure by working around it.
- If a command fails, show me its output verbatim and stop. Do not retry it with changes.
- Do not use sudo and do not ask for my password. Do not install anything else (no Homebrew, no Tailscale, no packages) and do not change any other setting.
- Do not read, print, copy, search or list the files inside `~/Library/Application Support/BridgeBoard/`. They hold secrets and the keys of my devices; the commands below write there on their own.
- If the sandbox or a permission rule blocks a command (for example it cannot write to `~/Library/LaunchAgents`, cannot run `launchctl`, or cannot reach github.com), do not try to get around it and do not turn the sandbox off. Tell me which command was blocked, and give me that exact command (with the real path filled in) as one line to run in my own Terminal app. Wait until I say it is done.

## Steps

1. Download the release `v0.1.0` into a new temporary folder (never into the folder I have open). This one command prints the commit id and then the absolute path of the downloaded folder. Tell me the commit id (I will compare it with the tag on GitHub) and keep the path:

   ```
   CLONE="$(mktemp -d)/bridgeboard-server" && git clone --depth 1 --branch v0.1.0 https://github.com/OneMoreLab-jp/bridgeboard-server.git "$CLONE" && git -C "$CLONE" rev-parse HEAD && echo "$CLONE"
   ```

   Shell variables and the working directory may not carry over from one command to the next, so every later command below starts with `cd` to the path printed by step 1 (write the path itself in place of `<path from step 1>`, in quotes).

2. Check this Mac. It prints one line per item, `ok`, `warn` or `fail`, and changes nothing:

   ```
   cd "<path from step 1>" && ./install.sh --check
   ```

   If any line starts with `fail` (the exit status is 3), show me the whole output verbatim and stop; add one sentence per `fail` line on how I can fix it, but do not fix it yourself. (For `fail magicdns` the fix is to turn on MagicDNS on the DNS page of the Tailscale admin console, https://login.tailscale.com/admin/dns.) A `warn remote-control` line is not a failure: go on, and tell me that the iPad lists only Claude Code sessions that run with Remote Control, so I must start one.

3. Install. This copies the server to `~/Library/Application Support/BridgeBoard/`, adds a LaunchAgent to `~/Library/LaunchAgents/` and starts it. It waits up to 15 seconds for the server to answer. Run it without options:

   ```
   cd "<path from step 1>" && ./install.sh
   ```

   If it exits with a non-zero status (4 means the server did not answer; the last lines of its log are in the output), show me the output verbatim and stop.

4. Ask me whether the iPad with the BridgeBoard app is ready. Then pair it:

   ```
   "$HOME/Library/Application Support/BridgeBoard/current/bbctl" pair
   ```

   The command first checks that Tailscale reports a MagicDNS name for this Mac and that the server answers; if not, it prints an `error:` line before it makes any code (show me that message and stop). Then it prints `QR image: <path>`, opens that image on this Mac's screen, and waits up to five minutes for the iPad to scan it. It then prints `paired: <device name>`, `expired`, or `replaced: ...` (another pairing was started). Because it can run for five minutes, give your shell tool a timeout of at least 330 seconds, or run the command in the background and read its output.

   - Do not open, read or display the QR image, and do not describe it. It holds a one-time pairing code, and it would end up in this conversation. I will scan it from the screen myself.
   - If it prints a warning that it could not open the QR image (`Open it yourself: <path>`), tell me that path so that I can open the image myself. Do not open it yourself.
   - As soon as the path is printed, tell me: "On the iPad, open BridgeBoard and scan the QR code that is now on this Mac's screen."
   - When it prints `paired:`, tell me. If it prints `expired` or `replaced`, tell me, and run the command again only if I ask. After pairing I can close the QR window: the code works once, expires after five minutes, and the image file is deleted.

5. Report in a few lines: the commit id from step 1, the version `install.sh` installed, and whether `paired:` appeared. Do not include anything else from the output.
