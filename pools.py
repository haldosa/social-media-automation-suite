import json
from config import _POOLS_PATH, PROFILE_IDS

with open(_POOLS_PATH, "r", encoding="utf-8") as _f:
    data = json.load(_f)

COMMENT_POOL = data["comments"]
# Captions for original posts.  Add / remove entries freely.
POST_CAPTION_POOL = data["post_captions"]
# Path to the persistent posting-state JSON (per-profile daily counts + age).

POST_CAPTION_SHORTS = data["POST_CAPTION_SHORTS"]

POST_CAPTION_EMOJIS = data["POST_CAPTION_EMOJIS"]

PREFLIGHT_SITES_POOL = data["PREFLIGHT_SITES_POOL"]

# Search query pool ,  generic topics typed into the Threads search bar.
# 70 % of search visits type one of these to model real query behaviour.
SEARCH_TOPIC_POOL = data["search_topics"]
#NICHE_KEYWORDS = data["NICHE_KEYWORDS"]

def _get_profile_pool_shard(pool: list, profile_id: str) -> list:
    """Return the deterministic subset of *pool* assigned to *profile_id*.

    Partitions *pool* across all known PROFILE_IDS (in their declared order)
    so no two profiles routinely draw from the same slice.  This eliminates
    the cross-account content correlation caused by all profiles sampling the
    exact same comment / caption pool.

    Edge cases
    ----------
    - Unknown profile_id (attached mode, "manual", etc.) â†’ full pool returned.
    - Pool smaller than the number of profiles â†’ round-robin single-item shards.
    - PROFILE_IDS has only one entry â†’ full pool is returned (nothing to split).
    """
    if not pool or not profile_id or profile_id in ("manual", ""):
        return pool

    try:
        idx = PROFILE_IDS.index(profile_id)
    except ValueError:
        return pool  # profile not in PROFILE_IDS list â†’ use full pool

    n = len(PROFILE_IDS)
    if n <= 1:
        return pool

    if len(pool) < n:
        # Pool too small to give every profile a unique item â€” assign by index
        return [pool[idx % len(pool)]]

    shard_size = len(pool) // n
    start = idx * shard_size
    # Last shard absorbs any remainder so every item is assigned somewhere
    end = start + shard_size if idx < n - 1 else len(pool)
    return pool[start:end]

