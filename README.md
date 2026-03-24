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

Joining march archers are rounded down to the nearest 1000. The calling march is not rounded.

## Commands
- `/calc archer_total:<int> march_count:<int> calling:<bool>`
- `/admin set-max-troop-size <int>`
- `/admin set-infantry-amount <int>`
- `/admin set-max-archers-amount <int>`
- `/admin show-settings`
- `/admin set-admin <@user>`

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
- Replies to users ephemerally for privacy.

## Troubleshooting
- Commands not visible: Ensure the bot has the application.commands scope authorized in your server, and wait up to a minute for global sync. We also force sync on startup.
- 401/unauthorized: Verify `DISCORD_TOKEN` is correct and the bot is invited to the guild.
- State not saved: Ensure the `/data` volume is mounted and writable by the container user.

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
