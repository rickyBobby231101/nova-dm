# Running the table

Everything is a user service and everything is enabled, so **after a reboot the
game is already up and already on the tailnet**. There is no startup ritual.

## Where it lives

| | |
|---|---|
| Game | `http://localhost:5050` |
| Same house, over wifi | `http://192.168.1.241:5050` |
| Anywhere, over the tailnet | `http://<this-machine>.<your-tailnet>.ts.net:5050` |
| ...or by address | `http://<tailnet-ip>:5050` |
| DM screen | add `/dm` to any of the above |

The wifi address can change if the router hands out a new lease. The tailnet
name never does.

## Codes

Printed every time the service starts, and stored in
`~/.config/nova-dm/secrets.json` (mode 0600, outside the repo).

```
systemctl --user status nova-dm    # the banner, including both codes
```

- **Join code** — give to players. Lets them play.
- **DM password** — keep. Lets you run the game: start fights, deal damage,
  award XP. A player who only has the join code cannot do any of it.

To reissue one without disturbing the other, or without logging anyone out:

```python
from engine import auth
auth.rotate("join_code")     # or "dm_password"
```

## The commands you'll actually use

```
systemctl --user status nova-dm      # is it up? what are the codes?
systemctl --user restart nova-dm     # after changing code
systemctl --user stop nova-dm        # done playing
journalctl --user -u nova-dm -f      # watch a turn happen live
```

## Inviting someone

1. `tailscale invite` — or the admin console at
   <https://login.tailscale.com/admin/users>
2. They install Tailscale (phone or desktop) and accept.
3. Send them the tailnet URL and the join code.

The player-facing walkthrough lives in `Join-The-Table.md`.

Nothing is ever exposed to the public internet. Only devices on your tailnet can
reach the game, and they still need the join code.

## When something's wrong

**Nobody can connect.** Check the game is actually up — `systemctl --user
status nova-dm`. Then check the tunnel — `tailscale status` should list your
machine and theirs, and `tailscale serve status` should show port 5050
forwarding to `127.0.0.1:5050`.

**A remote player sees nothing but the laptop works.** Their Tailscale toggle is
probably off. Being logged in is not the same as being connected.

**Turns suddenly take many minutes.** Run `ollama ps`. Only one model stays
resident, so if something left a different one loaded, the first turn pays to
swap it out — `ollama stop <model>` clears it. The startup banner warns about
this too. (This is *not* caused by Nova Cathedral; the two coexist fine.)

**No voice.** Narration is queued to a worker, so the audio arrives roughly
20 seconds *after* the text — that is normal, not a fault. On a phone, nothing
plays until the player taps **TAP TO ENABLE THE DM'S VOICE**; phones block
audio nobody asked for, silently.

## Notes on this machine

Tailscale is installed **rootless** — static binaries in `~/.local/bin`, running
in userspace-networking mode as a user service, because there was no root
available. Two consequences:

- Inbound traffic reaches the game through `tailscale serve`, not through the
  kernel. That is why the forward exists and must stay configured.
- This laptop **cannot reach its own tailnet address**. Testing
  `http://<tailnet-ip>:5050` from here will always fail; it works only from
  another device. Use `localhost` to test locally.

`~/.local/bin/tailscale` is a wrapper that points the CLI at the rootless
daemon's socket. Without it the CLI reports "not connected" while the tailnet is
perfectly healthy.
