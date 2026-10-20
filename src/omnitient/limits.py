"""Fixed input and output bounds (see docs/o1-contract.md, section 7).

Exceeding a bound refuses with a diagnostic; nothing required is truncated.
"""

# Field sizes, in characters.
SHORT_MAX = 200
LINE_MAX = 1000
BLOCK_MAX = 8000
PATH_MAX = 512
URL_MAX = 500

# List sizes, in items.
LIST_MAX = 100
ROOTS_MAX = 16
CONTEXT_MAX = 64
DECISIONS_MAX = 200
RECOMMENDATIONS_MAX = 20
EVIDENCE_MAX = 500
APPROVALS_MAX = 100

# Sizes, in bytes.
MAX_PACKAGE_BYTES = 262_144
MAX_EMBED_FILE_BYTES = 262_144
MAX_EMBED_TOTAL_BYTES = 786_432
MAX_REFERENCE_FILE_BYTES = 67_108_864
MAX_OUTPUT_BYTES = 1_048_576
