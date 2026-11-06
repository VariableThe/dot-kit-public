"""Adapter error taxonomy.

``AdapterError`` carries a stable ``code`` (the value dot and tests branch
on) plus optional sanitized ``details``. Tool errors from T3 keep their own
``code``/``_tag``; this layer only adds policy and protocol codes.
"""


class AdapterError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def as_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}