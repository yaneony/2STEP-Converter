import os
import sys
from contextlib import ExitStack, contextmanager


@contextmanager
def quiet():
    sys.stdout.flush()
    sys.stderr.flush()
    with ExitStack() as cleanup:
        for descriptor in (1, 2):
            saved = os.dup(descriptor)
            cleanup.callback(os.close, saved)
            cleanup.callback(os.dup2, saved, descriptor)
        with open(os.devnull, 'wb') as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        try:
            yield
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
