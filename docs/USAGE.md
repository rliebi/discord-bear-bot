# Usage Guide

This guide explains how to configure your server and use the bot’s commands.

If you haven’t created a Discord Application and Bot yet, follow docs/SETUP_DISCORD_APP.md first.

## Who is the Admin?
- The first person to use any command in a new guild (server) becomes the admin automatically.
- The admin can reassign admin rights with `/admin set-admin @User`.

## Admin: Server Settings
The bot needs three server-wide settings. Only the admin can set them.

- Max Troop Size (MTS): maximum units per march.
- Infantry Amount (INF): fixed number of infantry per march (rounded down to nearest 1000 on set).
- Max Archers Amount (MAA): cap for archers in a joining march (rounded down to nearest 1000 on set).

Commands:
- `/admin set-max-troop-size <int>`
- `/admin set-infantry-amount <int>`
- `/admin set-max-archers-amount <int>`
- `/admin show-settings` (view the current values)

Example setup:
```
/admin set-max-troop-size 300000
/admin set-infantry-amount 20000
/admin set-max-archers-amount 160000
```

## Users: Calculate Marches
Use the slash command:
```
/calc archer_total:<int> march_count:<int> calling:<true|false>
```
- archer_total: Your total number of archers
- march_count: How many joining marches you plan to send
- calling: true if you will call a rally, false otherwise

### What you get back
- Joining March table: Archers, Infantry, Cavalry for each joining march
- Calling March table: shown only if calling=true. Provides an Archer/Inf/Cav composition for your caller march

### Calculation Summary
Given server settings MTS, INF, MAA and user input TA, MC, Calling:
1) Caller archer value for joining marches
- Divisor = MC + (1 if Calling else 0)
- Base = floor(TA / Divisor)
- Round base down to nearest 1000
- Cap by MAA

2) Joining march
- Archers = caller archer value
- Infantry = INF
- Cavalry = floor_1000(MTS - Archers - Infantry)

3) Calling march (only when calling=true)
- Infantry = INF
- Archers = floor_1000(min(TA - (caller_archer_value * MC), MTS - Infantry))
- Cavalry = floor_1000(MTS - Infantry - Archers)

All rounding is to the nearest 1000 downward with a minimum of 0.

### Examples
Assume server settings: MTS=300000, INF=20000, MAA=160000

1) Not a caller
```
/calc archer_total:400000 march_count:2 calling:false
```
- Divisor = 2
- Base Archers = floor(400000 / 2) = 200000 → round to 200000 → cap at 160000
- Joining March = Archers 160000, Infantry 20000, Cavalry 120000

2) Is caller
```
/calc archer_total:400000 march_count:2 calling:true
```
- Divisor = 3
- Base Archers = floor(400000 / 3) = 133333 → round to 133000 → cap ≤ 160000 → 133000
- Joining March = Archers 133000, Infantry 20000, Cavalry 147000
- Calling March Archers = floor_1000(min(400000 - (133000*2)=134000, 300000-20000=280000)) = 134000
- Calling March Cavalry = 300000 - 20000 - 134000 = 146000

## Troubleshooting
- Slash commands not visible: Wait up to a minute after first run. Ensure the bot was invited with the `applications.commands` scope.
- Permission issues: The admin might be unassigned. Use `/admin show-settings` to verify the bot is configured.
- Data not saved: Mount a persistent volume or check write permissions for the container’s `/data` directory.
