"""
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) 2012-2018 Pupil Labs

Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
"""

import logging
import multiprocessing as mp
import zmq
from ctypes import c_bool

import zmq_tools

logger = logging.getLogger(__name__)


class EarlyCancellationError(Exception):
    pass


class Task_Proxy(object):
    """Future like object that runs a given generator in the background and returns is able to return the results incrementally"""

    def __init__(self, name, generator, args=(), kwargs={}):
        super().__init__()

        self._should_terminate_flag = mp.Value(c_bool, 0)
        self._completed = False
        self._canceled = False

        pipe_recv, pipe_send = mp.Pipe(False)
        wrapper_args = [pipe_send, self._should_terminate_flag, generator]
        wrapper_args.extend(args)
        self.process = mp.Process(
            target=self._wrapper, name=name, args=wrapper_args, kwargs=kwargs
        )
        self.process.daemon = True
        self.process.start()
        self.pipe = pipe_recv

    def _wrapper(self, pipe, _should_terminate_flag, generator, *args, **kwargs):
        """Executed in background, pipes generator results to foreground"""
        logger.debug("Entering _wrapper")

        try:
            for datum in generator(*args, **kwargs):
                if _should_terminate_flag.value:
                    raise EarlyCancellationError("Task was cancelled")
                pipe.send(datum)
        except Exception as e:
            pipe.send(e)
            if not isinstance(e, EarlyCancellationError):
                import traceback

                logger.info(traceback.format_exc())
        else:
            pipe.send(StopIteration())
        finally:
            pipe.close()
            logger.debug("Exiting _wrapper")

    def fetch(self):
        """Fetches progress and available results from background"""
        if self.completed or self.canceled:
            return

        while self.pipe.poll(0):
            try:
                datum = self.pipe.recv()
            except EOFError:
                logger.debug("Process canceled be user.")
                self._canceled = True
                return
            else:
                if isinstance(datum, StopIteration):
                    self._completed = True
                    return
                elif isinstance(datum, EarlyCancellationError):
                    self._canceled = True
                    return
                elif isinstance(datum, Exception):
                    raise datum
                else:
                    yield datum

    def cancel(self, timeout=1):
        if not (self.completed or self.canceled):
            self._should_terminate_flag.value = True
            for x in self.fetch():
                # fetch to flush pipe to allow process to react to cancel comand.
                pass
        if self.process is not None:
            self.process.join(timeout)
            self.process = None

    @property
    def completed(self):
        return self._completed

    @property
    def canceled(self):
        return self._canceled

    def __del__(self):
        self.cancel(timeout=0.1)
        self.process = None


class IPC_Logging_Task_Proxy(Task_Proxy):
    def __init__(self, ipc_push_url, name, generator, args=(), kwargs={}):
        extended_args = [ipc_push_url]
        extended_args.extend(args)
        super().__init__(name, generator, args=extended_args, kwargs=kwargs)

    def _wrapper(
        self, pipe, _should_terminate_flag, generator, ipc_push_url, *args, **kwargs
    ):
        self._enforce_IPC_logging(ipc_push_url)
        super()._wrapper(pipe, _should_terminate_flag, generator, *args, **kwargs)

    def _enforce_IPC_logging(self, ipc_push_url):
        """
        ZMQ_handler sockets from the foreground thread are broken in the background.
        Solution: Remove all potential broken handlers and replace by new oneself.

        Caveat: If a broken handler is present is incosistent across environments.
        """
        del logger.root.handlers[:]
        zmq_ctx = zmq.Context()
        handler = zmq_tools.ZMQ_handler(zmq_ctx, ipc_push_url)
        logger.root.addHandler(handler)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(processName)s - [%(levelname)s] %(name)s: %(message)s",
    )

    def example_generator(mu=0.0, sigma=1.0, steps=100):
        """samples `N(\mu, \sigma^2)`"""
        import numpy as np
        from time import sleep

        for i in range(steps):
            # yield progress, datum
            yield (i + 1) / steps, sigma * np.random.randn() + mu
            sleep(np.random.rand() * 0.1)

    # initialize task proxy
    task = Task_Proxy(
        "Background", example_generator, args=(5.0, 3.0), kwargs={"steps": 100}
    )

    from time import time, sleep

    start = time()
    maximal_duration = 2.0
    while time() - start < maximal_duration:
        # fetch all available results
        for progress, random_number in task.fetch():
            logger.debug("[{:3.0f}%] {:0.2f}".format(progress * 100, random_number))

        # test if task is completed
        if task.completed:
            break
        sleep(1.0)

    logger.debug("Canceling task")
    task.cancel(timeout=1)
    logger.debug("Task done")
