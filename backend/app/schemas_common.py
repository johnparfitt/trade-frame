from typing import Literal

# State of a manual background job (breadth refresh, report generation).
# Shared by the pane status schemas so the allowed values stay in one place.
JobState = Literal["idle", "running", "error"]
