"""Resource-lifecycle fixture for the seeded eval branch."""


class Handle:
    def __init__(self, name):
        self.name = name
        self.closed = False

    def close(self):
        self.closed = True


def open_handle(name):
    return Handle(name)


def process(handle):
    """Closes the handle once it's done with it."""
    handle.close()
    return handle.name


def run_job(name):
    """Seed #4: correctness/resource_double_release — `process` already closed
    the handle; closing it again here is a cross-function lifecycle bug that
    only shows up by reading `process`'s body, not `run_job`'s alone."""
    handle = open_handle(name)
    result = process(handle)
    handle.close()
    return result
