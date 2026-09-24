"""汽油指导价服务向 API 和 CLI 暴露的稳定错误。"""


class PricingError(RuntimeError):
    code = "pricing_error"
    status = 400


class NotFound(PricingError):
    code = "not_found"
    status = 404


class Conflict(PricingError):
    code = "conflict"
    status = 409


class Forbidden(PricingError):
    code = "forbidden"
    status = 403


class InvalidState(PricingError):
    code = "invalid_state"
    status = 409


class ValidationFailed(PricingError):
    code = "validation_failed"
    status = 422
