"""Distinguish durable-write failures from unsupported Facebook responses."""


class CaptureWriteError(RuntimeError):
    pass


def emit_capture(callback, post, comments):
    if callback is None:
        return
    try:
        callback(post, comments)
    except CaptureWriteError:
        raise
    except Exception as error:
        raise CaptureWriteError('Không lưu được dữ liệu vừa đọc vào kho: '+str(error)) from error
