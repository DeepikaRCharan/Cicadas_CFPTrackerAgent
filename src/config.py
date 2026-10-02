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

# --- Discovery (build step 8) -----------------------------------------------

# The discovery loop's model (overridable per run with --discovery-model).
MODEL_DISCOVERY = "claude-sonnet-5"

# Web tool versions per loop model. The _20260209 versions (dynamic
# filtering) need Sonnet 5 or later; Haiku 4.5 takes the basic versions.
WEB_TOOL_TYPES = {
    "claude-sonnet-5": ("web_search_20260209", "web_fetch_20260209"),
    "claude-haiku-4-5-20251001": ("web_search_20250305", "web_fetch_20250910"),
}

# Where each run's readable decision trace is written (committed, unlike logs/).
TRACES_DIR = "traces"

# Hard limits per profile per run, sized for a $0.25-per-profile ceiling
# (loop + hand-off). Hitting one stops that resource (or the loop) cleanly
# and is logged; there is no retry or fallback that could quietly spend past
# them (CLAUDE.md: the spend cap is a backstop, not a plan).
DISCOVERY_MAX_TURNS = 12
DISCOVERY_MAX_SEARCHES = 5  # 2 main + 3 probes
DISCOVERY_PROBE_RESERVE = 3  # of the searches, kept for one probe per other bucket
DISCOVERY_MAX_FETCHES = 3  # the model judges from search snippets unless it truly needs the page
DISCOVERY_MAX_CANDIDATES = 2
# Cap on how much of one fetched page enters the loop's context.
DISCOVERY_FETCH_MAX_CONTENT_TOKENS = 5000
# Estimated spend at which the loop stops (checked between turns, from the
# API's usage report). Leaves room under the $0.25-per-profile ceiling for
# the hand-off calls that follow.
DISCOVERY_MAX_LOOP_COST_USD = 0.15

# Search vocabulary per bucket (brief, Workflow > step 2). Main searches pair
# the paper's topics with its own bucket(s)' phrases; probes use the first
# phrase of every other bucket. Other Avenues has no vocabulary in the brief;
# "open call for submissions" is the catch-all.
BUCKET_VOCABULARY = {
    "academic_practitioner": ["call for papers", "call for abstracts"],
    "industry_technical_media": ["call for speakers", "call for presentations", "write for us"],
    "corporate_institutional": ["policy forum", "expert panel", "submissions invited"],
    "other_avenues": ["open call for submissions"],
}

# Listing sites, never a venue's own call page: a result here must be
# followed through to the venue's site before it can be accepted.
AGGREGATOR_DOMAINS = frozenset({
    "wikicfp.com",
    "conferenceindex.org",
    "allconferencealert.com",
    "conferencealerts.com",
    "10times.com",
    "call4paper.com",
    "conference-service.com",
    "waset.org",
    "easychair.org",  # its /cfp/ listings; a venue hosted there still has its own CFP page
})

# Blocked outright -- never followed through, never accepted, and hidden from
# search and fetch results via the web tools' blocked_domains. Starts with
# OMICS-group domains (mass-produced, often predatory conferences and
# journals); add others here as they turn up in traces.
BLOCKED_DOMAINS = frozenset({
    "conferenceseries.com",
    "omicsonline.org",
    "omicsonline.com",
    "omicsgroup.org",
    "omicsgroup.com",
    "longdom.org",
    "hilarispublisher.com",
    "imedpub.com",
    "scitechnol.com",
    "alliedacademies.org",
    "pulsus.com",
})

# For the run's cost estimate only (USD per million tokens: input, output;
# web search per query). Approximate list prices; the Console bill is the
# source of truth.
PRICE_PER_MTOK = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}
PRICE_PER_WEB_SEARCH = 0.01
# Prompt-cache pricing, as multiples of the input price.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10
