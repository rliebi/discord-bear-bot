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

from src.storage import (
    get_guild_settings,
    update_guild_settings,
    set_admin_if_unset,
    record_usage_event,
    get_usage_summary,
    get_user_usage,
    get_all_guilds_usage,
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

    # Register graceful shutdown to delete pending messages and cancel timers
    try:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(_shutdown_cleanup(s.name)))
            except NotImplementedError:
                # Signals not supported (e.g., on Windows); ignore
                pass
    except Exception as e:
        logger.debug(f"Signal handler setup failed or unsupported: {e}")

    bot.run(token)


if __name__ == "__main__":
    main()
