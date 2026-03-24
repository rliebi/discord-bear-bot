# Kingshot Bear Discord Bot

A lightweight Discord bot to calculate Kingshot Bear Troop Ratios for your server. It supports per-server admin configuration and a user-facing slash command to calculate personal march sizes.

Quick links (Wiki):
- How to set up a Discord Application and invite the bot: docs/SETUP_DISCORD_APP.md
- How to use the bot (admin and user guide): docs/USAGE.md

Features:
- Per-guild settings configurable by an admin:
  - Max Troop Size
  - Infantry Amount (fixed per march)
  - Max Archers Amount (cap per march)
- A user slash command `/calc` that takes:
  - Archer Total Amount
  - March Count
  - Whether the user is calling rallies
  - Optional: Max March Size override (affects ratio-mode threshold)
  - Optional: Hidden (make response ephemeral)
- Produces a concise table for:
  - Joining March (Archers, Infantry, Cavalry)
  - Calling March (Archers, Infantry, Cavalry) if the user is a caller
- First user to use the bot in a guild becomes the admin by default (can be changed later).
- Dockerized and ready for Docker Swarm.
- Uses Discord best practices: slash commands, minimal intents, env-based token.

## Calculation Rules
Given:
- Server settings: Max Troop Size (MTS), Infantry Amount (INF), Max Archers Amount (MAA)
- User input: Total Archers (TA), March Count (MC), Calling (C boolean)
- Optional: Max March Size override (MMS)

Ratio mode switch (caller only):
- If TA > (MC * MAA) + extra AND you are the caller, we switch to ratio mode only for the caller march and show 1% Infantry, 9% Cavalry, 90% Archers.
- Joining marches remain in normal mode with numeric values.
- extra defaults to 120,000. If you provide Max March Size (MMS), extra becomes floor(0.9 * MMS) to account for 90% archers on the caller march.

Normal mode:
1. Caller archer value for joining marches:
   - Divisor = MC + (1 if C else 0)
   - Base = floor(TA / Divisor)
   - Cap by MAA
   - This value is used as Archers for each joining march.
2. Joining march:
   - Archers = caller archer value (from step 1)
   - Infantry = INF (from server)
   - Cavalry = max(0, MTS - Archers - Infantry)
3. Calling march (only if C is true):
   - Infantry = INF
   - Archers = min( TA - (caller archer value * MC), MTS - Infantry )
   - Cavalry = Rest (i.e., MTS - Infantry - Archers)

Joining march archers are rounded down to the nearest 1000 only when you are the caller. If you are not calling, joining march archers are not rounded. The calling march is never rounded.

## Commands
- `/calc archer_total:<int> march_count:<int> calling:<bool>`
- `/admin set-max-troop-size <int>`
- `/admin set-infantry-amount <int>`
- `/admin set-max-archers-amount <int>`
- `/admin show-settings`
- `/admin set-admin <@user>`
- `/admin set-calc-message <text>` (message appended to every /calc result)
- `/admin clear-calc-message` (remove the message)
- `/admin resync-commands` (force re-sync of slash commands if they appear outdated)
- `/admin set-message-ttl-minutes <int>` (auto-delete /calc messages after N minutes; 0 disables)

Admin checks: The first user who runs any command in a guild becomes admin automatically. Only the admin can use `/admin` commands.

## Configuration and Running
The bot uses the environment variable `DISCORD_TOKEN` for the Discord Bot Token.

### Local (Python)
1. Create and activate a virtualenv (optional).
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Set environment variables and run:
   ```bash
   export DISCORD_TOKEN=your_token_here
   python -m src.bot
   ```

### Docker
Build and run:
```bash
docker build -t discord-bear-bot .
# Run with a persistent data volume for settings
docker run -e DISCORD_TOKEN=your_token_here -v bearbot-data:/data --name bearbot --restart unless-stopped discord-bear-bot
```

### Docker Swarm
Use the provided compose file. Example:
```bash
export DISCORD_TOKEN=your_token_here
docker stack deploy -c docker-compose.yml bearbot
```
This sets up a persistent named volume `discord-bear-bot-data` for guild settings.

## Data persistence
Per-guild settings are stored in JSON at `/data/guild_settings.json`. The container exposes `/data` as a volume. You can override the base directory with `DATA_DIR` if needed.

## Permissions and Best Practices
- The bot requests only the Guilds intent.
- Uses slash commands (interactions) and defers message content.
- By default, /calc replies are public so teammates can see compositions. You can pass hidden:true to receive the result privately (ephemeral). Admin commands and error/config messages remain ephemeral.
- Auto-delete: Non-ephemeral /calc messages are automatically deleted after 10 minutes by default. Admins can change this with `/admin set-message-ttl-minutes <N>` or set to `0` to disable. When auto-delete is enabled, the /calc embed shows a footer indicating in how many minutes it will be deleted.

## Make the bot private
There are two layers you can use, together or separately:

1) Discord setting: make the application/bot non-public
- In Discord Developer Portal → Your App → Bot → Bot Permissions, disable the toggle "Public Bot" (also labeled "Requires OAuth2 Code Grant" in some UIs; ensure Public is OFF).
- With Public Bot OFF, only users with the invite URL or appropriate permissions can add the bot; it will not be listed in the App Directory.

