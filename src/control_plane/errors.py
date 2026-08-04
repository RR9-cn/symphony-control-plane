class ControlPlaneError(RuntimeError):
    status_code = 400
    code = "control_plane_error"


class NotFoundError(ControlPlaneError):
    status_code = 404
    code = "not_found"


class ConflictError(ControlPlaneError):
    status_code = 409
    code = "conflict"


class InvalidTransitionError(ConflictError):
    code = "invalid_transition"


class ClaimError(ConflictError):
    code = "invalid_claim"


class AgentProfileConflictError(ConflictError):
    code = "agent_profile_conflict"


class RepositoryResolutionError(ControlPlaneError):
    status_code = 422
    code = "repository_resolution_failed"
