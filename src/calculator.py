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
    override_march_archers: Optional[int] = None,
    total_march_size: Optional[int] = None,
    is_calling: bool = True,
) -> MarchResult:
    # Divisor is total number of roles the player is taking
    divisor = march_count + (1 if is_calling else 0)
    if divisor <= 0:
        raise ValueError("Must have at least one march (join count > 0 or is calling)")
    
    # Server setting for joining marches
    server_max_troop_size = g.max_troop_size
    if server_max_troop_size <= 0:
        raise ValueError("Server Max Troop Size not configured")
    
    if g.infantry_amount < 0 or g.max_archers_amount < 0:
        raise ValueError("Invalid server settings")

    base = total_archers // divisor
    
    if override_march_archers is not None:
        joining_archers = override_march_archers
    else:
        # For coordination, joining archers are rounded down to nearest 1000 and capped by server MAA
        capped_base = min(base, g.max_archers_amount)
        joining_archers = (capped_base // 1000) * 1000

    # Joining march: infantry is server-set, cavalry fills up to min(physical size, server cap)
    joining_infantry = g.infantry_amount
    if total_march_size is not None:
        effective_joining_max = min(total_march_size, server_max_troop_size)
        joining_cavalry = max(0, effective_joining_max - joining_archers - joining_infantry)
    else:
        joining_cavalry = 0 # Bot shows "Rest"

    # Calling march: infantry is server-set, archers are the exact remainder
    if is_calling:
        remaining_archers = max(0, total_archers - (joining_archers * march_count))
        calling_infantry = g.infantry_amount
        
        if total_march_size is not None:
            # Caller is NOT limited by server max_troop_size, only by their physical limit
            max_archers_slot = max(0, total_march_size - calling_infantry)
            calling_archers = min(remaining_archers, max_archers_slot)
            calling_cavalry = max(0, total_march_size - calling_infantry - calling_archers)
        else:
            calling_archers = remaining_archers
            calling_cavalry = 0 # Bot shows "Rest"
    else:
        calling_archers = 0
        calling_infantry = 0
        calling_cavalry = 0

    return MarchResult(
        joining_archers=joining_archers if march_count > 0 else 0,
        joining_infantry=joining_infantry if march_count > 0 else 0,
        joining_cavalry=joining_cavalry if march_count > 0 else 0,
        calling_archers=calling_archers,
        calling_infantry=calling_infantry,
        calling_cavalry=calling_cavalry,
    )
