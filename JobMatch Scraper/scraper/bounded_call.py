"""Run a blocking discovery call in a disposable process with a real deadline.

Thread Future timeouts cannot stop JobSpy's internal network threads. A process
boundary lets the caller abandon one query while retaining earlier results.
"""
import importlib
import multiprocessing


def _worker(connection, module, function, args, kwargs):
    try:
        target = getattr(importlib.import_module(module), function)
        connection.send((True, target(*args, **kwargs)))
    except BaseException as exc:
        # Error messages from external clients can embed credentials or query URLs.
        connection.send((False, type(exc).__name__))
    finally:
        connection.close()


def call(module, function, args=(), kwargs=None, timeout=90):
    """Return the call result, or raise TimeoutError/RuntimeError; always reap the child."""
    if timeout <= 0:
        raise TimeoutError("Discovery budget exhausted")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(sender, module, function, args, kwargs or {}))
    try:
        process.start()
        sender.close()
        if not receiver.poll(timeout):
            raise TimeoutError("Discovery call exceeded %.1f seconds" % timeout)
        try:
            ok, result = receiver.recv()
        except EOFError as exc:
            raise RuntimeError("Discovery worker exited without a result") from exc
        if not ok:
            raise RuntimeError("Discovery worker failed: %s" % result)
        return result
    finally:
        sender.close()
        receiver.close()
        if process.pid is not None:
            process.join(timeout=0.2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            process.close()
