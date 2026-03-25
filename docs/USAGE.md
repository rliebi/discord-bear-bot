# Usage Guide

This guide explains how to configure your server and use the bot’s commands.

If you haven’t created a Discord Application and Bot yet, follow docs/SETUP_DISCORD_APP.md first.

## Who is the Admin?
- The first person to use any command in a new guild (server) becomes the admin automatically.
- The admin can reassign admin rights with `/admin set-admin @User`.

## Admin: Server Settings
The bot needs three server-wide settings. Only the admin can set them.

- Max Troop Size (MTS): maximum units per march.
- Infantry Amount (INF): fixed number of infantry per march.
- Max Archers Amount (MAA): cap for archers in a joining march.

Commands:
- `/admin set-max-troop-size <int>`
- `/admin set-infantry-amount <int>`
- `/admin set-max-archers-amount <int>`
- `/admin set-calc-message <text>` (append a custom message to every /calc result)
- `/admin clear-calc-message` (remove the custom message)
- `/admin show-settings` (view the current values)
- `/admin resync-commands` (force a re-sync of slash commands if Discord shows them as outdated)
- `/admin set-message-ttl-minutes <int>` (auto-delete /calc messages after N minutes; 0 disables)

## Admin: Usage stats
- `/admin usage [user:@User] [limit:<int>]` Show usage for this server. Omit `user` to see top users.
- `/admin usage-all-servers [limit_per_guild:<int>]` Show top usage per server across all servers this bot is in.

Example setup:
```
/admin set-max-troop-size 300000
/admin set-infantry-amount 20000
/admin set-max-archers-amount 160000
```

## Users: Calculate Marches
Use the slash command:
```
/calc archer_total:<int> march_count:<int> calling:<true|false> [max_march_size:<int>] [hidden:<true|false>]
```
- archer_total: Your total number of archers
- march_count: How many joining marches you plan to send
- calling: true if you will call a rally, false otherwise
- max_march_size (optional): Overrides the extra buffer used for ratio-mode; extra becomes floor(0.9 × max_march_size) instead of the default 120,000.
- hidden (optional): If true, the response is visible only to you (ephemeral). Defaults to public.

### What you get back
- Joining March table: Archers, Infantry, Cavalry for each joining march
- Calling March table: shown only if calling=true. Provides an Archer/Inf/Cav composition for your caller march

Note: By default, /calc results are posted publicly in the channel so your team can coordinate. Set `hidden:true` to receive the result privately. Admin commands remain ephemeral.

Auto-delete: Non-ephemeral /calc messages are automatically deleted after 10 minutes by default. Admins can change the timeout with `/admin set-message-ttl-minutes <N>` or set it to `0` to disable auto-deletion. When auto-delete is enabled (TTL > 0) and the response is public, the result embed includes a footer telling you in how many minutes it will be deleted.

### Ratio Mode (automatic, caller only)
We switch to simple ratio guidance only for the CALLING march when you have a surplus of archers:
- Condition: `TA > (MC × MAA) + extra` AND `calling = true`
  - `extra` defaults to 120,000
  - If `max_march_size` is provided, `extra = floor(0.9 × max_march_size)` to account for 90% archers on the caller march
- Output for the Calling March:
  - Infantry: 1%
  - Cavalry: 9%
  - Archers: 90%
- Joining Marches remain in normal mode with numeric values. Archers are capped by MAA and are rounded down to the nearest 1000 only when you are the caller; if you are not calling, they are not rounded.

### Calculation Summary (normal mode)
Given server settings MTS, INF, MAA and user input TA, MC, Calling:
1) Caller archer value for joining marches
- Divisor = MC + (1 if Calling else 0)
- Base = floor(TA / Divisor)
- Cap by MAA
- If calling=true, round DOWN to nearest 1000; otherwise do not round.
- caller_archer_value = (calling ? floor_1000(min(Base, MAA)) : min(Base, MAA))

2) Joining march
- Archers = caller_archer_value (rounded to 1000 only when calling)
- Infantry = INF
- Cavalry = max(0, MTS - Archers - Infantry)

3) Calling march (only when calling=true)
- Infantry = INF
- Archers = min(TA - (caller_archer_value * MC), MTS - Infantry)  (no extra rounding)
- Cavalry = Rest (i.e., MTS - Infantry - Archers)

Rounding rule: Joining march archers are rounded down to the nearest 1000 only when you are the caller; if you are not calling, they are not rounded. The calling march values are not rounded.

### Examples
1) Ratio mode kicks in with override
```
/calc archer_total:900000 march_count:3 calling:true max_march_size:300000
```
- MAA from server assumed large; threshold = (3 × MAA) + floor(0.9 × 300000) = (3 × MAA) + 270000
- If TA=900000 > threshold, output is 1% INF, 9% CAV, 90% ARCHERS

2) Normal mode remains
```
/calc archer_total:400000 march_count:2 calling:false
```
- Uses the standard computation with joining archers rounded down to the nearest 1000.

### Examples
Assume server settings: MTS=300000, INF=20000, MAA=160000

1) Not a caller
```
/calc archer_total:400000 march_count:2 calling:false
```
- Divisor = 2
- Base Archers = floor(400000 / 2) = 200000 → cap at 160000
- Joining March = Archers 160000, Infantry 20000, Cavalry 120000

2) Is caller
```
/calc archer_total:400000 march_count:2 calling:true
```
- Divisor = 3
- Base Archers = floor(400000 / 3) = 133333 → round to 133000 → cap ≤ 160000 → 133000
- Joining March = Archers 133000, Infantry 20000, Cavalry 147000
- Calling March Archers = min(400000 - (133000*2)=134000, 300000-20000=280000) = 134000
- Calling March Cavalry = 300000 - 20000 - 134000 = 146000

## Running multiple instances
- Avoid running multiple replicas of the same bot token at the same time. Discord will deliver the same interactions to all of them, causing duplicate replies.
- This project includes a singleton guard: on startup the bot acquires a file lock at `${DATA_DIR}/bot.lock` and exits if it cannot obtain it. This prevents accidental duplicates when sharing the same `/data` volume.
- To intentionally allow multiple instances (advanced; e.g., when implementing sharding), set `ALLOW_MULTI_INSTANCE=true`.
- Recommendation: In Docker Swarm/Compose keep `replicas: 1` for this service.

## Troubleshooting
- Slash commands not visible: Wait up to a minute after first run. Ensure the bot was invited with the `applications.commands` scope.
- Permission issues: The admin might be unassigned. Use `/admin show-settings` to verify the bot is configured.
- Data not saved: Mount a persistent volume or check write permissions for the container’s `/data` directory.
