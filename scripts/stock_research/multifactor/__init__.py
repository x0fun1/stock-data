"""Native, auditable multi-factor preparation and historical research.

These scores and studies are research artifacts.  They do not register or
activate an investment-probability model.
"""

from .catalog import FACTOR_DIRECTIONS, factor_group, group_equal_weight_score
from .diagnostics import factor_diagnostics, training_correlation_clusters
from .preprocessing import prepare_cross_section

__all__ = [
    "FACTOR_DIRECTIONS", "factor_group", "group_equal_weight_score",
    "factor_diagnostics", "training_correlation_clusters", "prepare_cross_section",
]
