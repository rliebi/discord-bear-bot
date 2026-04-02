from dataclasses import dataclass
from typing import Tuple, Optional


@dataclass
class GuildConfig:
    max_troop_size: int
    infantry_amount: int
    max_archers_amount: int


@dataclass
class MarchResult:
    joining_archers: int
    joining_infantry: int
    joining_cavalry: int
    calling_archers: int
    calling_infantry: int
    calling_cavalry: int


def compute_kingshot(
    g: GuildConfig,
    total_archers: int,
    march_count: int,
    is_caller: bool,
    override_march_archers: Optional[int] = None,
    total_march_size: Optional[int] = None,
) -> MarchResult:
    if march_count <= 0:
        raise ValueError("march_count must be > 0")
    
    # Server setting for joining marches
    server_max_troop_size = g.max_troop_size
    if server_max_troop_size <= 0:
        raise ValueError("Server Max Troop Size not configured")
    
    if g.infantry_amount < 0 or g.max_archers_amount < 0:
        raise ValueError("Invalid server settings")

    # Per-joiner archer value baseline
    divisor = march_count + (1 if is_caller else 0)
    base = total_archers // max(1, divisor)
    
    if override_march_archers is not None:
        capped = override_march_archers
        # If user provides override, DO NOT round it down to 1000 even if caller
        caller_archer_value = capped
    else:
        capped = min(base, g.max_archers_amount)
        # Rounding rule: only round down to nearest 1000 if the user IS the caller.
        # If not calling, do not round the joining march archers.
        caller_archer_value = ((capped // 1000) * 1000) if is_caller else capped

    # Joining march values (rounded to 1000 only when calling)
    # Joiners are limited by both their physical march size AND server setting
    joining_archers = caller_archer_value
    joining_infantry = g.infantry_amount
    
    if total_march_size is not None:
        effective_joining_max = min(total_march_size, g.max_troop_size)
        joining_cavalry = max(0, effective_joining_max - joining_archers - joining_infantry)
    else:
        # If no physical limit given, we don't know the exact cavalry (will show "Rest" in bot)
        joining_cavalry = 0

    # Calling march values (no 1000 rounding on caller march)
    if is_caller:
        # Use the rounded joining value for remaining archers calc
        remaining_archers = total_archers - (caller_archer_value * march_count)
        remaining_archers = max(0, remaining_archers)
        calling_infantry = g.infantry_amount
        
        # Fit archers then cav into the caller's march size
        if total_march_size is not None:
            # If explicit override provided, cap at it and calculate cavalry
            max_archers_slot = max(0, total_march_size - calling_infantry)
            calling_archers = min(remaining_archers, max_archers_slot)
            calling_cavalry = max(0, total_march_size - calling_infantry - calling_archers)
        else:
            # If no override, server max troop size DOES NOT apply to caller
            # We show all remaining archers and let them figure out cavalry (Rest)
            calling_archers = remaining_archers
            calling_cavalry = 0 # In bot.py, caller cavalry is shown as "Rest"
    else:
        calling_archers = 0
        calling_infantry = 0
        calling_cavalry = 0

    return MarchResult(
        joining_archers=joining_archers,
        joining_infantry=joining_infantry,
        joining_cavalry=joining_cavalry,
        calling_archers=calling_archers,
        calling_infantry=calling_infantry,
        calling_cavalry=calling_cavalry,
    )
