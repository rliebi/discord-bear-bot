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
/calc archer_total:<int> march_count:<int> [is_calling:<true|false>] [override_march_archers:<int>] [total_march_size:<int>] [hidden:<true|false>]
```
- archer_total: Your total number of archers
- march_count: How many joining marches you are sending/participating in
- is_calling (optional): Whether you are also calling a rally (Default: true)
- override_march_archers (optional): Force joining archers to this specific amount
- total_march_size (optional): Your personal physical march capacity. Used to calculate exactly how much cavalry to send.
- hidden (optional): If true, the response is visible only to you (ephemeral). Defaults to public.

### What you get back
The bot provides calculations based on the roles you specified:
- Your Joining March: Shown if `march_count > 0`.
  - Archers are capped by the server's MAA setting and rounded down to the nearest 1000 for easier coordination.
  - Cavalry calculation respects both your `total_march_size` (if provided) and the server's `Max Troop Size` limit.
- Your Calling March: Shown if `is_calling` is true.
  - Archers are the exact remainder needed to reach your total archer pool, and are NOT rounded.
  - Calculation is limited only by your `total_march_size` (the server's `Max Troop Size` limit does not apply to the caller).

Note: By default, /calc results are posted publicly in the channel so your team can coordinate. Set `hidden:true` to receive the result privately. Admin commands remain ephemeral.

Auto-delete: Non-ephemeral /calc messages are automatically deleted after 10 minutes by default. Admins can change the timeout with `/admin set-message-ttl-minutes <N>` or set it to `0` to disable auto-deletion. When auto-delete is enabled (TTL > 0) and the response is public, the result embed includes a footer telling you in how many minutes it will be deleted.

Graceful shutdown: When the bot is stopped (e.g., container/app shutdown), any pending auto-delete timers are cancelled and the bot will attempt to immediately delete all messages that were scheduled for deletion. This reduces the chance that result messages linger if the app restarts before timers fire.

### Ratio Mode (automatic, Calling March only)
We switch to simple ratio guidance for the Calling March when you have a surplus of archers:
- Condition: `TA > (MC × MAA) + extra`
  - `extra` defaults to 120,000
  - If `total_march_size` is provided, `extra = floor(0.9 × total_march_size)`
- Output for the Calling March:
  - Infantry: 1%
  - Cavalry: Rest (≈9%)
  - Archers: 90%
- Joining Marches remain in normal mode with numeric values.

### Calculation Summary
Given server settings MTS, INF, MAA and user input TA, MC, and IS_CALLING:
1) Divisor = MC + (1 if IS_CALLING else 0)
2) Base Archers = floor(TA / Divisor)
3) Your Joining March (if MC > 0)
   - Archers = min(Base Archers, MAA) rounded down to nearest 1000
   - Infantry = INF
   - Cavalry = max(0, min(total_march_size, MTS) - Archers - Infantry) (if total_march_size provided)
4) Your Calling March (if IS_CALLING)
   - Archers = min(TA - (Joining Archers * MC), total_march_size - INF)
   - Infantry = INF
   - Cavalry = max(0, total_march_size - Archers - Infantry) (if total_march_size provided)

### Examples
Assume server settings: MTS=300000, INF=20000, MAA=160000

1) Only joining 2 rallies (No calling)
```
/calc archer_total:400000 march_count:2 is_calling:false
```
- Divisor = 2
- Base Archers = floor(400000 / 2) = 200000 → cap at 160000
- Joining March = Archers 160000, Infantry 20000, Cavalry 120000 (if MTS=300k)

2) Calling 1 and joining 2 (Total 3 marches)
```
/calc archer_total:400000 march_count:2
```
- Divisor = 3 (Default is_calling:true)
- Base Archers = floor(400000 / 3) = 133333 → round to 133000 → cap ≤ 160000 → 133000
- Joining March = Archers 133000, Infantry 20000
- Calling March Archers = 400000 - (133000*2) = 134000

3) Only calling 1 rally
```
/calc archer_total:200000 march_count:0
```
- Divisor = 1
- Calling March Archers = 200000, Infantry 20000

## Last: Show your most recent calculation
Use the slash command:
```
/last [hidden:<true|false>]
```
- hidden (optional): If true, the response is visible only to you (ephemeral). Defaults to hidden.

This shows your most recent /calc inputs and outputs in this server. If you haven’t used /calc yet here, it will tell you there’s no previous calculation.

## Running multiple instances
- Avoid running multiple replicas of the same bot token at the same time. Discord will deliver the same interactions to all of them, causing duplicate replies.
- This project includes a singleton guard: on startup the bot acquires a file lock at `${DATA_DIR}/bot.lock` and exits if it cannot obtain it. This prevents accidental duplicates when sharing the same `/data` volume.
- To intentionally allow multiple instances (advanced; e.g., when implementing sharding), set `ALLOW_MULTI_INSTANCE=true`.
- Recommendation: In Docker Swarm/Compose keep `replicas: 1` for this service.

## Troubleshooting
- Slash commands not visible: Wait up to a minute after first run. Ensure the bot was invited with the `applications.commands` scope.
- Permission issues: The admin might be unassigned. Use `/admin show-settings` to verify the bot is configured.
- Data not saved: Mount a persistent volume or check write permissions for the container’s `/data` directory.
