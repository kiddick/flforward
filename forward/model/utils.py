import asyncio
import concurrent
import threading
import traceback
from asyncio import AbstractEventLoop, _get_running_loop
from inspect import isawaitable
from typing import Callable

from loguru import logger


def db_session_scope():
    """ Custom scope function for SQLAlchemy DB sessions

    Returns current task id if available, or current thread id.
    Caller must do db.session.remove() themselves, cause there is no garbage collected thread-local used.
    """
    try:
        task = asyncio.Task.current_task()
    except RuntimeError:
        task = None
    if task is not None:
        logger.error("ASYNC_DB_USAGE: {}", "".join(traceback.format_stack()))
        return id(task)
    return threading.get_ident()


def call_async(loop: AbstractEventLoop, func: Callable, *args, **kwargs):
    """
    Call the given callable in the event loop thread.

    If the call returns an awaitable, it is resolved before returning to the caller.

    If you need to pass keyword arguments named ``loop`` or ``func`` to the callable, use
    :func:`functools.partial` for that.

    :param func: a regular function or a coroutine function
    :param args: positional arguments to call with
    :param loop: the event loop in which to call the function
    :param kwargs: keyword arguments to call with
    :return: the return value of the function call

    """

    async def callback():
        try:
            retval = func(*args, **kwargs)
            if isawaitable(retval):
                retval = await retval
        except BaseException as e:
            f.set_exception(e)
        else:
            f.set_result(retval)

    if _get_running_loop():
        raise RuntimeError('call_async() must not be called from an event loop thread')

    f = concurrent.futures.Future()
    loop.call_soon_threadsafe(loop.create_task, callback())
    return f.result()
