# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

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
DELIVERY_MAX = 100  # acceptances + withdrawals, reserved withdrawal slots included
DELIVERY_EVIDENCE_MAX = 20  # evidence ids per acceptance
MAX_BUNDLE_OUTPUTS = 8  # files a completion manifest may list

# Sizes, in bytes.
MAX_PACKAGE_BYTES = 262_144  # stored manifest; for format 2 it includes the reserve
DELIVERY_REASON_MAX_BYTES = 8192  # UTF-8 bytes of an acceptance or withdrawal reason
MAX_EMBED_FILE_BYTES = 262_144
MAX_EMBED_TOTAL_BYTES = 786_432
MAX_REFERENCE_FILE_BYTES = 67_108_864
MAX_OUTPUT_BYTES = 1_048_576

# Overview of a collection (docs/o2-overview-contract.md, section 7).
# The output bound is MAX_OUTPUT_BYTES, counted over the bytes actually written.
MAX_COLLECTION_ENTRIES = 1000  # immediate entries, hidden ones included
MAX_COLLECTION_CANDIDATES = 100  # potential package locations
