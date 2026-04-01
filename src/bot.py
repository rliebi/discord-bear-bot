import os
import logging
import asyncio
import contextlib
import signal
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, Any, List
from datetime import datetime, timezone, timedelta
import random

from src.storage import (
    get_guild_settings,
    update_guild_settings,
    set_admin_if_unset,
    record_usage_event,
    get_usage_summary,
    get_user_usage,
    get_all_guilds_usage,
    add_many_bear_points,
    get_bear_top,
)
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

# Track pending auto-delete tasks and their target messages so we can clean them up on shutdown
_PENDING_DELETE_TASKS: "set[asyncio.Task]" = set()
_PENDING_DELETE_TARGETS: "set[tuple[int,int]]" = set()  # (channel_id, message_id)
_shutdown_in_progress = False

async def _delete_message_by_ids(client: discord.Client, channel_id: int, message_id: int) -> None:
    chan = client.get_channel(int(channel_id))
    if chan is None:
        with contextlib.suppress(Exception):
            chan = await client.fetch_channel(int(channel_id))  # type: ignore[attr-defined]
    if hasattr(chan, "fetch_message"):
        with contextlib.suppress(Exception):
            m = await chan.fetch_message(int(message_id))  # type: ignore[assignment]
            await m.delete()

async def _shutdown_cleanup(signal_name: str) -> None:
    global _shutdown_in_progress
    if _shutdown_in_progress:
        return
    _shutdown_in_progress = True
    try:
        # Try to delete all pending targets immediately
        targets = list(_PENDING_DELETE_TARGETS)
        if targets:
            logger.info(f"Shutdown: deleting {len(targets)} pending messages before exit (signal={signal_name})")
        for chan_id, msg_id in targets:
            with contextlib.suppress(Exception):
                await _delete_message_by_ids(bot, chan_id, msg_id)
            with contextlib.suppress(Exception):
                _PENDING_DELETE_TARGETS.discard((chan_id, msg_id))
        # Cancel all pending deletion tasks
        tasks = list(_PENDING_DELETE_TASKS)
        for t in tasks:
            with contextlib.suppress(Exception):
                t.cancel()
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Shutdown: cleanup complete. Closing bot.")
        with contextlib.suppress(Exception):
            await bot.close()
    finally:
        pass

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


# --- KVK API helpers ---
_API_BASE = os.environ.get("KINGSHOT_API_BASE", "https://kingshot.net/api").rstrip("/")

async def _http_get_json(session: aiohttp.ClientSession, url: str, params: Optional[dict] = None) -> Any:
    timeout = aiohttp.ClientTimeout(total=10)
    async with session.get(url, params=params or {}, timeout=timeout) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
        return await resp.json()

async def fetch_kvk_seasons(kingdom_id: int, limit: Optional[int] = None) -> List[dict]:
    # Uses /kvk/matches and filters by kingdom_a only, as the API supports filtering only on kingdom_a.
    url = f"{_API_BASE}/kvk/matches"
    params = {
        "kingdom_a": int(kingdom_id),
        "status": "all",
        "page": 1,
    }
    if isinstance(limit, int) and limit > 0:
        params["limit"] = int(limit)
    async with aiohttp.ClientSession(headers={"Accept": "application/json"}) as session:
        data = await _http_get_json(session, url, params=params)
    # Normalize response shapes
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            return data["data"]
        if isinstance(data.get("items"), list):
            return data["items"]
        inner = data.get("data")
        if isinstance(inner, dict):
            for key in ("items", "results", "list"):
                if isinstance(inner.get(key), list):
                    return inner[key]
    # Unknown format
    raise RuntimeError("Unexpected API response format for KVK matches")


