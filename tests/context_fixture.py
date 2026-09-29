"""TestCase context cleanup compatible with Python 3.10."""


def enter_context(test, context):
    cls = type(context)
    result = cls.__enter__(context)
    test.addCleanup(cls.__exit__, context, None, None, None)
    return result
