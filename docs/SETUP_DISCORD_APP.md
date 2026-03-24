# How to Create and Configure a Discord Application (Bot)

Follow these steps to create your Discord bot application, get the token, set permissions, and invite it to your server.

Prerequisites:
- A Discord account with permission to manage apps at https://discord.com/developers/applications
- Permission to invite bots to your server (Manage Server)

## 1) Create the Application and Bot User
1. Go to https://discord.com/developers/applications and click "New Application".
2. Name it (e.g., "Kingshot Bear Bot") and create.
3. In the left sidebar, go to "Bot" and click "Add Bot" → "Yes, do it!".
4. (Optional) Upload an avatar and set the username.

## 2) Get the Bot Token
1. On the "Bot" page, under the Token section, click "Reset Token" or "View Token".
2. Copy the token and store it securely. You will set it as the environment variable `DISCORD_TOKEN`.
   - Never commit your token to source control.
   - If your token ever leaks, reset it immediately.

## 3) Bot Privileged Intents (Recommended Minimal)
The bot in this project only needs minimal intents.
- On the "Bot" page → Privileged Gateway Intents:
  - Message Content: OFF (not needed)
  - Server Members: OFF (not needed)
  - Presence: OFF (not needed)

## 4) OAuth2: Configure Scopes and Permissions
1. Go to "OAuth2" → "URL Generator".
2. In Scopes, select:
   - `bot`
   - `applications.commands`
3. In Bot Permissions (for `bot` scope), select minimal permissions:
   - "Send Messages"
   - "Embed Links"
   - "Read Message History"
   - (Optionally) "Use Slash Commands" if available (some UIs include this under scopes instead)
4. Copy the generated URL at the bottom.

## 5) Invite the Bot to Your Server
1. Paste the generated URL into your browser.
2. Choose the server where you have permission to add a bot.
3. Authorize.

## 6) Run the Bot Locally (Sanity Check)
- Set the environment variable and run:
  ```bash
  export DISCORD_TOKEN=your_bot_token_here
  python -m src.bot
  ```
- Or use Docker (see USAGE docs and README).

## 7) Slash Commands Visibility
- Slash commands are globally registered at startup. They may take up to ~1 minute to appear the first time.
- If you still don't see them, ensure the bot has the `applications.commands` scope in the invite, then re-invite.

## 8) Production Tips
- Run inside Docker with a persistent volume for `/data` so server settings persist across restarts.
- Keep your token in a secret store (GitHub Actions Secrets, Docker Swarm secrets, etc.).
