import os
import logging
import asyncio
import contextlib
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

from src.storage import get_guild_settings, update_guild_settings, set_admin_if_unset
from src.calculator import GuildConfig, compute_kingshot

# Allowed guilds allowlist (optional). If set, the bot only works in these guild IDs and will leave others.
_ALLOWED_GUILDS_ENV = os.environ.get("ALLOWED_GUILDS", "").strip()
if _ALLOWED_GUILDS_ENV:
    try:
        ALLOWED_GUILDS = {int(x) for x in _ALLOWED_GUILDS_ENV.split(",") if x.strip()}
    except ValueError:
        ALLOWED_GUILDS = set()
else:
    ALLOWED_GUILDS = set()

def is_guild_allowed(guild: Optional[discord.Guild]) -> bool:
    if guild is None:
        return False
    if not ALLOWED_GUILDS:
        return True  # no restrictions
    return int(guild.id) in ALLOWED_GUILDS

# Configure logging
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("bearbot")

INTENTS = discord.Intents.none()
INTENTS.guilds = True
INTENTS.message_content = False


def is_admin_check(interaction: discord.Interaction) -> bool:
    guild = interaction.guild
    if guild is None:
        return False
    settings = get_guild_settings(guild.id)
    admin_id = settings.get("admin_user_id")
    if admin_id is None:
        # First user becomes admin
        admin_id = set_admin_if_unset(guild.id, interaction.user.id)
    return int(admin_id) == int(interaction.user.id) if admin_id is not None else False


class BearBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=INTENTS)

    async def setup_hook(self):
        # Force a global sync on startup
        try:
            synced_global = await self.tree.sync()
            logger.info(f"Slash commands globally synced: {len(synced_global)} commands")
        except Exception as e:
            logger.error(f"Global command sync failed on startup: {e}")

        # Also perform a guild-scoped sync for each guild for faster availability/updates
        for g in list(self.guilds):
            if ALLOWED_GUILDS and not is_guild_allowed(g):
                # Defer leaving unauthorized guilds to the pruning step below
                continue
            try:
                sg = await self.tree.sync(guild=g)
                logger.info(f"Guild {g.id} command sync complete: {len(sg)} commands")
            except Exception as ge:
                logger.warning(f"Guild-scoped sync failed for {g.id}: {ge}")

        # Optionally prune unauthorized guilds at startup if an allowlist is set
        if ALLOWED_GUILDS:
            for g in list(self.guilds):
                if not is_guild_allowed(g):
                    logger.warning(f"Leaving unauthorized guild {g.name} ({g.id}) due to ALLOWED_GUILDS policy")
                    try:
                        await g.leave()
                    except Exception as e:
                        logger.error(f"Failed to leave guild {g.id}: {e}")

bot = BearBot()