2) Runtime allowlist: restrict to specific server IDs
- Set the environment variable `ALLOWED_GUILDS` to a comma-separated list of guild (server) IDs. Example:
  - ALLOWED_GUILDS=123456789012345678,987654321098765432
- Behavior when ALLOWED_GUILDS is set:
  - The bot will only respond in those servers.
  - It will auto-leave any other server it is invited to.
- Where to find a Guild ID: Enable Developer Mode in Discord → Right-click the server icon → Copy Server ID.

Examples:
- Docker Compose (docker-compose.yml):
  environment:
    - DISCORD_TOKEN=${DISCORD_TOKEN}
    - LOG_LEVEL=INFO
    - DATA_DIR=/data
    - ALLOWED_GUILDS=123456789012345678,987654321098765432

- Raw Docker:
  docker run -e DISCORD_TOKEN=... -e ALLOWED_GUILDS=123,456 -v bearbot-data:/data --restart unless-stopped ghcr.io/rliebi/discord-bear-bot:latest

Optional: Make your container image private
- In GitHub → Packages → your image (ghcr.io/rliebi/discord-bear-bot), you can change the package visibility to Private. Consumers must docker login ghcr.io to pull.

## Troubleshooting
- Commands not visible: Ensure the bot has the application.commands scope authorized in your server, and wait up to a minute for global sync. We also force sync on startup.
- 401/unauthorized: Verify `DISCORD_TOKEN` is correct and the bot is invited to the guild.
- State not saved: Ensure the `/data` volume is mounted and writable by the container user.
- Docker volume chown error: If you see an error like "failed to chown ... operation not permitted" when deploying with a named volume, note that this image now runs as root and does not chown or pre-create `/data` at build time. This avoids permission changes on the mounted volume. Re-deploy with the latest image and ensure your orchestrator mounts the volume to `/data` (default).

## Development quickstart (local and Docker)
We provide a Makefile with handy targets.

- Create venv and install deps:
  - make install
- Run locally (Python):
  - make dev-run DISCORD_TOKEN=your_token_here
- Build Docker image (defaults to ghcr.io/<youruser>/discord-bear-bot:latest):
  - make docker-build
  - Override image name: make docker-build IMAGE=myrepo/discord-bear-bot:dev
- Run container locally:
  - make docker-run DISCORD_TOKEN=your_token_here
  - Stop and remove: make docker-stop
  - Clean images: make docker-clean

You can also use raw Docker:
- docker build -t discord-bear-bot:local .
- docker run -e DISCORD_TOKEN=your_token_here -v bearbot-data:/data --name bearbot --restart unless-stopped discord-bear-bot:local

## CI/CD (GitHub Actions → GHCR)
This repository includes a workflow at .github/workflows/docker-publish.yml that:
- Builds the Docker image on PRs to main (no push)
- Builds and pushes to GitHub Container Registry ghcr.io on pushes to main and version tags (vX.Y.Z)

Tags published include branch, semantic tag, commit SHA, and latest (on the default branch). No secrets needed beyond the default GITHUB_TOKEN.

Registry URL for this repository:
- ghcr.io/rliebi/discord-bear-bot
- Package page (web UI): https://github.com/rliebi/discord-bear-bot/pkgs/container/discord-bear-bot

Common tags you will see:
- :latest (latest successful build from the default branch)
- :main (latest build from main)
- :sha-<shortsha> (commit-specific)
- :vX.Y.Z (when you push a version tag)

To pull the image:
- docker login ghcr.io -u <your_github_username> -p <a_personal_access_token_if_needed>
- docker pull ghcr.io/rliebi/discord-bear-bot:latest
- docker pull ghcr.io/rliebi/discord-bear-bot:main
- docker pull ghcr.io/rliebi/discord-bear-bot:v0.1.0  # example
- docker pull ghcr.io/rliebi/discord-bear-bot:sha-<shortsha>

## Contributing
Contributions are welcome via pull requests. Please keep changes minimal and focused. Add tests where appropriate.

## License
This project is licensed under the Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0) license. See LICENSE for details.


## Running multiple instances (important)
What happens if you run multiple containers of the same bot token at once?
- Discord will allow multiple gateway connections for the same bot token. Without coordination, each instance will receive the same interactions and likely respond twice (duplicate messages), and admin auto-assignment or settings writes can race.

To prevent this by default, this project includes a simple singleton guard:
- On startup, the bot tries to acquire an exclusive file lock at `${DATA_DIR}/bot.lock`.
- If the lock is already held (another instance is running against the same data directory), this instance exits with an error.
- This protects you from accidental duplicate instances on the same host or on any cluster setup where `/data` is a shared volume.

Advanced: allow multiple instances (not recommended unless you know why)
- Set `ALLOW_MULTI_INSTANCE=true` to bypass the lock. Only do this if you are implementing proper sharding or otherwise ensuring that only one instance will handle a given interaction.
- discord.py supports sharding, but this template does not set it up. If you need true horizontal scale, prefer a single replica per token or implement shard awareness explicitly.

Recommendations
- Docker Compose/Swarm: keep `replicas: 1` for this service.
- If you need high availability, run a supervisor to restart on failure rather than running concurrent replicas of the same token.
- If you run multiple nodes with a non-shared volume driver, the file lock will not coordinate across nodes. Use a shared volume for `/data` or keep replicas at 1 to avoid duplicates.
