class APIError(Exception):
    pass


class RetriesExhausted(APIError):
    """Every retry for a request was used up and the call still failed.

    Distinct from a plain ``APIError`` because the two mean opposite things to a
    caller: a 4xx says the data is genuinely not there, while this says we could
    not find out. ``_get_optional`` swallows the former (correctly) and MUST NOT
    swallow this one — doing so turned exhausted fetches into "no step results"
    and silently dropped them, with no log line and no counter, on 14
    executions of one migrated project.
    """
