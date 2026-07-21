"""Configuration for the v0.4 DAG planning foundation."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanningConfigType(BaseModel):
    """Runtime limits for optional DAG planning."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True

    max_nodes: int = Field(default=32, ge=1)
    max_dependencies_per_node: int = Field(default=16, ge=0)

    max_ready_nodes_in_context: int = Field(default=5, ge=1)
    max_plan_get_limit: int = Field(default=20, ge=1)
    max_reconciliation_attempts: int = Field(default=2, ge=1)

    max_title_chars: int = Field(default=160, ge=20)
    max_objective_chars: int = Field(default=2000, ge=100)
    max_outcome_summary_chars: int = Field(default=4000, ge=100)
    max_success_criteria_per_node: int = Field(default=20, ge=1)

    @model_validator(mode="after")
    def validate_cross_field_limits(self) -> "PlanningConfigType":
        if self.max_plan_get_limit > self.max_nodes:
            raise ValueError("max_plan_get_limit must not exceed max_nodes")
        return self