@bot.tree.command(name="calc", description="Calculate Kingshot Bear Troop Ratio for your marches")
@app_commands.describe(
    archer_total="Your total number of archers",
    march_count="How many joining marches (excluding caller march)",
    calling="Are you the rally caller?",
    override_march_archers="Optional: force joining archers to this amount (e.g. 50000)",
    total_march_size="Optional: your total march capacity (e.g. 250000). Also overrides threshold calculation.",
    hidden="Optional: if true, the response is visible only to you"
)
async def calc(
    interaction: discord.Interaction,
    archer_total: app_commands.Range[int, 0, 100000000],
    march_count: app_commands.Range[int, 1, 50],
    calling: bool,
    override_march_archers: Optional[app_commands.Range[int, 0, 2000000]] = None,
    total_march_size: Optional[app_commands.Range[int, 1000, 2000000]] = None,
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
    # extra is 120k by default, or floor(0.9 * total_march_size) if provided by user
    extra = 120000
    if total_march_size is not None:
        extra = int(0.9 * int(total_march_size))
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
    if override_march_archers is not None:
        user_input_lines.append(f"Override March Archers: {int(override_march_archers)}")
    if total_march_size is not None:
        user_input_lines.append(f"Total March Size: {int(total_march_size)}")
    embed.add_field(name="Your Input", value="\n".join(user_input_lines), inline=False)

    # Always compute normal results for accurate joining values (and calling when not in ratio mode)
    try:
        result = compute_kingshot(
            g,
            int(archer_total),
            int(march_count),
            bool(calling),
            override_march_archers=int(override_march_archers) if override_march_archers is not None else None,
            total_march_size=int(total_march_size) if total_march_size is not None else None,
        )
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

    # Record usage for admin analytics (best-effort; do not fail command if storage errors)
    try:
        record_usage_event(
            guild_id=int(guild.id),
            user_id=int(interaction.user.id),
            user_display=str(interaction.user),
            total_archers=int(archer_total),
            march_count=int(march_count),
            calling=bool(calling),
            joining_archers=int(result.joining_archers),
            calling_archers=int(result.calling_archers),
            server_id=int(guild.id),
            server_name=str(guild.name),
            server_max_troop_size=int(g.max_troop_size),
        )
    except Exception as e:
        logger.debug(f"record_usage_event failed: {e}")

    # Auto-delete after configured TTL (minutes) for non-ephemeral messages; 0 means do not delete
    if (not bool(hidden)) and (ttl_seconds is not None):
        message_id: Optional[int] = None
        channel_id: Optional[int] = None
        try:
            # Obtain the created message (Interaction original response) and capture IDs
            imsg = await interaction.original_response()
            message_id = int(imsg.id)
            if interaction.channel is not None:
                channel_id = int(interaction.channel.id)
        except Exception as e:
            logger.warning(f"Could not fetch original response message for auto-delete: {e}")

        async def _del_later_v2(delay: int, msg_id: Optional[int], chan_id: Optional[int]):
            try:
                await asyncio.sleep(delay)
                # Attempt 1: delete via webhook token (works for ~15 minutes after creation)
                with contextlib.suppress(Exception):
                    await interaction.delete_original_response()
                    return
                # Attempt 2: fetch the message via the bot token and delete it
                if msg_id is not None and chan_id is not None:
                    try:
                        await _delete_message_by_ids(interaction.client, chan_id, msg_id)
                    finally:
                        with contextlib.suppress(Exception):
                            _PENDING_DELETE_TARGETS.discard((int(chan_id), int(msg_id)))
            except Exception as e:
                logger.warning(f"Auto-delete task error: {e}")
            finally:
                # Ensure target is cleaned up even if we returned early via webhook delete
                if msg_id is not None and chan_id is not None:
                    with contextlib.suppress(Exception):
                        _PENDING_DELETE_TARGETS.discard((int(chan_id), int(msg_id)))

        # Register this message as a pending deletion target if we have both IDs
        if (message_id is not None) and (channel_id is not None):
            with contextlib.suppress(Exception):
                _PENDING_DELETE_TARGETS.add((int(channel_id), int(message_id)))
        try:
            t = asyncio.create_task(_del_later_v2(ttl_seconds, message_id, channel_id))
            with contextlib.suppress(Exception):
                _PENDING_DELETE_TASKS.add(t)
                t.add_done_callback(lambda _t: _PENDING_DELETE_TASKS.discard(_t))
        except RuntimeError:
            # Fallback if no running loop (shouldn't happen inside command handler)
            pass


@bot.tree.command(name="last", description="Show your last Kingshot calculation in this server")
@app_commands.describe(hidden="Optional: if true, the response is visible only to you")
async def last(
    interaction: discord.Interaction,
    hidden: Optional[bool] = True,
):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return
    if not is_guild_allowed(guild):
        await interaction.response.send_message("This bot is private and not enabled for this server.", ephemeral=True)
        return

    try:
        u = get_user_usage(int(guild.id), int(interaction.user.id))
    except Exception as e:
        await interaction.response.send_message(f"Failed to load last calculation: {e}", ephemeral=True)
        return

    if not u:
        await interaction.response.send_message(
            "No previous calculation found. Use /calc to create one.",
            ephemeral=bool(hidden),
        )
        return

    # Build an embed summarizing the last calculation stored for this user in this server
    title = "Your Last Kingshot Calculation"
    embed = discord.Embed(title=title, color=discord.Color.blue())

    # Timestamp and server info
    last_ts = u.get("last_use_ts", "?")
    server_name = u.get("last_server_name", str(guild.name))
    server_id = u.get("last_server_id", int(guild.id))
    mts = u.get("last_server_max_troop_size", "?")
    embed.add_field(name="Server", value=f"{server_name} ({server_id})", inline=True)
    embed.add_field(name="When (UTC)", value=str(last_ts), inline=True)
    embed.add_field(name="Max Troop Size", value=str(mts), inline=True)

    # Inputs
    ta = u.get("last_total_archers", "?")
    mc = u.get("last_march_count", "?")
    calling = bool(u.get("last_calling", False))
    embed.add_field(
        name="Input",
        value=f"Total Archers: {ta}\nMarch Count: {mc}\nRally Caller: {'Yes' if calling else 'No'}",
        inline=False,
    )

    # Outputs
    join_a = u.get("last_joining_archers", "?")
    call_a = u.get("last_calling_archers", 0)
    embed.add_field(
        name="Joining March (per march)",
        value=f"Archers: {join_a}",
        inline=True,
    )
    if calling:
        embed.add_field(
            name="Calling March",
            value=f"Archers: {call_a}",
            inline=True,
        )
    else:
        embed.add_field(name="Calling March", value="N/A (not a caller)", inline=True)

    # Respond (default to ephemeral=True unless user requests otherwise)
    await interaction.response.send_message(embed=embed, ephemeral=bool(hidden))

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
        kingdom = s.get("kingdom_id")
        kingdom_str = str(kingdom) if kingdom else "(not set)"
        await interaction.response.send_message(
            f"Admin: <@{s['admin_user_id']}>\nMax Troop Size: {s['max_troop_size']}\nInfantry Amount: {s['infantry_amount']}\nMax Archers Amount: {s['max_archers_amount']}\nMessage TTL: {ttl_str}\nKingdom: {kingdom_str}\nCalc Message: {calc_msg_status}",
            ephemeral=True,
        )

    @app_commands.command(name="usage", description="Show usage stats. Omit user to see top users.")
    @app_commands.describe(user="Optional: show details for a specific user", limit="Number of top users to list (default 20)")
    async def usage(self, interaction: discord.Interaction, user: Optional[discord.User] = None, limit: Optional[app_commands.Range[int,1,100]] = 20):
        try:
            if user is not None:
                u = get_user_usage(interaction.guild.id, int(user.id))
                if not u:
                    await interaction.response.send_message(f"No usage recorded for <@{user.id}>.", ephemeral=True)
                    return
                last_ts = u.get("last_use_ts", "?")
                msg = (
                    f"User: <@{user.id}> ({u.get('user_display','')})\n"
                    f"Uses: {u.get('count',0)}\n"
                    f"Last Use (UTC): {last_ts}\n"
                    f"Last Input: archers={u.get('last_total_archers','?')}, marches={u.get('last_march_count','?')}, calling={u.get('last_calling', False)}\n"
                    f"Last Output: joining_archers={u.get('last_joining_archers','?')}, calling_archers={u.get('last_calling_archers','?')}\n"
                    f"Server: {u.get('last_server_name','?')} ({u.get('last_server_id','?')}) | Max Troop Size: {u.get('last_server_max_troop_size','?')}"
                )
                await interaction.response.send_message(msg, ephemeral=True)
                return

            # Summary view
            lim = int(limit) if limit is not None else 20
            items = get_usage_summary(interaction.guild.id, lim)
            if not items:
                await interaction.response.send_message("No usage recorded yet.", ephemeral=True)
                return
            lines = []
            for idx, (uid, info) in enumerate(items, start=1):
                disp = info.get("user_display") or f"<@{uid}>"
                count = int(info.get("count", 0))
                last_ts = info.get("last_use_ts", "?")
                # Show last archers or joining archers as a quick reference
                last_arch = info.get("last_total_archers", "?")
                last_join = info.get("last_joining_archers", "?")
                lines.append(f"{idx}. <@{uid}> ({disp}): {count} uses | last UTC: {last_ts} | TA: {last_arch} | joinA: {last_join}")
            # Discord message length limit safeguards
            out = "\n".join(lines)
            if len(out) > 1800:
                out = out[:1797] + "..."
            await interaction.response.send_message(out, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Failed to fetch usage: {e}", ephemeral=True)

    @app_commands.command(name="usage-all-servers", description="Show top usage for all servers (this bot across all guilds)")
    @app_commands.describe(limit_per_guild="Number of top users per server to list (default 5)")
    async def usage_all_servers(self, interaction: discord.Interaction, limit_per_guild: Optional[app_commands.Range[int,1,50]] = 5):
        try:
            lim = int(limit_per_guild) if limit_per_guild is not None else 5
            all_usage = get_all_guilds_usage(lim)
            if not all_usage:
                await interaction.response.send_message("No usage recorded in any server yet.", ephemeral=True)
                return
            lines = []
            client = interaction.client
            for gid, items in all_usage.items():
                # Resolve guild name
                gobj = client.get_guild(int(gid)) if hasattr(client, "get_guild") else None
                gname = gobj.name if gobj is not None else None
                if not gname:
                    # Fallback to last_server_name from first user entry if present
                    if items:
                        gname = items[0][1].get("last_server_name") or str(gid)
                    else:
                        gname = str(gid)
                lines.append(f"Server: {gname} ({gid})")
                if not items:
                    lines.append("  - no usage")
                    continue
                for idx, (uid, info) in enumerate(items, start=1):
                    disp = info.get("user_display") or f"<@{uid}>"
                    count = int(info.get("count", 0))
                    last_ts = info.get("last_use_ts", "?")
                    last_arch = info.get("last_total_archers", "?")
                    last_join = info.get("last_joining_archers", "?")
                    lines.append(f"  {idx}. <@{uid}> ({disp}): {count} uses | last UTC: {last_ts} | TA: {last_arch} | joinA: {last_join}")
            out = "\n".join(lines)
            if len(out) > 1800:
                out = out[:1797] + "..."
            await interaction.response.send_message(out, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Failed to fetch all-servers usage: {e}", ephemeral=True)

    @app_commands.command(name="set-kingdom", description="Set the default Kingdom ID used for /kvk commands in this server")
    async def set_kingdom(self, interaction: discord.Interaction, kingdom: app_commands.Range[int, 1, 100000]):
        s = update_guild_settings(interaction.guild.id, {"kingdom_id": int(kingdom)})
        await interaction.response.send_message(f"Default Kingdom set to {s.get('kingdom_id')}", ephemeral=True)

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


class KvkGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="kvk", description="Kingshot KVK utilities")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return False
        if not is_guild_allowed(interaction.guild):
            await interaction.response.send_message("This bot is private and not enabled for this server.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="seasons", description="Show an overview of past KVK seasons for a kingdom")
    @app_commands.describe(
        kingdom="Optional kingdom ID; if omitted, uses the server's default",
        limit="Number of seasons to show (default 10, max 25)",
        hidden="If true, only you can see the response"
    )
    async def seasons(
        self,
        interaction: discord.Interaction,
        kingdom: Optional[app_commands.Range[int,1,100000]] = None,
        limit: Optional[app_commands.Range[int,1,25]] = 10,
        hidden: Optional[bool] = True,
    ):
        gid = interaction.guild.id if interaction.guild else 0
        s = get_guild_settings(gid) if gid else {}
        k = int(kingdom) if kingdom is not None else int(s.get("kingdom_id") or 0)
        if k <= 0:
            await interaction.response.send_message(
                "No kingdom specified and no default set. Use /admin set-kingdom or pass the kingdom parameter.",
                ephemeral=True,
            )
            return
        lim = int(limit) if limit is not None else 10
        try:
            await interaction.response.defer(ephemeral=bool(hidden))
            seasons = await fetch_kvk_seasons(k, lim)
        except Exception as e:
            await interaction.followup.send(f"Failed to fetch KVK seasons for kingdom {k}: {e}", ephemeral=True)
            return
        if not seasons:
            await interaction.followup.send(f"No KVK seasons found for kingdom {k}.", ephemeral=True)
            return

        # Optional: sort by most recent first using season_date (fallback to season_id)
        try:
            seasons_sorted = sorted(
                seasons,
                key=lambda x: (x.get("season_date") or str(x.get("season_id") or "")),
                reverse=True,
            )
        except Exception:
            seasons_sorted = seasons

        items = seasons_sorted[:lim]

        # Compute overall record from the perspective of the requested kingdom
        wins = losses = draws = unknown = 0
        outcomes: List[str] = []  # per-item outcome label
        for it in items:
            ka = it.get("kingdom_a")
            kb = it.get("kingdom_b")
            cw = it.get("castle_winner")
            pw = it.get("prep_winner")
            winner = cw if cw is not None else pw
            if winner is None:
                # Try infer from castle_captured with attacker/defender if provided
                if it.get("castle_captured") is True:
                    winner = it.get("attacker") or it.get("castle_winner") or it.get("prep_winner")
            # Determine outcome for kingdom k
            if winner is None or (ka is None and kb is None):
                unknown += 1
                outcomes.append("?")
            else:
                if int(winner) == int(k):
                    wins += 1
                    outcomes.append("W")
                else:
                    # If the kingdom participated and wasn't the winner, count as loss; if not present, unknown
                    if int(k) in {int(ka) if ka is not None else -1, int(kb) if kb is not None else -2}:
                        losses += 1
                        outcomes.append("L")
                    else:
                        unknown += 1
                        outcomes.append("?")

        # Choose an embed color based on record
        color = discord.Color.green() if wins > losses else (discord.Color.red() if losses > wins else discord.Color.orange())
        embed = discord.Embed(title=f"KVK Matches for Kingdom {k}", color=color)
        embed.description = f"Record: {wins}-{losses}" + (f"-{draws}" if draws else "") + (f"  |  Unknown: {unknown}" if unknown else "")

        for idx, it in enumerate(items, start=1):
            title = (
                str(it.get("kvk_title")
                    or (f"KvK #{it.get('season_id')}" if it.get('season_id') is not None else None)
                    or f"KVK {it.get('kvk_id') or idx}")
            )

            date = it.get("season_date") or it.get("created_at") or it.get("updated_at") or "?"
            ka = it.get('kingdom_a')
            kb = it.get('kingdom_b')
            kingdoms = f"{ka if ka is not None else '?'} vs {kb if kb is not None else '?'}"

            cw = it.get("castle_winner")
            pw = it.get("prep_winner")
            winner = cw if cw is not None else pw
            # Loser = the other side if winner present
            loser = None
            if winner is not None and (ka is not None or kb is not None):
                if int(winner) == int(ka if ka is not None else -1):
                    loser = kb
                elif int(winner) == int(kb if kb is not None else -1):
                    loser = ka

            # Perspective outcome for k
            outcome = outcomes[idx-1] if idx-1 < len(outcomes) else "?"
            emoji = "🏆" if outcome == "W" else ("❌" if outcome == "L" else "❔")
            outcome_str = f"{emoji} {'Win' if outcome=='W' else ('Loss' if outcome=='L' else 'Unknown')} for {k}"

            bits = []
            if winner is not None:
                bits.append(f"Winner: **{winner}**")
                if loser is not None:
                    bits.append(f"Loser: **{loser}**")
            if it.get("attacker") is not None or it.get("defender") is not None:
                bits.append(f"⚔️ Attacker: {it.get('attacker','?')} | 🛡️ Defender: {it.get('defender','?')}")
            if "castle_captured" in it:
                bits.append(f"🏰 Castle captured: {'✅' if it['castle_captured'] else '❌'}")

            desc = it.get("description")

            value_lines = [
                f"Match: **{kingdoms}**",
                f"Date: {date}",
                outcome_str,
            ]
            if bits:
                value_lines.append(" | ".join(bits))
            if desc:
                value_lines.append(f"Note: {desc}")

            field_name = f"{idx}. {title}"
            embed.add_field(name=field_name, value="\n".join(value_lines)[:1024], inline=False)

        await interaction.followup.send(embed=embed, ephemeral=bool(hidden))


bot.tree.add_command(KvkGroup())


# --- Bear Mini-Game ---
# Event: lasts 30 minutes. Once per day per guild. Users can launch 5-minute rallies and others can join.
# Each user can participate in up to 6 rallies per event (launch counts as 1). Points are random for now.

from typing import Dict, Set, Tuple

class _BearEventState:
    def __init__(self, guild_id: int, channel_id: int, start: datetime, end: datetime):
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.start = start
        self.end = end
        self.rallies: Dict[int, dict] = {}
        self.next_rally_id: int = 1
        self.user_points: Dict[int, int] = {}
        self.user_joins: Dict[int, int] = {}
        self.lock = asyncio.Lock()
        self.end_task: Optional[asyncio.Task] = None
        # Dashboard message ("start window") that tracks open rallies and contains Join buttons
        self.dashboard_channel_id: Optional[int] = int(channel_id) if channel_id else None
        self.dashboard_message_id: Optional[int] = None

    def time_left_seconds(self) -> int:
        now = datetime.now(timezone.utc)
        return max(0, int((self.end - now).total_seconds()))

_BEAR_EVENTS: Dict[int, _BearEventState] = {}


def _fmt_duration(sec: int) -> str:
    m, s = divmod(max(0, int(sec)), 60)
    return f"{m}m {s}s"

async def _announce(channel: Optional[discord.abc.Messageable], content: Optional[str] = None, embed: Optional[discord.Embed] = None):
    if channel is None:
        return
    with contextlib.suppress(Exception):
        await channel.send(content=content, embed=embed)

async def _build_dashboard_embed(ev: "_BearEventState") -> discord.Embed:
    now = datetime.now(timezone.utc)
    left = ev.time_left_seconds()
    embed = discord.Embed(title="🐻 Bear Event", color=discord.Color.gold())
    embed.description = (
        "Launch 5-minute rallies to attack the bear!\n"
        "You can participate in up to 6 rallies at once.\n"
        "Use the buttons to launch or join rallies."
    )
    embed.add_field(name="Time Remaining", value=_fmt_duration(left), inline=True)
    # List active rallies with basic info
    lines = []
    active = []
    async with ev.lock:
        for rid, r in sorted(ev.rallies.items()):
            if r.get("done"):
                continue
            if r.get("end") and r.get("end") <= now:
                continue
            active.append((rid, r))
    for rid, r in active:
        rleft = max(0, int((r.get("end") - now).total_seconds())) if r.get("end") else 0
        lines.append(f"ID {rid}: {r.get('title')} — {len(r.get('participants') or [])} joined — ends in {_fmt_duration(rleft)}")
    embed.add_field(name="Open Rallies", value=("\n".join(lines) if lines else "None"), inline=False)
    return embed

class _DashboardView(discord.ui.View):
    def __init__(self, guild_id: int, rally_ids: List[int], timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.guild_id = int(guild_id)
        # Add Launch button
        self.add_item(discord.ui.Button(label="Launch Rally", style=discord.ButtonStyle.primary, emoji="🚩"))
        # Wire handler for the first item (Launch)
        self.children[0].callback = self._launch_clicked  # type: ignore[assignment]
        # Add Join buttons for rallies (cap to 20 to stay under 25 component limit with 1 launch + potential extras)
        for rid in rally_ids[:20]:
            btn = discord.ui.Button(label=f"Join #{rid}", style=discord.ButtonStyle.success, emoji="➕")
            # bind callback with closure
            async def _on_join(interaction: discord.Interaction, rid_local=int(rid)):
                await self._handle_join(interaction, rid_local)
            btn.callback = _on_join  # type: ignore[assignment]
            self.add_item(btn)

    async def _launch_clicked(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None or int(guild.id) != self.guild_id:
            await interaction.response.send_message("This control is not valid here.", ephemeral=True)
            return
        # Defer quickly to avoid timeout
        with contextlib.suppress(Exception):
            await interaction.response.defer(ephemeral=True)
        ev = _BEAR_EVENTS.get(int(guild.id))
        if ev is None or ev.time_left_seconds() <= 0:
            await interaction.followup.send("No active Bear event.", ephemeral=True)
            return
        # Create rally with helper
        res = await _create_rally(int(guild.id), int(interaction.user.id), interaction.channel)  # type: ignore[arg-type]
        if isinstance(res, str):
            await interaction.followup.send(res, ephemeral=True)
        else:
            rid = res
            await interaction.followup.send(f"Rally #{rid} launched! Others can now join.", ephemeral=True)
            await _update_event_dashboard(int(guild.id))

    async def _handle_join(self, interaction: discord.Interaction, rally_id: int):
        guild = interaction.guild
        # Acknowledge
        with contextlib.suppress(Exception):
            await interaction.response.defer(ephemeral=True)
        if guild is None or int(guild.id) != self.guild_id:
            await interaction.followup.send("This control is not valid here.", ephemeral=True)
            return
        ev = _BEAR_EVENTS.get(int(guild.id))
        if ev is None or ev.time_left_seconds() <= 0:
            await interaction.followup.send("No active Bear event.", ephemeral=True)
            return
        uid = int(interaction.user.id)
        msg_text = None
        async with ev.lock:
            if int(ev.user_joins.get(uid, 0)) >= 6:
                msg_text = "You have reached the limit of 6 concurrent rallies. Wait for one to finish, then try again."
            else:
                r = ev.rallies.get(int(rally_id))
                if not r:
                    msg_text = f"Rally {int(rally_id)} not found."
                elif r.get("done"):
                    msg_text = f"Rally {int(rally_id)} has already finished."
                elif datetime.now(timezone.utc) >= r.get("end"):
                    msg_text = f"Rally {int(rally_id)} is no longer rallying."
                else:
                    parts = r.get("participants")
                    if uid in parts:
                        msg_text = "You are already in this rally."
                    else:
                        parts.add(uid)
                        ev.user_joins[uid] = int(ev.user_joins.get(uid, 0)) + 1
                        left = max(0, 6 - int(ev.user_joins.get(uid, 0)))
                        msg_text = f"Joined rally {int(rally_id)}! You can still join {left} more rally(ies) at once."
        await interaction.followup.send(msg_text, ephemeral=True)
        # Refresh dashboard to update counts
        await _update_event_dashboard(int(guild.id))

async def _update_event_dashboard(guild_id: int) -> None:
    ev = _BEAR_EVENTS.get(int(guild_id))
    if ev is None:
        return
    channel = bot.get_channel(ev.dashboard_channel_id) if ev.dashboard_channel_id else None
    if channel is None:
        return
    # Gather active rally ids
    now = datetime.now(timezone.utc)
    active_ids: List[int] = []
    async with ev.lock:
        for rid, r in sorted(ev.rallies.items()):
            if r.get("done"):
                continue
            if r.get("end") and r.get("end") <= now:
                continue
            active_ids.append(int(rid))
    embed = await _build_dashboard_embed(ev)
    timeout = max(1, ev.time_left_seconds())
    view = _DashboardView(guild_id=int(guild_id), rally_ids=active_ids, timeout=timeout)
    # Send or edit existing dashboard message
    try:
        if ev.dashboard_message_id is None:
            m = await channel.send(embed=embed, view=view)
            async with ev.lock:
                ev.dashboard_message_id = int(m.id)
        else:
            # Edit existing
            ch = channel
            if hasattr(ch, "fetch_message"):
                m = await ch.fetch_message(int(ev.dashboard_message_id))  # type: ignore[assignment]
                with contextlib.suppress(Exception):
                    await m.edit(embed=embed, view=view)
    except Exception as e:
        logger.debug(f"Failed to update dashboard: {e}")

async def _create_rally(guild_id: int, caller_id: int, channel: Optional[discord.abc.Messageable]) -> Any:
    """Create a rally for caller. Returns rally_id on success, or error string on failure.
    Enforces: user concurrent join cap and only one active hosted rally at a time.
    """
    ev = _BEAR_EVENTS.get(int(guild_id))
    if ev is None or ev.time_left_seconds() <= 0:
        return "No active Bear event."
    now = datetime.now(timezone.utc)
    # Create under lock
    async with ev.lock:
        # Concurrent joins cap applies to launching, too
        if int(ev.user_joins.get(int(caller_id), 0)) >= 6:
            return "You have reached the limit of 6 concurrent rallies. Wait for one to finish, then try again."
        # Only one active hosted rally at a time
        for r in ev.rallies.values():
            try:
                if int(r.get("caller_id")) == int(caller_id) and (not r.get("done")) and (r.get("end") and r.get("end") > now):
                    return "You can only host one rally at a time. Wait for your current rally to land."
            except Exception:
                pass
        rid = ev.next_rally_id
        ev.next_rally_id += 1
        rally_end = now + timedelta(minutes=5)
        entry = {
            "id": int(rid),
            "title": f"Rally #{rid}",
            "caller_id": int(caller_id),
            "start": now,
            "end": rally_end,
            "participants": set([int(caller_id)]),
            "done": False,
            "task": None,
            "message_id": None,
            "channel_id": int(getattr(channel, 'id', 0)) if channel else None,
        }
        ev.rallies[int(rid)] = entry
        ev.user_joins[int(caller_id)] = int(ev.user_joins.get(int(caller_id), 0)) + 1

    async def _finish_rally(gid: int, rally_id: int, rally_end_dt: datetime, default_channel: Optional[discord.abc.Messageable]):
        try:
            await asyncio.sleep(max(0, int((rally_end_dt - datetime.now(timezone.utc)).total_seconds())))
            ev2 = _BEAR_EVENTS.get(int(gid))
            if ev2 is None:
                return
            await _finalize_rally_and_announce(int(gid), ev2, int(rally_id), default_channel)
        except Exception as e:
            logger.debug(f"_finish_rally (helper) error: {e}")
    try:
        t = asyncio.create_task(_finish_rally(int(guild_id), int(rid), rally_end, channel))
        with contextlib.suppress(Exception):
            # Store task under lock
            async with ev.lock:
                ev.rallies[int(rid)]["task"] = t
            _PENDING_DELETE_TASKS.add(t)
            t.add_done_callback(lambda _t: _PENDING_DELETE_TASKS.discard(_t))
    except RuntimeError:
        pass
    return int(rid)

async def _finalize_rally_and_announce(gid: int, ev: "_BearEventState", rally_id: int, default_channel: Optional[discord.abc.Messageable] = None) -> None:
    """Finalize a rally, assign points, update persistent leaderboard, and announce a per-rally leaderboard."""
    # Collect data under lock and assign random points
    points_map: dict[int, int] = {}
    caller_id: Optional[int] = None
    title: str = f"Rally #{rally_id}"
    parts_list: list[int] = []
    start_dt: Optional[datetime] = None
    end_dt: Optional[datetime] = None
    async with ev.lock:
        r = ev.rallies.get(int(rally_id))
        if not r or r.get("done"):
            return
        r["done"] = True
        caller_id = r.get("caller_id")
        title = r.get("title") or title
        start_dt = r.get("start")
        end_dt = r.get("end")
        participants = set(r.get("participants") or [])
        parts_list = list(participants)
        for pid in participants:
            pts = random.randint(8, 15) if pid == caller_id else random.randint(5, 12)
            points_map[int(pid)] = pts
            ev.user_points[int(pid)] = int(ev.user_points.get(int(pid), 0)) + int(pts)
            cur = int(ev.user_joins.get(int(pid), 0))
            if cur > 0:
                ev.user_joins[int(pid)] = cur - 1
    # Update eternal leaderboard (best-effort)
    with contextlib.suppress(Exception):
        if points_map:
            add_many_bear_points(int(gid), points_map)
    # Build announcement embed with per-rally leaderboard
    ch = bot.get_channel(ev.channel_id) if ev and ev.channel_id else default_channel
    # Sort by points desc
    sorted_pts = sorted(points_map.items(), key=lambda kv: kv[1], reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, (uid, pts) in enumerate(sorted_pts, start=1):
        prefix = medals[idx-1] if idx <= 3 else f"{idx}."
        lines.append(f"{prefix} <@{uid}> — {pts} pts")
    # Include small summary
    color = discord.Color.green()
    embed = discord.Embed(title=f"🏁 Rally Landed (ID {rally_id})", color=color)
    embed.add_field(name="Title", value=str(title), inline=False)
    if caller_id:
        embed.add_field(name="Caller", value=f"<@{caller_id}>", inline=True)
    participants_count = len(parts_list)
    embed.add_field(name="Participants", value=str(participants_count), inline=True)
    # Duration info
    try:
        if start_dt and end_dt:
            dur = int((end_dt - start_dt).total_seconds())
            embed.add_field(name="Rally Duration", value=_fmt_duration(dur), inline=True)
    except Exception:
        pass
    if lines:
        # Discord field value limit guard
        value = "\n".join(lines)
        if len(value) > 1024:
            value = value[:1021] + "..."
        embed.add_field(name="Rally Leaderboard", value=value, inline=False)
    # Also show current top 5 of this event as context
    try:
        tops_event = sorted(ev.user_points.items(), key=lambda kv: kv[1], reverse=True)[:5]
        if tops_event:
            ev_lines = [f"{i+1}. <@{uid}> — {pts} pts" for i, (uid, pts) in enumerate(tops_event)]
            embed.add_field(name="Event Top 5 (so far)", value="\n".join(ev_lines)[:1024], inline=False)
    except Exception:
        pass
    await _announce(ch, embed=embed)
    # Refresh dashboard to remove the finished rally
    with contextlib.suppress(Exception):
        await _update_event_dashboard(int(gid))

class LaunchRallyView(discord.ui.View):
    def __init__(self, guild_id: int, timeout: Optional[float] = None):
        # Legacy view (kept for compatibility); dashboard view is preferred now.
        super().__init__(timeout=timeout)
        self.guild_id = int(guild_id)

    @discord.ui.button(label="Launch Rally", style=discord.ButtonStyle.primary, emoji="🚩")
    async def launch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None or int(guild.id) != self.guild_id:
            await interaction.response.send_message("This button is not valid here.", ephemeral=True)
            return
        with contextlib.suppress(Exception):
            await interaction.response.defer(ephemeral=True)
        res = await _create_rally(int(guild.id), int(interaction.user.id), interaction.channel)  # type: ignore[arg-type]
        if isinstance(res, str):
            await interaction.followup.send(res, ephemeral=True)
        else:
            await interaction.followup.send(f"Rally #{res} launched!", ephemeral=True)
            await _update_event_dashboard(int(guild.id))


class JoinRallyView(discord.ui.View):
    def __init__(self, guild_id: int, rally_id: int, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.guild_id = int(guild_id)
        self.rally_id = int(rally_id)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, emoji="➕")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        # Immediately acknowledge to avoid interaction timeout causing "This interaction failed"
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            # If already responded for any reason, continue with followup
            pass
        if guild is None or int(guild.id) != self.guild_id:
            await interaction.followup.send("This button is not valid here.", ephemeral=True)
            return
        ev = _BEAR_EVENTS.get(int(guild.id))
        if ev is None or ev.time_left_seconds() <= 0:
            await interaction.followup.send("No active Bear event.", ephemeral=True)
            return
        uid = int(interaction.user.id)
        # Decide message under lock
        msg_text = None
        async with ev.lock:
            if int(ev.user_joins.get(uid, 0)) >= 6:
                msg_text = "You have reached the limit of 6 concurrent rallies. Wait for one to finish, then try again."
            else:
                r = ev.rallies.get(int(self.rally_id))
                if not r:
                    msg_text = f"Rally {int(self.rally_id)} not found."
                elif r.get("done"):
                    msg_text = f"Rally {int(self.rally_id)} has already finished."
                elif datetime.now(timezone.utc) >= r.get("end"):
                    msg_text = f"Rally {int(self.rally_id)} is no longer rallying."
                else:
                    parts = r.get("participants")
                    if uid in parts:
                        msg_text = "You are already in this rally."
                    else:
                        parts.add(uid)
                        ev.user_joins[uid] = int(ev.user_joins.get(uid, 0)) + 1
                        left = max(0, 6 - int(ev.user_joins.get(uid, 0)))
                        msg_text = f"Joined rally {int(self.rally_id)}! You can still join {left} more rally(ies) at once."
        await interaction.followup.send(msg_text, ephemeral=True)


class BearGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="bear", description="Bear mini-game: daily 30-min rally event")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return False
        if not is_guild_allowed(interaction.guild):
            await interaction.response.send_message("This bot is private and not enabled for this server.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="start", description="Start today's 30-minute Bear event (admin only; once per day)")
    async def start(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        if not is_admin_check(interaction):
            await interaction.response.send_message("Only the configured admin can start the event.", ephemeral=True)
            return
        s = get_guild_settings(guild.id)
        today = datetime.now(timezone.utc).date().isoformat()
        last = (s.get("bear_event_last_start") or "").strip()
        if last == today:
            await interaction.response.send_message("Today's Bear event has already been started. Try again tomorrow.", ephemeral=True)
            return
        # Prevent duplicate events if one is active
        if _BEAR_EVENTS.get(int(guild.id)) is not None and _BEAR_EVENTS[int(guild.id)].time_left_seconds() > 0:
            await interaction.response.send_message("An event is already active in this server.", ephemeral=True)
            return
        start_ts = datetime.now(timezone.utc)
        end_ts = start_ts + timedelta(minutes=30)
        chan = interaction.channel
        st = _BearEventState(guild.id, int(chan.id) if chan else 0, start_ts, end_ts)
        _BEAR_EVENTS[int(guild.id)] = st
        update_guild_settings(guild.id, {"bear_event_last_start": today})

        # Post the dashboard (start window) that tracks open rallies and has Join buttons
        dash_embed = await _build_dashboard_embed(st)
        dash_view = _DashboardView(guild_id=int(guild.id), rally_ids=[], timeout=max(1, int((end_ts - start_ts).total_seconds())))
        await interaction.response.send_message(embed=dash_embed, view=dash_view)
        try:
            imsg = await interaction.original_response()
            async with st.lock:
                st.dashboard_message_id = int(imsg.id)
        except Exception:
            pass

        async def _end_event_later(gid: int, until: datetime):
            try:
                await asyncio.sleep(max(0, int((until - datetime.now(timezone.utc)).total_seconds())))
                ev = _BEAR_EVENTS.get(int(gid))
                if ev is None:
                    return
                # Mark as ended; do not delete immediately to allow late status/leaderboard
                end_embed = discord.Embed(title="🐻 Bear Event Ended", color=discord.Color.dark_grey())
                # Build quick leaderboard top 10
                tops = sorted(ev.user_points.items(), key=lambda kv: kv[1], reverse=True)[:10]
                if tops:
                    lines = [f"{idx+1}. <@{uid}> — {pts} pts" for idx, (uid, pts) in enumerate(tops)]
                    end_embed.add_field(name="Top Players", value="\n".join(lines), inline=False)
                else:
                    end_embed.description = (end_embed.description or "") + "\nNo points scored."
                ch = bot.get_channel(ev.channel_id) if ev.channel_id else interaction.channel
                await _announce(ch, embed=end_embed)
            finally:
                pass
        try:
            t = asyncio.create_task(_end_event_later(int(guild.id), end_ts))
            st.end_task = t
            with contextlib.suppress(Exception):
                _PENDING_DELETE_TASKS.add(t)
                t.add_done_callback(lambda _t: _PENDING_DELETE_TASKS.discard(_t))
        except RuntimeError:
            pass

    @app_commands.command(name="reset", description="Admin: reset the Bear event now (cancels timers and clears daily cooldown)")
    async def reset(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        if not is_admin_check(interaction):
            await interaction.response.send_message("Only the configured admin can reset the event.", ephemeral=True)
            return
        gid = int(guild.id)
        ev = _BEAR_EVENTS.get(gid)
        # Cancel running tasks safely
        if ev is not None:
            with contextlib.suppress(Exception):
                if ev.end_task:
                    ev.end_task.cancel()
            with contextlib.suppress(Exception):
                if isinstance(ev.rallies, dict):
                    for r in list(ev.rallies.values()):
                        t = r.get("task") if isinstance(r, dict) else None
                        if t:
                            with contextlib.suppress(Exception):
                                t.cancel()
            with contextlib.suppress(Exception):
                del _BEAR_EVENTS[gid]
        # Clear daily cooldown to allow restart
        with contextlib.suppress(Exception):
            update_guild_settings(gid, {"bear_event_last_start": ""})
        # Notify
        embed = discord.Embed(title="♻️ Bear Event Reset", color=discord.Color.red())
        embed.description = "The current event (if any) was reset. You can /bear start again now."
        ch = interaction.channel
        if ch is not None:
            await _announce(ch, embed=embed)
        await interaction.response.send_message("Event reset complete.", ephemeral=True)

    @app_commands.command(name="abort", description="Admin: abort an active rally by ID (no points awarded)")
    async def abort(self, interaction: discord.Interaction, rally_id: app_commands.Range[int,1,1000000]):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        if not is_admin_check(interaction):
            await interaction.response.send_message("Only the configured admin can abort a rally.", ephemeral=True)
            return
        ev = _BEAR_EVENTS.get(int(guild.id))
        if ev is None or ev.time_left_seconds() <= 0:
            await interaction.response.send_message("No active Bear event.", ephemeral=True)
            return
        # Find and mark rally as done, cancel its task
        ch_id = None
        msg_id = None
        caller_id = None
        parts_count = 0
        async with ev.lock:
            r = ev.rallies.get(int(rally_id))
            if not r:
                await interaction.response.send_message(f"Rally {int(rally_id)} not found.", ephemeral=True)
                return
            if r.get("done"):
                await interaction.response.send_message(f"Rally {int(rally_id)} has already finished.", ephemeral=True)
                return
            r["done"] = True
            t = r.get("task")
            if t:
                with contextlib.suppress(Exception):
                    t.cancel()
            ch_id = r.get("channel_id")
            msg_id = r.get("message_id")
            caller_id = r.get("caller_id")
            parts = r.get("participants") or set()
            # Decrement active rally counts for all participants since this rally is aborted
            for pid in list(parts):
                try:
                    cur = int(ev.user_joins.get(pid, 0))
                    if cur > 0:
                        ev.user_joins[pid] = cur - 1
                except Exception:
                    pass
            parts_count = len(parts)
        # Try to remove the Join button from the original message if available
        if ch_id and msg_id:
            try:
                chan = interaction.client.get_channel(int(ch_id))
                if chan and hasattr(chan, "fetch_message"):
                    m = await chan.fetch_message(int(msg_id))  # type: ignore[assignment]
                    with contextlib.suppress(Exception):
                        await m.edit(view=None)
            except Exception as e:
                logger.debug(f"Failed to edit rally message on abort: {e}")
        # Announce the abort
        ch = bot.get_channel(ev.channel_id) if ev and ev.channel_id else interaction.channel
        embed = discord.Embed(title=f"🛑 Rally Aborted (ID {int(rally_id)})", color=discord.Color.red())
        if caller_id:
            embed.add_field(name="Caller", value=f"<@{caller_id}>", inline=True)
        embed.add_field(name="Participants", value=str(parts_count), inline=True)
        embed.set_footer(text="No points were awarded.")
        await _announce(ch, embed=embed)
        await interaction.response.send_message(f"Rally {int(rally_id)} aborted.", ephemeral=True)
        # Refresh dashboard to remove aborted rally from open list
        with contextlib.suppress(Exception):
            await _update_event_dashboard(int(guild.id))

    @app_commands.command(name="launch", description="Launch a 5-minute rally to the bear (counts toward your 6 concurrent joins)")
    @app_commands.describe(title="Optional short title for your rally (ignored for now; rallies are auto-titled)")
    async def launch(self, interaction: discord.Interaction, title: Optional[str] = None):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        ev = _BEAR_EVENTS.get(int(guild.id))
        if ev is None or ev.time_left_seconds() <= 0:
            await interaction.response.send_message("No active Bear event. Ask an admin to /bear start.", ephemeral=True)
            return
        # Use common helper to enforce caps and create rally; do not post a separate rally message.
        with contextlib.suppress(Exception):
            await interaction.response.defer(ephemeral=True)
        res = await _create_rally(int(guild.id), int(interaction.user.id), interaction.channel)  # type: ignore[arg-type]
        if isinstance(res, str):
            await interaction.followup.send(res, ephemeral=True)
            return
        rid = int(res)
        await interaction.followup.send(f"Rally #{rid} launched! Others can now join from the event window.", ephemeral=True)
        await _update_event_dashboard(int(guild.id))

    @app_commands.command(name="join", description="Join an active rally by ID (max 6 concurrent rallies)")
    async def join(self, interaction: discord.Interaction, rally_id: app_commands.Range[int,1,1000000]):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        ev = _BEAR_EVENTS.get(int(guild.id))
        if ev is None or ev.time_left_seconds() <= 0:
            await interaction.response.send_message("No active Bear event.", ephemeral=True)
            return
        uid = int(interaction.user.id)
        async with ev.lock:
            if int(ev.user_joins.get(uid, 0)) >= 6:
                await interaction.response.send_message("You have reached the limit of 6 concurrent rallies. Wait for one to finish, then try again.", ephemeral=True)
                return
            r = ev.rallies.get(int(rally_id))
            if not r:
                await interaction.response.send_message(f"Rally {int(rally_id)} not found.", ephemeral=True)
                return
            if r.get("done"):
                await interaction.response.send_message(f"Rally {int(rally_id)} has already finished.", ephemeral=True)
                return
            # Check rally still rallying
            if datetime.now(timezone.utc) >= r.get("end"):
                await interaction.response.send_message(f"Rally {int(rally_id)} is no longer rallying.", ephemeral=True)
                return
            parts: Set[int] = r.get("participants")
            if uid in parts:
                await interaction.response.send_message("You are already in this rally.", ephemeral=True)
                return
            parts.add(uid)
            ev.user_joins[uid] = int(ev.user_joins.get(uid, 0)) + 1
            left = max(0, 6 - int(ev.user_joins.get(uid, 0)))
            await interaction.response.send_message(f"Joined rally {int(rally_id)}! You can still join {left} more rally(ies) this event.", ephemeral=True)
        # Refresh dashboard to reflect new participant counts
        with contextlib.suppress(Exception):
            await _update_event_dashboard(int(guild.id))

    @app_commands.command(name="status", description="Show current event status and your progress")
    @app_commands.describe(hidden="If true, only you can see the response")
    async def status(self, interaction: discord.Interaction, hidden: Optional[bool] = True):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        ev = _BEAR_EVENTS.get(int(guild.id))
        if ev is None or ev.time_left_seconds() <= 0:
            await interaction.response.send_message("No active Bear event.", ephemeral=True)
            return
        now = datetime.now(timezone.utc)
        left = ev.time_left_seconds()
        embed = discord.Embed(title="🐻 Bear Event Status", color=discord.Color.orange())
        embed.add_field(name="Time Remaining", value=_fmt_duration(left), inline=True)
        # List active rallies
        lines = []
        async with ev.lock:
            for rid, r in sorted(ev.rallies.items()):
                if r.get("done"):
                    continue
                rleft = max(0, int((r.get("end") - now).total_seconds()))
                lines.append(f"ID {rid}: {r.get('title')} — {len(r.get('participants'))} joined — ends in {_fmt_duration(rleft)}")
        embed.add_field(name="Active Rallies", value=("\n".join(lines) if lines else "None"), inline=False)
        uid = int(interaction.user.id)
        pts = int(ev.user_points.get(uid, 0))
        joins = int(ev.user_joins.get(uid, 0))
        embed.add_field(name="You", value=f"Points: {pts}\nActive rallies: {joins}/6", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=bool(hidden))

    @app_commands.command(name="leaderboard", description="Show the current event leaderboard")
    @app_commands.describe(limit="How many top players to show (default 10)", hidden="If true, only you can see the response")
    async def leaderboard(self, interaction: discord.Interaction, limit: Optional[app_commands.Range[int,1,25]] = 10, hidden: Optional[bool] = True):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        ev = _BEAR_EVENTS.get(int(guild.id))
        if ev is None:
            await interaction.response.send_message("No Bear event data available.", ephemeral=True)
            return
        lim = int(limit) if limit is not None else 10
        tops = sorted(ev.user_points.items(), key=lambda kv: kv[1], reverse=True)[:lim]
        embed = discord.Embed(title="🏅 Bear Event Leaderboard", color=discord.Color.green())
        if not tops:
            embed.description = "No points recorded yet. Launch or join a rally!"
        else:
            lines = [f"{idx+1}. <@{uid}> — {pts} pts" for idx, (uid, pts) in enumerate(tops)]
            embed.add_field(name=f"Top {len(tops)}", value="\n".join(lines)[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=bool(hidden))

    @app_commands.command(name="top", description="Show the all-time Bear leaderboard (eternal)")
    @app_commands.describe(limit="How many top players to show (default 10)", hidden="If true, only you can see the response")
    async def top(self, interaction: discord.Interaction, limit: Optional[app_commands.Range[int,1,50]] = 10, hidden: Optional[bool] = True):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        lim = int(limit) if limit is not None else 10
        try:
            items = get_bear_top(int(guild.id), lim)
        except Exception as e:
            await interaction.response.send_message(f"Failed to fetch eternal leaderboard: {e}", ephemeral=True)
            return
        embed = discord.Embed(title="🏆 Bear Eternal Leaderboard", color=discord.Color.gold())
        if not items:
            embed.description = "No points have been recorded yet. Start an event with /bear start!"
        else:
            lines = [f"{idx+1}. <@{uid}> — {pts} pts" for idx, (uid, pts) in enumerate(items)]
            embed.add_field(name=f"Top {len(items)}", value="\n".join(lines)[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=bool(hidden))


bot.tree.add_command(BearGroup())


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




async def _async_main(token: str) -> None:
    # Register graceful shutdown to delete pending messages and cancel timers
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(_shutdown_cleanup(s.name)))
            except NotImplementedError:
                # Signals not supported (e.g., on Windows); ignore
                pass
    except Exception as e:
        logger.debug(f"Signal handler setup failed or unsupported: {e}")

    # Start bot once and let discord.py manage reconnects. If login fails, exit and let the orchestrator restart.
    try:
        logger.info("Starting Discord bot...")
        await bot.start(token, reconnect=True)
    finally:
        # Ensure the client session is closed on any failure to avoid leaks
        with contextlib.suppress(Exception):
            await bot.close()


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

    # Run the async main with our retrying starter
    asyncio.run(_async_main(token))


if __name__ == "__main__":
    main()
