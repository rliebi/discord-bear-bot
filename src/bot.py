import os
import logging
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

from src.storage import get_guild_settings, update_guild_settings, set_admin_if_unset
from src.calculator import GuildConfig, compute_kingshot

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
        # Sync tree per guild on ready
        await self.tree.sync()
        logger.info("Slash commands synced")

bot = BearBot()


def to_int_1000_multiple(x: int) -> int:
    return (int(x) // 1000) * 1000


@bot.tree.command(name="calc", description="Calculate Kingshot Bear Troop Ratio for your marches")
@app_commands.describe(
    archer_total="Your total number of archers",
    march_count="How many joining marches (excluding caller march)",
    calling="Are you the rally caller?"
)
async def calc(interaction: discord.Interaction, archer_total: app_commands.Range[int, 0, 100000000], march_count: app_commands.Range[int, 1, 50], calling: bool):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    s = get_guild_settings(guild.id)
    # Ensure first user becomes admin if not set yet
    set_admin_if_unset(guild.id, interaction.user.id)
    g = GuildConfig(
        max_troop_size=int(s.get("max_troop_size", 0)),
        infantry_amount=int(s.get("infantry_amount", 0)),
        max_archers_amount=int(s.get("max_archers_amount", 0)),
    )

    # Validate server settings
    if g.max_troop_size <= 0 or g.infantry_amount < 0 or g.max_archers_amount < 0:
        await interaction.response.send_message(
            "Server settings are not configured yet. Ask an admin to run /admin settings.", ephemeral=True
        )
        return

    try:
        result = compute_kingshot(g, int(archer_total), int(march_count), bool(calling))
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        return

    # Build response embed
    embed = discord.Embed(title="Kingshot Bear Troop Ratio", color=discord.Color.green())
    embed.add_field(name="Server Settings", value=(
        f"Max Troop Size: {g.max_troop_size}\n"
        f"Infantry Amount: {g.infantry_amount}\n"
        f"Max Archers Amount: {g.max_archers_amount}"
    ), inline=False)
    embed.add_field(name="Your Input", value=(
        f"Total Archers: {archer_total}\n"
        f"March Count: {march_count}\n"
        f"Rally Caller: {'Yes' if calling else 'No'}"
    ), inline=False)

    # Joining march table
    embed.add_field(name="Joining March (per march)", value=(
        f"Archers: {result.joining_archers}\n"
        f"Infantry: {result.joining_infantry}\n"
        f"Cavalry: {result.joining_cavalry}"
    ), inline=True)

    # Calling march table
    if calling:
        embed.add_field(name="Calling March", value=(
            f"Archers: {result.calling_archers}\n"
            f"Infantry: {result.calling_infantry}\n"
            f"Cavalry: {result.calling_cavalry}"
        ), inline=True)
    else:
        embed.add_field(name="Calling March", value="N/A (not a caller)", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# Admin group
class AdminGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="admin", description="Admin configuration commands")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
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
        v = to_int_1000_multiple(value)
        s = update_guild_settings(interaction.guild.id, {"infantry_amount": v})
        await interaction.response.send_message(f"Infantry Amount set to {s['infantry_amount']} (rounded to 1000)", ephemeral=True)

    @app_commands.command(name="set-max-archers-amount", description="Set max archers cap per march")
    async def set_max_archers_amount(self, interaction: discord.Interaction, value: app_commands.Range[int, 0, 1000000]):
        v = to_int_1000_multiple(value)
        s = update_guild_settings(interaction.guild.id, {"max_archers_amount": v})
        await interaction.response.send_message(f"Max Archers Amount set to {s['max_archers_amount']} (rounded to 1000)", ephemeral=True)

    @app_commands.command(name="show-settings", description="Show current server settings")
    async def show_settings(self, interaction: discord.Interaction):
        s = get_guild_settings(interaction.guild.id)
        await interaction.response.send_message(
            f"Admin: <@{s['admin_user_id']}>\nMax Troop Size: {s['max_troop_size']}\nInfantry Amount: {s['infantry_amount']}\nMax Archers Amount: {s['max_archers_amount']}",
            ephemeral=True,
        )

    @app_commands.command(name="set-admin", description="Set or change the admin user")
    async def set_admin(self, interaction: discord.Interaction, user: discord.User):
        s = update_guild_settings(interaction.guild.id, {"admin_user_id": int(user.id)})
        await interaction.response.send_message(f"Admin set to <@{s['admin_user_id']}>", ephemeral=True)


bot.tree.add_command(AdminGroup())


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info("------")


def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN environment variable not set")
    bot.run(token)


if __name__ == "__main__":
    main()
