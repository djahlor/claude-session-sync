"""JSON accepted by both Python and Claude's JavaScript readers."""

import json as _json
import math

JSONDecodeError = _json.JSONDecodeError


def _invalid(message):
    raise JSONDecodeError(message, "", 0)


def _constant(value):
    return _invalid("Non-finite JSON number: {}".format(value))


def _float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        return _constant(value)
    return parsed


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            return _invalid("Duplicate JSON object key")
        result[key] = value
    return result


def loads(value, **kwargs):
    kwargs.update(
        parse_constant=_constant, parse_float=_float, object_pairs_hook=_object
    )
    return _json.loads(value, **kwargs)


def load(stream, **kwargs):
    return loads(stream.read(), **kwargs)


def dumps(value, **kwargs):
    kwargs["allow_nan"] = False
    return _json.dumps(value, **kwargs)


def dump(value, stream, **kwargs):
    stream.write(dumps(value, **kwargs))