@bot.tree.command(name="calc", description="Calculate Kingshot Bear Troop Ratio for your marches")
@app_commands.describe(
    archer_total="Your total number of archers",
    march_count="How many joining marches (excluding caller march)",
    calling="Are you the rally caller?",
    max_march_size="Optional: override for threshold (uses 90% of this instead of 120k)",
    hidden="Optional: if true, the response is visible only to you"
)
async def calc(
    interaction: discord.Interaction,
    archer_total: app_commands.Range[int, 0, 100000000],
    march_count: app_commands.Range[int, 1, 50],
    calling: bool,
    max_march_size: Optional[app_commands.Range[int, 1000, 2000000]] = None,
    hidden: Optional[bool] = False,
):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return
    if not is_guild_allowed(guild):
        await interaction.response.send_message("This bot is private and not enabled for this server.", ephemeral=True)
        return

    s = get_guild_settings(guild.id)
    # Ensure first user becomes admin if not set yet
    set_admin_if_unset(guild.id, interaction.user.id)
    g = GuildConfig(
        max_troop_size=int(s.get("max_troop_size", 0)),
        infantry_amount=int(s.get("infantry_amount", 0)),
        max_archers_amount=int(s.get("max_archers_amount", 0)),
    )
    calc_message = str(s.get("calc_message", "") or "").strip()
    ttl_minutes = int(s.get("message_ttl_minutes", 10) or 0)
    ttl_seconds = ttl_minutes * 60 if ttl_minutes > 0 else None

    # Validate server settings
    if g.max_troop_size <= 0 or g.infantry_amount < 0 or g.max_archers_amount < 0:
        await interaction.response.send_message(
            "Server settings are not configured yet. Ask an admin to run /admin settings.", ephemeral=True
        )
        return

    # Ratio mode: if TA > (MC * MAA) + extra
    # extra is 120k by default, or floor(0.9 * max_march_size) if provided by user
    extra = 120000
    if max_march_size is not None:
        extra = int(0.9 * int(max_march_size))
    threshold = (int(march_count) * int(g.max_archers_amount)) + extra
    ratio_mode = int(archer_total) > threshold

    # Build response embed
    embed = discord.Embed(title="Kingshot Bear Troop Ratio", color=discord.Color.green())
    embed.add_field(name="Server Settings", value=(
        f"Max Troop Size: {g.max_troop_size}\n"
        f"Infantry Amount: {g.infantry_amount}\n"
        f"Max Archers Amount: {g.max_archers_amount}"
    ), inline=False)
    user_input_lines = [
        f"Total Archers: {archer_total}",
        f"March Count: {march_count}",
        f"Rally Caller: {'Yes' if calling else 'No'}",
    ]
    if max_march_size is not None:
        user_input_lines.append(f"Max March Size (override): {int(max_march_size)}")
    embed.add_field(name="Your Input", value="\n".join(user_input_lines), inline=False)

    # Always compute normal results for accurate joining values (and calling when not in ratio mode)
    try:
        result = compute_kingshot(g, int(archer_total), int(march_count), bool(calling))
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        return

    # Joining march is always numeric (with 1000-floor and MAA cap per calculator)
    embed.add_field(name="Joining March (per march)", value=(
        f"Archers: {result.joining_archers}\n"
        f"Infantry: {result.joining_infantry}\n"
        f"Cavalry: {result.joining_cavalry}"
    ), inline=True)

    # Calling march: if ratio-mode AND user is caller → show 1/9/90. Otherwise show numeric result or N/A.
    if calling:
        if ratio_mode:
            embed.add_field(name="Mode", value=f"Ratio mode (caller only). Trigger: TA > MC*MAA + extra = {march_count}*{g.max_archers_amount} + {extra} = {threshold}", inline=False)
            embed.add_field(name="Calling March", value=(
                "Archers: 90%\n"
                "Infantry: 1%\n"
                "Cavalry: Rest (≈9%)"
            ), inline=True)
        else:
            embed.add_field(name="Calling March", value=(
                f"Archers: {result.calling_archers}\n"
                f"Infantry: {result.calling_infantry}\n"
                f"Cavalry: Rest"
            ), inline=True)
    else:
        embed.add_field(name="Calling March", value="N/A (not a caller)", inline=True)

    # Optional server message
    if calc_message:
        # Discord embed field value limit is 1024; truncate if necessary
        msg = calc_message if len(calc_message) <= 1024 else (calc_message[:1021] + "...")
        embed.add_field(name="Message from Admin", value=msg, inline=False)

    # If this message will be auto-deleted (non-ephemeral and TTL > 0), add a footer notice
    if (not bool(hidden)) and (ttl_minutes > 0):
        unit = "minute" if int(ttl_minutes) == 1 else "minutes"
        embed.set_footer(text=f"Auto-delete: this message will be deleted in approximately {ttl_minutes} {unit}.")

    # Send response first (without delete_after to ensure reliability on interactions)
    await interaction.response.send_message(embed=embed, ephemeral=bool(hidden))

    # Auto-delete after configured TTL (minutes) for non-ephemeral messages; 0 means do not delete
    if (not bool(hidden)) and (ttl_seconds is not None):
        try:
            # Obtain the created message object now, while the interaction context is fresh
            msg = await interaction.original_response()
        except Exception as e:
            logger.warning(f"Could not fetch original response message for auto-delete: {e}")
            msg = None

        if msg is not None:
            async def _del_later(message: discord.Message, delay: int):
                try:
                    await asyncio.sleep(delay)
                    with contextlib.suppress(Exception):
                        await message.delete()
                except Exception as e:
                    logger.warning(f"Auto-delete task error: {e}")
            try:
                asyncio.create_task(_del_later(msg, ttl_seconds))
            except RuntimeError:
                # Fallback if no running loop (shouldn't happen inside command handler)
                pass


