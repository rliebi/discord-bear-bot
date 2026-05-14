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
    override_max_troop_size: Optional[int] = None,
    override_infantry_amount: Optional[int] = None,
    override_max_archers_amount: Optional[int] = None,
) -> MarchResult:
    # Divisor is total number of roles the player is taking
    divisor = march_count + (1 if is_calling else 0)
    if divisor <= 0:
        raise ValueError("Must have at least one march (join count > 0 or is calling)")
    
    # Effective settings (defaults to guild config, but can be overridden)
    max_troop_size = override_max_troop_size if override_max_troop_size is not None else g.max_troop_size
    infantry_amount = override_infantry_amount if override_infantry_amount is not None else g.infantry_amount
    max_archers_amount = override_max_archers_amount if override_max_archers_amount is not None else g.max_archers_amount

    if infantry_amount < 0 or max_archers_amount < 0:
        raise ValueError("Invalid settings")

    base = total_archers // divisor
    
    if override_march_archers is not None:
        joining_archers = override_march_archers
    else:
        # For coordination, joining archers are rounded down to nearest 1000 and capped by MAA
        capped_base = min(base, max_archers_amount)
        joining_archers = (capped_base // 1000) * 1000

    # Joining march: infantry is set, cavalry fills up to capacity
    joining_infantry = infantry_amount
    
    # Joining Marches always respect the Max Joining Troop Count (max_troop_size)
    effective_joining_max = max_troop_size if max_troop_size > 0 else None
    
    # If the user provides an individual total_march_size, it might be smaller than the server's max_troop_size.
    # A player cannot send more than their own capacity.
    if total_march_size is not None:
        if effective_joining_max is None or total_march_size < effective_joining_max:
            effective_joining_max = total_march_size

    if effective_joining_max is not None:
        joining_archers = min(joining_archers, max(0, effective_joining_max - joining_infantry))
        joining_cavalry = max(0, effective_joining_max - joining_archers - joining_infantry)
    else:
        joining_cavalry = 0  # Still "Rest" if no capacity known at all

    # Calling march: infantry is set, archers are the exact remainder
    if is_calling:
        remaining_archers = max(0, total_archers - (joining_archers * march_count))
        calling_infantry = infantry_amount
        
        # If user enters max troop size (total_march_size), this affects the calling march.
        if total_march_size is not None:
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
