# Central place for constants that must never be duplicated or hardcoded
# elsewhere in the codebase (see CLAUDE.md "Hard rules").

# Model used for extraction (pulling structured facts out of CFP text).
# Cheap and fast — extraction is a well-defined, low-reasoning task.
MODEL_EXTRACTION = "claude-haiku-4-5-20251001"

# Model used for fit judgment (scoring theme/nature/logistics fit).
# Not called yet — this is declared now so no future file ever hardcodes
# a model name of its own.
MODEL_JUDGE = "claude-sonnet-5"

# Where the tracker data file lives, relative to the project root.
TRACKER_PATH = "tracker.json"

# Tier Alert threshold (brief, "Monthly digest > Tier Alert"): a match with
# theme_fit >= this but overall < 50, call not closed, gets flagged as a
# possible venue-tier misread rather than staying silent. Computed purely
# from stored scores — no extra LLM call.
TIER_ALERT_THEME_MIN = 75
