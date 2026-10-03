"""Editable local script starter. No computation is supplied or inferred."""
import json
import os
from pathlib import Path

output = Path(os.environ["PWB_OUTPUT_DIR"])
output.mkdir(parents=True, exist_ok=True)
# Add your computation here. Preserve input snapshots and write outputs below output.
(output / "script_started.json").write_text(json.dumps({"status": "starter executed", "scientific_computation": "NOT RUN"}))
