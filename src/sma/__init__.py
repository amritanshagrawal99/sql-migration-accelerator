"""sql-migration-accelerator: convert legacy warehouse SQL to cloud warehouses and prove the data matches."""
__version__ = "0.1.0"

from .converter import convert_script, convert_statement  # noqa: E402
from .reconcile import reconcile  # noqa: E402

__all__ = ["convert_script", "convert_statement", "reconcile", "__version__"]
