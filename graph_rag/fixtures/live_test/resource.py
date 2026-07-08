"""Report generation."""


class ReportHandle:
    def __init__(self, name):
        self.name = name
        self.closed = False

    def close(self):
        self.closed = True


def open_report(name):
    return ReportHandle(name)


def process_report(handle):
    handle.close()
    return f"processed {handle.name}"


def generate_report(name):
    handle = open_report(name)
    result = process_report(handle)
    handle.close()
    return result