# Admin group
class AdminGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="admin", description="Admin configuration commands")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return False
        if not is_guild_allowed(interaction.guild):
            await interaction.response.send_message("This bot is private and not enabled for this server.", ephemeral=True)
            return False
        if not is_admin_check(interaction):
            await interaction.response.send_message("Only the configured admin can use this.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="set-max-troop-size", description="Set server max troop size")
    async def set_max_troop_size(self, interaction: discord.Interaction, value: app_commands.Range[int, 1000, 1000000]):
        s = update_guild_settings(interaction.guild.id, {"max_troop_size": int(value)})
        await interaction.response.send_message(f"Max Troop Size set to {s['max_troop_size']}", ephemeral=True)

    @app_commands.command(name="set-infantry-amount", description="Set fixed infantry amount per march")
    async def set_infantry_amount(self, interaction: discord.Interaction, value: app_commands.Range[int, 0, 1000000]):
        v = int(value)
        s = update_guild_settings(interaction.guild.id, {"infantry_amount": v})
        await interaction.response.send_message(f"Infantry Amount set to {s['infantry_amount']}", ephemeral=True)

    @app_commands.command(name="set-max-archers-amount", description="Set max archers cap per march")
    async def set_max_archers_amount(self, interaction: discord.Interaction, value: app_commands.Range[int, 0, 1000000]):
        v = int(value)
        s = update_guild_settings(interaction.guild.id, {"max_archers_amount": v})
        await interaction.response.send_message(f"Max Archers Amount set to {s['max_archers_amount']}", ephemeral=True)

    @app_commands.command(name="set-calc-message", description="Set a message to include with every /calc result")
    @app_commands.describe(message="Text to show with every calculation (suggest ≤ 1000 chars)")
    async def set_calc_message(self, interaction: discord.Interaction, message: str):
        msg = (message or "").strip()
        # Discord embed field limit is 1024; allow longer but inform about truncation
        update_guild_settings(interaction.guild.id, {"calc_message": msg})
        preview = msg if len(msg) <= 140 else (msg[:137] + "...")
        note = " (will be truncated in embeds)" if len(msg) > 1024 else ""
        await interaction.response.send_message(f"Calculation message set to: {preview}{note}", ephemeral=True)

    @app_commands.command(name="clear-calc-message", description="Clear the message shown with /calc results")
    async def clear_calc_message(self, interaction: discord.Interaction):
        update_guild_settings(interaction.guild.id, {"calc_message": ""})
        await interaction.response.send_message("Calculation message cleared.", ephemeral=True)

    @app_commands.command(name="set-message-ttl-minutes", description="Set auto-delete time for /calc messages in minutes (0 disables)")
    @app_commands.describe(value="Minutes to keep messages before auto-delete; 0 = do not delete")
    async def set_message_ttl_minutes(self, interaction: discord.Interaction, value: app_commands.Range[int, 0, 10080]):
        v = int(value)
        s = update_guild_settings(interaction.guild.id, {"message_ttl_minutes": v})
        msg = "disabled (0)" if v == 0 else f"{v} minutes"
        await interaction.response.send_message(f"Message auto-delete set to {msg}.", ephemeral=True)

    @app_commands.command(name="show-settings", description="Show current server settings")
    async def show_settings(self, interaction: discord.Interaction):
        s = get_guild_settings(interaction.guild.id)
        calc_msg = (s.get("calc_message") or "").strip()
        calc_msg_status = "(not set)" if not calc_msg else ((calc_msg if len(calc_msg) <= 140 else calc_msg[:137] + "...") )
        ttl = int(s.get("message_ttl_minutes", 10) or 0)
        ttl_str = f"{ttl} min" if ttl > 0 else "disabled (0)"
        await interaction.response.send_message(
            f"Admin: <@{s['admin_user_id']}>\nMax Troop Size: {s['max_troop_size']}\nInfantry Amount: {s['infantry_amount']}\nMax Archers Amount: {s['max_archers_amount']}\nMessage TTL: {ttl_str}\nCalc Message: {calc_msg_status}",
            ephemeral=True,
        )

    @app_commands.command(name="set-admin", description="Set or change the admin user")
    async def set_admin(self, interaction: discord.Interaction, user: discord.User):
        s = update_guild_settings(interaction.guild.id, {"admin_user_id": int(user.id)})
        await interaction.response.send_message(f"Admin set to <@{s['admin_user_id']}>", ephemeral=True)

    @app_commands.command(name="resync-commands", description="Force re-sync of slash commands (use if commands look outdated)")
    async def resync_commands(self, interaction: discord.Interaction):
        # Try guild-scoped sync first for speed, then global as fallback
        try:
            await interaction.response.defer(ephemeral=True)
            synced_guild = await interaction.client.tree.sync(guild=interaction.guild)
            # Also ensure global sync happens in background
            try:
                synced_global = await interaction.client.tree.sync()
            except Exception as eg:
                logger.warning(f"Global sync failed: {eg}")
                synced_global = []
            await interaction.followup.send(
                f"Slash commands re-synced. Guild commands: {len(synced_guild)}, Global commands: {len(synced_global)}.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"Failed to sync commands: {e}", ephemeral=True)


bot.tree.add_command(AdminGroup())


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info("------")

@bot.event
async def on_guild_join(guild: discord.Guild):
    # If an allowlist is configured, automatically leave unauthorized guilds
    if ALLOWED_GUILDS and not is_guild_allowed(guild):
        logger.warning(f"Joined unauthorized guild {guild.name} ({guild.id}); leaving due to ALLOWED_GUILDS policy")
        try:
            await guild.leave()
        except Exception as e:
            logger.error(f"Failed to auto-leave guild {guild.id}: {e}")
        return

    # For allowed guilds, proactively sync commands for this guild
    try:
        sg = await bot.tree.sync(guild=guild)
        logger.info(f"Guild {guild.id} joined; command sync complete: {len(sg)} commands")
    except Exception as e:
        logger.warning(f"Guild-scoped sync on join failed for {guild.id}: {e}")


def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN environment variable not set")

    # Prevent multiple instances from running against the same data dir/token unless explicitly allowed
    allow_multi = os.environ.get("ALLOW_MULTI_INSTANCE", "false").strip().lower() in {"1", "true", "yes"}
    if not allow_multi:
        try:
            from src.singleton import setup_singleton_lock
            setup_singleton_lock()
        except Exception as e:
            raise SystemExit(f"Another instance appears to be running (singleton lock failed): {e}")

    bot.run(token)


if __name__ == "__main__":
    main()
