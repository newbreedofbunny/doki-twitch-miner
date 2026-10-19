# 💚 Doki Twitch Miner

**Earns Twitch drops for you and claims them automatically**, dressed up like
*Doki Doki Literature Club* — Monika edition.

Pick the campaigns you want, queue specific streamer drops, and let it watch in the
background (no video is downloaded). It can mine on several Twitch accounts at once.

![Mining page, Doki Doki theme](docs/screenshot-mining.png)

## Features

- **Every live drop campaign on Twitch**, in a searchable list with a progress bar for each drop.
- **Automatic claiming**: finished drops are claimed the moment they complete.
- **Drop queue**: pick exactly which drops to mine, in order. Drag to reorder, or right-click
  a drop to *Move to position* / *Swap with*. If the channels for the top drop are offline,
  the miner moves down the queue, and switches back as soon as a higher one comes online.
- **Streamer drops first**: channel-specific drops (e.g. Rust's streamer drops) are mined
  before general drops. General drops keep progressing on streamer channels anyway.
- **Rust streamer drops** are read from [twitch.facepunch.com](https://twitch.facepunch.com/),
  so you can queue a streamer's drop even while they're offline.
- **Multiple accounts at once**: tick "Mine" on as many accounts as you like. They share one
  campaign scan but keep their own progress and claims.
- **Auto-claim per account**: untick an account's **Claim** box to collect its finished drops
  yourself. They show as *Ready to claim* with a **Claim** button, while other accounts keep
  claiming automatically.
- **Picks up new campaigns by itself**: Twitch is rescanned regularly, and newly started
  campaigns for your games are mined automatically.
- **Two themes**: *Doki Doki* (pink polka dots) and *Just Monika* (her starry space room).
- **Optional start with Windows**, minimized, mining straight away.

![Campaigns page with the drop queue](docs/screenshot-campaigns.png)

## Download and run

1. Go to [**Releases**](../../releases) and download **`DokiTwitchMiner.exe`**.
2. Double-click it. Nothing to install.
   - The .exe isn't code-signed, so Windows SmartScreen may say *"Windows protected your PC"*:
     click **More info → Run anyway**.
   - Some antivirus programs flag PyInstaller apps by mistake. The full source is in this repo
     if you'd rather [run or build it yourself](#run-from-source).
3. On first launch it downloads its fonts (a few seconds). Until then it uses Windows fonts.

## Getting started

1. **Add account** (Mining page). Your browser opens `twitch.tv/activate`; confirm the code
   shown in the app. Repeat to add more accounts.
2. Open **Campaigns** and either
   - click **+ Queue** on the drops you want (the order is the mining order), or
   - tick **Mine game** to mine all of a game's campaigns.
3. Click **Start mining**. Progress bars fill up, and drops are claimed automatically.

Make sure each game's account is linked on
[twitch.tv/drops/campaigns](https://www.twitch.tv/drops/campaigns), or Twitch won't count any progress.

![Just Monika theme](docs/screenshot-just-monika.png)

## Troubleshooting

| Problem | Fix |
|---|---|
| A red banner says watch time isn't reaching Twitch, or that drops are progressing slower | The banner names the cause and the fix (see [the table below](#connection-problems)). Click it to copy the details, e.g. to send to someone helping you. |
| A drop is stuck at the same percentage | Twitch isn't counting that campaign at the moment. After 8 minutes without progress the miner skips it (one campaign for an hour, or the whole game for 2 hours if the game account isn't linked), then tries again. |
| A campaign shows *Not progressing* | No progress for 8 minutes, usually because the game account isn't linked. Link it, then click **Refresh campaigns**. |
| It's mining games I didn't pick | That's the **Also mine games I haven't picked** option (Settings → Mining). Untick it and the miner only mines your queue and picked games, and waits when they have nothing left. |
| A streamer drop shows *Not started* | The streamer is live, but Twitch hasn't switched their drop on yet. It's retried on every rescan. |
| Where are my settings? | `%APPDATA%\TwitchDropClaimer\` (settings, saved logins, downloaded fonts). |
| Something went wrong and I want to report it | Attach `%APPDATA%\TwitchDropClaimer\miner.log`. It records what the console showed, plus a health line every 5 minutes (memory, handles, cache size). It stays around 1 MB and contains no passwords or login tokens. |

### Connection problems

Twitch only counts watch time when the stream's video is requested from its video servers (`*.ttvnw.net`),
which the miner does every 20 seconds. A connection that's cut is retried once straight away. If requests
still fail for about a minute, the red banner says *watch time isn't reaching Twitch*. If only some fail
(a connection that drops now and then), it says *drops are progressing slower* and how many of the last 15
failed. Drops still progress then, just slower. Either way, the banner names one of these causes:

| The banner says | Fix |
|---|---|
| your DNS can't find Twitch's video servers | A DNS blocker (Pi-hole, AdGuard, NextDNS, router parental controls, a VPN's ad / tracker blocking) is blocking `*.ttvnw.net`. Allow-list it or turn the blocker off. |
| something is intercepting secure connections | Turn off your antivirus's "HTTPS scanning" / "web shield" (Avast, AVG, Kaspersky, ESET, Bitdefender) or add an exception for `DokiTwitchMiner.exe`. School and work networks can do this too. |
| a firewall or antivirus is blocking this app's internet access | Allow `DokiTwitchMiner.exe` in Windows Firewall and your antivirus. |
| the connection ... keeps getting cut | Usually a VPN, an antivirus "web shield" / HTTPS scanning, or unstable Wi-Fi. Try without the VPN, pause the web shield (or add an exception for `DokiTwitchMiner.exe`), restart the router or use Ethernet. |
| connections ... are timing out / being refused | Usually a VPN, proxy or firewall. Try without the VPN or on another network. |

A browser that still plays Twitch video doesn't rule out a DNS block: browsers often use their own "Secure DNS".
Quick checks in PowerShell: `Resolve-DnsName usher.ttvnw.net` (fails = DNS block) and
`Test-NetConnection usher.ttvnw.net -Port 443` (`TcpTestSucceeded : False` = firewall / VPN block).

## Run from source

Needs **Python 3.10+** on Windows; only the standard library is used.

```powershell
python twitch_drop_claimer.py
```

`Start.bat` launches it without a console window, and `Create Shortcut.ps1` makes Desktop and
Start-menu shortcuts with the app icon.

### Build the .exe

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

This creates `dist\DokiTwitchMiner.exe` with [PyInstaller](https://pyinstaller.org), in a
separate virtual environment.

## Notes

- **Use at your own risk.** Watching streams automatically isn't something Twitch officially
  allows, so there is some risk to your account, and more with several accounts from one PC.
- Not affiliated with, or endorsed by, **Twitch** or **Team Salvato**. *Doki Doki Literature
  Club* is © Team Salvato; this is an unofficial fan project.
- Fonts aren't bundled: [Riffic Free](https://www.dafont.com/riffic.font) (Inkyotter, free for
  personal use) and [Aller](https://www.fontsquirrel.com/fonts/aller) (Dalton Maag) are
  downloaded from their official pages on first launch, under their own licences.
- Monika illustration used with permission.
- Inspired by [DevilXD/TwitchDropsMiner](https://github.com/DevilXD/TwitchDropsMiner).
