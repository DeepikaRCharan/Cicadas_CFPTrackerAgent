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

# The researcher's decisions file, relative to the project root. Read every
# run, never written by the agent.
DECISIONS_PATH = "decisions.json"

# Tier Alert threshold (brief, "Monthly digest > Tier Alert"): a match with
# theme_fit >= this but overall < 50, call not closed, gets flagged as a
# possible venue-tier misread rather than staying silent. Computed purely
# from stored scores — no extra LLM call.
TIER_ALERT_THEME_MIN = 75

# New-fit threshold (brief, Workflow > Notify): a match enters the digest's
# New section only if some bucket's overall is strictly above this.
NEW_FIT_MIN = 50

# Where live paper profiles live, and how many may be live at once (brief,
# Data model > Paper profiles: a deliberate cost cap, not a bug).
PROFILES_DIR = "profiles"
MAX_LIVE_PROFILES = 2

# Sender for every Cicadas email. Resend's shared onboarding address works
# without a verified domain, but only delivers to the Resend account's own
# email -- which is what NOTIFY_TO is (brief, Setup section).
RESEND_FROM = "Cicadas <onboarding@resend.dev>"
